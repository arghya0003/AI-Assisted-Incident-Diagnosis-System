"""Past-incident corpus files (corpus/incidents/*.md): parsing and validation.

Each file is YAML front matter plus a markdown body with Symptoms, Root cause and Resolution
sections. `services` lists where the root cause was, not every service that showed symptoms:
candidate scoring boosts the services an incident names, so naming a symptom there would
boost a symptom.
"""

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

CORPUS_DIR = Path(__file__).resolve().parent.parent / "corpus" / "incidents"
REQUIRED_SECTIONS = ("**Symptoms:**", "**Root cause:**", "**Resolution:**")

# Broader than the injector's three fault types, so public postmortems can be labelled honestly.
CorpusFaultType = Literal[
    "bad_deploy_latency",
    "bad_deploy_errors",
    "service_crash",
    "db_pool_saturation",
    "db_contention",
    "capacity",
    "config_error",
    "dependency_failure",
    "benign",
]


class IncidentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(pattern=r"^incident-\d{4}$")
    title: str = Field(min_length=1)
    services: list[str] = []
    fault_type: CorpusFaultType | None = None
    source: Literal["synthetic", "public_postmortem"]
    source_url: str | None = None
    body: str = Field(min_length=1)

    @property
    def symptoms(self) -> str:
        """The Symptoms section: what was observed, which is all an anomaly query can describe."""
        match = re.search(r"\*\*Symptoms:\*\*\s*(.*?)(?=\n\s*\*\*[A-Z][A-Za-z ]*:\*\*|\Z)", self.body, re.DOTALL)
        return match.group(1).strip() if match else ""

    @model_validator(mode="after")
    def _consistent(self) -> "IncidentRecord":
        missing = [section for section in REQUIRED_SECTIONS if section not in self.body]
        if missing:
            raise ValueError(f"body is missing sections {missing}")
        if self.source == "public_postmortem":
            if not (self.source_url or "").startswith(("http://", "https://")):
                raise ValueError("public_postmortem records need an http(s) source_url")
            if self.services:
                raise ValueError("public_postmortem records must not name Sock Shop services")
        elif not self.services:
            raise ValueError("synthetic records must name the root-cause service(s)")
        return self


def parse_incident(text: str, name: str = "<string>") -> IncidentRecord:
    text = text.replace("\r\n", "\n")  # tolerate a checkout with CRLF line endings
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not match:
        raise ValueError(f"{name}: expected YAML front matter between '---' lines")
    front = yaml.safe_load(match.group(1))
    if not isinstance(front, dict):
        raise ValueError(f"{name}: front matter must be a mapping")
    return IncidentRecord.model_validate({**front, "body": match.group(2).strip()})


def load_corpus(directory: Path = CORPUS_DIR) -> list[IncidentRecord]:
    paths = sorted(directory.glob("incident-*.md"))
    if not paths:
        raise FileNotFoundError(f"no incident files found in {directory}")
    records = []
    for path in paths:
        record = parse_incident(path.read_text(encoding="utf-8"), path.name)
        if record.incident_id != path.stem:
            raise ValueError(f"{path.name}: incident_id {record.incident_id!r} does not match the filename")
        records.append(record)
    return records
