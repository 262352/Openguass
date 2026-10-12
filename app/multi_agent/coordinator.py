from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.phase2.space import FullKnobSpace

from .models import CrossReview, ExpertProposal, ExperimentSelection


class Coordinator:
    """Transparent rules for retaining competition and choosing one serialized experiment."""

    def __init__(self, space: FullKnobSpace | None = None):
        self.space = space or FullKnobSpace()

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
               remaining_measurements: int = 1) -> ExperimentSelection:
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
            score = 4.0 * differentiators + 2.0 * len(proposal.alternatives) - constraints - restart_penalty - knob_penalty - cost_penalty
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
        required_measurements = 4
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
        if len(families) > 1 and not proposal.interaction_factors:
            return "action crosses independent mechanisms without a pre-registered interaction design"
        return None

    @staticmethod
    def _family(knob: str) -> str:
        if knob.startswith(("autovacuum", "vacuum")) or knob == "default_statistics_target": return "maintenance"
        if knob.startswith(("wal", "checkpoint", "commit")) or knob in {"min_wal_size", "max_wal_size"}: return "wal"
        if knob.startswith("bgwriter") or knob == "backend_flush_after": return "writeback"
        if "mem" in knob or knob in {"shared_buffers", "effective_cache_size"}: return "memory"
        if "lock" in knob or knob == "deadlock_timeout": return "concurrency"
        if knob in {"random_page_cost", "seq_page_cost", "jit", "from_collapse_limit", "join_collapse_limit", "effective_io_concurrency"}: return "query"
        if "parallel" in knob or "worker" in knob: return "workers"
        return "other"
