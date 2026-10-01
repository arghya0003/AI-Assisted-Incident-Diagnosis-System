"""
Fault injection harness (Phase 8, pulled forward). Runs real, physically
enforced faults against the Sock Shop testbed via the Docker Engine API
(mounted socket - Docker-outside-of-Docker), and records ground truth in
the shape M2's evaluation runner needs (CONTRACTS.md's "eval hooks":
scenario_id, fault_type, ground_truth_service, t_inject).

Seven scenario types, all real (not simulated metrics) and cheap to
implement without re-architecting the testbed's network topology:

  bad_deploy_latency  - records a real deploy via deploy-emitter (Phase 5),
                        then CPU-throttles the target container via cgroups
                        (docker update --cpus equivalent) for the duration.
                        A deploy that quietly regresses per-request CPU
                        cost is a common real-world cause of latency
                        regressions, so this is a faithful mechanism, not
                        a shortcut - and it closes the loop with the
                        deploy log M3 will correlate against.
  service_crash       - stops the container, waits, restarts it. Simulates
                        a hard outage / dependency-cascade trigger.
  db_pool_saturation  - holds a write lock on the `sock` table catalogue
                        reads, so its queries block and its connection
                        pool stays checked out (issue #35).
                        catalogue only (MySQL); Mongo-backed services are a
                        documented gap - see docs/phase8-fault-injection.md.
  dependency_timeout  - freezes a dependency (docker pause) so callers hang.
  config_error        - points a dependency's hostname at loopback in the
                        service's /etc/hosts; the service stays up, erroring.
  memory_exhaustion   - lowers the cgroup memory limit below the working set
                        so the kernel OOM-kills the process.
  resource_exhaustion - tightens the CPU quota in steps: the one gradual
                        fault, where every other one is a step change.

Every fault undoes itself in a `finally`, and `recover_interrupted` undoes
any fault a previous process was killed in the middle of - see that
function for why both are needed.
"""

import os
import json
import random
import logging
import threading
import time
from datetime import datetime, timezone

import docker
import psycopg2
import psycopg2.extras
import pymysql
import requests
from flask import Flask, request, jsonify

from params import FAULT_TYPES, build_params, ramp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fault-injector")

PG_HOST = os.environ.get("PG_HOST", "timescaledb")
PG_PORT = os.environ.get("PG_PORT", "5432")
PG_DB = os.environ.get("PG_DB", "metrics")
PG_USER = os.environ.get("PG_USER", "postgres")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "Abcd1234#")

DEPLOY_EMITTER_URL = os.environ.get("DEPLOY_EMITTER_URL", "http://deploy-emitter:5000")
LOAD_GENERATOR_URL = os.environ.get("LOAD_GENERATOR_URL", "http://load-generator:5002")
COMPOSE_PROJECT = os.environ.get("COMPOSE_PROJECT_NAME", "incident-diagnosis-system")

CATALOGUE_DB = {"host": "catalogue-db", "user": "root", "password": "Abcd1234#",
                "database": "socksdb"}
# The table every catalogue read touches (socksdb holds sock, sock_tag, tag).
LOCKED_TABLE = "sock"
# How long past the fault's end MySQL may keep an abandoned locking session.
LOCK_REAP_GRACE_SECONDS = 30

# Tags every /etc/hosts line config_error adds, so the undo can remove
# exactly those and nothing else.
HOSTS_MARKER = "# fault-injector:config_error"
HOSTS_REASSERT_SECONDS = 2

# Below this the testbed is effectively idle (Prometheus scraping is most of
# it) and a fault won't show up in the metrics - warn rather than refuse, so
# a deliberate idle-baseline run is still possible.
MIN_USEFUL_RPS = 1.0

docker_client = docker.from_env()


def connect_postgres():
    while True:
        try:
            conn = psycopg2.connect(
                host=PG_HOST, port=PG_PORT, dbname=PG_DB,
                user=PG_USER, password=PG_PASSWORD,
            )
            conn.autocommit = True
            return conn
        except psycopg2.OperationalError as exc:
            log.warning("timescaledb not reachable yet (%s), retrying in 3s", exc)
            time.sleep(3)


conn = connect_postgres()
app = Flask(__name__)


def ensure_db_connection():
    global conn
    if conn is None or conn.closed:
        log.warning("postgres connection closed; reconnecting")
        conn = connect_postgres()
    return conn


def get_container(service: str, include_stopped: bool = False):
    matches = docker_client.containers.list(
        all=include_stopped,
        filters={"label": f"com.docker.compose.service={service}"},
    )
    if not matches:
        raise ValueError(f"no running container found for service {service!r}")
    return matches[0]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def record_scenario(scenario_id, fault_type, service, params) -> None:
    db = ensure_db_connection()
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO fault_scenarios
               (scenario_id, fault_type, ground_truth_service, t_inject, status, params)
               VALUES (%s, %s, %s, now(), 'running', %s)""",
            (scenario_id, fault_type, service, json.dumps(params)),
        )


def mark_recovered(scenario_id: str) -> None:
    db = ensure_db_connection()
    with db.cursor() as cur:
        cur.execute(
            "UPDATE fault_scenarios SET status = 'recovered', t_recovered = now() WHERE scenario_id = %s",
            (scenario_id,),
        )


def annotate_scenario(scenario_id: str, extra: dict) -> None:
    """Merge keys into a running scenario's params - state a restart needs."""
    db = ensure_db_connection()
    with db.cursor() as cur:
        cur.execute(
            "UPDATE fault_scenarios SET params = params || %s::jsonb WHERE scenario_id = %s",
            (json.dumps(extra), scenario_id),
        )


def mark_failed(scenario_id: str, error: str) -> None:
    db = ensure_db_connection()
    with db.cursor() as cur:
        cur.execute(
            "UPDATE fault_scenarios SET status = 'failed', t_recovered = now(), "
            "params = params || %s::jsonb WHERE scenario_id = %s",
            (json.dumps({"error": error}), scenario_id),
        )


def new_scenario_id(fault_type: str) -> str:
    return f"scn-{fault_type.replace('_', '-')}-{int(time.time())}"


def offered_rps() -> float | None:
    """Load being driven through the testbed right now, or None if unknown.

    A fault injected into an idle testbed moves no metric, so nothing can
    detect or diagnose it (issue #6) - and from `fault_scenarios` alone that
    looks identical to a detector that simply missed it. Recording the
    offered rate with each scenario makes an idle run visible in the ground
    truth instead of silently deflating the evaluation numbers.
    """
    try:
        resp = requests.get(f"{LOAD_GENERATOR_URL}/stats", timeout=3)
        resp.raise_for_status()
        stats = resp.json()
        # recent_rps, not the lifetime average: a generator that ran for an
        # hour and then stalled still has a healthy-looking average.
        return float(stats.get("recent_rps", stats["achieved_rps"]))
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        log.warning("could not read load-generator stats: %s", exc)
        return None


# ---------------------------------------------------------------------
# Fault implementations
# ---------------------------------------------------------------------

def undo_cpu_limit(service: str) -> None:
    get_container(service).update(cpu_period=100000, cpu_quota=-1)
    log.info("restored %s CPU", service)


def run_bad_deploy_latency(service: str, duration_s: int, cpu_limit: float):
    try:
        requests.post(
            f"{DEPLOY_EMITTER_URL}/deploys",
            json={"service": service, "config_diff": "perf regression: inefficient loop introduced"},
            timeout=5,
        )
    except requests.RequestException as exc:
        log.warning("could not record companion deploy event: %s", exc)

    container = get_container(service)
    period = 100000
    quota = max(int(period * cpu_limit), 1000)
    log.info("throttling %s to %.0f%% CPU for %ss", service, cpu_limit * 100, duration_s)
    container.update(cpu_period=period, cpu_quota=quota)
    try:
        time.sleep(duration_s)
    finally:
        undo_cpu_limit(service)


def undo_service_crash(service: str) -> None:
    container = get_container(service, include_stopped=True)
    if container.status != "running":
        container.start()
        log.info("restarted %s", service)


def run_service_crash(service: str, duration_s: int):
    container = get_container(service)
    log.info("stopping %s for %ss", service, duration_s)
    container.stop(timeout=5)
    try:
        time.sleep(duration_s)
    finally:
        undo_service_crash(service)


def lock_holders(cur) -> list[tuple]:
    """Sessions that look like a table lock left behind by an earlier run.

    Catalogue connects as `catalogue_user`; the injector is the only client
    that uses `root` against socksdb. So another idle root session there is a
    lock holder from a run that never cleaned up, and a session waiting on a
    table lock means one is being held right now.
    """
    cur.execute(
        "SELECT id, user, command, time, state FROM information_schema.processlist "
        "WHERE id <> CONNECTION_ID() AND ("
        "  (user = 'root' AND db = %s AND command = 'Sleep')"
        "  OR state LIKE 'Waiting for table%%lock')",
        (CATALOGUE_DB["database"],),
    )
    return list(cur.fetchall())


def run_db_pool_saturation(service: str, duration_s: int):
    """Hold a write lock on the table catalogue reads, for `duration_s`.

    The old mechanism held ~155 connections to push catalogue-db past
    max_connections=151 and never touched catalogue: it keeps exactly two
    pooled connections open and reuses them, so it never asks for a new one
    and a server-side limit can't reach it (p95 stayed at 4.8ms through a
    full 90s fault). Those two connections *are* the pool this fault class is
    about. While `sock` is write-locked, every catalogue SELECT blocks, its
    pooled connections stay checked out waiting, and the pool is genuinely
    saturated - then it heals the moment the lock is released. (Measured: the
    blocked queries made catalogue open more connections, 26 waiting on the
    lock within 40s, and every one of them stayed checked out.)

    Table-level `LOCK TABLES ... WRITE`, not `SELECT ... FOR UPDATE`: that
    takes row locks, and InnoDB serves plain SELECTs from an MVCC snapshot
    without waiting on row locks, so catalogue would read straight through.

    The hazard is an orphaned lock, not a deadlock (the injector waits on
    nothing). catalogue-db runs with lock_wait_timeout = 1 year and
    wait_timeout = 8 hours, so if this process vanishes while holding the
    lock, catalogue could block behind the dead session for hours with
    nothing detecting it. Hence, on the locking session:
      - wait_timeout = duration + grace: the session idles while the lock is
        held, so MySQL reaps it - and the lock - shortly after the fault's
        intended end even if no UNLOCK ever arrives;
      - lock_wait_timeout = 10: our own LOCK TABLES fails fast rather than
        queueing a year behind someone else's;
    plus UNLOCK in a `finally`, and a preflight that refuses to inject while
    a lock from an earlier run still appears to be held.
    """
    if service != "catalogue":
        raise ValueError("db_pool_saturation only supports service=catalogue (MySQL)")

    conn = pymysql.connect(connect_timeout=5, autocommit=True, **CATALOGUE_DB)
    try:
        with conn.cursor() as cur:
            leftovers = lock_holders(cur)
            if leftovers:
                raise RuntimeError(
                    "refusing to lock: catalogue-db already has a lock holder or waiter "
                    f"(id, user, command, time, state): {leftovers}"
                )
            cur.execute("SET SESSION wait_timeout = %s", (duration_s + LOCK_REAP_GRACE_SECONDS,))
            cur.execute("SET SESSION lock_wait_timeout = 10")
            cur.execute(f"LOCK TABLES {LOCKED_TABLE} WRITE")
            log.info("holding WRITE lock on %s.%s for %ss", CATALOGUE_DB["database"],
                     LOCKED_TABLE, duration_s)
        time.sleep(duration_s)
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("UNLOCK TABLES")
            log.info("released lock on %s", LOCKED_TABLE)
        except pymysql.MySQLError as exc:
            # Closing the session releases its locks too; wait_timeout is the
            # backstop if even the close never reaches the server.
            log.error("UNLOCK TABLES failed (%s); closing the session to release it", exc)
        finally:
            conn.close()


def undo_dependency_timeout(dependency: str) -> None:
    container = get_container(dependency)
    container.reload()
    if container.status == "paused":
        container.unpause()
        log.info("unpaused %s", dependency)


def run_dependency_timeout(service: str, duration_s: int, dependency: str):
    """Freeze a dependency so its callers block until they give up.

    Pause (cgroup freezer), not stop: a stopped container refuses connections
    immediately and the caller sees an error, while a frozen one accepts the
    connection and never answers. The second is what a hung dependency looks
    like, and it is the one that produces a timeout cascade upstream.

    `service` stays the detection ground truth - it is the one that visibly
    degrades - while `dependency` records where the fault was applied, which
    is the harder answer M3's ranker has to reach.
    """
    container = get_container(dependency)
    log.info("pausing %s (dependency of %s) for %ss", dependency, service, duration_s)
    container.pause()
    try:
        time.sleep(duration_s)
    finally:
        # Raises if it fails: a container left frozen would poison every
        # later run, so the scenario must be marked failed, loudly.
        undo_dependency_timeout(dependency)


def undo_config_error(service: str) -> None:
    # Drops only the lines this injector added, so it is idempotent and safe
    # after a restart (when Docker has already regenerated the file). Written
    # back with `cat >`: /etc/hosts is a bind mount, so it can be rewritten in
    # place but not replaced, which rules out `sed -i`.
    restore = (f"grep -v '{HOSTS_MARKER}' /etc/hosts > /tmp/hosts.restore; "
               f"cat /tmp/hosts.restore > /etc/hosts && rm -f /tmp/hosts.restore")
    result = get_container(service).exec_run(["sh", "-c", restore], user="root")
    if result.exit_code != 0:
        raise RuntimeError(f"could not restore /etc/hosts in {service}: {result.output[:200]!r}")
    log.info("restored %s /etc/hosts", service)


def assert_config_error(service: str, dependency: str) -> bool:
    """Make sure the bad entry is in place; return True if it had to be added."""
    entry = f"127.0.0.1 {dependency} {HOSTS_MARKER}"
    apply = f"grep -q '{HOSTS_MARKER}' /etc/hosts && exit 0; echo '{entry}' >> /etc/hosts && exit 3"
    result = get_container(service).exec_run(["sh", "-c", apply], user="root")
    if result.exit_code not in (0, 3):
        raise RuntimeError(f"could not edit /etc/hosts in {service}: {result.output[:200]!r}")
    return result.exit_code == 3


def run_config_error(service: str, duration_s: int, dependency: str):
    """Point a dependency's hostname at loopback in the service's /etc/hosts.

    A service configured to reach a host that will never answer is what a bad
    config value looks like from outside: connections are refused at once, so
    this produces errors where dependency_timeout produces hangs.

    The entry is re-asserted every few seconds. Docker regenerates /etc/hosts
    when a container restarts, so a caller that crashes on the refused
    connection (front-end, measured, restarts within ~10s) would otherwise
    shed the fault early while the scenario still claimed it was running. A
    real bad config survives a restart too. Each added line carries a marker
    so the undo removes exactly those lines and nothing Docker wrote.
    `dependency` is validated against a fixed set of names before it reaches
    the shell.
    """
    get_container(service)  # fail before recording anything if it is not there
    assert_config_error(service, dependency)
    log.info("%s now resolves %s to 127.0.0.1 for %ss", service, dependency, duration_s)
    deadline = time.monotonic() + duration_s
    try:
        while (remaining := deadline - time.monotonic()) > 0:
            time.sleep(min(HOSTS_REASSERT_SECONDS, remaining))
            if remaining <= HOSTS_REASSERT_SECONDS:
                break
            try:
                if assert_config_error(service, dependency):
                    log.info("%s restarted mid-fault; re-applied the bad hosts entry", service)
            except Exception as exc:
                # Mid-restart the container may briefly not exist; try again
                # on the next tick rather than abandoning the fault.
                log.warning("could not re-assert hosts entry in %s yet: %s", service, exc)
    finally:
        undo_config_error(service)


def undo_memory_exhaustion(service: str, original: int, original_swap: int) -> None:
    container = get_container(service, include_stopped=True)
    # An update cannot remove a limit: 0 means "leave unchanged" and -1 is
    # rejected (measured: "Minimum memory limit allowed is 6MB"). So an
    # unlimited original comes back as the host's total memory - the same
    # ceiling in practice - with unlimited swap.
    memory = original or docker_client.info()["MemTotal"]
    container.update(mem_limit=memory, memswap_limit=original_swap or -1)
    container.reload()
    if container.status != "running":
        # OOM-killed repeatedly, it sits in Docker's restart backoff, which
        # doubles per crash; restarting now ends the fault on schedule
        # instead of up to a minute later.
        container.restart(timeout=5)
    log.info("restored %s memory limit to %s", service, original or f"host total ({memory})")


def run_memory_exhaustion(scenario_id: str, service: str, duration_s: int, limit_mb: int):
    """Squeeze the container's memory limit below its working set.

    The apps report their own process memory, so allocating memory from
    outside the process would move nothing anyone measures. Lowering the
    cgroup limit makes the kernel do what a leak eventually causes: the OOM
    killer takes the process, and `restart: always` brings it back into the
    same limit until the fault ends. Swap is capped at the same value, or the
    process would page out instead of being killed.
    """
    container = get_container(service)
    host_config = container.attrs["HostConfig"]
    original = host_config.get("Memory") or 0
    original_swap = host_config.get("MemorySwap") or 0
    # Persisted before the limit changes, so a restart can put it back.
    annotate_scenario(scenario_id, {"original_memory": original, "original_memswap": original_swap})

    log.info("capping %s memory at %sMB for %ss (was %s)", service, limit_mb, duration_s,
             original or "unlimited")
    container.update(mem_limit=f"{limit_mb}m", memswap_limit=f"{limit_mb}m")
    try:
        time.sleep(duration_s)
    finally:
        undo_memory_exhaustion(service, original, original_swap)


def run_resource_exhaustion(service: str, duration_s: int, cpu_limit: float,
                            start_cpu_limit: float, steps: int):
    """Tighten the CPU quota gradually instead of all at once.

    `bad_deploy_latency` is a step change: the fault arrives at full strength
    and a per-sample test sees it immediately. Real resource exhaustion
    creeps, which is the case a cumulative statistic like CUSUM is supposed to
    win on (issue #34). This is the only fault in the set that degrades a
    service slowly. The schedule is geometric - see `params.ramp`.
    """
    container = get_container(service)
    period = 100000
    limits = ramp(start_cpu_limit, cpu_limit, steps)
    interval = duration_s / len(limits)
    log.info("ramping %s CPU through %s over %ss", service,
             ", ".join(f"{limit:.4f}" for limit in limits), duration_s)
    try:
        for limit in limits:
            container.update(cpu_period=period, cpu_quota=max(int(period * limit), 1000))
            time.sleep(interval)
    finally:
        undo_cpu_limit(service)


def undo(fault_type: str, service: str, params: dict) -> None:
    """Put back whatever `fault_type` changed. Safe to call when nothing did."""
    if fault_type in ("bad_deploy_latency", "resource_exhaustion"):
        undo_cpu_limit(service)
    elif fault_type == "service_crash":
        undo_service_crash(service)
    elif fault_type == "dependency_timeout":
        undo_dependency_timeout(params["dependency"])
    elif fault_type == "config_error":
        undo_config_error(service)
    elif fault_type == "memory_exhaustion":
        if "original_memory" in params:
            undo_memory_exhaustion(service, params["original_memory"], params["original_memswap"])
    # db_pool_saturation: the lock belonged to this process's session, which
    # died with it; wait_timeout reaps a session the server still thinks is open.


def recover_interrupted() -> None:
    """Undo faults a previous process was killed in the middle of.

    The `finally` in each fault only runs if this process lives to the end of
    it. A `docker compose up --build` or a crash mid-fault would otherwise
    leave a container paused, throttled, memory-capped or resolving its
    dependency to loopback - silently, and for every run after. A scenario
    still `running` at startup is by definition one that never finished, so
    its fault is undone here and the scenario marked failed.
    """
    db = ensure_db_connection()
    with db.cursor() as cur:
        cur.execute(
            "SELECT scenario_id, fault_type, ground_truth_service, params "
            "FROM fault_scenarios WHERE status = 'running'"
        )
        rows = cur.fetchall()
    for scenario_id, fault_type, service, params in rows:
        try:
            undo(fault_type, service, params or {})
            mark_failed(scenario_id, "fault-injector restarted mid-fault; fault undone at startup")
            log.warning("undid interrupted scenario %s (%s on %s)", scenario_id, fault_type, service)
        except Exception as exc:
            log.exception("could not undo interrupted scenario %s", scenario_id)
            mark_failed(scenario_id, f"fault-injector restarted mid-fault; undo failed: {exc}")


def execute(scenario_id: str, fault_type: str, service: str, params: dict):
    try:
        if fault_type == "bad_deploy_latency":
            run_bad_deploy_latency(service, params["duration_s"], params["cpu_limit"])
        elif fault_type == "service_crash":
            run_service_crash(service, params["duration_s"])
        elif fault_type == "db_pool_saturation":
            run_db_pool_saturation(service, params["duration_s"])
        elif fault_type == "dependency_timeout":
            run_dependency_timeout(service, params["duration_s"], params["dependency"])
        elif fault_type == "config_error":
            run_config_error(service, params["duration_s"], params["dependency"])
        elif fault_type == "memory_exhaustion":
            run_memory_exhaustion(scenario_id, service, params["duration_s"], params["limit_mb"])
        elif fault_type == "resource_exhaustion":
            run_resource_exhaustion(service, params["duration_s"], params["cpu_limit"],
                                    params["start_cpu_limit"], params["steps"])
        mark_recovered(scenario_id)
        log.info("scenario %s recovered", scenario_id)
    except Exception as exc:
        log.exception("scenario %s failed", scenario_id)
        mark_failed(scenario_id, str(exc))


# ---------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.get("/fault-types")
def fault_types():
    return jsonify(sorted(FAULT_TYPES))


@app.post("/faults")
def post_fault():
    body = request.get_json(force=True, silent=True) or {}
    fault_type = body.get("fault_type")
    service = body.get("service")

    try:
        params = build_params(fault_type, service, body)
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400

    # Ground truth for the evaluation runner: how much traffic the testbed
    # was actually serving when this fault landed.
    rps = offered_rps()
    params["offered_rps_at_inject"] = rps

    scenario_id = new_scenario_id(fault_type)
    try:
        record_scenario(scenario_id, fault_type, service, params)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    threading.Thread(target=execute, args=(scenario_id, fault_type, service, params), daemon=True).start()

    response = {
        "scenario_id": scenario_id, "fault_type": fault_type,
        "ground_truth_service": service, "t_inject": now_iso(),
        "params": params, "status": "running",
    }
    if rps is None:
        response["warning"] = ("could not reach the load generator - if no traffic is running, "
                               "this fault will change no metric and nothing can detect it")
    elif rps < MIN_USEFUL_RPS:
        response["warning"] = (f"testbed is nearly idle ({rps} req/s offered) - "
                               "this fault may change no metric")
    return jsonify(response), 202


@app.get("/faults")
def get_faults():
    limit = min(int(request.args.get("limit", 20)), 200)
    db = ensure_db_connection()
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM fault_scenarios ORDER BY t_inject DESC LIMIT %s", (limit,))
        rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["t_inject"] = r["t_inject"].isoformat()
        if r["t_recovered"]:
            r["t_recovered"] = r["t_recovered"].isoformat()
    return jsonify(rows)


if __name__ == "__main__":
    recover_interrupted()
    app.run(host="0.0.0.0", port=5001)
