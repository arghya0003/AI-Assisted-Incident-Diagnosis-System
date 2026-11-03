import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from params import (  # noqa: E402
    APP_FAULTS, FAULT_TYPES, MAX_DURATION_SECONDS, MIN_MEMORY_LIMIT_MB, build_params, ramp,
    rate_query, site_entry,
)


def test_all_seven_fault_classes_and_the_app_fault_are_offered():
    assert FAULT_TYPES == {
        "bad_deploy_latency", "service_crash", "db_pool_saturation", "dependency_timeout",
        "config_error", "memory_exhaustion", "resource_exhaustion", "app_fault",
    }


def test_each_app_fault_defaults_to_its_own_service_and_effect():
    slow = build_params("app_fault", "stream-storage", {"mode": "slow_storage"})
    assert (slow["extra_latency_ms"], slow["error_rate"], slow["decay_s"]) == (400.0, 0.0, 0.0)

    flaky = build_params("app_fault", "ci-runner", {"mode": "flaky_build"})
    assert (flaky["extra_latency_ms"], flaky["error_rate"]) == (0.0, 0.3)


def test_a_flushed_cache_warms_back_up_over_the_fault_duration():
    params = build_params("app_fault", "stream-cache", {"mode": "cache_flush", "duration_s": 90})
    assert params["decay_s"] == 90.0 and params["extra_latency_ms"] > 0


def test_a_stuck_runner_is_slow_enough_that_two_slots_cannot_keep_up():
    """60 ms + the extra must exceed 2 slots / 5 req/s = 400 ms, or no queue builds."""
    params = build_params("app_fault", "ci-runner", {"mode": "stuck_runner"})
    assert 60 + params["extra_latency_ms"] > 400


def test_an_app_fault_only_applies_where_it_makes_sense():
    with pytest.raises(ValueError, match="stream-storage"):
        build_params("app_fault", "ci-runner", {"mode": "slow_storage"})
    with pytest.raises(ValueError, match="applies to"):
        build_params("app_fault", "catalogue", {"mode": "flaky_build"})


def test_an_app_fault_needs_a_known_mode():
    for body in ({}, {"mode": "melt"}):
        with pytest.raises(ValueError, match="mode must be one of"):
            build_params("app_fault", "ci-runner", body)


def test_an_app_fault_can_be_made_stronger_or_milder():
    mild = build_params("app_fault", "stream-storage", {"mode": "slow_storage", "extra_latency_ms": 80})
    assert mild["extra_latency_ms"] == 80.0


def test_an_app_fault_that_would_change_nothing_is_refused():
    with pytest.raises(ValueError, match="above zero"):
        build_params("app_fault", "stream-storage", {"mode": "slow_storage", "extra_latency_ms": 0})


def test_every_app_fault_targets_a_service_the_injector_knows():
    from params import KNOWN_SERVICES
    assert all(s in KNOWN_SERVICES for preset in APP_FAULTS.values() for s in preset["services"])


def test_a_sites_offered_load_is_read_at_its_entry_service():
    assert site_entry("stream-cache") == "stream-web:8080"
    assert site_entry("ci-runner") == "ci-web:8080"
    assert site_entry("catalogue") is None


def test_the_rate_query_asks_for_one_instance():
    assert 'instance="ci-web:8080"' in rate_query("ci-web:8080")


@pytest.mark.parametrize("fault_type, service", [("nope", "catalogue"), ("service_crash", "kafka")])
def test_unknown_fault_type_or_service_is_rejected(fault_type, service):
    with pytest.raises(ValueError):
        build_params(fault_type, service, {})


def test_a_dependency_fault_on_the_streaming_site_defaults_to_its_next_hop():
    assert build_params("dependency_timeout", "stream-web", {})["dependency"] == "stream-playback"
    assert build_params("config_error", "stream-playback", {})["dependency"] == "stream-cache"


def test_a_dependency_fault_on_the_cicd_site_defaults_to_its_next_hop():
    assert build_params("dependency_timeout", "ci-scheduler", {})["dependency"] == "ci-runner"


@pytest.mark.parametrize("leaf", ["stream-storage", "stream-catalog", "ci-runner", "ci-deployer"])
def test_a_leaf_service_has_no_dependency_to_fault(leaf):
    with pytest.raises(ValueError):
        build_params("dependency_timeout", leaf, {})


def test_db_pool_saturation_stays_catalogue_only():
    with pytest.raises(ValueError):
        build_params("db_pool_saturation", "stream-storage", {})


def test_duration_is_capped():
    assert build_params("service_crash", "payment", {"duration_s": 9999})["duration_s"] == MAX_DURATION_SECONDS


def test_db_pool_saturation_is_a_table_lock_and_ignores_connections():
    params = build_params("db_pool_saturation", "catalogue", {"connections": 155})
    assert params["mechanism"] == "table_lock"
    assert "connections" not in params


def test_db_pool_saturation_only_targets_catalogue():
    with pytest.raises(ValueError):
        build_params("db_pool_saturation", "carts", {})


def test_dependency_defaults_from_the_graph():
    assert build_params("dependency_timeout", "catalogue", {})["dependency"] == "catalogue-db"
    assert build_params("config_error", "front-end", {})["dependency"] == "carts"


def test_service_without_a_dependency_must_name_one():
    with pytest.raises(ValueError):
        build_params("dependency_timeout", "payment", {})


@pytest.mark.parametrize("dependency", ["timescaledb", "kafka", "fault-injector", "x; rm -rf /"])
def test_dependency_cannot_reach_outside_the_testbed(dependency):
    # Also what keeps config_error's shell command safe: only fixed names pass.
    with pytest.raises(ValueError):
        build_params("config_error", "front-end", {"dependency": dependency})


def test_dependency_cannot_be_the_service_itself():
    with pytest.raises(ValueError):
        build_params("dependency_timeout", "catalogue", {"dependency": "catalogue"})


def test_memory_limit_has_a_floor():
    assert build_params("memory_exhaustion", "carts", {"limit_mb": 8})["limit_mb"] == MIN_MEMORY_LIMIT_MB


def test_resource_exhaustion_ramp_only_tightens():
    with pytest.raises(ValueError):
        build_params("resource_exhaustion", "user", {"cpu_limit": 0.1, "start_cpu_limit": 0.05})


def test_ramp_is_geometric_and_ends_at_the_target():
    limits = ramp(0.05, 0.002, 6)
    assert len(limits) == 6
    assert limits[-1] == 0.002
    assert all(a > b for a, b in zip(limits, limits[1:]))
    ratios = [b / a for a, b in zip(limits, limits[1:])]
    assert max(ratios) - min(ratios) < 1e-9
    # A linear ramp would still be above 0.01 at step 5 of 6; this one is not.
    assert limits[3] < 0.01


def test_single_step_ramp_is_the_target():
    assert ramp(0.05, 0.002, 1) == [0.002]
