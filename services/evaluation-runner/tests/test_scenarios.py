import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scenarios import (  # noqa: E402
    DEFAULT_SUITE,
    FULL_SUITE,
    KNOWN_SERVICES,
    MAX_DURATION_S,
    MISSING_CLASS_SCENARIOS,
    REQUIRED_PARAMS,
    SUITES,
    ScenarioSpec,
    partition_by_support,
)

ALL_SPECS = [(name, spec) for name, suite in SUITES.items() for spec in suite]

# The six classes the project plan names. Kept as a literal so that dropping
# one from the suite fails a test instead of quietly shrinking the claim.
PLANNED_CLASSES = {
    "bad_deploy_latency",
    "service_crash",
    "db_pool_saturation",
    "dependency_timeout",
    "config_error",
    "memory_exhaustion",
    "resource_exhaustion",
}


@pytest.mark.parametrize("name,spec", ALL_SPECS, ids=[f"{n}:{s.label}" for n, s in ALL_SPECS])
def test_every_scenario_is_injectable(name, spec):
    """Each spec must be something the injector's validation will accept."""
    assert spec.service in KNOWN_SERVICES, f"{name}: unknown service {spec.service}"
    assert spec.fault_type in REQUIRED_PARAMS, f"{name}: unknown fault type"
    # The injector truncates anything longer, which would make the recorded
    # fault window wider than the fault actually was and score recovery wrong.
    assert 0 < spec.duration_s <= MAX_DURATION_S, f"{name}: duration out of range"
    for key in REQUIRED_PARAMS[spec.fault_type]:
        assert key in spec.params, f"{name}: {spec.label} is missing {key}"


def test_request_body_carries_params_flat():
    spec = ScenarioSpec("memory_exhaustion", "carts", 90, {"limit_mb": 64})
    assert spec.request_body() == {
        "fault_type": "memory_exhaustion",
        "service": "carts",
        "duration_s": 90,
        "limit_mb": 64,
    }


def test_params_cannot_override_the_envelope():
    """A stray `service` key in params must not change which service is hit.

    The scenario's service is the ground-truth label the whole evaluation is
    scored against, so it has to win over anything in params.
    """
    spec = ScenarioSpec("service_crash", "payment", 60, {"service": "user"})
    assert spec.request_body()["service"] == "payment"


def test_full_suite_covers_every_planned_fault_class():
    assert {spec.fault_type for spec in FULL_SUITE} == PLANNED_CLASSES


def test_default_suite_holds_only_what_the_injector_ships_today():
    """Guard against the new classes reaching `default` before they work.

    Until the injector implements them, a scenario in `default` would fail to
    inject on every run and drag the headline detection rate down.
    """
    assert {spec.fault_type for spec in DEFAULT_SUITE} == {
        "bad_deploy_latency", "service_crash", "db_pool_saturation",
    }


def test_dependency_faults_name_a_service_other_than_the_target():
    """The point of these two classes is that cause and symptom differ."""
    for spec in MISSING_CLASS_SCENARIOS:
        if spec.fault_type in ("dependency_timeout", "config_error"):
            assert spec.params["dependency"] != spec.service


def test_resource_exhaustion_is_the_only_gradual_fault():
    """It exists to separate CUSUM from EWMA, which needs >1 step."""
    ramped = [s for s in FULL_SUITE if s.fault_type == "resource_exhaustion"]
    assert ramped, "the slow-drift scenario went missing"
    for spec in ramped:
        assert spec.params["steps"] > 1
        # Each step must outlast the 1m metric window, or the ramp reaches the
        # detector as a single step change and the comparison is meaningless.
        assert spec.duration_s / spec.params["steps"] >= 15


def test_partition_splits_on_what_the_injector_supports():
    runnable, unsupported = partition_by_support(
        FULL_SUITE, {"bad_deploy_latency", "service_crash", "db_pool_saturation"}
    )
    assert {s.fault_type for s in runnable} == {
        "bad_deploy_latency", "service_crash", "db_pool_saturation",
    }
    assert {s.fault_type for s in unsupported} == {
        "dependency_timeout", "config_error", "memory_exhaustion", "resource_exhaustion",
    }
    assert len(runnable) + len(unsupported) == len(FULL_SUITE)


def test_partition_attempts_everything_when_the_injector_cannot_be_asked():
    """None means "unknown", not "supports nothing"."""
    runnable, unsupported = partition_by_support(FULL_SUITE, None)
    assert runnable == FULL_SUITE
    assert unsupported == []


def test_partition_runs_the_whole_suite_once_the_injector_catches_up():
    runnable, unsupported = partition_by_support(FULL_SUITE, PLANNED_CLASSES)
    assert runnable == FULL_SUITE
    assert unsupported == []


def test_partition_preserves_order():
    """Scenario order is chosen so a crash never precedes its own dependents."""
    runnable, _ = partition_by_support(FULL_SUITE, PLANNED_CLASSES)
    assert [s.label for s in runnable] == [s.label for s in FULL_SUITE]
