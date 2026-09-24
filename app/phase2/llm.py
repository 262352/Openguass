from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from openai import OpenAI
from pydantic import BaseModel, Field, model_validator

from app.config import load_settings
from app.phase2.space import FullKnobSpace


class Hypothesis(BaseModel):
    name: str
    evidence_ids: list[str]
    confidence: float = Field(ge=0, le=1)


class MetricNarrative(BaseModel):
    observations: list[str]
    workload_semantic_changes: list[str]
    bottleneck_hypotheses: list[Hypothesis]
    retrieval_terms: list[str]


class KnobChange(BaseModel):
    knob: str
    value: str | float | int | bool
    metric_evidence_ids: list[str]
    knowledge_ids: list[str]
    rationale: str


class Diagnosis(BaseModel):
    decision: Literal["change", "no_safe_change", "insufficient_evidence"]
    changes: list[KnobChange] = Field(default_factory=list, max_length=3)
    expected_tps_direction: Literal["increase", "unchanged", "uncertain"]
    verification: str

    @model_validator(mode="after")
    def validate_changes(self):
        if self.decision == "change" and not self.changes:
            raise ValueError("decision=change requires at least one change")
        if self.decision != "change" and self.changes:
            raise ValueError("only decision=change may contain changes")
        return self


def retrieve_catalog(narrative: MetricNarrative, limit: int = 15) -> list[dict[str, Any]]:
    catalog = json.loads((Path(__file__).resolve().parents[2] / "knowledge/postgres_60_knobs.json").read_text(encoding="utf-8"))["knobs"]
    query = " ".join(narrative.retrieval_terms + [x.name for x in narrative.bottleneck_hypotheses]).lower()
    tokens = set(re.findall(r"[a-z][a-z0-9_]+", query))
    scored = []
    for item in catalog:
        text = (item["name"] + " " + item["short_desc"]).lower()
        score = sum(3 if token in item["name"] else 1 for token in tokens if token in text)
        scored.append((score, item["name"], item))
    return [item for _, _, item in sorted(scored, reverse=True)[:limit]]


class DiagnosticLLM:
    def __init__(self, directory: Path):
        settings = load_settings()
        self.settings = settings
        self.client = OpenAI(api_key=settings.api_key, base_url=settings.base_url, timeout=settings.timeout_seconds, max_retries=settings.max_retries)
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    def _call(self, filename: str, instruction: str, payload: dict[str, Any], schema):
        prompt = instruction + "\nReturn exactly one JSON object matching this JSON Schema:\n" + json.dumps(schema.model_json_schema()) + "\nEVIDENCE:\n" + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        response = self.client.chat.completions.create(
            model=self.settings.model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or "{}"
        parsed = schema.model_validate_json(raw)
        record = {
            "model": self.settings.model,
            "base_url": self.settings.base_url,
            "prompt": prompt,
            "response": raw,
            "parsed": parsed.model_dump(mode="json"),
            "usage": response.usage.model_dump() if response.usage else {},
        }
        (self.dir / filename).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        return parsed

    def compose(self, iteration: int, payload: dict[str, Any]) -> MetricNarrative:
        instruction = """You are Andromeda's PostgreSQL workload-transition question composer.
The sole optimization objective is target-workload TPS. Latency, locks, waits, buffers, WAL,
checkpoints, table activity and host observations are diagnostic evidence only. Describe every
material measured change with direction and magnitude and cite the supplied evidence IDs.
Separate expected differences between unlike workload semantics from suspected configuration
bottlenecks. Never call the absolute TPS difference between TPC-C and Twitter a regression by
itself. Do not claim pressure without a corresponding measurement. Do not recommend knobs.
Do not infer a hidden target optimum, future result, GP history, or ground-truth label."""
        return self._call(f"iteration_{iteration:03d}_question.json", instruction, payload, MetricNarrative)

    def diagnose(self, iteration: int, payload: dict[str, Any], space: FullKnobSpace) -> Diagnosis:
        instruction = """You are Andromeda's PostgreSQL 14 diagnosis tuner. Maximize only target
TPS. Other metrics are evidence, never objective terms. Select zero to three changes only when
the metric evidence and supplied PostgreSQL documentation support them. Every change must cite
metric evidence IDs and knowledge IDs. Values must be raw pg_settings values within the supplied
bounds (bytes/pages/kB/MB/ms/s use the catalog's stated unit). Consider reload/restart cost. Do
not use target reference values, future outcomes, GP observations, or unstated external facts."""
        diagnosis = self._call(f"iteration_{iteration:03d}_diagnosis.json", instruction, payload, Diagnosis)
        seen = set()
        for change in diagnosis.changes:
            if change.knob in seen:
                raise ValueError(f"duplicate knob in diagnosis: {change.knob}")
            seen.add(change.knob)
            space.clamp(change.knob, change.value)
        return diagnosis
