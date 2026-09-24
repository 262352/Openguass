from __future__ import annotations
import json
from pathlib import Path


class KnobCatalog:
    def __init__(self, path: Path):
        self._items = json.loads(path.read_text(encoding="utf-8"))

    def get(self, name: str) -> dict:
        normalized = name.lower().strip()
        if normalized not in self._items:
            raise ValueError(f"knob is not allowlisted: {name}")
        return self._items[normalized]

    def validate_change(self, name: str, value: int | float) -> dict:
        item = self.get(name)
        if not item["auto_execute"] or not item["dynamic"] or item["restart_required"]:
            raise ValueError(f"knob is not safe for automatic execution: {name}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("knob value must be numeric")
        if not item["minimum"] <= value <= item["maximum"]:
            raise ValueError(f"value outside catalog range for {name}")
        return item
