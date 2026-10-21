from __future__ import annotations

import json
import math
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from app.multi_agent.models import ExpertProposal, ValidationOutcome, utc_now
from app.phase2.space import FullKnobSpace


HypothesisStatus = Literal[
    "proposed", "testing", "parked", "underpowered", "tested_supported", "tested_refuted",
    "tested_inconclusive", "rejected_unsafe", "rejected_duplicate",
    "rejected_infeasible", "superseded_by_evidence",
    "paused_low_yield", "action_space_boundary",
]
TERMINAL_STATUSES = {
    "tested_supported", "tested_refuted", "tested_inconclusive",
    "rejected_unsafe", "rejected_duplicate", "rejected_infeasible",
    "superseded_by_evidence",
    "parked", "underpowered",
    "paused_low_yield", "action_space_boundary",
}


def action_signature(proposal: ExpertProposal) -> str:
    return json.dumps(sorted((item.knob, item.new_value) for item in proposal.knob_changes),
                      ensure_ascii=False, default=str)


class HypothesisStateTable:
    """Append-only status history plus the current executable graph frontier."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.current_path = directory / "hypothesis_status.json"
        self.events_path = directory / "hypothesis_status_events.jsonl"
        self.records: dict[str, dict[str, Any]] = {}
        self.proposals: dict[str, ExpertProposal] = {}
        self.signature_owner: dict[tuple[str, str], str] = {}
        self.recommendation_counts: Counter[str] = Counter()
        self.new_counts: list[int] = []
        self.evidence_epoch = 0
        self.effect_history: dict[str, list[float]] = {}

    def register_round(self, round_number: int, proposals: list[ExpertProposal],
                       current_config_hash: str) -> int:
        added = 0
        for proposal in proposals:
            signature = action_signature(proposal)
            signature_key = (current_config_hash, signature)
            self.recommendation_counts[signature] += 1
            record = {
                "proposal_id": proposal.proposal_id, "expert": proposal.expert,
                "title": proposal.title, "mechanism": proposal.mechanism,
                "action_signature": signature,
                "knob_changes": [item.model_dump(mode="json") for item in proposal.knob_changes],
                "round_proposed": round_number, "status": "proposed",
                "reason": "new expert hypothesis", "updated_at": utc_now(),
                "recommendation_count": self.recommendation_counts[signature],
                "incumbent_config_hash": proposal.incumbent_config_hash,
                "evidence_epoch": self.evidence_epoch,
            }
            if proposal.incumbent_config_hash != current_config_hash:
                record.update(status="rejected_infeasible", reason="proposal context does not match incumbent")
            elif not proposal.executable:
                record.update(status="rejected_infeasible",
                              reason="observation-only hypothesis has no executable parameter action")
            elif signature_key in self.signature_owner and self.records[
                    self.signature_owner[signature_key]]["status"] in {
                        "tested_supported", "tested_refuted"}:
                record.update(status="rejected_duplicate", reason=(
                    "same canonical configuration already has an adequately powered conclusion in "
                    f"this context: {self.signature_owner[signature_key]}"))
            else:
                self.signature_owner[signature_key] = proposal.proposal_id
                added += 1
            self.proposals[proposal.proposal_id] = proposal
            self.records[proposal.proposal_id] = record
            self._event("hypothesis_registered", record)
        self.new_counts.append(added)
        self.write()
        return added

    def transition(self, proposal_id: str, status: HypothesisStatus, reason: str,
                   **details: Any) -> None:
        if proposal_id not in self.records:
            raise KeyError(proposal_id)
        before = self.records[proposal_id]["status"]
        self.records[proposal_id].update(status=status, reason=reason,
                                         updated_at=utc_now(), **details)
        self._event("hypothesis_status_changed", {
            "proposal_id": proposal_id, "from": before, "to": status,
            "reason": reason, **details,
        })
        self.write()

    def proposed(self, config_hash: str) -> list[ExpertProposal]:
        values = []
        for proposal_id, proposal in self.proposals.items():
            record = self.records[proposal_id]
            if record["status"] != "proposed":
                continue
            if proposal.incumbent_config_hash != config_hash:
                self.transition(proposal_id, "rejected_infeasible",
                                "incumbent changed before this hypothesis was tested")
                continue
            values.append(proposal)
        return values

    def finish_test(self, proposal: ExpertProposal, outcome: ValidationOutcome) -> None:
        paired_verdict = (outcome.verified_performance or {}).get(
            "paired_decision", {}).get("verdict")
        if outcome.validity == "unstable":
            status: HypothesisStatus = "underpowered"
        elif outcome.validity == "invalid":
            status = "tested_inconclusive"
        elif paired_verdict == "reject_harmful":
            status = "tested_refuted"
        elif paired_verdict == "accept" and outcome.mechanism_verdict == "supported":
            status = "tested_supported"
        else:
            status = "tested_inconclusive"
        self.transition(proposal.proposal_id, status,
                        f"paired verdict={paired_verdict}; mechanism={outcome.mechanism_verdict}; "
                        f"performance={outcome.performance_verdict}",
                        experiment_id=outcome.experiment_id,
                        performance_verdict=outcome.performance_verdict,
                        mechanism_verdict=outcome.mechanism_verdict)

    def record_effect(self, proposal: ExpertProposal, log_effect: float,
                      *, delta_min: float = .03) -> bool:
        """Pause a mechanism only after three valid effects have a low confidence upper bound."""
        def family(name: str) -> str:
            if name.startswith(("wal", "checkpoint", "commit")): return "wal"
            if name.startswith("bgwriter") or name == "backend_flush_after": return "writeback"
            if "mem" in name or name in {"shared_buffers", "effective_cache_size"}: return "memory"
            if "lock" in name or "worker" in name: return "concurrency"
            if name.startswith(("autovacuum", "vacuum")): return "maintenance"
            return "query"
        key = f"{proposal.expert}:" + ",".join(sorted({family(item.knob) for item in proposal.knob_changes}))
        values = self.effect_history.setdefault(key, [])
        values.append(float(log_effect))
        if len(values) < 3:
            self.write()
            return False
        deviation = statistics.stdev(values) if len(values) > 1 else 0.0
        upper = statistics.mean(values) + 1.96 * deviation / math.sqrt(len(values))
        if upper < delta_min:
            self.transition(proposal.proposal_id, "paused_low_yield",
                            "three or more valid effects have a confidence upper bound below delta_min",
                            effect_count=len(values), effect_mean=statistics.mean(values),
                            effect_ci_upper=upper,
                            reactivation_conditions=["new anomaly", "interaction evidence", "periodic exploration"])
            return True
        self.write()
        return False

    def reactivate_paused(self, *, experts: set[str], reason: str) -> list[str]:
        changed = []
        for proposal_id, record in self.records.items():
            if record["status"] == "paused_low_yield" and record["expert"] in experts:
                self.transition(proposal_id, "tested_inconclusive", f"reactivated: {reason}")
                changed.append(proposal_id)
        return changed

    def supersede_pending(self, *, tested_proposal_id: str,
                          experiment_result: dict[str, Any]) -> list[str]:
        """Park sibling proposals so every expert can revise them after new evidence.

        Proposals from one parallel consultation are observations about the same
        pre-experiment graph version. Once any sibling is tested, the graph has
        changed; executing the remaining actions without letting their authors
        observe that result would use stale reasoning.
        """
        changed = []
        for proposal_id, record in list(self.records.items()):
            if proposal_id == tested_proposal_id or record["status"] != "proposed":
                continue
            self.transition(proposal_id, "parked",
                            f"new evidence from {tested_proposal_id} requires expert revision",
                            superseding_experiment=experiment_result,
                            revisit_epoch=self.evidence_epoch + 1)
            changed.append(proposal_id)
        return changed

    def advance_epoch(self, experiment_id: str) -> int:
        self.evidence_epoch += 1
        self._event("evidence_epoch_advanced", {
            "evidence_epoch": self.evidence_epoch, "experiment_id": experiment_id})
        self.write()
        return self.evidence_epoch

    def feedback(self) -> list[dict[str, Any]]:
        return [{"proposal_id": item["proposal_id"], "expert": item["expert"],
                 "status": item["status"], "reason": item["reason"],
                 "knob_changes": item["knob_changes"]}
                for item in self.records.values()
                if item["status"] in {"rejected_unsafe", "rejected_infeasible",
                                      "superseded_by_evidence", "parked", "underpowered"}][-20:]

    def can_exhaust(self, consecutive_no_new: int) -> bool:
        return consecutive_no_new >= 2 and all(
            item["status"] in TERMINAL_STATUSES for item in self.records.values())

    def write_exhaustion_report(self, *, stop_reason: str, consecutive_no_new: int,
                                coordinator_summary: str) -> dict[str, Any]:
        counts = Counter(item["status"] for item in self.records.values())
        payload = {
            "schema_version": "andromeda.diagnosis-exhaustion/v1",
            "stopped_at": utc_now(), "stop_reason": stop_reason,
            "trigger_expression": (
                "all(status in tested_* or rejected_*) and consecutive_rounds_without_new_proposed >= 2"),
            "trigger_satisfied": self.can_exhaust(consecutive_no_new),
            "consecutive_rounds_without_new_proposed": consecutive_no_new,
            "new_hypotheses_by_round": self.new_counts,
            "last_two_round_counts": self.new_counts[-2:],
            "status_counts": dict(counts),
            "active_hypotheses": [item for item in self.records.values()
                                  if item["status"] in {"proposed", "testing"}],
            "rejected_hypotheses": [item for item in self.records.values()
                                    if item["status"].startswith("rejected_")],
            "hypotheses": list(self.records.values()),
            "coordinator_reasoning_summary": coordinator_summary,
        }
        (self.directory / "DIAGNOSIS_EXHAUSTION.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    def write(self) -> None:
        payload = {"schema_version": "andromeda.hypothesis-status/v2",
                   "updated_at": utc_now(), "evidence_epoch": self.evidence_epoch,
                   "effect_history": self.effect_history,
                   "hypotheses": list(self.records.values())}
        self.current_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _event(self, event: str, payload: dict[str, Any]) -> None:
        with self.events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": utc_now(), "event": event, **payload},
                                    ensure_ascii=False, default=str) + "\n")


class ActionConsistencyAuditor:
    """Reject substitutions between an expert action and the applied candidate."""

    def __init__(self, directory: Path, space: FullKnobSpace):
        self.path = directory / "action_consistency.jsonl"
        self.space = space

    @staticmethod
    def _norm(value: Any) -> str:
        if isinstance(value, dict):
            value = value.get("setting")
        try:
            return f"{float(value):.8g}"
        except (TypeError, ValueError):
            return str(value).lower()

    def freeze(self, round_number: int, proposal: ExpertProposal,
               incumbent: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        expert = {item.knob: item.new_value for item in proposal.knob_changes}
        candidate = dict(incumbent)
        unsafe = []
        for item in proposal.knob_changes:
            try:
                clamped = self.space.clamp(item.knob, item.new_value)
            except (TypeError, ValueError) as error:
                unsafe.append(f"{item.knob}: {error}")
                continue
            if self._norm(clamped) != self._norm(item.new_value):
                unsafe.append(f"{item.knob}: requested {item.new_value}, safe catalog value would be {clamped}")
            candidate[item.knob] = clamped
        reconciled = self.space.reconcile(candidate)
        actual = {name: value for name, value in reconciled.items()
                  if self._norm(value) != self._norm(incumbent.get(name))}
        substitutions = []
        for name in sorted(set(expert) | set(actual)):
            if name not in expert:
                substitutions.append(f"dependency added unrequested knob {name}={actual[name]}")
            elif name not in actual:
                substitutions.append(f"requested knob {name} produced no effective change")
            elif self._norm(expert[name]) != self._norm(actual[name]):
                substitutions.append(f"{name}: expert={expert[name]} candidate={actual[name]}")
        accepted = not unsafe and not substitutions
        record = {
            "at": utc_now(), "round": round_number, "stage": "mechanism_diagnosis",
            "proposal_id": proposal.proposal_id,
            "expert_changes": [item.model_dump(mode="json") for item in proposal.knob_changes],
            "candidate_changes": actual, "applied_changes": None,
            "verdict": "exact_match" if accepted else "action_substitution",
            "reasons": [*unsafe, *substitutions],
        }
        self._append(record)
        return (reconciled if accepted else None), record

    def applied(self, round_number: int, proposal: ExpertProposal,
                expected: dict[str, Any], observed: dict[str, Any]) -> dict[str, Any]:
        mismatches = {name: {"expected": value, "observed": observed.get(name)}
                      for name, value in expected.items()
                      if name not in observed or self._norm(value) != self._norm(observed[name])}
        record = {
            "at": utc_now(), "round": round_number, "stage": "mechanism_diagnosis",
            "proposal_id": proposal.proposal_id,
            "expert_changes": [item.model_dump(mode="json") for item in proposal.knob_changes],
            "candidate_changes": expected,
            "applied_changes": {name: observed.get(name) for name in expected},
            "verdict": "exact_match" if not mismatches else "action_substitution",
            "reasons": [] if not mismatches else [f"applied mismatch: {mismatches}"],
        }
        self._append(record)
        return record

    def _append(self, value: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")
