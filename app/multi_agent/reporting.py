from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .graph import GraphStore
from .models import IncumbentSnapshot, ValidationOutcome


class ReportBuilder:
    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.performance = directory / "performance.csv"
        if not self.performance.exists():
            with self.performance.open("w", newline="", encoding="utf-8") as stream:
                csv.writer(stream).writerow(["round", "experiment_id", "proposal_id", "validity", "control_tps",
                    "treatment_tps", "verified_tps", "performance_verdict", "mechanism_verdict", "accepted", "unexplained_performance"])

    def append_round(self, round_number: int, proposal_id: str, controls: list[dict[str, Any]], treatments: list[dict[str, Any]], outcome: ValidationOutcome) -> None:
        with self.performance.open("a", newline="", encoding="utf-8") as stream:
            csv.writer(stream).writerow([round_number, outcome.experiment_id, proposal_id, outcome.validity,
                ";".join(str(x.get("throughput_rps")) for x in controls),
                ";".join(str(x.get("throughput_rps")) for x in treatments),
                (outcome.verified_performance or {}).get("throughput_rps"), outcome.performance_verdict,
                outcome.mechanism_verdict, outcome.accepted_as_incumbent, outcome.unexplained_performance])

    def build(self, incumbent: IncumbentSnapshot, graph: GraphStore, ledger_summary: dict[str, Any]) -> dict[str, Any]:
        tested = graph.snapshot("tested")
        effective = graph.snapshot("effective")
        report = {
            "schema_version": "andromeda.multi-agent-report/v1", "incumbent": incumbent.model_dump(mode="json"),
            "effective_path_count": graph.effective_path_count(),
            "supported_edge_count": sum(e.state == "tested_supported" for e in tested.edges),
            "refuted_edge_count": sum(e.state == "tested_refuted" for e in tested.edges),
            "inconclusive_edge_count": sum(e.state == "inconclusive" for e in tested.edges),
            "unexplained_performance": ledger_summary.get("unexplained_performance", False), **ledger_summary,
        }
        (self.directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = ["# 多智能体诊断调优报告", "", f"- 当前 verified TPS：`{incumbent.verified_performance.get('throughput_rps')}`",
                 f"- episode 初始 TPS：`{report.get('episode_initial_tps')}`",
                 f"- 有效因果路径：`{report['effective_path_count']}`", f"- 支持/反驳/不确定边：`{report['supported_edge_count']}/{report['refuted_edge_count']}/{report['inconclusive_edge_count']}`",
                 f"- 未解释性能收益：`{report['unexplained_performance']}`", "", "## 有效路径", ""]
        if not effective.edges:
            lines.append("当前没有形成从干预到目标 TPS 的完整受支持路径。")
        else:
            lines.extend(f"- `{e.source}` → `{e.target}`（{e.relation}），实验：{', '.join(e.validation_experiment_ids)}" for e in effective.edges)
        lines += ["", "每条边的冻结预测、实验和原始观测可在 `effective_graph.json`、`tested_graph.json` 与 `experiment_ledger.jsonl` 中追溯。", ""]
        (self.directory / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
        tested_nodes = {n.node_id: n for n in tested.nodes}
        diagram = ["# 增量因果图", "", "```mermaid", "flowchart LR"]
        for node in tested.nodes:
            safe = node.label.replace('"', "'")
            diagram.append(f'  {self._mermaid_id(node.node_id)}["{safe}"]')
        for edge in tested.edges:
            experiment = edge.validation_experiment_ids[-1] if edge.validation_experiment_ids else "unvalidated"
            diagram.append(f'  {self._mermaid_id(edge.source)} -->|"{edge.state}; {experiment}"| {self._mermaid_id(edge.target)}')
        if not tested.edges:
            diagram.append('  empty["尚无经过实验的边"]')
        diagram += ["```", "", "边标签中的实验 ID 可在 `experiment_ledger.jsonl` 中查到冻结预测、动作与原始测量。", ""]
        (self.directory / "CAUSAL_GRAPH.md").write_text("\n".join(diagram), encoding="utf-8")
        return report

    def add_operational_cost(self, cost: dict[str, Any]) -> None:
        path = self.directory / "report.json"
        if not path.exists():
            return
        report = json.loads(path.read_text(encoding="utf-8"))
        report["operational_cost"] = cost
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        markdown = self.directory / "REPORT.md"
        with markdown.open("a", encoding="utf-8") as stream:
            stream.write("\n## 实际成本\n\n")
            stream.write(f"- 正式负载测量：`{cost['workload_measurements']}`\n")
            stream.write(f"- warmup 运行：`{cost['warmup_runs']}`\n")
            stream.write(f"- PostgreSQL 重启（含最终恢复）：`{cost['postgresql_restarts']}`\n")
            stream.write(f"- 模型调用：`{cost['model_calls']}`\n")
            stream.write(f"- 模型 tokens：`{cost['model_tokens']}`\n")
            stream.write(f"- 最终恢复：`{cost['final_rollback'].get('restored')}`\n")

    @staticmethod
    def _mermaid_id(value: str) -> str:
        return "n_" + "".join(ch if ch.isalnum() else "_" for ch in value)
