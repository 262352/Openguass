# Andromeda PostgreSQL 单智能体诊断链：9-29_v2

更新日期：2026-09-29

本版本把“模型一次给出很多旋钮”改造成可实际执行的顺序诊断闭环。在线目标只有目标负载 TPS：系统不知道全局最优 TPS，也不会用历史最优配置、参考 TPS、GP 标签或所谓解释覆盖率决定下一步。最终报告只说明在当前观测、60 旋钮动作空间、`single_run_directional` 协议和预算下得到的局部证据。

## 1. 已实现的方法

一个方向的真实流程如下：

```text
保存实验开始配置
  → 在源负载快照上测量 steady state
  → 保持配置，切换并恢复目标负载快照
  → 预热并实测 initial control
  → 同一个 DeepSeek 智能体提出一个瓶颈假设和一个验证前沿
  → 冻结 CounterfactualContract
  → 在相同快照、terminals、duration、seed 和预热条件下各跑一次 control/treatment
  → 只更新本合同选择的边
  → IncumbentPolicy 接受配置或立即回滚
  → 用新状态继续、修订、分支或关闭当前假设
  → 停止后恢复实验开始配置
```

模型固定为 `deepseek-flash`，接口为 `https://api.deepseek.com`。密钥只从 `DEEPSEEK_API_KEY` 或受保护的 `.env` 读取，不写入代码、提示词产物和报告。

### 假设和验证前沿

`HypothesisRecord` 保存父假设、修订号、初始/当前状态、候选/支持/反驳边、合同、实验和被接受干预。旧修订追加到 `hypotheses.jsonl`，不会覆盖。

`SequentialGraphSession` 同时只允许一个 active contract。合同完成后，即使图中仍有未验证的 candidate 边，同一智能体也可以选择新的 `ValidationFrontier`。本轮实验只能改变前沿声明的 `selected_edge_ids`；未选边继续保持 candidate。

### 单智能体输出

`SingleAgentChainDiagnoser` 每轮接收当前配置、当前目标 TPS、真实 PostgreSQL 遥测、此前 control/treatment 结论、当前假设和剩余预算。它输出一个严格 Pydantic JSON 对象，动作限定为：

- `continue`、`revise`、`branch`；
- `abandon`、`close_hypothesis`、`propose_next_bottleneck`；
- `stop`。

可执行链必须是“负载上下文 → 机制 → 真实证据 → TPS 限制”，并选择证据的可控上游旋钮。证据 metric ID 必须存在于本次遥测。模型只给诊断建议，不能执行 SQL。

### 冻结合同与实验计划

`CounterfactualContract` 是 frozen Pydantic model，包含状态和配置哈希、旋钮动作、分组、控制变量、逐边预测、预先冻结的阈值、风险、重启和回滚规则。指标缺失返回 `inconclusive`，不能当作反驳。

`ExperimentPlanner` 支持：

- `independent`：多个候选动作拆成单旋钮 treatment；
- `coupled`：只有给出 catalog 依赖知识时才原子执行；
- `interaction`：生成各单旋钮主效应和联合 treatment，control 由执行循环单独测量；
- 预算不足时在执行前拒绝计划。

没有 top-3 或最多三个旋钮的截断。执行器的依赖修复记录在 `reconciliation_changes`，请求配置和实际配置都进入实验产物。

### 逐边证据边界

`CounterfactualValidator` 只验证合同绑定的边。干预使直接证据按冻结预测变化，可以支持“干预 → 证据”；证据与 TPS 同时符合预测，局部路径标记为 `supported_under_current_protocol`。没有直接检验的内部机制边不会被批量改成 supported；它仍带有 `mechanism_inferred=true`，竞争解释也应继续保留。

`effective_graph` 只包含实验支持的边，并标明 `validation_mode=single_run_directional`。模型置信度不等同于实验支持。

### 瓶颈变化与配置接受

`BottleneckFrontierAnalyzer` 按预设方向和阈值把证据分类为 `resolved`、`improved`、`persistent`、`worsened`、`emerged`、`unchanged`、`missing` 或 `unknown`，写入 `bottleneck_transitions.jsonl`。新异常只是后续诊断候选，不会自动变成根因。

`IncumbentPolicy` 仅在配置实际应用、数据库健康、关键证据符合预测、局部路径得到支持且 TPS 改善达到阈值时接受 treatment。证据改善但 TPS 不改善时保留局部证据并回滚；TPS 改善而机制预测失败时记录为 unexplained improvement 并回滚。后续 control 总是从已接受的 incumbent 开始。

## 2. 在线决策已删除的依赖

V3/V4 的预检、提示词、合同、接受策略和停止条件均不读取：

- 目标参考 TPS 或参考配置；
- initial/current TPS gap；
- explanation coverage；
- GP 历史、GP 标签或已知正确旋钮。

历史 phase-1/phase-2 代码仍用于旧实验溯源，但不是 `chain` 命令的在线输入。

## 3. 预检

```bash
cd /root/Andromeda
python3 -m app.lab.cli chain preflight
```

预检 fail closed 地检查 PostgreSQL/BenchBase、TPC-C 与 Twitter 快照、60 旋钮 catalog、遥测、2 GiB 磁盘空间、配置备份/回滚能力以及 DeepSeek 严格 JSON。HTTP 402、认证错误、模型不可用、空响应、非法 JSON和 schema 错误都会得到 `preflight_failed`，不会伪装成无安全修改。

## 4. 命令行

快速单方向工程验收：

```bash
python3 -m app.lab.cli chain run \
  --source tpcc --target twitter --mode quick \
  --max-experiments 2
```

正式运行两个方向：

```bash
python3 -m app.lab.cli chain run-directions \
  --directions tpcc:twitter,twitter:tpcc --mode formal
```

查询、停止、恢复和汇总：

```bash
ID=$(cat artifacts/current_v3_v4_experiment_id.txt 2>/dev/null || cat artifacts/current_experiment_id.txt)
python3 -m app.lab.cli chain status --experiment-id "$ID"
python3 -m app.lab.cli chain stop --experiment-id "$ID"
python3 -m app.lab.cli chain resume --experiment-id "$ID"
python3 -m app.lab.cli chain report --experiment-id "$ID"
```

恢复未完成方向前会从该方向保存的 `postgresql.auto.conf.before` 恢复安全 checkpoint，并归档中断目录，再重新初始化该方向。两个方向各自捕获开始配置并在结束时恢复，前一方向的 incumbent 不会污染后一方向。

## 5. 后台正式运行

```bash
cd /root/Andromeda
nohup ./scripts/run_v3_v4_single_agent_chain.sh \
  > artifacts/v3_v4-formal-nohup.log 2>&1 &
echo $!
```

脚本默认值为两个方向、每次测量 30 秒、预热 30 秒、2 terminals、最多 12 个 treatment、最多 6 次 PostgreSQL 重启、7200 秒墙钟预算、TPS 最小改善 5%、连续 3 个有效干预未改善后停止。可以用以下环境变量覆盖：

```bash
MODE=formal \
DIRECTIONS=tpcc:twitter,twitter:tpcc \
DURATION=30 WARMUP_SECONDS=30 TERMINALS=2 \
MAX_VALIDATION_EXPERIMENTS=12 MAX_RESTARTS=6 MAX_WALL_SECONDS=7200 \
VALIDATION_MODE=single_run_directional TPS_MIN_IMPROVEMENT_PCT=5 \
NO_IMPROVEMENT_PATIENCE=3 \
nohup ./scripts/run_v3_v4_single_agent_chain.sh \
  > artifacts/v3_v4-formal-nohup.log 2>&1 &
```

脚本使用严格 Shell 选项、全局 `flock` 和 PID 文件。SIGINT/SIGTERM 会创建 STOP checkpoint，Python 循环在当前数据库动作结束后进入 `finally` 回滚。

## 6. 产物

每个方向写入：

```text
artifacts/v3_v4/<id>/<source>_to_<target>/
├── RESULTS.md
├── performance.csv
├── manifest.json
├── status.json
├── LIVE_PROGRESS.md
├── source_steady.json
├── initial_control.json
├── states.jsonl
├── hypotheses.jsonl
├── frontiers.jsonl
├── candidate_graph.{json,md,svg}
├── effective_graph.{json,md,svg}
├── graph_events.jsonl
├── bottleneck_transitions.jsonl
├── incumbent_history.jsonl
├── contracts/
├── experiments/
├── llm/
├── final_chain.json
├── unresolved_evidence.json
├── chain_report.{json,md}
└── system/
    ├── start_config.json
    ├── postgresql.auto.conf.before
    ├── rollback.json
    └── final_rollback.json
```

日常查看只需要两个入口：实验根目录的 `SUMMARY.md` 汇总各方向状态、停止原因与回滚结果；方向目录的 `RESULTS.md` 用一行表示一次干预，直接列出旋钮、control TPS、treatment TPS、变化比例、证据状态和是否接受。`performance.csv` 提供同样内容，便于画图。其余 JSON、JSONL、SVG 和 `llm/` 文件用于复核，不需要逐个阅读。

`LIVE_PROGRESS.md` 展示初始/current/control/treatment TPS、相对初始变化、active hypothesis、验证前沿、最近结论、已接受变化、候选/支持边、剩余预算和回滚状态。它不展示参考 TPS、目标缺口或 coverage。

## 7. 当前验证边界

本阶段只做 `single_run_directional`，不重复测量、不估计噪声、不做 Bootstrap、置信区间或显著性检验，也不运行 GP 比较、消融、多智能体和长期记忆。一次方向性联合变化只能支持当前协议下的局部关系，不能证明唯一根因或全局最优。

测试命令：

```bash
python3 -m pytest -q
```

真实 quick 运行的实验 ID、停止原因、支持边与回滚状态应以本次生成的 `chain_report.json` 为准，不能从单元测试结果推断机制有效。


## 8. 2026-09-29 实地 quick 验收

最终代码版本的工程验收实验 ID 为 `chain-20260929T084745Z-30b0f7`，方向为 TPC-C→Twitter，硬限制为两个 treatment。结果如下：

- 真实 DeepSeek preflight 通过；PostgreSQL、BenchBase、两个快照、60 旋钮、遥测、磁盘、回滚和严格 JSON 均为 `ok`；
- 35 项 pytest 全部通过；
- initial Twitter control 为 3118.168 TPS；
- 第一步真实修改 `jit=off`：control 4263.779 TPS，treatment 3484.184 TPS，TPS 下降 18.284%，直接证据也与预测冲突，两条验证边均被反驳，配置回滚；
- 智能体关闭第一条假设后提出第二个瓶颈假设；
- 第二步真实修改 `random_page_cost=1.1`：control 3813.566 TPS，treatment 4011.187 TPS，提升 5.182%；合同选择的中间证据按冻结方向变化 74.421%，两条局部前沿边在当前协议下得到支持，配置被接受为本方向 incumbent；
- 两个 treatment 用尽 quick 预算后以 `budget_exhausted` 停止；
- 有效图包含两条局部支持边，四条内部机制边仍为 candidate。因此结果支持当前协议下的局部路径，不证明模型描述的是唯一根因；
- `failure=null`。方向结束后仍执行全局回滚，`start_config_restored=true`；数据库当前 60 个 GUC 已只读核对，与 `experiment_start_config` 完全一致。

每个 control/treatment 的 BenchBase XML 都写入同一个固定 `randomSeed=20260929`。这次结果证明最终版本能够实际调用模型、冻结合同、应用旋钮、运行 control/treatment、逐边更新、接受局部支持的 incumbent，并在方向结束时恢复实验开始配置。5 秒 quick 测量仍可能有明显波动；本阶段按既定范围没有加入重复测量或统计模型，因此不能把 5.182% 直接外推为稳定的正式收益。
# 10-08 verified-incumbent comparison protocol

The V3/V4 online baseline is now a repeated, verified incumbent.  Every candidate is measured in the order `C1 -> T1 -> C2 -> T2`; the lower of the two treatment TPS values is compared with the current verified incumbent.  A candidate is accepted only when both paired gains are positive, the conservative repeated score clears `MIN_TPS_PERCENT`, settings were observed as active, and latency/rollback/health guardrails pass.  Performance and mechanism verdicts are stored separately.

The diagnosis stage ranks 3-8 mechanism-specific knob groups by expected TPS impact. For each step, the model selects one mechanism and emits one coherent configuration containing every knob that jointly controls it. The executor evaluates that group as a single intervention with one `C1 -> T1 -> C2 -> T2` experiment; it does not expand one knob into several probe values. A one-knob group is allowed when the diagnosed mechanism is isolated. Independent mechanisms must remain separate groups. By default, at least four distinct groups are evaluated before an evidence-based early stop; configure this with `MIN_SCREENING_GROUPS`. Group membership, every old/new knob value, performance verdict, and mechanism verdict are written to `knob_group_effects.csv` and `knob_group_effects.jsonl`.

Formal workload transitions require one explicit 60-knob source configuration per source workload.  Load the intended source reference configuration into PostgreSQL, then capture it:

```bash
python -m app.lab.cli chain capture-source-config --workload tpcc
python -m app.lab.cli chain capture-source-config --workload twitter
```

Do this after loading each workload's own reference configuration; capturing the same active cluster state twice will honestly produce equal hashes and the aggregate report will warn that transition attribution is limited.

Run both directions with the existing launcher:

```bash
nohup bash scripts/run_v3_v4_single_agent_chain.sh \
  > artifacts/v3_v4-formal-nohup.log 2>&1 &
```

The live status is in `artifacts/v3_v4/<experiment-id>/<direction>/LIVE_PROGRESS.md`.  `performance.csv` and `RESULTS.md` retain all four measurements, both verdicts, and the incumbent comparison.  Candidate, tested, and effective chain files are emitted separately.  The launcher reads the API key only from the environment.
