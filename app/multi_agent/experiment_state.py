from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import IncumbentSnapshot, utc_now


class CandidateArchive:
    """Append-only candidate lineage; champion promotion never erases evidence."""

    def __init__(self, directory: Path):
        self.path = directory / "candidate_archive.jsonl"
        self.champion_path = directory / "champion.json"

    def set_champion(self, champion: IncumbentSnapshot, *, reason: str) -> None:
        payload = {"schema_version": "andromeda.champion/v1", "updated_at": utc_now(),
                   "reason": reason, **champion.model_dump(mode="json")}
        self.champion_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def append(self, *, candidate_config: dict[str, Any], candidate_hash: str,
               experiment_parent: IncumbentSnapshot, experiment_id: str,
               proposal_id: str, paired: dict[str, Any], outcome: dict[str, Any]) -> None:
        payload = {
            "at": utc_now(), "experiment_id": experiment_id, "proposal_id": proposal_id,
            "candidate_config": candidate_config, "candidate_config_hash": candidate_hash,
            "experiment_parent": {"config_hash": experiment_parent.config_hash,
                                  "accepted_experiment_id": experiment_parent.accepted_experiment_id,
                                  "verified_performance": experiment_parent.verified_performance},
            "measurements": {"controls": paired.get("controls", []),
                             "treatments": paired.get("treatments", []),
                             "blocks": paired.get("block_records", [])},
            "sequential_decision": paired.get("sequential_decision"), "outcome": outcome,
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, default=str,
                                    sort_keys=True, separators=(",", ":")) + "\n")
