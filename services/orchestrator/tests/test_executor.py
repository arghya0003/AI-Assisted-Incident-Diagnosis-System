from app.executor import build_intent, execute
from tests.conftest import make_hypothesis


def test_build_intent_splits_the_proposed_action():
    intent = build_intent("inc-1", make_hypothesis(action="rollback_deploy:dep-42"), "alice")
    assert intent.verb == "rollback_deploy"
    assert intent.target == "dep-42"
    assert intent.approved_by == "alice"
    assert "single-service" in intent.blast_radius


def test_build_intent_handles_no_action():
    intent = build_intent("inc-1", make_hypothesis(action="no_action"), "alice")
    assert intent.verb == "no_action"
    assert intent.target is None
    assert intent.blast_radius == "none"


def test_execute_never_marks_anything_as_executed():
    intent = build_intent("inc-1", make_hypothesis(action="restart_service:carts"), "bob")
    result = execute(intent)
    assert result["executed"] is False
    assert result["verb"] == "restart_service"
    assert result["target"] == "carts"
    assert "never acts" in result["note"]


def test_executor_module_has_no_outbound_capability():
    """The safety gate is that this module cannot reach the running system, not merely that
    it chooses not to. Parse its imports (not a raw text search, which would also flag this
    module's own explanatory comments) and assert none of them could reach a control plane --
    so a future edit that quietly adds a docker/kubernetes/httpx/subprocess client fails here."""
    import ast

    import app.executor as executor_module

    banned_modules = {"docker", "kubernetes", "httpx", "requests", "subprocess", "socket", "paramiko", "os"}
    tree = ast.parse(open(executor_module.__file__).read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    forbidden = imported & banned_modules
    assert not forbidden, f"executor.py must not import {forbidden}: it must have no way to act on the running system"
