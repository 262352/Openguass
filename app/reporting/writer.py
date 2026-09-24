from __future__ import annotations
import json
from pathlib import Path
from typing import Any


class JsonReportWriter:
    def __init__(self, root: Path):
        self.root = root

    def write(self, run_id: str, payload: dict[str, Any]) -> Path:
        target = self.root / run_id / "report.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return target
