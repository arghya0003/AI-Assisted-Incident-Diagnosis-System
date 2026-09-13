"""Hand-written anomaly fixtures (fixtures/anomalies/*.json).

Each file is an anomalies.detected event plus a "_fixture" block describing the scenario it
stands for. With that block removed, the file is exactly M2's wire shape, so it can be stored
directly or published to Kafka. The block is the ground truth later phases test against.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import AnomalyEvent

FIXTURE_ID_PREFIX = "anom-fx-"
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "anomalies"

# Fault types fault-injector can produce (services/fault-injector/main.py, FAULT_TYPES).
FaultType = Literal["bad_deploy_latency", "service_crash", "db_pool_saturation"]


class FixtureMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1)
    # "m2_current": single service and metric, zero-width window, as the detector emits today.
    # "contract": grouped multi-service event, as CONTRACTS.md describes after M2's grouping.
    shape: Literal["m2_current", "contract"]
    fault_type: FaultType | None
    ground_truth_service: str | None
    tags: list[str] = []
    notes: str = ""

    @model_validator(mode="after")
    def _ground_truth_is_complete(self) -> "FixtureMeta":
        if (self.fault_type is None) != (self.ground_truth_service is None):
            raise ValueError("fault_type and ground_truth_service must both be set or both be null")
        return self


@dataclass(frozen=True)
class Fixture:
    path: Path
    meta: FixtureMeta
    raw: dict  # the event exactly as it would arrive on anomalies.detected
    event: AnomalyEvent


def load_fixture(path: Path) -> Fixture:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "_fixture" not in data:
        raise ValueError(f"{path.name}: missing the _fixture block")
    meta = FixtureMeta.model_validate(data.pop("_fixture"))
    event = AnomalyEvent.model_validate(data)
    if not event.anomaly_id.startswith(FIXTURE_ID_PREFIX):
        # Keeps fixtures distinguishable from real M2 ids (anom-<epoch ms>) everywhere.
        raise ValueError(f"{path.name}: anomaly_id must start with {FIXTURE_ID_PREFIX!r}")
    return Fixture(path=path, meta=meta, raw=data, event=event)


def load_fixtures(directory: Path = FIXTURES_DIR) -> list[Fixture]:
    paths = sorted(directory.glob("*.json"))
    if not paths:
        raise FileNotFoundError(f"no fixtures found in {directory}")
    return [load_fixture(path) for path in paths]
