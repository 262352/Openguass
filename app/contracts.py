from __future__ import annotations
from pathlib import Path
from typing import Any, Protocol
from app.schemas.models import KnowledgeDocument, TelemetrySummary, VerificationResult


class KnowledgeRetriever(Protocol):
    def search(self, query: str, top_k: int = 5) -> list[KnowledgeDocument]: ...


class TelemetryCollector(Protocol):
    def collect(self) -> dict[str, float]: ...


class TelemetryAnalyzer(Protocol):
    def analyze(self, before: dict[str, float], after: dict[str, float], seconds: float) -> TelemetrySummary: ...


class LLMReasoner(Protocol):
    def identify(self, payload: dict[str, Any]): ...
    def recommend(self, payload: dict[str, Any]): ...


class ConfigExecutor(Protocol):
    def apply(self, knob: str, value: int | float) -> Any: ...
    def rollback(self, snapshot: Any) -> None: ...


class BenchmarkRunner(Protocol):
    def run(self, profile: str) -> dict[str, Any]: ...


class Verifier(Protocol):
    def verify(self, before: dict[str, Any], after: dict[str, Any], metric: str) -> VerificationResult: ...


class ReportWriter(Protocol):
    def write(self, run_id: str, payload: dict[str, Any]) -> Path: ...
