from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from app.recommendation.catalog import KnobCatalog


@dataclass(frozen=True)
class ConfigSnapshot:
    knob: str
    original_value: int | float


class MySQLConfigExecutor:
    def __init__(self, connection: Any, catalog: KnobCatalog):
        self.connection = connection
        self.catalog = catalog

    def current_value(self, knob: str) -> int | float:
        self.catalog.get(knob)
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT VARIABLE_VALUE FROM performance_schema.global_variables WHERE VARIABLE_NAME=%s", (knob,))
            row = cursor.fetchone()
        if not row:
            raise RuntimeError(f"server does not expose variable: {knob}")
        raw = row[0] if not isinstance(row, dict) else row["VARIABLE_VALUE"]
        return float(raw) if "." in str(raw) else int(raw)

    def apply(self, knob: str, value: int | float) -> ConfigSnapshot:
        item = self.catalog.validate_change(knob, value)
        normalized = knob.lower().strip()
        original = self.current_value(normalized)
        if original and abs(value - original) / original > item["max_step_ratio"]:
            raise ValueError(f"change exceeds max_step_ratio for {normalized}")
        with self.connection.cursor() as cursor:
            cursor.execute(f"SET GLOBAL {normalized} = %s", (value,))
        return ConfigSnapshot(normalized, original)

    def rollback(self, snapshot: ConfigSnapshot) -> None:
        self.catalog.validate_change(snapshot.knob, snapshot.original_value)
        with self.connection.cursor() as cursor:
            cursor.execute(f"SET GLOBAL {snapshot.knob} = %s", (snapshot.original_value,))
