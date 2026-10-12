from __future__ import annotations

import html
import json
import re
import textwrap
from collections import defaultdict
from pathlib import Path

from .models import EvidenceGraph, GraphNode


NODE_COLORS = {
    "knob": "#7c3aed", "workload_context": "#475569", "mechanism": "#d97706",
    "evidence_metric": "#0284c7", "target_metric": "#dc2626",
    "intervention": "#7c3aed", "experiment_result": "#059669",
}
EDGE_COLORS = {
    "candidate": "#94a3b8", "supported": "#16a34a", "refuted": "#dc2626",
    "inconclusive": "#d97706", "superseded": "#64748b",
}
LAYERS = {
    "workload_context": 0, "knob": 0, "intervention": 0, "mechanism": 1,
    "evidence_metric": 2, "experiment_result": 3, "target_metric": 4,
}


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", value)


def _short(value: str, width: int = 46) -> str:
    return textwrap.shorten(" ".join(value.split()), width=width, placeholder="…")


def _node_label(node: GraphNode) -> str:
    label = node.name
    if node.type == "evidence_metric":
        current = f"{node.current_value:g}" if node.current_value is not None else "?"
        if node.baseline_value is not None:
            label += f"\\n{node.baseline_value:g} → {current} {node.unit or ''}"
        else:
            label += f"\\n{current} {node.unit or ''}"
    return _short(label, 64)


def mermaid(graph: EvidenceGraph) -> str:
    lines = ["flowchart LR"]
    for node in graph.nodes:
        shape = f'(["{_node_label(node)}"])' if node.type == "target_metric" else f'["{_node_label(node)}"]'
        lines.append(f"  {_safe(node.node_id)}{shape}")
    for edge in graph.edges:
        label = _short(f"{edge.relation} · {edge.status}", 36).replace('"', "'")
        lines.append(f"  {_safe(edge.cause_node_id)} -->|\"{label}\"| {_safe(edge.effect_node_id)}")
    for node in graph.nodes:
        lines.append(f"  style {_safe(node.node_id)} fill:{NODE_COLORS[node.type]},color:#fff,stroke:#0f172a,stroke-width:1px")
    for index, edge in enumerate(graph.edges):
        dash = ",stroke-dasharray: 5 4" if edge.status in {"candidate", "inconclusive"} else ""
        lines.append(f"  linkStyle {index} stroke:{EDGE_COLORS[edge.status]},stroke-width:2px{dash}")
    return "\n".join(lines)


def _layout(graph: EvidenceGraph):
    grouped: dict[int, list[GraphNode]] = defaultdict(list)
    for node in graph.nodes:
        grouped[LAYERS[node.type]].append(node)
    positions = {}
    max_rows = max((len(nodes) for nodes in grouped.values()), default=1)
    for layer, nodes in grouped.items():
        for row, node in enumerate(sorted(nodes, key=lambda item: item.node_id)):
            positions[node.node_id] = (60 + layer * 300, 105 + row * 120)
    return positions, max(420, 180 + max_rows * 120)


def svg(graph: EvidenceGraph) -> str:
    positions, height = _layout(graph)
    width, node_w, node_h = 1440, 230, 72
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="context-stroke"/></marker></defs>',
        '<style>text{font-family:Inter,Arial,sans-serif}.title{font-size:22px;font-weight:700;fill:#0f172a}.small{font-size:11px;fill:#334155}.node{font-size:12px;font-weight:600;fill:#fff}.edge{font-size:10px;fill:#334155}</style>',
        f'<rect width="100%" height="100%" fill="#f8fafc"/><text class="title" x="36" y="38">{html.escape(graph.title)}</text>',
        '<text class="small" x="36" y="61">实线绿色=supported　红色=refuted　虚线灰色=candidate　虚线橙色=inconclusive</text>',
    ]
    for edge in graph.edges:
        x1, y1 = positions[edge.cause_node_id]; x2, y2 = positions[edge.effect_node_id]
        sx, sy, ex, ey = x1 + node_w, y1 + node_h / 2, x2, y2 + node_h / 2
        color = EDGE_COLORS[edge.status]
        dash = ' stroke-dasharray="7 5"' if edge.status in {"candidate", "inconclusive"} else ""
        mid = (sx + ex) / 2
        parts.append(f'<path d="M {sx} {sy} C {mid} {sy}, {mid} {ey}, {ex} {ey}" fill="none" stroke="{color}" stroke-width="2" marker-end="url(#arrow)"{dash}/>')
        parts.append(f'<text class="edge" x="{mid}" y="{(sy + ey) / 2 - 5}" text-anchor="middle">{html.escape(_short(edge.relation + " · " + edge.status, 34))}</text>')
    for node in graph.nodes:
        x, y = positions[node.node_id]; color = NODE_COLORS[node.type]
        parts.append(f'<g><title>{html.escape(node.description)}</title><rect x="{x}" y="{y}" width="{node_w}" height="{node_h}" rx="12" fill="{color}" stroke="#0f172a"/>')
        label = _node_label(node).replace("\\n", " | ")
        wrapped = textwrap.wrap(label, 30)[:3]
        for line_no, line in enumerate(wrapped):
            parts.append(f'<text class="node" x="{x + node_w / 2}" y="{y + 23 + line_no * 17}" text-anchor="middle">{html.escape(line)}</text>')
        parts.append(f'<text x="{x + 8}" y="{y + node_h - 7}" font-size="9" fill="#e2e8f0">{html.escape(node.type)} · {html.escape(node.status)}</text></g>')
    parts.append('</svg>')
    return "\n".join(parts)


def markdown(graph: EvidenceGraph, svg_name: str) -> str:
    counts = defaultdict(int)
    for edge in graph.edges:
        counts[edge.status] += 1
    lines = [
        f"# {graph.title}", "",
        "> 图中候选边表示尚未经过反事实实验验证的假设，不能作为已验证根因。", "",
        f"![诊断证据图]({svg_name})", "", "## 可缩放 Mermaid 图", "", "```mermaid",
        mermaid(graph), "```", "", "## 边状态", "",
        "| 状态 | 数量 | 含义 |", "|---|---:|---|",
        f"| candidate | {counts['candidate']} | 模型提出，尚未验证 |",
        f"| supported | {counts['supported']} | 反事实结果达到预先冻结阈值 |",
        f"| refuted | {counts['refuted']} | 观测与机制预测冲突 |",
        f"| inconclusive | {counts['inconclusive']} | 数据缺失或实验不足 |",
        f"| superseded | {counts['superseded']} | 已被后续假设替代 |", "", "## 节点明细", "",
        "| 节点 | 类型 | 状态 | 描述 |", "|---|---|---|---|",
    ]
    for node in graph.nodes:
        lines.append(f"| {node.name.replace('|', '/')} | {node.type} | {node.status} | {_short(node.description, 110).replace('|', '/')} |")
    return "\n".join(lines) + "\n"


def render_graph(graph: EvidenceGraph, directory: Path, stem: str = "diagnosis_graph") -> dict[str, str]:
    directory.mkdir(parents=True, exist_ok=True)
    json_path, md_path, svg_path = (directory / f"{stem}.{suffix}" for suffix in ("json", "md", "svg"))
    json_path.write_text(json.dumps(graph.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
    svg_path.write_text(svg(graph), encoding="utf-8")
    md_path.write_text(markdown(graph, svg_path.name), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path), "svg": str(svg_path)}
