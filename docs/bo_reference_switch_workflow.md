# 先建立 BO 参考，再执行证据图负载切换诊断

## 实际组件映射

- BO：`app.phase2.bayesian_baseline.BayesianBaseline`，使用 60 旋钮 `FullKnobSpace`、Latin hypercube、项目 GP 和 expected improvement。
- 数据库参数：`PostgreSQLKnobExecutor`，记录请求值、实际值、reload/restart 和最终恢复。
- 工作负载：`PostgresBenchBaseAdapter`，当前流程只允许 `tpcc` 与 `twitter`。
- 遥测：`DiagnosticSampler`、`normalize_by_transactions`、BenchBase TPS/P95 和客户端健康字段。
- 在线诊断：`MultiAgentRunner`，使用当前 incumbent、源/目标 profile、GraphPatch 黑板和确定性验证器。
- 离线评价：`OracleReferenceStore`，只有它读取目标负载 BO reference。

## 两类图

`agent_graph.json` 只记录专家角色、黑板/协调器通信边、每轮调用和消息上限。默认拓扑是 `blackboard`，每轮三个专家各调用一次，不执行全连接互审。

`evidence_graph.json` 是按事务串行提交的 GraphPatch 黑板。支持 `add_hypothesis`、`add_observation_request`、`add_intervention`、`add_prediction`、`challenge`、`merge_or_refine` 和 `report_result`。数据库验证后的细粒度边状态仍保存在 `pending_graph.json`、`tested_graph.json` 和 `effective_graph.json`。

## 一条命令运行全部阶段

环境、TPC-C/Twitter 数据和快照已经准备好后：

```bash
cd /root/Andromeda

SUITE_ID="bo-switch-$(date -u +%Y%m%dT%H%M%SZ)" \
TPCC_BO_BUDGET=100 TWITTER_BO_BUDGET=100 \
BO_INITIAL_POINTS=10 BO_RETEST_REPEATS=3 \
DIRECTIONS="tpcc:twitter,twitter:tpcc" \
REPETITIONS=1 \
DIAGNOSIS_ROUNDS=12 DIAGNOSIS_MEASUREMENTS=60 \
DURATION=120 WARMUP_SECONDS=30 TPCC_TERMINALS=10 TWITTER_TERMINALS=16 \
BASELINE_REPEATS=3 SOURCE_PROFILE_REPEATS=1 \
MAX_OBSERVATION_ROUNDS=2 BENCHMARK_SEED=20261012 \
bash scripts/run_bo_reference_then_switch.sh
```

长时间运行可使用：

```bash
cd /root/Andromeda
mkdir -p artifacts
SUITE_ID="bo-switch-$(date -u +%Y%m%dT%H%M%SZ)"

nohup env \
  SUITE_ID="$SUITE_ID" \
  TPCC_BO_BUDGET=100 \
  TWITTER_BO_BUDGET=100 \
  BO_INITIAL_POINTS=10 \
  BO_RETEST_REPEATS=3 \
  DIAGNOSIS_ROUNDS=12 \
  DIAGNOSIS_MEASUREMENTS=60 \
  DURATION=120 \
  WARMUP_SECONDS=30 \
  TPCC_TERMINALS=10 \
  TWITTER_TERMINALS=16 \
  bash scripts/run_bo_reference_then_switch.sh \
  > "artifacts/${SUITE_ID}.nohup.log" 2>&1 &

echo "PID=$!"
echo "SUITE_ID=$SUITE_ID"
```

正式 profile 为：TPC-C `scale factor=20`、`terminals=10`；Twitter
`scale factor=100`、`terminals=16`。两者均使用
`TRANSACTION_SERIALIZABLE`、`rate=unlimited`、120 秒正式测量。30 秒预热由执行器作为
独立阶段运行，随后等待 PostgreSQL 后台活动收敛，再开始计入结果的正式测量。

修改 scale factor 或工作负载 XML 后，旧快照会被配置指纹校验拒绝。重新生成：

```bash
sudo bash scripts/bootstrap_test_environment.sh --mirror tsinghua --force-reload
```

## 分阶段运行

只运行两个 BO 参考：

```bash
STAGE=bo SUITE_ID=bo-switch-formal bash scripts/run_bo_reference_then_switch.sh
```

BO 完成后只运行双向诊断：

```bash
STAGE=diagnose SUITE_ID=bo-switch-formal bash scripts/run_bo_reference_then_switch.sh
```

也可以直接使用 Python 入口：

```bash
.venv/bin/python -m app.experiment.cli bo --suite-id bo-switch-formal \
  --tpcc-bo-budget 100 --twitter-bo-budget 100 --bo-initial-points 10

.venv/bin/python -m app.experiment.cli diagnose --suite-id bo-switch-formal \
  --directions tpcc:twitter,twitter:tpcc --diagnosis-rounds 12 \
  --diagnosis-measurements 60 --source-profile-repeats 1
```

## 输出结构

```text
artifacts/workload_switch/<suite_id>/
├── suite_manifest.json
├── bo_initial_config.json
├── bo_stage_summary.json
├── diagnosis_stage_summary.json
├── oracle/
│   ├── bo_references/{tpcc,twitter}/reference.json
│   ├── runs/...
│   └── evaluations/*.json
└── episodes/
    └── repeat_01/
        ├── tpcc_to_twitter/
        └── twitter_to_tpcc/
```

每个 episode 包含：

- `diagnosis_visible_context.json`：允许进入模型的上下文；
- `source_workload_profile.json`：源负载 BO 配置下的稳态；
- `agent_graph.json`：有界通信记录；
- `evidence_graph.json` 和 `graph_patches.jsonl`：共享黑板；
- `pending_graph.json`、`tested_graph.json`、`effective_graph.json`：数据库证据边状态；
- `round_NNN_decision.json`、`experiment_ledger.jsonl`、`performance.csv`；
- `incumbent.json`、`report.json`、`REPORT.md` 和 `final_rollback.json`。

目标负载 BO 参考及 gap 只出现在 `oracle/evaluations/`。诊断请求、`diagnosis_visible_context.json` 和 agent/evidence graph 不包含目标参考配置、参考 TPS、gap 或推荐方向。

## 预算含义

BO budget 是搜索 trial 数；另有一次起点基线和 `BO_RETEST_REPEATS` 次最佳配置复测。诊断 measurement budget 统计目标负载基线、补充观测和 control/treatment；源 workload profile 单独计数。报告分别保留 benchmark 测量、模型调用、tokens、重启和配置应用成本。

“BO 最优”只表示当前搜索空间、预算和环境内的最佳已测配置，不表示全局最优。

无法启动、超时或违反 SQL 错误策略的候选配置作为零吞吐不可行样本反馈给 GP，帮助代理模型避开失败区域，但不会参与最佳 TPS 选择。最终候选必须通过全部复测；复测失败时会记录到 `reference_candidate_rejections.jsonl`，并按搜索 TPS 从高到低尝试下一候选，直至基线。针对串行化的 scale-100 Twitter，`max_pred_locks_per_transaction` 搜索范围为 256–2048，避免 PostgreSQL 默认 64 对应的谓词锁共享表过小。

BO 的 TPS 定义为 BenchBase Goodput，即每秒成功完成的事务数。`40P01` 死锁和 `40001`
序列化冲突在比例不超过 `BENCHBASE_MAX_TRANSIENT_ERROR_RATIO`（默认 0.001，即 0.1%）时
作为有记录的暂态事务中止接受；超过阈值或出现其他 SQLSTATE 时，该配置测量无效且不进入 GP
训练。原始 BenchBase Throughput、错误数量、SQLSTATE 和错误率仍保存在每次测量结果中。
