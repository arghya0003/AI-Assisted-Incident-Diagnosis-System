import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from params import (  # noqa: E402
    FAULT_TYPES, MAX_DURATION_SECONDS, MIN_MEMORY_LIMIT_MB, build_params, ramp,
)


def test_all_seven_fault_classes_are_offered():
    assert FAULT_TYPES == {
        "bad_deploy_latency", "service_crash", "db_pool_saturation", "dependency_timeout",
        "config_error", "memory_exhaustion", "resource_exhaustion",
    }


@pytest.mark.parametrize("fault_type, service", [("nope", "catalogue"), ("service_crash", "kafka")])
def test_unknown_fault_type_or_service_is_rejected(fault_type, service):
    with pytest.raises(ValueError):
        build_params(fault_type, service, {})


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
