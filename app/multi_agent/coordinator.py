from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from app.phase2.space import FullKnobSpace

from .models import CrossReview, ExpertProposal, ExperimentSelection


@dataclass
class ExpertCredit:
    ewma_log_gain: float = 0.0
    samples: int = 0
    consecutive_negative: int = 0
    last_selected_round: int = 0
    state: str = "active"
    gains: list[float] = field(default_factory=list)


class ExpertCreditScheduler:
    """Fair softmax scheduling driven by measured paired log gains."""

    def __init__(self, roles: tuple[str, ...], *, alpha: float = .3, beta: float = 5.0,
                 seed: int = 20261021):
        self.roles = tuple(roles)
        self.alpha, self.beta = float(alpha), float(beta)
        self.rng = random.Random(seed)
        self.credit = {role: ExpertCredit() for role in self.roles}

    def update(self, role: str, log_effect: float) -> None:
        item = self.credit[role]
        item.ewma_log_gain = (float(log_effect) if item.samples == 0 else
                              self.alpha * float(log_effect) + (1 - self.alpha) * item.ewma_log_gain)
        item.samples += 1
        item.gains.append(float(log_effect))
        item.consecutive_negative = item.consecutive_negative + 1 if log_effect < 0 else 0
        if item.samples >= 3 and item.consecutive_negative >= 3:
            item.state = "paused_low_yield"

    def probabilities(self, round_number: int) -> dict[str, float]:
        # A paused route gets an explicit periodic 0.05 wake-up opportunity.
        waking = {role for role, item in self.credit.items()
                  if item.state == "paused_low_yield" and round_number % 5 == 0}
        active = [role for role, item in self.credit.items()
                  if item.state != "paused_low_yield"]
        if not active and not waking:
            active = list(self.roles)
        weights: dict[str, float] = {}
        for role in active:
            item = self.credit[role]
            if item.samples < 3 and item.ewma_log_gain <= 0:
                weights[role] = 1.0
            else:
                weights[role] = math.exp(self.beta * item.ewma_log_gain)
        result = {role: 0.0 for role in self.roles}
        wake_mass = min(.05 * len(waking), 1.0)
        for role in waking:
            result[role] = .05
        total = sum(weights.values())
        if active:
            for role, value in weights.items():
                result[role] = (1.0 - wake_mass) * (value / total if total else 1 / len(active))
        return result

    def choose(self, available_roles: set[str], round_number: int,
               anomaly_priority: list[str] | None = None) -> tuple[str | None, dict[str, float], str]:
        if not available_roles:
            return None, self.probabilities(round_number), "no expert has an executable proposal"
        never = [role for role in self.roles if role in available_roles and self.credit[role].samples == 0]
        if never:
            chosen = never[0]
            reason = "mandatory first sample for every expert"
        else:
            overdue = [role for role in self.roles if role in available_roles
                       and round_number - self.credit[role].last_selected_round >= 4]
            if overdue:
                chosen = max(overdue, key=lambda role: round_number - self.credit[role].last_selected_round)
                reason = "fairness rule: expert was not selected for three consecutive rounds"
            else:
                probabilities = self.probabilities(round_number)
                candidates = list(available_roles)
                weights = [probabilities[role] for role in candidates]
                if anomaly_priority:
                    for index, role in enumerate(candidates):
                        if role in anomaly_priority and self.credit[role].state != "paused_low_yield":
                            weights[index] *= 1.5
                chosen = self.rng.choices(candidates, weights=weights, k=1)[0]
                reason = "softmax EWMA gain sample with bounded anomaly priority"
        self.credit[chosen].last_selected_round = round_number
        return chosen, self.probabilities(round_number), reason

    def reactivate(self, role: str, reason: str) -> None:
        item = self.credit[role]
        item.state = "active"
        item.consecutive_negative = 0

    def snapshot(self, round_number: int) -> dict[str, Any]:
        return {"round": round_number, "alpha": self.alpha, "beta": self.beta,
                "probabilities": self.probabilities(round_number),
                "experts": {role: asdict(item) for role, item in self.credit.items()},
                "reactivation": "new anomaly, interaction evidence, or every fifth-round wake-up"}


class Coordinator:
    """Transparent rules for retaining competition and choosing one serialized experiment."""

    def __init__(self, space: FullKnobSpace | None = None, *, required_measurements: int = 4):
        self.space = space or FullKnobSpace()
        self.required_measurements = required_measurements

    def merge_exact_duplicates(self, proposals: list[ExpertProposal]) -> tuple[list[ExpertProposal], dict[str, list[ExpertProposal]]]:
        buckets: dict[tuple[Any, ...], list[ExpertProposal]] = defaultdict(list)
        for proposal in proposals:
            buckets[proposal.merge_signature()].append(proposal)
        canonical, merged = [], {}
        for values in buckets.values():
            canonical.append(values[0])
            if len(values) > 1:
                merged[values[0].proposal_id] = values[1:]
        return canonical, merged

    def select(self, proposals: list[ExpertProposal], reviews: list[CrossReview], *,
               current_config: dict[str, Any], refuted_signatures: set[tuple[Any, ...]] | None = None,
               remaining_measurements: int = 1,
               knob_experiment_counts: Counter[str] | None = None) -> ExperimentSelection:
        refuted_signatures = refuted_signatures or set()
        review_map: dict[str, list[CrossReview]] = defaultdict(list)
        for review in reviews:
            review_map[review.proposal_id].append(review)
        rejected, candidates = [], []
        for proposal in proposals:
            reason = self._reject_reason(proposal, current_config, refuted_signatures, remaining_measurements)
            if reason:
                rejected.append({"proposal_id": proposal.proposal_id, "reason": reason}); continue
            differentiators = sum(1 for r in review_map[proposal.proposal_id] if r.discriminating_prediction)
            constraints = sum(len(r.side_effect_constraints) for r in review_map[proposal.proposal_id])
            restart_penalty = 2.0 if proposal.cost.restart_required else 0.0
            knob_penalty = max(0, len(proposal.knob_changes) - 1) * .25
            cost_penalty = proposal.cost.estimated_seconds / 600.0
            # Different predictions are information; executable incumbent benefit remains mandatory.
            coverage_bonus = sum(2.0 / (1 + (knob_experiment_counts or Counter())[change.knob])
                                 for change in proposal.knob_changes)
            score = (4.0 * differentiators + 2.0 * len(proposal.alternatives)
                     + coverage_bonus - constraints - restart_penalty - knob_penalty - cost_penalty)
            candidates.append((score, proposal))
        if not candidates:
            return ExperimentSelection(selected_proposal_id=None, reason="no executable evidence-backed proposal remains",
                                       rejected=rejected, requires_more_observation=True)
        score, chosen = max(candidates, key=lambda x: (x[0], -x[1].cost.workload_measurements, x[1].proposal_id))
        for other_score, other in candidates:
            if other.proposal_id != chosen.proposal_id:
                rejected.append({"proposal_id": other.proposal_id,
                                 "reason": f"lower transparent score {other_score:.3f} than selected {score:.3f}"})
        differentiating = [r.review_id for r in review_map[chosen.proposal_id] if r.discriminating_prediction]
        reason = (f"selected {chosen.proposal_id}: executable against current incumbent; score={score:.3f}; "
                  f"discriminating reviews={differentiating or 'proposal alternatives'}")
        return ExperimentSelection(selected_proposal_id=chosen.proposal_id, score=score, reason=reason, rejected=rejected)

    def _reject_reason(self, proposal: ExpertProposal, current: dict[str, Any], refuted: set[tuple[Any, ...]], budget: int) -> str | None:
        if not proposal.executable:
            return "observation-only proposal"
        if proposal.merge_signature() in refuted:
            return "same action and context were already sufficiently refuted"
        required_measurements = self.required_measurements
        if proposal.interaction_design:
            required_measurements = 4 * proposal.interaction_design.repeats_per_cell
        if max(proposal.cost.workload_measurements, required_measurements) > budget:
            return "insufficient workload-measurement budget"
        families = set()
        for change in proposal.knob_changes:
            if change.knob not in self.space.names:
                return f"unknown knob {change.knob}"
            try:
                value = self.space.clamp(change.knob, change.new_value)
            except (TypeError, ValueError) as error:
                return f"illegal value for {change.knob}: {error}"
            if str(value).lower() != str(change.new_value).lower():
                return f"value outside safe range for {change.knob}"
            if str(current.get(change.knob)).lower() != str(change.old_value).lower():
                return f"proposal old value for {change.knob} does not match incumbent"
            families.add(self._family(change.knob))
        semantic_error = self.semantic_consistency_error(proposal)
        if semantic_error:
            return semantic_error
        # A diagnosis may require a coherent multi-knob action spanning catalog
        # families (for example shared_buffers plus bgwriter controls). It is
        # still one mechanism test; factorial registration is required only
        # when the proposal claims an interaction effect.
        return None

    @classmethod
    def semantic_consistency_error(cls, proposal: ExpertProposal) -> str | None:
        """Catch obvious title/mechanism/action substitutions before any measurement."""
        actions = {change.knob for change in proposal.knob_changes}
        if not actions:
            return None
        text = " ".join((proposal.title, proposal.configuration_cause, proposal.mechanism,
                         proposal.expected_tps_effect, proposal.expected_latency_effect)).lower()
        mentioned_catalog = {name for name in FullKnobSpace().names if name.lower() in text}
        if mentioned_catalog and mentioned_catalog.isdisjoint(actions):
            return ("semantic_mismatch: narrative names " + ",".join(sorted(mentioned_catalog))
                    + " but knob_changes contains " + ",".join(sorted(actions)))
        return None

    @staticmethod
    def _family(knob: str) -> str:
        if knob.startswith(("autovacuum", "vacuum")) or knob == "default_statistics_target": return "maintenance"
        if knob.startswith(("wal", "checkpoint", "commit")) or knob in {"min_wal_size", "max_wal_size"}: return "wal"
        if knob.startswith("bgwriter") or knob == "backend_flush_after": return "writeback"
        if "mem" in knob or knob == "shared_buffers": return "memory"
        if "lock" in knob or knob == "deadlock_timeout": return "concurrency"
        if knob in {"random_page_cost", "seq_page_cost", "effective_cache_size", "jit", "from_collapse_limit", "join_collapse_limit", "effective_io_concurrency"}: return "query"
        if "parallel" in knob or "worker" in knob: return "workers"
        return "other"
