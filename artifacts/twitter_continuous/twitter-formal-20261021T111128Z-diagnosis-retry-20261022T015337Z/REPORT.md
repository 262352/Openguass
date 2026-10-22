# 多智能体诊断调优报告

- 当前 verified TPS：`29216.334887861933`
- episode 初始 TPS：`20596.238097430927`
- 有效因果路径：`0`
- 支持/反驳/不确定边：`0/16/0`
- 未解释性能收益：`True`

## 有效路径

当前没有形成从干预到目标 TPS 的完整受支持路径。

每条边的冻结预测、实验和原始观测可在 `effective_graph.json`、`tested_graph.json` 与 `experiment_ledger.jsonl` 中追溯。

## 实际成本

- 正式负载测量：`141`
- warmup 运行：`141`
- PostgreSQL 重启（含最终恢复）：`14`
- 模型调用：`45`
- 模型 tokens：`1824809`
- 最终恢复：`True`
