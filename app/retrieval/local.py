from __future__ import annotations
import json
import re
from pathlib import Path
from app.schemas.models import KnowledgeDocument


class LocalKnowledgeRetriever:
    """Dependency-free baseline adapter for Andromeda-style local retrieval."""

    def __init__(self, path: Path):
        self.documents = [
            KnowledgeDocument.model_validate(json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @staticmethod
    def _terms(text: str) -> set[str]:
        return set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text.lower()))

    def search(self, query: str, top_k: int = 5) -> list[KnowledgeDocument]:
        terms = self._terms(query)
        ranked = []
        for document in self.documents:
            haystack = f"{document.title} {document.text} {' '.join(document.knobs)}"
            score = len(terms & self._terms(haystack))
            ranked.append((score, document.source_id, document))
        ranked.sort(key=lambda row: (-row[0], row[1]))
        return [row[2] for row in ranked[:top_k] if row[0] > 0]
