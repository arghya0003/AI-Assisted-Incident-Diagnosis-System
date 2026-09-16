"""Phase 8: run identity (ids, model version, configuration fingerprint) and the stored-run record."""

import dataclasses
import shutil

from app import analyses
from app.analyses import NO_MODEL, config_fingerprint, model_version_for, new_analysis_id, stored_analysis
from app.fixtures import load_fixtures
from app.graph import load_graph
from app.models import AnalyzeResponse
from app.pipeline import DiagnosisPipeline, PipelineConfig
from app.prompts import PROMPTS_DIR
from app.scoring import ScoringConfig
from app.settings import settings

SCORING = ScoringConfig.from_settings(settings)
CONFIG = PipelineConfig.from_settings(settings)


def fingerprint(**overrides):
    args = {"service_version": "0.8.0", "settings": settings, "scoring": SCORING, "pipeline": CONFIG, **overrides}
    return config_fingerprint(**args)


def test_model_version_depends_on_whether_the_mode_uses_the_llm():
    assert model_version_for("full", settings) == settings.llm_model
    assert model_version_for("llm_only", settings) == settings.llm_model
    assert model_version_for("deterministic", settings) == NO_MODEL


def test_analysis_ids_are_unique():
    ids = {new_analysis_id() for _ in range(200)}
    assert len(ids) == 200 and all(i.startswith("an-") for i in ids)


def test_fingerprint_is_stable():
    assert fingerprint() == fingerprint() and len(fingerprint()) == 12


def test_fingerprint_changes_with_anything_that_shapes_an_answer():
    base = fingerprint()
    assert fingerprint(service_version="0.8.1") != base
    assert fingerprint(scoring=SCORING.without_graph()) != base
    assert fingerprint(settings=dataclasses.replace(settings, llm_temperature=0.5)) != base
    assert fingerprint(pipeline=dataclasses.replace(CONFIG, retrieval_top_k=5)) != base


def test_fingerprint_changes_when_a_prompt_file_changes(tmp_path, monkeypatch):
    copy = tmp_path / "prompts"
    shutil.copytree(PROMPTS_DIR, copy)
    monkeypatch.setattr(analyses, "PROMPTS_DIR", copy)
    unchanged = fingerprint()
    (copy / "analyze_system.txt").write_text("a different system prompt", encoding="utf-8")
    assert fingerprint() != unchanged


def test_fingerprint_changes_when_the_graph_or_the_corpus_changes(tmp_path, monkeypatch):
    """Both shape the answer: the graph decides candidates and distances, the corpus decides what
    retrieval can return. A stored run from before an edit must not be served after it."""
    graph_copy = tmp_path / "dependency_graph.yaml"
    shutil.copy(analyses.DEFAULT_GRAPH_PATH, graph_copy)
    monkeypatch.setattr(analyses, "DEFAULT_GRAPH_PATH", graph_copy)
    corpus_copy = tmp_path / "incidents"
    shutil.copytree(analyses.CORPUS_DIR, corpus_copy)
    monkeypatch.setattr(analyses, "CORPUS_DIR", corpus_copy)

    unchanged = fingerprint()
    graph_copy.write_text(graph_copy.read_text(encoding="utf-8") + "# an edited graph", encoding="utf-8")
    after_graph = fingerprint()
    assert after_graph != unchanged

    (corpus_copy / "incident-0001.md").write_text("# a rewritten incident", encoding="utf-8")
    assert fingerprint() not in (unchanged, after_graph)

    # An added incident counts too, not only an edited one.
    before_addition = fingerprint()
    (corpus_copy / "incident-9999.md").write_text("# a new incident", encoding="utf-8")
    assert fingerprint() != before_addition


def test_a_pipeline_result_becomes_a_stored_run():
    class EmptyCorpus:
        def incident_count(self):
            return 0

    fixture = next(f for f in load_fixtures() if f.event.anomaly_id == "anom-fx-01")
    pipeline = DiagnosisPipeline(EmptyCorpus(), lambda texts: [], None, load_graph(), SCORING, CONFIG)
    result = pipeline.analyze_inputs(fixture.scoring_inputs(), "deterministic")
    stored = stored_analysis(result, "an-test", "anom-fx-01", NO_MODEL, "abc123abc123")
    assert (stored.pipeline_mode, stored.answered_by, stored.llm_attempts) == ("deterministic", "deterministic", 0)
    assert [h.service for h in stored.hypotheses] == result.services
    assert stored.response() == AnalyzeResponse.model_validate(result.response.model_dump())
