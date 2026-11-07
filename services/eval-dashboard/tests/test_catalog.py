"""What the inject tab offers for each system, and that the injector accepts all of it.

The injector is the authority on what a fault may target. The dashboard keeps its
own table so the form can narrow without a round trip, which means the two can
drift; these tests compare them so that happens in CI and not in front of a user.
"""
import importlib.util
from pathlib import Path

import pytest

import main

INJECTOR_PARAMS = Path(__file__).resolve().parents[2] / "fault-injector" / "params.py"


@pytest.fixture(scope="module")
def injector():
    if not INJECTOR_PARAMS.exists():
        pytest.skip(f"{INJECTOR_PARAMS} not found - run from a full checkout")
    spec = importlib.util.spec_from_file_location("injector_params", INJECTOR_PARAMS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def catalog():
    return main.injection_catalog()


def site(catalog, key):
    return next(s for s in catalog["sites"] if s["key"] == key)


def targets(catalog, key, fault):
    return next(f for f in site(catalog, key)["faults"] if f["type"] == fault)["services"]


def test_the_three_systems_partition_the_services(catalog):
    seen = [s for entry in catalog["sites"] for s in entry["services"]]
    assert sorted(seen) == sorted(main.KNOWN_SERVICES)
    assert len(seen) == len(set(seen)), "a service sits under two systems"


def test_sock_shop_has_the_database_fault_and_no_application_fault(catalog):
    shop = {f["type"] for f in site(catalog, "shop")["faults"]}
    assert "db_pool_saturation" in shop
    assert "app_fault" not in shop
    assert targets(catalog, "shop", "db_pool_saturation") == ["catalogue"]


@pytest.mark.parametrize("key", ["streaming", "cicd"])
def test_the_dummy_sites_have_application_faults_and_no_database_fault(catalog, key):
    offered = {f["type"] for f in site(catalog, key)["faults"]}
    assert "app_fault" in offered
    assert "db_pool_saturation" not in offered


def test_each_application_fault_mode_stays_on_its_own_site(catalog):
    assert targets(catalog, "streaming", "app_fault") == ["stream-cache", "stream-storage"]
    assert targets(catalog, "cicd", "app_fault") == ["ci-runner", "ci-deployer"]


def test_dependency_faults_are_not_offered_on_a_service_with_nothing_downstream(catalog):
    for fault in ("dependency_timeout", "config_error"):
        offered = targets(catalog, "streaming", fault)
        assert "stream-storage" not in offered and "stream-catalog" not in offered
        assert "stream-web" in offered
        assert "payment" not in targets(catalog, "shop", fault)


def test_every_service_is_accounted_for_under_every_fault(catalog):
    """Applicable or blocked-with-a-reason; never dropped, never both."""
    for entry in catalog["sites"]:
        for fault in entry["faults"]:
            applicable = set(fault["services"])
            blocked = set(fault["unavailable"])
            assert applicable | blocked == set(entry["services"]), (entry["key"], fault["type"])
            assert not applicable & blocked, (entry["key"], fault["type"])
            assert all(fault["unavailable"].values()), "a blocked service has no reason"


def test_blocked_services_carry_the_right_reason(catalog):
    assert "MySQL" in site(catalog, "shop")["faults"][-1]["unavailable"]["payment"]
    streaming = {f["type"]: f for f in site(catalog, "streaming")["faults"]}
    assert "downstream" in streaming["dependency_timeout"]["unavailable"]["stream-storage"]
    assert "application fault" in streaming["app_fault"]["unavailable"]["stream-web"]


def test_a_system_says_why_a_fault_is_missing(catalog):
    for entry in catalog["sites"]:
        offered = {f["type"] for f in entry["faults"]}
        for fault in entry["not_applicable"]:
            assert fault not in offered


# ----------------------------------------- the dashboard against the injector

def test_service_list_matches_the_injector(injector):
    assert main.KNOWN_SERVICES == list(injector.KNOWN_SERVICES)


def test_every_injector_fault_type_is_offered_somewhere(catalog, injector):
    offered = {f["type"] for entry in catalog["sites"] for f in entry["faults"]}
    assert offered == set(injector.FAULT_TYPES)


def test_application_fault_modes_match_the_injector(injector):
    assert {m: sorted(s) for m, s in main.APP_FAULT_MODES.items()} == \
           {m: sorted(p["services"]) for m, p in injector.APP_FAULTS.items()}


def test_default_dependencies_match_the_injector(injector):
    assert main.DEFAULT_DEPENDENCY == injector.DEPENDENCIES


def test_everything_the_form_offers_is_accepted_by_the_injector(catalog, injector):
    """Offered means valid: no (fault, service) pair on the form should be a 400."""
    checked = 0
    for entry in catalog["sites"]:
        for fault in entry["faults"]:
            for service in fault["services"]:
                if fault["type"] == "app_fault":
                    modes = [m for m, svcs in main.APP_FAULT_MODES.items() if service in svcs]
                    bodies = [{"mode": m} for m in modes]
                else:
                    bodies = [{}]
                for body in bodies:
                    # Raises ValueError, failing the test, if the injector refuses it.
                    injector.build_params(fault["type"], service, {"duration_s": 60, **body})
                    checked += 1
    assert checked > 50     # guards against the loop quietly checking nothing


def test_fault_types_endpoint_carries_the_catalog(monkeypatch):
    class Reply:
        def json(self):
            return ["service_crash"]

    monkeypatch.setattr(main.requests, "get", lambda url, timeout: Reply())
    body = main.app.test_client().get("/api/fault-types").get_json()

    assert body["fault_types"] == ["service_crash"]       # the existing keys are unchanged
    assert body["services"] == main.KNOWN_SERVICES
    assert [s["key"] for s in body["catalog"]["sites"]] == ["shop", "streaming", "cicd"]
