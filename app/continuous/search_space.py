from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.multi_agent.models import ExpertProposal, KnobChange, ValidationOutcome, utc_now
from app.phase2.space import FullKnobSpace

from .diagnosis_control import HypothesisStateTable
from .search import GROUPS, exploration_proposal


def _same(a: Any, b: Any) -> bool:
    try:
        return f"{float(a):.8g}" == f"{float(b):.8g}"
    except (TypeError, ValueError):
        return str(a).lower() == str(b).lower()


class SearchSpaceCompiler:
    """Compile a frozen bounded space only from expert diagnosis records."""

    def __init__(self, directory: Path, space: FullKnobSpace):
        self.directory, self.space = directory, space
        self.by_name = {item.name: item for item in space.dimensions}

    def compile(self, table: HypothesisStateTable,
                outcomes: list[dict[str, Any]], incumbent: dict[str, Any]) -> dict[str, Any]:
        outcome_by_id = {item["proposal"].proposal_id: item["outcome"] for item in outcomes}
        suggestions: dict[str, list[dict[str, Any]]] = defaultdict(list)
        joint_constraints = []
        for proposal_id, record in table.records.items():
            proposal = table.proposals.get(proposal_id)
            if proposal is None or record["status"] in {
                    "rejected_unsafe", "rejected_infeasible", "superseded_by_evidence"}:
                continue
            if len(proposal.knob_changes) > 1 and record["status"] not in {
                    "tested_refuted", "rejected_unsafe", "rejected_infeasible"}:
                joint_constraints.append({"proposal_id": proposal_id,
                    "knobs": [item.knob for item in proposal.knob_changes],
                    "mechanism": proposal.mechanism})
            outcome = outcome_by_id.get(proposal_id)
            for change in proposal.knob_changes:
                suggestions[change.knob].append({"proposal": proposal, "change": change,
                                                 "record": record, "outcome": outcome})

        entries = []
        excluded = []
        for knob, values in sorted(suggestions.items()):
            tested = [item for item in values if item["outcome"] is not None]
            improved = [item for item in tested
                        if item["outcome"].performance_verdict == "verified_improvement"]
            supported = [item for item in tested
                         if item["outcome"].performance_verdict == "no_improvement"
                         and item["outcome"].mechanism_verdict == "supported"]
            inconclusive = [item for item in tested
                            if item["outcome"].mechanism_verdict == "inconclusive"
                            and item["outcome"].validity != "invalid"]
            refuted = [item for item in tested if item["outcome"].mechanism_verdict == "refuted"]
            recommendation_count = len(values)
            if improved:
                source, rule = improved[-1], "verified improvement: successful value ± one initial step"
                mode = "improved"
            elif supported:
                source, rule = supported[-1], "supported mechanism without TPS gain: middle third"
                mode = "supported_no_improvement"
            elif inconclusive:
                source = inconclusive[-1]
                rule = "expert-rational but experimentally inconclusive: narrow target neighborhood"
                mode = "inconclusive_narrow"
            elif recommendation_count >= 2 and not tested:
                source, rule = values[-1], "repeated untested expert recommendation: full expert range"
                mode = "repeated_untested"
            elif refuted and len(refuted) == len(tested) and tested:
                excluded.append({"knob": knob, "reason": "all tested mechanism hypotheses were refuted"})
                continue
            else:
                excluded.append({"knob": knob, "reason": "no improved/supported/repeated-untested evidence"})
                continue
            entry = self._entry(knob, source["change"], incumbent[knob], mode)
            if entry is None:
                excluded.append({"knob": knob, "reason": "expert range is absent or has no legal variation"})
                continue
            entry.update({"compile_rule": rule,
                          "source_proposal_ids": [item["proposal"].proposal_id for item in values],
                          "recommendation_count": recommendation_count,
                          "source_expert": source["proposal"].expert,
                          "evidence_confidence": ("high" if mode == "improved" else
                                                  "medium" if mode == "supported_no_improvement"
                                                  else "low")})
            entries.append(entry)

        allowed = {item["knob"] for item in entries}
        joint_constraints = [item for item in joint_constraints
                             if set(item["knobs"]) <= allowed]
        # Preserve bounded cross-hypothesis combinations. In particular, a
        # supported memory/writeback anchor must be testable together with a
        # narrower WAL/storage dimension proposed by the same specialist.
        anchors = [item for item in entries if item["evidence_confidence"] in {"high", "medium"}]
        uncertain = [item for item in entries if item["evidence_confidence"] == "low"]
        existing = {tuple(sorted(item["knobs"])) for item in joint_constraints}
        for anchor in anchors:
            for candidate in uncertain:
                if anchor["source_expert"] != candidate["source_expert"]:
                    continue
                knobs = tuple(sorted({anchor["knob"], candidate["knob"]}))
                if len(knobs) != 2 or knobs in existing:
                    continue
                joint_constraints.append({
                    "proposal_id": "compiled_cross_hypothesis",
                    "knobs": list(knobs),
                    "mechanism": "bounded combination of a supported anchor and an inconclusive expert dimension",
                })
                existing.add(knobs)
        payload = {
            "schema_version": "andromeda.frozen-diagnostic-search-space/v1",
            "compiled_at": utc_now(), "frozen": True,
            "parameters": entries, "joint_constraints": joint_constraints,
            "excluded": excluded,
            "provenance": "diagnosis hypothesis table and validation outcomes only; no BO reference",
        }
        (self.directory / "FROZEN_SEARCH_SPACE.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    def _entry(self, knob: str, change: KnobChange, default: Any,
               mode: str) -> dict[str, Any] | None:
        dim = self.by_name[knob]
        if dim.choices:
            choices = list(dim.choices)
            if mode == "improved":
                choices = [change.new_value]
            if all(_same(item, default) for item in choices):
                return None
            return {"knob": knob, "type": "categorical", "choices": choices,
                    "initial_step": None, "source_target": change.new_value}

        try:
            old, target = float(change.old_value), float(change.new_value)
            low = float(change.recommended_min) if change.recommended_min is not None else min(old, target)
            high = float(change.recommended_max) if change.recommended_max is not None else max(old, target)
        except (TypeError, ValueError):
            return None
        if low > high:
            low, high = high, low
        base_step = float(change.initial_step) if change.initial_step is not None else abs(high - low) / 4.0
        if base_step <= 0:
            base_step = max(abs(target - old) / 4.0, 1.0 if dim.vartype == "integer" else .01)
        if mode == "improved":
            low, high = target - base_step, target + base_step
        elif mode == "supported_no_improvement":
            width = high - low
            low, high = low + width / 3.0, high - width / 3.0
        elif mode == "inconclusive_narrow":
            low, high = max(low, target - base_step), min(high, target + base_step)
            base_step = max(base_step / 2.0, 1.0 if dim.vartype == "integer" else .01)
        if low > 0 and high / max(low, 1e-12) > 10 and abs(high - low) > abs(float(default)) * 10:
            log_low, log_high = math.log(low), math.log(high)
            low = math.exp(log_low + (log_high - log_low) / 3.0)
            high = math.exp(log_low + 2.0 * (log_high - log_low) / 3.0)
        low, high = self.space.clamp(knob, low), self.space.clamp(knob, high)
        target = self.space.clamp(knob, target)
        if _same(low, high) and _same(low, default):
            return None
        anchor = target if float(low) <= float(target) <= float(high) else self.space.clamp(
            knob, (float(low) + float(high)) / 2.0)
        return {"knob": knob, "type": "numeric", "low": low, "high": high,
                "initial_step": base_step, "source_target": anchor,
                "expert_target": target,
                "log_scale": bool(dim.log)}


class BoundedSearchPlanner:
    """Deterministic candidates inside the compiler's immutable boundaries."""

    def __init__(self, definition: dict[str, Any], space: FullKnobSpace):
        self.definition, self.space = definition, space
        self.entries = {item["knob"]: item for item in definition["parameters"]}
        self.plans = self._plans()
        self.cursor = 0

    def _values(self, item: dict[str, Any]) -> list[Any]:
        if item["type"] == "categorical":
            return item["choices"]
        low, high, target = item["low"], item["high"], item["source_target"]
        middle = (float(low) + float(high)) / 2.0
        return [self.space.clamp(item["knob"], value) for value in (target, middle, low, high)]

    def _plans(self) -> list[dict[str, Any]]:
        plans, seen = [], set()
        for item in self.definition["parameters"]:
            for value in self._values(item):
                signature = json.dumps({item["knob"]: value}, sort_keys=True, default=str)
                if signature not in seen:
                    plans.append({item["knob"]: value}); seen.add(signature)
        for constraint in self.definition["joint_constraints"]:
            value = {knob: self.entries[knob]["source_target"] for knob in constraint["knobs"]}
            signature = json.dumps(value, sort_keys=True, default=str)
            if signature not in seen:
                plans.insert(0, value); seen.add(signature)
        return plans

    def candidate(self, incumbent: dict[str, Any], *, round_number: int,
                  config_hash: str, workload_hash: str, duration: int,
                  workload: str) -> tuple[dict[str, Any], ExpertProposal] | None:
        while self.cursor < len(self.plans):
            plan = self.plans[self.cursor]
            self.cursor += 1
            candidate = dict(incumbent)
            changes = []
            for knob, value in plan.items():
                if _same(value, incumbent[knob]):
                    continue
                candidate[knob] = value
                entry = self.entries[knob]
                changes.append(KnobChange(knob=knob, old_value=incumbent[knob], new_value=value,
                    recommended_min=entry.get("low"), recommended_max=entry.get("high"),
                    initial_step=entry.get("initial_step"),
                    numerical_basis="frozen diagnosis-compiled search boundary",
                    mechanism_role="bounded_parameter_search"))
            reconciled = self.space.reconcile(candidate)
            requested = {item.knob for item in changes}
            implicit = {name for name in self.space.names
                        if not _same(reconciled[name], incumbent[name]) and name not in requested}
            if changes and not implicit:
                group = next((name for name, knobs in GROUPS.items()
                              if changes[0].knob in knobs), "query_planner")
                proposal = exploration_proposal(round_number=round_number, group=group,
                    changes=changes, config_hash=config_hash, workload_hash=workload_hash,
                    duration=duration, stage="diagnosis_bounded_search",
                    reason="candidate generated strictly inside FROZEN_SEARCH_SPACE.json",
                    workload=workload)
                return reconciled, proposal
        return None
