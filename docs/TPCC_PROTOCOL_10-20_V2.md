# TPC-C BO 与证据图诊断协议（10-20 v2）

本版修复了 10/19 实验中最影响结论的三类问题，并暂时关闭诊断后的有界搜索。

## 统一旋钮目录

BO 与诊断现在共同使用 `FullKnobSpace` 的 62 个旋钮。原有 60 个旋钮本来已经包含
`shared_buffers`、`effective_io_concurrency`、`commit_delay`、`commit_siblings` 和
`max_pred_locks_per_transaction`；本版补入：

- `max_pred_locks_per_relation`
- `max_pred_locks_per_page`

每次实验都会从 `pg_settings` 重新采集 setting、boot/reset value、unit、context、source
和 pending_restart，静态目录只负责给出经过约束的实验范围。BO manifest 与诊断 baseline
plan 均记录完整旋钮名称和数量。旧的 60 维 source config 会用服务器当前值补齐新增维度。

组合入口在 BO 开始前只捕获一次 `bo_initial_config.json`，随后把同一文件传给诊断。
两条轨迹因此使用相同的初始配置、快照、seed、instrumentation、TPC-C XML 和代码版本。

## 为什么上一轮没有调整 shared_buffers

10/19 的 33 个专家提案只覆盖 6 个旋钮：`wal_writer_flush_after`、
`autovacuum_max_workers`、`default_statistics_target`、`autovacuum_vacuum_cost_limit`、
`wal_writer_delay` 和 `wal_buffers`。`shared_buffers` 虽然始终在允许目录中，但只在前两轮
作为 alternative 出现，从未进入 `knob_changes`，所以协调器没有可以执行的
`shared_buffers` 动作。

本版把每个旋钮的已实验次数和高优先级未覆盖旋钮传给所有专家。当
`low_buffer_hit_ratio` 活跃且 `shared_buffers` 尚未测试时，memory expert 必须提出一个
有界、可执行的 `shared_buffers` 机制实验，或者明确说明缺少哪一个标量证据；仅写入
alternative 不再满足覆盖要求。SERIALIZABLE retry 活跃时，对三个 `max_pred_locks_*`
旋钮采用同样规则。协调器仍然只能执行专家的原始动作，但会给未覆盖维度加信息增益分，
不会自行生成或替换旋钮值。

## 测量与状态语义

- 每个候选默认采用 3 个随机顺序的 control/treatment 区组。
- 按 BenchBase `server_retry_ratio` 把运行标记为 clean、transition 或 storm；一个区组内
  control 与 treatment 不同 regime 时，结果为 underpowered，不视为反驳。
- 对有效区组的相对 TPS 差做 90% block-bootstrap CI。CI 下界高于最小有价值增益才接受；
  明显有害、等价和证据不足分别记录，不再用臂内 spread 代替效应不确定性。
- 旧的 `superseded_by_evidence` 改为 `parked`。欠采样或 parked 的动作可在新 evidence
  epoch 被专家修订重提；只有同 context 中已有充分 supported/refuted 结论才硬去重。

## 当前实验边界

本轮只执行 100 次基础 BO 和 15 轮证据图诊断。`SEARCH_ROUNDS=0`，不会运行旧的冻结
有界搜索器。DDL、索引创建和 SQL 重写仍只记录为机会，不会执行。
