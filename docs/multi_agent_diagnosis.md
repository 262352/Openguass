# 基于竞争假设与增量因果图的多智能体诊断调优

## 方法边界

该入口使用三个并行领域专家：查询计划与执行、内存与存储、并发与维护。专家只能提交结构化局部假设、冻结预测和相关旋钮组。协调器按可执行性、区分竞争解释的信息量、预计成本和重启代价选择一个实验。PostgreSQL 参数修改和 BenchBase 测量始终串行。

该方法内部不调用 GP、EI 或其他贝叶斯搜索器。`app.phase2.bayesian_baseline` 保留为独立对照，不能向专家或协调器提供候选配置。

每轮固定使用当前 verified incumbent 及其遥测。输出包括：

- `hypothesis_pool.jsonl`：三个专家的原始结构化提案；
- `round_NNN_decision.json`：交叉审查、选中及拒绝理由；
- `pending_graph.json`、`tested_graph.json`、`effective_graph.json`：当前三层图；
- `graph_versions/`：每个图版本的不可覆盖快照；
- `experiment_ledger.jsonl`：冻结动作、预测、执行顺序、实际生效值、原始测量和判定；
- `performance.csv`：逐轮性能轨迹；
- `CAUSAL_GRAPH.md`：可读 Mermaid 因果图；
- `REPORT.md` 和 `report.json`：最终结论与真实不确定性；
- `final_rollback.json`：实验开始配置恢复结果。

性能提升与机制支持分别判定。性能独立复测通过时允许更新 incumbent；若中间机制预测失败，对应边仍为 `tested_refuted` 或 `inconclusive`，报告保留 `unexplained_performance=true`。有效图只包含从 intervention 到 objective 的完整受支持路径。

独立根因保持为通向 TPS 的独立分支。顺序瓶颈转移通过 incumbent 和图版本记录。专家若主张真正交互，必须提交 `InteractionDesign`，把动作旋钮严格分成 A、B 两组，并在执行前冻结 baseline、A-only、B-only、A+B 的 TPS 预测、加法尺度和非加性阈值。预算至少支持每格两次、共 8 次测量时，执行器才运行交替顺序的四格实验；预算不足或设计不完整时只保留 pending，不生成 supported interaction。

正式实验前先对不修改参数的相同配置测量至少三次。最大值与最小值达到倍数差异，或相对中位数的跨度超过阈值时，实验以 `unstable_baseline` 停止。程序还把同一配置、快照、硬件、负载和测量条件的基线写入 `artifacts/multi_agent/baseline_registry.jsonl`；即使一次运行内部看似稳定，只要其中位数与同上下文历史基线相差两倍以上，也会停止正式诊断。

## 在新服务器安装

部署目标为 Ubuntu 22.04 aarch64，支持 Python 3.10 及以上。下面的路径由操作者决定，程序不会写死项目目录、PostgreSQL 数据目录或内存大小。

```bash
git clone https://github.com/262352/Openguass.git
cd Openguass

# 一次完成 PostgreSQL、aarch64 Java、BenchBase、Python、数据、快照并启动实验。
sudo bash scripts/setup_and_run_multi_agent.sh --mirror tsinghua
# 或：sudo bash scripts/setup_and_run_multi_agent.sh --mirror aliyun

# 检查已有环境，不重装。
./scripts/bootstrap_test_environment.sh --check-only
```

一键入口会保留 `.env` 中已有的非空配置，并验证已有 PostgreSQL 用户的密码；它不会
用新值覆盖现有数据库密码。仅准备环境而不启动时增加 `--prepare-only`。零字节快照
不会被复用，三个快照必须同时非空并通过 `pg_restore --list` 后实验才会启动。

多智能体入口新增的 Python 依赖是 `httpx[socks]`（运行时包为 `socksio`）。它已在
`pyproject.toml` 声明，上面的一键脚本通过 `pip install -e .` 自动安装；PostgreSQL
连接、BenchBase 和 60 旋钮执行器没有新增系统依赖。

在项目根目录创建 `.env`。密钥只从环境变量或该本地文件读取，不会进入实验报告：

```bash
cp .env.example .env
chmod 600 .env
```

需要设置的变量示例：

```dotenv
DEEPSEEK_API_KEY=替换为你的密钥
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_TIMEOUT_SECONDS=120
POSTGRES_BENCH_PASSWORD=安装脚本生成的测试用户密码
PGHOST=127.0.0.1
PGPORT=5432
PGUSER=andromeda_bench
```

准备并固定三个数据快照：

```bash
./scripts/run_benchmarks.sh prepare 10 1
```

正式运行前捕获真正的源工作负载配置。应先把数据库切换到希望作为源状态的配置，再执行：

```bash
python3 -m app.lab.cli chain capture-source-config --workload tpcc --output artifacts/source_configs/tpcc.json
python3 -m app.lab.cli chain capture-source-config --workload twitter --output artifacts/source_configs/twitter.json
```

## 小规模验证

以下命令使用 3 次无改动基线测量，最多 1 轮诊断和总计 7 次正式测量。若基线不稳定会自动停止：

```bash
SOURCE_WORKLOAD=tpcc TARGET_WORKLOAD=twitter \
MAX_ROUNDS=1 MAX_MEASUREMENTS=7 DURATION=10 WARMUP_SECONDS=5 TERMINALS=1 \
./scripts/run_multi_agent_diagnosis.sh
```

## 正式实验

```bash
cd /path/to/Openguass
EXPERIMENT_ID="multi-agent-$(date -u +%Y%m%dT%H%M%SZ)"

nohup env \
  EXPERIMENT_ID="$EXPERIMENT_ID" \
  SOURCE_WORKLOAD=tpcc \
  TARGET_WORKLOAD=twitter \
  MAX_ROUNDS=12 \
  MAX_MEASUREMENTS=60 \
  DURATION=30 \
  WARMUP_SECONDS=30 \
  TERMINALS=2 \
  BASELINE_REPEATS=3 \
  DRIFT_PERCENT=25 \
  BENCHMARK_SEED=20261012 \
  bash scripts/run_multi_agent_diagnosis.sh \
  > "artifacts/${EXPERIMENT_ID}.nohup.log" 2>&1 &

echo "PID=$!"
echo "EXPERIMENT_ID=$EXPERIMENT_ID"
```

查看结果：

```bash
EXPERIMENT_ID=$(cat artifacts/current_multi_agent_experiment_id.txt)
tail -f "artifacts/${EXPERIMENT_ID}.nohup.log"
cat "artifacts/multi_agent/${EXPERIMENT_ID}/tpcc_to_twitter/REPORT.md"
cat "artifacts/multi_agent/${EXPERIMENT_ID}/tpcc_to_twitter/performance.csv"
```

反向实验应使用新的实验 ID 单独运行：

```bash
SOURCE_WORKLOAD=twitter TARGET_WORKLOAD=tpcc ./scripts/run_multi_agent_diagnosis.sh
```

## 与 BO 公平比较

两种方法必须复用同一源配置、快照、硬件、旋钮边界、随机种子、测量窗口和护栏，并同时报告总工作负载测量次数与墙钟时间。多智能体的一轮通常消耗四次配对测量，不能与 BO 的一次单点测量视为相同预算。双方最终配置还需要用完全相同、与搜索过程隔离的重复协议复测。报告应同时给出 TPS/P95、模型调用和 tokens、重启次数、有效路径数、反驳假设数、未解释收益及交互是否完成四组对照。

当前代码不会自动宣称多智能体优于 BO；只有上述共同复测结果才可用于该结论。
