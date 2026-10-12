from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def load_source_config(root: Path, workload: str, captured: dict[str, Any], directory: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve an auditable workload-specific starting configuration."""
    candidates: list[Path] = []
    explicit = os.getenv(f"ANDROMEDA_SOURCE_CONFIG_{workload.upper()}")
    if explicit:
        candidates.append(Path(explicit))
    if directory:
        candidates.append(Path(directory) / f"{workload}.json")
    candidates.append(root / "artifacts" / "source_configs" / f"{workload}.json")
    for candidate in candidates:
        path = candidate if candidate.is_absolute() else root / candidate
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            value = raw.get("config", raw)
            if not isinstance(value, dict) or not value:
                raise ValueError(f"source config is not a non-empty JSON object: {path}")
            return value, {"origin": "workload_specific_file", "path": str(path.resolve()), "explicit": True}
    return dict(captured), {"origin": "captured_cluster_fallback", "path": None, "explicit": False,
                            "warning": f"no workload-specific source config supplied for {workload}"}
