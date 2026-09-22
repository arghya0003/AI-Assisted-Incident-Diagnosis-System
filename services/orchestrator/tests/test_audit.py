from app.audit import GENESIS_HASH, hash_entry


def test_hash_is_deterministic_for_the_same_content():
    h1 = hash_entry(GENESIS_HASH, 1, "inc-1", "INCIDENT_DETECTED", "orchestrator", {"a": 1})
    h2 = hash_entry(GENESIS_HASH, 1, "inc-1", "INCIDENT_DETECTED", "orchestrator", {"a": 1})
    assert h1 == h2


def test_hash_changes_if_any_field_changes():
    base = hash_entry(GENESIS_HASH, 1, "inc-1", "INCIDENT_DETECTED", "orchestrator", {"a": 1})
    assert hash_entry(GENESIS_HASH, 1, "inc-1", "INCIDENT_DETECTED", "orchestrator", {"a": 2}) != base
    assert hash_entry(GENESIS_HASH, 1, "inc-1", "INCIDENT_DETECTED", "someone-else", {"a": 1}) != base
    assert hash_entry(GENESIS_HASH, 2, "inc-1", "INCIDENT_DETECTED", "orchestrator", {"a": 1}) != base
    assert hash_entry(GENESIS_HASH, 1, "inc-2", "INCIDENT_DETECTED", "orchestrator", {"a": 1}) != base
    assert hash_entry("f" * 64, 1, "inc-1", "INCIDENT_DETECTED", "orchestrator", {"a": 1}) != base


def test_hash_is_insensitive_to_dict_key_order():
    h1 = hash_entry(GENESIS_HASH, 1, "inc-1", "E", "actor", {"a": 1, "b": 2})
    h2 = hash_entry(GENESIS_HASH, 1, "inc-1", "E", "actor", {"b": 2, "a": 1})
    assert h1 == h2


def test_chain_detects_a_swapped_row(fake_store):
    fake_store.record_event("A", "orchestrator", {"n": 1})
    fake_store.record_event("B", "orchestrator", {"n": 2})
    fake_store.record_event("C", "orchestrator", {"n": 3})
    ok, broken_at = fake_store.verify_audit_chain()
    assert ok is True
    assert broken_at is None

    # Tamper with the middle entry directly, bypassing the append-only API -- the same thing
    # the Postgres trigger in 008_incidents.sql exists to block in the real store.
    fake_store._audit[1].detail["n"] = 999
    ok, broken_at = fake_store.verify_audit_chain()
    assert ok is False
    assert broken_at == fake_store._audit[1].audit_id
