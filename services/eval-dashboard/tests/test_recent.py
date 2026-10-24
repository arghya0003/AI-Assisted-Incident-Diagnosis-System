import csv
from datetime import datetime, timedelta, timezone

import pytest

import recent

NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)


def ago(minutes: float) -> datetime:
    return NOW - timedelta(minutes=minutes)


def anomaly(**over) -> dict:
    row = {
        "anomaly_id": "anom-1", "detector": "ewma", "services": ["catalogue"],
        "metrics": ["latency_p95_ms"], "severity": "high",
        "t_onset": ago(31), "t_detected": ago(30),
    }
    row.update(over)
    return row


INJECTED = {"scenario_id": "sc-1", "fault_type": "bad_deploy_latency",
            "ground_truth_service": "catalogue", "t_inject": ago(30.5)}

HYPS = [
    {"rank": 1, "service": "catalogue", "cause": "slow deploy", "confidence": 0.84,
     "proposed_action": "rollback_deploy:dep-1"},
    {"rank": 2, "service": "front-end", "cause": "caller", "confidence": 0.3,
     "proposed_action": "restart_service:front-end"},
]


# ------------------------------------------------------------- resolution

def test_injected_fault_is_resolved_when_the_injector_recorded_recovery():
    status, at, basis = recent.resolution(anomaly(**INJECTED, t_recovered=ago(28)), NOW)
    assert (status, at) == ("resolved", ago(28))
    assert "injector" in basis


def test_injected_fault_still_running_is_ongoing_even_after_the_quiet_window():
    status, at, _ = recent.resolution(anomaly(**INJECTED, fault_status="running"), NOW)
    assert (status, at) == ("ongoing", None)


def test_injected_fault_with_no_recovery_and_odd_status_is_unknown():
    status, _, basis = recent.resolution(anomaly(**INJECTED, fault_status="failed"), NOW)
    assert status == "unknown" and "failed" in basis


def test_unattributed_anomaly_that_came_back_within_the_quiet_window_recurred():
    row = anomaly(next_anomaly_at=ago(26))
    status, at, basis = recent.resolution(row, NOW)
    assert (status, at) == ("recurred", None)
    assert "4 min later" in basis


def test_unattributed_anomaly_quiet_long_enough_is_resolved_and_says_it_is_inferred():
    status, at, basis = recent.resolution(anomaly(next_anomaly_at=ago(5)), NOW)
    assert (status, at) == ("resolved", None)
    assert basis.startswith("inferred")


def test_unattributed_anomaly_inside_the_quiet_window_is_still_being_watched():
    status, _, _ = recent.resolution(anomaly(t_detected=ago(3)), NOW)
    assert status == "ongoing"


def test_silent_service_is_resolved_only_once_it_reports_again():
    row = anomaly(metrics=["liveness"], detector="staleness")
    assert recent.resolution(row, NOW)[0] == "ongoing"
    # Quiet for 30 minutes would count as resolved for a latency anomaly; a dead
    # service is quiet too, so silence must not be read as recovery here.
    status, at, basis = recent.resolution({**row, "reporting_again_at": ago(29)}, NOW)
    assert (status, at) == ("resolved", ago(29))
    assert "reporting" in basis


def test_silent_service_older_than_metrics_retention_is_unknown_not_ongoing():
    row = anomaly(metrics=["liveness"], t_detected=NOW - timedelta(hours=25))
    assert recent.resolution(row, NOW)[0] == "unknown"


# --------------------------------------------------------------------- fix

def test_approved_fix_is_the_chosen_hypothesis_and_says_it_was_not_executed():
    row = anomaly(decision="approved", decided_by="harshit", decided_hypothesis_rank=1,
                  incident_hypotheses=HYPS, incident_state="APPROVED", executed=False)
    action, status = recent.fix(row, recent.diagnosis(row))
    assert action == "rollback_deploy:dep-1"
    assert status == "approved by harshit - logged, not executed"


def test_approving_a_lower_ranked_hypothesis_records_that_one_and_its_rank():
    row = anomaly(decision="approved", decided_by="op", decided_hypothesis_rank=2,
                  incident_hypotheses=HYPS)
    action, status = recent.fix(row, recent.diagnosis(row))
    assert action == "restart_service:front-end"
    assert "(hypothesis #2)" in status


@pytest.mark.parametrize("state,expected", [
    ("AWAITING_APPROVAL", "awaiting approval"),
    ("EXPIRED", "expired without a decision"),
    ("ANALYSIS_FAILED", "diagnosis failed"),
    ("ANALYZING", "being diagnosed"),
])
def test_no_fix_is_claimed_without_an_approval(state, expected):
    row = anomaly(incident_state=state, incident_hypotheses=HYPS)
    assert recent.fix(row, recent.diagnosis(row)) == ("", expected)


def test_fix_service_is_the_approved_hypothesis_service_even_when_not_rank_1():
    row = anomaly(decision="approved", decided_hypothesis_rank=2, incident_hypotheses=HYPS)
    assert recent.fix_service(row, recent.diagnosis(row)) == "front-end"


def test_fix_service_is_empty_without_an_approval():
    # The diagnosis-service reads this column as a confirmed root cause, so a
    # proposal, an expiry or a rejection must never fill it.
    for over in ({"decision": "rejected"}, {"incident_state": "EXPIRED"}, {}):
        row = anomaly(incident_hypotheses=HYPS, **over)
        assert recent.fix_service(row, recent.diagnosis(row)) == ""


def test_rejected_incident_has_no_fix():
    row = anomaly(decision="rejected", decided_by="op", incident_state="REJECTED")
    assert recent.fix(row, recent.diagnosis(row)) == ("", "rejected by op")


def test_no_incident_distinguishes_diagnosed_from_not():
    assert recent.fix(anomaly(), {})[1] == "no incident was opened"
    assert recent.fix(anomaly(), {"service": "catalogue"})[1] == "diagnosed, but no incident was opened"


# --------------------------------------------------------------- diagnosis

def test_the_incident_copy_of_the_diagnosis_wins_over_a_later_stored_one():
    row = anomaly(incident_hypotheses=HYPS, incident_answered_by="llm",
                  stored_service="payment", stored_cause="re-run in another mode")
    d = recent.diagnosis(row)
    assert (d["service"], d["answered_by"]) == ("catalogue", "llm")


def test_stored_diagnosis_is_used_when_no_incident_was_opened():
    row = anomaly(stored_service="payment", stored_action="no_action",
                  stored_answered_by="deterministic_fallback")
    d = recent.diagnosis(row)
    assert (d["service"], d["action"], d["answered_by"]) == (
        "payment", "no_action", "deterministic_fallback")


# --------------------------------------------------------------- summarise

def test_summarise_produces_every_column_as_a_string():
    row = anomaly(**INJECTED, t_recovered=ago(28), incident_hypotheses=HYPS,
                  incident_id="inc-1", incident_state="APPROVED", decision="approved",
                  decided_by="op", decided_hypothesis_rank=1)
    out = recent.summarise(row, NOW)
    assert list(out) == recent.COLUMNS
    assert all(isinstance(v, str) for v in out.values())
    assert out["cause_source"] == "injected_fault"
    assert out["diagnosis_correct"] == "yes"
    assert out["detect_seconds"] == "30.0"
    assert out["services"] == "catalogue"
    assert out["fix"] == "rollback_deploy:dep-1"
    assert out["resolution"] == "resolved"


def test_summarise_leaves_correctness_blank_without_ground_truth():
    out = recent.summarise(anomaly(stored_service="catalogue"), NOW)
    assert (out["cause_source"], out["diagnosis_correct"]) == ("unattributed", "")


# ----------------------------------------------------------------- dataset

def read(path):
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_dataset_keeps_rows_the_database_no_longer_has(tmp_path):
    path = tmp_path / "out" / "anomaly_dataset.csv"
    old = recent.summarise(anomaly(anomaly_id="anom-old", t_detected=ago(600)), NOW)
    recent.write_dataset(path, [old])

    # A volume wipe: the database now only knows about a newer anomaly.
    new = recent.summarise(anomaly(anomaly_id="anom-new", t_detected=ago(1)), NOW)
    assert recent.write_dataset(path, [new]) == 2
    assert [r["anomaly_id"] for r in read(path)] == ["anom-new", "anom-old"]


def test_dataset_updates_a_row_in_place_when_its_resolution_changes(tmp_path):
    path = tmp_path / "anomaly_dataset.csv"
    row = anomaly(t_detected=ago(3))
    recent.write_dataset(path, [recent.summarise(row, NOW)])
    assert read(path)[0]["resolution"] == "ongoing"

    later = NOW + timedelta(minutes=20)
    recent.write_dataset(path, [recent.summarise(row, later)])
    rows = read(path)
    assert len(rows) == 1 and rows[0]["resolution"] == "resolved"


def test_dataset_header_is_the_column_list_and_no_temp_file_is_left(tmp_path):
    path = tmp_path / "anomaly_dataset.csv"
    recent.write_dataset(path, [])
    assert path.read_text(encoding="utf-8").splitlines()[0] == ",".join(recent.COLUMNS)
    assert not (tmp_path / "anomaly_dataset.csv.tmp").exists()
