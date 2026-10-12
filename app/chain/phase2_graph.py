from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .models import EvidenceGraph, GraphEdge, GraphNode
from .visualize import render_graph


METRICS = {
    "lock": ("locks_waiting", "max", "count"),
    "wait": ("waiting_backends", "p95", "count"),
    "rollback": ("rollback_ratio", "rate", "ratio"),
    "abort": ("xact_rollback", "rate", "transactions/s"),
    "wal": ("wal_bytes", "rate", "bytes/s"),
    "sync": ("wal_sync", "rate", "syncs/s"),
    "checkpoint": ("checkpoint_write_time", "rate", "ms/s"),
    "buffer": ("buffer_hit_ratio", "rate", "ratio"),
    "read": ("blks_read", "rate", "blocks/s"),
    "scan": ("seq_scan", "rate", "scans/s"),
    "temporary": ("temp_bytes", "rate", "bytes/s"),
    "temp": ("temp_bytes", "rate", "bytes/s"),
    "vacuum": ("autovacuum_count", "rate", "runs/s"),
    "maintenance": ("autovacuum_count", "rate", "runs/s"),
    "dead tuple": ("n_dead_tup", "rate", "tuples/s"),
    "backend": ("active_backends", "p95", "count"),
    "i/o": ("blks_read", "rate", "blocks/s"),
}


def _id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode()).hexdigest()[:10]
    return f"{prefix}:{digest}"


def _payload(record: dict[str, Any]) -> dict[str, Any]:
    prompt = record.get("prompt", "")
    marker = "EVIDENCE:\n"
    if marker not in prompt:
        return {}
    try:
        return json.loads(prompt.split(marker, 1)[1])
    except json.JSONDecodeError:
        return {}


def _metric(summary: dict[str, Any], metric_id: str, aggregation: str) -> float | None:
    telemetry = summary.get("telemetry", summary)
    if aggregation == "rate":
        value = telemetry.get("rates_per_second", {}).get(metric_id)
    else:
        value = telemetry.get("gauges", {}).get(metric_id, {}).get(aggregation)
    return float(value) if isinstance(value, (int, float)) else None


def _direction(before: float | None, after: float | None) -> str:
    if before is None or after is None:
        return "measured"
    if after > before:
        return "increased"
    if after < before:
        return "decreased"
    return "unchanged"


def _metrics_for(text: str) -> list[tuple[str, str, str]]:
    lower = text.lower()
    selected = []
    for token, metric in METRICS.items():
        if token in lower and metric not in selected:
            selected.append(metric)
    return selected or [("buffer_hit_ratio", "rate", "ratio")]


def build_phase2_candidate_graph(direction_dir: Path) -> EvidenceGraph:
    report_path = direction_dir / "comparison_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    transition = report["transition"]
    source, target = transition.split("->", 1)
    baseline_path = direction_dir / "target_baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    source_path = direction_dir / "source_steady.json"
    source_data = json.loads(source_path.read_text(encoding="utf-8"))
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []

    context_id = "context:transition"
    target_id = "target:tps"
    nodes[context_id] = GraphNode(
        node_id=context_id, type="workload_context", name=transition,
        description=f"从 {source} 切换到 {target}；30 秒、2 terminals；相同目标快照。",
        source_ids=[str(report_path)], status="observed",
        metadata={"source": source, "target": target, "fairness": report.get("fairness", {})},
    )
    nodes[target_id] = GraphNode(
        node_id=target_id, type="target_metric", name=f"{target} TPS",
        description="唯一优化目标：目标负载吞吐量。",
        source_ids=[str(baseline_path)], status="observed",
        metadata={"metric_id": "target_workload_throughput_rps", "value": baseline["throughput_rps"], "unit": "requests/s"},
    )

    question_files = sorted((direction_dir / "andromeda" / "llm").glob("iteration_*_question.json"))
    seen_hypotheses: set[str] = set()
    selected_question: str | None = None
    for question_path in question_files:
        record = json.loads(question_path.read_text(encoding="utf-8"))
        parsed = record.get("parsed", {})
        proposed = parsed.get("bottleneck_hypotheses", [])
        if not proposed:
            continue
        facts = _payload(record)
        source_fact = facts.get("source_steady", source_data)
        target_fact = facts.get("target_current", baseline)
        # Historical phase-2 did not have an explicit selected_hypothesis_id.
        # Render exactly one first-step chain: the highest-confidence proposal
        # from the earliest successful round. Later rounds may only be appended
        # by the sequential V3/V4 loop after this step has a terminal result.
        hypothesis = max(proposed, key=lambda item: float(item.get("confidence", 0.0)))
        selected_question = question_path.name
        for hypothesis in [hypothesis]:
            text = hypothesis.get("name", "").strip()
            if not text or text in seen_hypotheses:
                continue
            seen_hypotheses.add(text)
            confidence = float(hypothesis.get("confidence", 0.0))
            mechanism_id = _id("mechanism", text)
            nodes[mechanism_id] = GraphNode(
                node_id=mechanism_id, type="mechanism", name=f"假设 {len(seen_hypotheses)}",
                description=text, source_ids=[str(question_path)], status="hypothesized",
                metadata={"confidence": confidence, "llm_evidence_ids": hypothesis.get("evidence_ids", [])},
            )
            edge_seed = f"{context_id}>{mechanism_id}"
            edges.append(GraphEdge(
                edge_id=_id("edge", edge_seed), cause_node_id=context_id, effect_node_id=mechanism_id,
                relation="工作负载条件可能触发", mechanism_text=text, confidence=confidence,
                evidence_ids=hypothesis.get("evidence_ids", []),
                alternative_explanations=["工作负载语义差异", "短窗口测量波动"],
            ))
            for metric_id, aggregation, unit in _metrics_for(text):
                before = _metric(source_fact, metric_id, aggregation)
                after = _metric(target_fact, metric_id, aggregation)
                if after is None:
                    continue
                evidence_id = _id("evidence", f"{question_path.name}:{metric_id}:{after}")
                source_window = source_fact.get("telemetry", source_fact).get("elapsed_seconds")
                target_window = target_fact.get("telemetry", target_fact).get("elapsed_seconds")
                nodes[evidence_id] = GraphNode(
                    node_id=evidence_id, type="evidence_metric", name=metric_id,
                    description=f"真实遥测：{metric_id} {_direction(before, after)}。",
                    source_ids=[str(source_path), str(question_path)], status="observed",
                    metric_id=metric_id, metric_source="PostgreSQL one-second telemetry",
                    sampling_window={"source_seconds": source_window, "target_seconds": target_window},
                    baseline_value=before, current_value=after, aggregation=aggregation,
                    direction=_direction(before, after), unit=unit,
                )
                edges.append(GraphEdge(
                    edge_id=_id("edge", f"{mechanism_id}>{evidence_id}"), cause_node_id=mechanism_id,
                    effect_node_id=evidence_id, relation="预测产生", mechanism_text=text,
                    confidence=confidence, evidence_ids=[evidence_id],
                    alternative_explanations=["工作负载本身的事务结构变化"],
                ))
                edges.append(GraphEdge(
                    edge_id=_id("edge", f"{mechanism_id}:{evidence_id}>{target_id}"), cause_node_id=evidence_id,
                    effect_node_id=target_id, relation="可能影响", mechanism_text=f"{metric_id} 与目标 TPS 的候选关系",
                    confidence=confidence, evidence_ids=[evidence_id],
                    alternative_explanations=["其他未测机制", "同配置运行噪声"],
                ))
        break

    if not seen_hypotheses:
        failure_id = "result:diagnosis_unavailable"
        nodes[failure_id] = GraphNode(
            node_id=failure_id, type="experiment_result", name="诊断未完成",
            description="历史运行未产生任何通过 schema 校验的候选假设；评估记录显示模型调用失败。",
            source_ids=[str(direction_dir / "andromeda" / "trials.jsonl")], status="inconclusive",
            metadata={"reason": "structured_llm_output_unavailable"},
        )
        edges.append(GraphEdge(
            edge_id="edge:diagnosis_unavailable", cause_node_id=context_id, effect_node_id=failure_id,
            relation="诊断中断", mechanism_text="没有模型生成的因果假设可供验证",
            status="inconclusive", confidence=0.0,
            alternative_explanations=["API 余额、认证或网络失败"],
        ))
        for metric_id, aggregation, unit in (
            ("buffer_hit_ratio", "rate", "ratio"),
            ("locks_waiting", "max", "count"),
            ("rollback_ratio", "rate", "ratio"),
        ):
            before = _metric(source_data, metric_id, aggregation)
            after = _metric(baseline, metric_id, aggregation)
            if after is None:
                continue
            evidence_id = f"evidence:fallback_{metric_id}"
            nodes[evidence_id] = GraphNode(
                node_id=evidence_id, type="evidence_metric", name=metric_id,
                description=f"已采集的真实目标基线遥测；没有可关联的结构化模型假设。",
                source_ids=[str(source_path), str(baseline_path)], status="observed",
                metric_id=metric_id, metric_source="PostgreSQL one-second telemetry",
                sampling_window={
                    "source_seconds": source_data.get("telemetry", {}).get("elapsed_seconds"),
                    "target_seconds": baseline.get("telemetry", {}).get("elapsed_seconds"),
                }, baseline_value=before, current_value=after, aggregation=aggregation,
                direction=_direction(before, after), unit=unit,
            )
            edges.append(GraphEdge(
                edge_id=f"edge:fallback_{metric_id}_target", cause_node_id=evidence_id,
                effect_node_id=target_id, relation="因果关系未验证",
                mechanism_text="模型失败，不能从观测相关性推断因果", status="inconclusive",
                confidence=0.0, evidence_ids=[evidence_id],
                alternative_explanations=["工作负载语义差异", "运行噪声", "其他未测机制"],
            ))
    return EvidenceGraph(
        graph_id=f"phase2:{report['experiment_id']}:{transition}", graph_kind="candidate",
        title=f"{transition} 假设—证据诊断图", nodes=list(nodes.values()), edges=edges,
        metadata={
            "experiment_id": report["experiment_id"], "transition": transition,
            "warning": "This graph contains candidate hypotheses from the historical phase-2 run; no counterfactual edge validation was performed.",
            "hypothesis_count": len(seen_hypotheses), "source_report": str(report_path),
            "diagnosis_complete": bool(seen_hypotheses),
            "rendering_policy": "one_active_chain_per_step",
            "selected_historical_question": selected_question,
        },
    )


def generate_phase2_graphs(experiment_id: str, root: Path = Path("artifacts/phase2"), direction: str | None = None) -> list[dict[str, Any]]:
    experiment = root / experiment_id
    directories = [experiment / direction] if direction else sorted(path for path in experiment.iterdir() if path.is_dir() and "_to_" in path.name)
    output = []
    for directory in directories:
        graph = build_phase2_candidate_graph(directory)
        paths = render_graph(graph, directory)
        effective = graph.effective()
        candidate_paths = render_graph(graph, directory, "candidate_graph")
        effective_paths = render_graph(effective, directory, "effective_graph")
        output.append({
            "direction": directory.name, "nodes": len(graph.nodes), "edges": len(graph.edges),
            "diagnosis_graph": paths,
            "candidate_graph": candidate_paths,
            "effective_graph": effective_paths,
        })
    return output
