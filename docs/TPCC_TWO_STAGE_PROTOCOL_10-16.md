# TPC-C 两阶段诊断与有界搜索协议（10/16）

当前流程先运行 100 轮全参数贝叶斯优化作为离线性能参照，再从 PostgreSQL 默认配置独立运行
Andromeda。BO 结果不会进入 Andromeda 的在线提示词、诊断状态或搜索空间。

Andromeda 第一阶段由三个专家向增量证据图添加机制假设。系统先测试图上已有的 `proposed`
假设，只有当前 frontier 已清空时才再次调用专家。协调器只能从专家已经提出的假设中选择下一条
最有信息价值的边，不能生成参数、替换参数或调整步长。

每个专家动作经过两次一致性检查：应用前比较 `knob_changes` 与冻结候选；应用后比较冻结值与
PostgreSQL 实际生效值。安全裁剪、隐式依赖参数、旋钮替换、方向变化和实际值不一致都会写入
`action_consistency.jsonl`，verdict 为 `action_substitution`。系统不会用另一个动作代替被拒绝
动作。

`hypothesis_status.json` 保存每条假设的当前状态，状态集合为：

- `proposed`、`testing`；
- `tested_supported`、`tested_refuted`、`tested_inconclusive`；
- `rejected_unsafe`、`rejected_duplicate`、`rejected_infeasible`。

安全或可执行性拒绝会在下一次专家调用时作为反馈输入。只有全部假设进入 `tested_*` 或
`rejected_*`，并且连续两次专家咨询没有新增 `proposed` 假设时，才能触发
`diagnosis_exhausted`。完整停止依据写入 `DIAGNOSIS_EXHAUSTION.json`。

第二阶段读取假设表和验证结果，输出冻结的 `FROZEN_SEARCH_SPACE.json`：

- TPS 已验证改善：以成功值为中心向两侧扩展一个专家初始步长；
- TPS 未改善但机制得到支持：保留专家范围的中间三分之一；
- 机制被反驳：排除；
- 尚未测试但被重复建议：保留完整专家范围；
- 被安全门控拒绝：排除；
- 过宽的正数范围：使用对数中间区间。

搜索器只生成冻结边界中的单参数候选和专家原始联合旋钮组，不允许添加其他旋钮。默认预算是
15 轮诊断和 15 轮有界搜索，每个候选采用两次 control 与两次 treatment；加三次默认基线后，
总工作负载测量上限为 123 次。

主要审计输出：

- `action_consistency.jsonl`：专家动作、冻结动作与生效动作；
- `hypothesis_status.json`、`hypothesis_status_events.jsonl`：假设状态及转换历史；
- `DIAGNOSIS_EXHAUSTION.json`：停止条件和全部最终状态；
- `FROZEN_SEARCH_SPACE.json`：第二阶段不可变范围和来源；
- `experiment_ledger.jsonl`：诊断和有界搜索的完整实验事件。
