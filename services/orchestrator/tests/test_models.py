import pytest
from pydantic import ValidationError

from app.models import Hypothesis, action_verb, parse_action


@pytest.mark.parametrize(
    "action",
    ["rollback_deploy:dep-1", "restart_service:catalogue", "scale_service:carts", "no_action"],
)
def test_valid_actions_accepted(action):
    h = Hypothesis(rank=1, cause="x", confidence=0.5, evidence_ids=["a"], proposed_action=action)
    assert h.proposed_action == action


@pytest.mark.parametrize(
    "action",
    ["delete_service:catalogue", "rollback_deploy", "no_action:extra", "", "rm -rf /"],
)
def test_invalid_actions_rejected(action):
    with pytest.raises(ValidationError):
        Hypothesis(rank=1, cause="x", confidence=0.5, evidence_ids=["a"], proposed_action=action)


def test_hypothesis_requires_at_least_one_evidence_id():
    with pytest.raises(ValidationError):
        Hypothesis(rank=1, cause="x", confidence=0.5, evidence_ids=[], proposed_action="no_action")


def test_blast_radius_is_always_single_service_or_none():
    for action in ["rollback_deploy:dep-1", "restart_service:catalogue", "scale_service:carts", "no_action"]:
        h = Hypothesis(rank=1, cause="x", confidence=0.5, evidence_ids=["a"], proposed_action=action)
        assert "single-service" in h.blast_radius or h.blast_radius == "none"


def test_parse_action_splits_verb_and_target():
    assert parse_action("rollback_deploy:dep-1") == ("rollback_deploy", "dep-1")
    assert parse_action("no_action") == ("no_action", None)
    assert action_verb("scale_service:carts") == "scale_service"
