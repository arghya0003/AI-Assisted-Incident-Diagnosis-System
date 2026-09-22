"""Client for M3's `POST /analyze` (CONTRACTS.md). Owns "handles retries, timeouts and the
case where the LLM is slow or returns garbage" (PLAN.md, M4 responsibilities).

Three distinct failure modes, handled differently:
  - Connection refused / DNS failure -- diagnosis-service is down or still starting. Retried.
  - Timeout -- the LLM is slow (a cold model load, per its own README, can take ~30s on top
    of ordinary generation). Retried with backoff, within a budget that stays under the
    orchestrator's own timeout so a caller never hangs indefinitely.
  - 200 with a body that fails our own Hypothesis validation ("returns garbage") -- M3's own
    Pydantic response_model should make this impossible, but we validate again on this side of
    the network boundary rather than trust it blindly. Treated as a non-retryable failure: a
    malformed body will not un-malform itself on a second attempt.

A 404 (anomaly not yet visible to M3 -- it reads the same `anomalies` table M2 writes, which
can lag a hair behind the Kafka event this orchestrator consumed) is retried like a timeout,
since it is expected to resolve within a second or two, not treated as "garbage".
"""

import logging
import time

import httpx
from pydantic import TypeAdapter, ValidationError

from app.models import Hypothesis
from app.settings import Settings

log = logging.getLogger("orchestrator.diagnosis_client")

_HypothesesAdapter = TypeAdapter(list[Hypothesis])


class DiagnosisFailed(RuntimeError):
    """All attempts to reach diagnosis-service, or to get a valid answer from it, were
    exhausted. `reason` is a short machine-readable tag; `detail` is for logs/audit."""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class DiagnosisResult:
    def __init__(self, hypotheses: list[Hypothesis], analysis_id: str, model_version: str, answered_by: str):
        self.hypotheses = hypotheses
        self.analysis_id = analysis_id
        self.model_version = model_version
        self.answered_by = answered_by


class DiagnosisClient:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self._settings = settings
        self._client = client or httpx.Client(timeout=settings.diagnosis_timeout_seconds)

    def analyze(self, anomaly_id: str) -> DiagnosisResult:
        last_reason, last_detail = "unknown", "no attempt made"
        for attempt in range(1, self._settings.diagnosis_max_attempts + 1):
            try:
                response = self._client.post(
                    f"{self._settings.diagnosis_service_url}/analyze",
                    json={"anomaly_id": anomaly_id},
                )
            except httpx.TimeoutException as exc:
                last_reason, last_detail = "timeout", str(exc)
                log.warning("analyze anomaly_id=%s attempt=%d timed out: %s", anomaly_id, attempt, exc)
            except httpx.RequestError as exc:
                last_reason, last_detail = "connection_error", str(exc)
                log.warning("analyze anomaly_id=%s attempt=%d connection error: %s", anomaly_id, attempt, exc)
            else:
                if response.status_code == 200:
                    return self._parse(response, anomaly_id)
                if response.status_code == 404:
                    last_reason, last_detail = "anomaly_not_found", response.text[:500]
                    log.info("analyze anomaly_id=%s attempt=%d: not yet visible to diagnosis-service", anomaly_id, attempt)
                elif response.status_code == 503:
                    last_reason, last_detail = "diagnosis_service_unavailable", response.text[:500]
                    log.warning("analyze anomaly_id=%s attempt=%d: diagnosis-service unavailable", anomaly_id, attempt)
                else:
                    # An unexpected status is treated as garbage, not retried: a 4xx/5xx we do
                    # not recognise will not resolve itself by asking again.
                    raise DiagnosisFailed("unexpected_status", f"HTTP {response.status_code}: {response.text[:500]}")

            if attempt < self._settings.diagnosis_max_attempts:
                time.sleep(self._settings.diagnosis_retry_backoff_seconds * attempt)

        raise DiagnosisFailed(last_reason, last_detail)

    def _parse(self, response: httpx.Response, anomaly_id: str) -> DiagnosisResult:
        try:
            body = response.json()
            hypotheses = _HypothesesAdapter.validate_python(body["hypotheses"])
        except (ValueError, KeyError, ValidationError) as exc:
            raise DiagnosisFailed("invalid_response", f"anomaly_id={anomaly_id}: {exc}") from exc
        analysis_id = response.headers.get("X-Analysis-Id", "")
        return DiagnosisResult(
            hypotheses=hypotheses,
            analysis_id=analysis_id,
            model_version=self._model_version_for(anomaly_id, analysis_id),
            answered_by=response.headers.get("X-Diagnosis-Mode", "unknown"),
        )

    def _model_version_for(self, anomaly_id: str, analysis_id: str) -> str:
        """The stored run's model_version (e.g. `phi4-mini`, or `none` in deterministic mode)
        so the audit log can record "with which model version" (PLAN.md) -- /analyze's own
        response headers describe how the answer was produced (X-Diagnosis-Mode) but not
        which model produced it, so this looks the run up by the id that request just made."""
        try:
            response = self._client.get(f"{self._settings.diagnosis_service_url}/hypotheses/{anomaly_id}", params={"limit": 5})
            response.raise_for_status()
            for run in response.json():
                if run.get("analysis_id") == analysis_id:
                    return run.get("model_version", "unknown")
        except (httpx.RequestError, ValueError) as exc:
            log.warning("could not resolve model_version for analysis_id=%s: %s", analysis_id, exc)
        return "unknown"

    def close(self) -> None:
        self._client.close()
