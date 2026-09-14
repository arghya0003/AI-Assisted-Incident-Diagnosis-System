"""Phase 6: prompt content, per-candidate citable ids and actions, the response schema, and the
context budget."""

import dataclasses
import string

import pytest

from app.fixtures import load_fixtures
from app.graph import load_graph
from app.hypotheses import candidate_options, rollback_window_minutes
from app.models import SimilarIncident
from app.prompts import (
    LLM_ONLY_SYSTEM_PROMPT,
    PROMPTS_DIR,
    SYSTEM_PROMPT,
    PromptTooLarge,
    build_llm_only_prompt,
    build_prompt,
    estimate_tokens,
    render_prompt,
    retry_message,
)
from app.scoring import ScoringConfig, score_candidates
from app.settings import settings

GRAPH = load_graph()
CONFIG = ScoringConfig.from_settings(settings)
FIXTURES = {f.event.anomaly_id: f for f in load_fixtures()}
BIG = {"context_tokens": 100_000, "response_reserve_tokens": 1024}

INCIDENT = SimilarIncident(
    incident_id="incident-0007",
    title="shipping release retries queue publishes without backoff",
    services=["shipping"],
    fault_type="bad_deploy_latency",
    source="synthetic",
    similarity=0.74,
    root_cause="The release retried failed publishes to rabbitmq immediately in a tight loop.",
    resolution="Rolled back shipping; the retry was changed to exponential backoff. " * 5,
)


def report_for(anomaly_id, incidents=()):
    inputs = dataclasses.replace(FIXTURES[anomaly_id].scoring_inputs(), similar_incidents=list(incidents))
    return inputs.anomaly, score_candidates(inputs, GRAPH, CONFIG)


def user_text(prompt):
    return prompt.messages[1]["content"]


def options_by_service(prompt):
    return {option.service: option for option in prompt.options}


def test_prompt_files_exist_and_every_placeholder_is_filled():
    for name in ("analyze_system.txt", "analyze_user.txt", "analyze_retry.txt",
                 "analyze_llm_only_system.txt", "analyze_llm_only_user.txt"):
        assert (PROMPTS_DIR / name).is_file()
    anomaly, report = report_for("anom-fx-01")
    prompt = build_prompt(anomaly, report, **BIG)
    assert prompt.messages[0] == {"role": "system", "content": SYSTEM_PROMPT}
    leftovers = [m for m in string.Template.pattern.finditer(user_text(prompt)) if m.group("named")]
    assert leftovers == []


def test_prompt_carries_the_anomaly_candidates_and_deploy_diff():
    anomaly, report = report_for("anom-fx-01")
    text = user_text(build_prompt(anomaly, report, **BIG))
    for fact in ("anom-fx-01", "latency_p95_ms", "catalogue (service)", "dep-fx-01-inj",
                 "perf regression: inefficient loop introduced", "anom-fx-01-p99",
                 "not provided by the anomaly detector", "may cite:", "may propose:"):
        assert fact in text


def test_each_candidate_may_cite_only_its_own_evidence():
    anomaly, report = report_for("anom-fx-01")
    options = options_by_service(build_prompt(anomaly, report, **BIG))
    assert options["catalogue"].citable_ids == ["anom-fx-01", "dep-fx-01-inj", "anom-fx-01-p99"]
    assert options["catalogue-db"].citable_ids == ["anom-fx-01"]


def test_rollback_is_offered_only_for_a_deploy_shortly_before_onset():
    anomaly, report = report_for("anom-fx-01")
    options = options_by_service(build_prompt(anomaly, report, **BIG))
    assert options["catalogue"].actions == ["no_action", "rollback_deploy:dep-fx-01-inj"]
    assert options["catalogue-db"].actions == ["no_action"]

    # fx-07's deploys (payment 8 min, front-end 12 min) are citable but too old to roll back.
    anomaly, report = report_for("anom-fx-07")
    options = options_by_service(build_prompt(anomaly, report, **BIG))
    assert "dep-fx-07-bg2" in options["front-end"].citable_ids
    assert all(option.actions == ["no_action"] for option in options.values())


def test_restart_and_scale_are_never_offered():
    for anomaly_id in FIXTURES:
        anomaly, report = report_for(anomaly_id)
        actions = build_prompt(anomaly, report, **BIG).allowed_actions
        assert not [a for a in actions if a.startswith(("restart_service:", "scale_service:"))]


def test_prompt_shows_the_top_five_candidates_only():
    anomaly, report = report_for("anom-fx-08", [INCIDENT])
    prompt = build_prompt(anomaly, report, **BIG)
    assert [o.service for o in prompt.options] == [c.service for c in report.candidates[:5]]
    assert prompt.citable_ids[0] == "anom-fx-08"
    hidden = {c.deploy_id for c in report.candidates[5:] if c.deploy_id}
    assert not hidden & set(prompt.citable_ids)


def test_schema_binds_each_hypothesis_to_one_candidate():
    anomaly, report = report_for("anom-fx-01")
    prompt = build_prompt(anomaly, report, **BIG)
    hypotheses = prompt.schema["properties"]["hypotheses"]
    assert hypotheses["maxItems"] == 3
    variants = [v["properties"] for v in hypotheses["items"]["anyOf"]]
    assert len(variants) == len(prompt.options)
    for variant, option in zip(variants, prompt.options):
        assert variant["service"]["enum"] == [option.service]
        assert variant["evidence_ids"]["items"]["enum"] == option.citable_ids
        assert variant["proposed_action"]["enum"] == option.actions
        # Bounded, so constrained decoding can't loop on a repeated id or an endless cause.
        assert variant["evidence_ids"]["maxItems"] == min(6, len(option.citable_ids))
        assert variant["cause"]["maxLength"] == 400


def test_prompt_options_match_candidate_options():
    anomaly, report = report_for("anom-fx-08")
    prompt = build_prompt(anomaly, report, **BIG)
    assert prompt.options == [candidate_options(report, c) for c in report.candidates[:5]]


def test_past_incidents_appear_without_their_root_cause_or_resolution_text():
    anomaly, report = report_for("anom-fx-08", [INCIDENT])
    text = user_text(build_prompt(anomaly, report, **BIG))
    assert "incident-0007: shipping release retries queue publishes without backoff" in text
    assert "fault type: bad_deploy_latency" in text and "root cause in: shipping" in text
    assert "tight loop" not in text and "exponential backoff" not in text


def test_candidate_options_know_whether_a_deploy_exists():
    anomaly, report = report_for("anom-fx-07")
    options = options_by_service(build_prompt(anomaly, report, **BIG))
    assert options["front-end"].has_recent_deploy  # a routine deploy 12 minutes earlier
    assert not options["catalogue"].has_recent_deploy


def test_prompt_without_retrieval_says_so():
    anomaly, report = report_for("anom-fx-09")
    assert "none retrieved" in user_text(build_prompt(anomaly, report, **BIG))


def test_estimated_tokens_cover_system_and_user_text():
    anomaly, report = report_for("anom-fx-01")
    prompt = build_prompt(anomaly, report, **BIG)
    assert prompt.estimated_tokens == estimate_tokens(SYSTEM_PROMPT + user_text(prompt))
    assert prompt.truncations == []


def test_context_budget_truncates_in_the_planned_order():
    anomaly, report = report_for("anom-fx-08", [INCIDENT])

    def size(**options):
        return render_prompt(anomaly, report, **options).estimated_tokens

    full = size(candidates=5, config_diffs=True)
    no_diffs = size(candidates=5, config_diffs=False)
    three = size(candidates=3, config_diffs=False)
    assert full > no_diffs > three

    def build(total_tokens):
        return build_prompt(anomaly, report, context_tokens=total_tokens + 1024, response_reserve_tokens=1024)

    assert build(full).truncations == []

    second = build(no_diffs)
    assert second.truncations == ["dropped deploy config diffs"]
    assert "perf regression" not in user_text(second)

    third = build(three)
    assert third.truncations[-1] == "kept only the top 3 candidates"
    assert len(third.options) == 3 and "\n4. " not in user_text(third)

    with pytest.raises(PromptTooLarge):
        build(three - 1)


def llm_only_prompt(anomaly_id, context_tokens=100_000):
    inputs = FIXTURES[anomaly_id].scoring_inputs()
    return build_llm_only_prompt(
        inputs.anomaly,
        inputs.related,
        inputs.deploys,
        sorted(GRAPH.nodes),
        window_seconds=120,
        lookback_minutes=30,
        rollback_max_minutes=rollback_window_minutes(10),
        context_tokens=context_tokens,
        response_reserve_tokens=1024,
    )


def test_llm_only_prompt_has_no_scores_graph_or_past_incidents():
    prompt = llm_only_prompt("anom-fx-08")
    text = user_text(prompt).lower()
    assert prompt.messages[0]["content"] == LLM_ONLY_SYSTEM_PROMPT
    for absent in ("score", "downstream", "deepest", "incident", "position"):
        assert absent not in text, absent
    assert [o.service for o in prompt.options] == sorted(GRAPH.nodes)
    assert "dep-fx-08-inj" in text and "perf regression" in text


def test_llm_only_options_follow_the_same_rollback_rule_from_raw_deploy_times():
    assert rollback_window_minutes(10) == pytest.approx(6.93, abs=0.01)
    options = options_by_service(llm_only_prompt("anom-fx-01"))
    assert options["catalogue"].actions == ["no_action", "rollback_deploy:dep-fx-01-inj"]  # 14 s before onset
    assert "dep-fx-01-bg3" in options["catalogue"].citable_ids  # 18.8 min: citable, too old to roll back
    assert "anom-fx-01-p99" in options["catalogue"].citable_ids
    assert options["user"].citable_ids == ["anom-fx-01"] and not options["user"].has_recent_deploy


def test_llm_only_prompt_drops_config_diffs_when_over_budget():
    full = llm_only_prompt("anom-fx-08")
    trimmed = llm_only_prompt("anom-fx-08", context_tokens=full.estimated_tokens - 1 + 1024)
    assert trimmed.truncations == ["dropped deploy config diffs"]
    assert "perf regression" not in user_text(trimmed)
    with pytest.raises(PromptTooLarge):
        llm_only_prompt("anom-fx-08", context_tokens=1100)


def test_retry_message_lists_every_error():
    text = retry_message(["confidence must be at most 1", "proposed_action not allowed"])
    assert "- confidence must be at most 1" in text and "- proposed_action not allowed" in text
