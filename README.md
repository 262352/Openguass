# Andromeda PostgreSQL 诊断调优

当前版本实现基于竞争假设与增量因果图的多智能体 PostgreSQL 诊断调优。三个领域专家并行提出查询执行、内存存储、并发维护方面的假设；协调器串行选择实验；确定性验证器分别判断实验有效性、TPS 收益和机制证据。在线诊断不使用 GP/EI，贝叶斯优化仅作为独立对照。

## 部署要求

- Ubuntu 22.04 aarch64；
- root 或 sudo 权限；
- 可访问所选 APT、PyPI、Maven 镜像和 DeepSeek API；
- Python 3.10 及以上由安装脚本检查；
- PostgreSQL 14、aarch64 Temurin Java 23 和固定版本 BenchBase 由脚本安装。

## 从零部署：一条命令完成配置并运行

正式部署假设服务器上只有 Ubuntu 22.04 aarch64 和当前 Git 仓库，不要求预先创建
Python 虚拟环境、PostgreSQL 用户、测试数据库、BenchBase、快照或 `.env`。脚本会
建立完整环境；运行时只需在隐藏提示中输入 DeepSeek API Key。

```bash
cd /root/Openguass
git pull origin main
sudo bash scripts/setup_and_run_multi_agent.sh --mirror tsinghua
```

也可以选择阿里云或官方源：

```bash
sudo bash scripts/setup_and_run_multi_agent.sh --mirror aliyun
sudo bash scripts/setup_and_run_multi_agent.sh --mirror official
```

该入口按顺序完成：

1. 从 `.env.example` 创建权限为 `0600` 的本机 `.env`，隐藏输入 DeepSeek API Key；
2. 安装并启动 PostgreSQL 14；
3. 生成随机数据库密码，创建 `andromeda_bench` 登录角色以及 TPC-C、Twitter、YCSB 三个数据库，并验证 TCP 密码连接；
4. 安装 aarch64 Java 23 和固定提交的 BenchBase；
5. 创建 `.venv`，安装 `pyproject.toml` 中的全部 Python 依赖，包括 `httpx[socks]`；
6. 运行完整单元与契约测试；
7. 装载 TPC-C、Twitter、YCSB；
8. 删除认证失败等原因留下的零字节快照，重新生成并用 `pg_restore --list` 校验三个快照；
9. 完成冒烟验证后，以 nohup 启动 `TPC-C → Twitter` 多智能体实验。

首次构建 BenchBase和装载三个数据集需要一些时间。脚本结束时会打印 PID、实验 ID 和日志路径。

### 全新部署与安全重跑

脚本不会修改已有 PostgreSQL 用户的密码：

- 全新部署没有该角色时，脚本生成随机密码、写入本机 `.env` 并创建角色；
- `.env` 已有非空 `POSTGRES_BENCH_PASSWORD` 时，用它验证现有用户；
- PostgreSQL 用户已存在但 `.env` 密码为空时，交互式询问当前密码并保存到本机 `.env`；
- 只有 PostgreSQL 用户尚不存在时，才为新用户生成密码；
- `.env` 密码与现有用户不一致时立即停止，并明确报告错误。

`.env`、`.venv`、`.benchbase`、快照和运行结果均被 Git 忽略，不会上传。不要把 API Key 写入命令行或提交到仓库。

## 只配置环境，不启动实验

```bash
cd /root/Openguass
sudo bash scripts/setup_and_run_multi_agent.sh --mirror tsinghua --prepare-only
```

环境检查：

```bash
bash scripts/bootstrap_test_environment.sh --check-only
```

强制重新装载数据并生成快照：

```bash
sudo bash scripts/bootstrap_test_environment.sh --mirror tsinghua --force-reload
```

## 自定义正式实验

变量放在命令前即可改变方向和预算，安装及已有密码仍会保留：

```bash
cd /root/Openguass

SOURCE_WORKLOAD=twitter \
TARGET_WORKLOAD=tpcc \
MAX_ROUNDS=12 \
MAX_MEASUREMENTS=60 \
DURATION=30 \
WARMUP_SECONDS=30 \
TERMINALS=2 \
BASELINE_REPEATS=3 \
DRIFT_PERCENT=25 \
sudo --preserve-env=SOURCE_WORKLOAD,TARGET_WORKLOAD,MAX_ROUNDS,MAX_MEASUREMENTS,DURATION,WARMUP_SECONDS,TERMINALS,BASELINE_REPEATS,DRIFT_PERCENT \
  bash scripts/setup_and_run_multi_agent.sh --mirror tsinghua
```

环境已经完成准备时，可以直接启动而不重复安装：

```bash
SOURCE_WORKLOAD=tpcc TARGET_WORKLOAD=twitter bash scripts/run_multi_agent_diagnosis.sh
```

直接启动入口会检查 `.venv`、BenchBase 和目标负载快照，缺少任何一项都会在测量前停止并给出明确提示。

## 查看运行状态

```bash
EXPERIMENT_ID=$(cat artifacts/current_multi_agent_experiment_id.txt)
PID=$(cat artifacts/current_multi_agent_pid.txt)

ps -p "$PID" -o pid,etime,cmd
tail -f "artifacts/${EXPERIMENT_ID}.nohup.log"
```

查看最终报告和每轮性能：

```bash
cat "artifacts/multi_agent/${EXPERIMENT_ID}/tpcc_to_twitter/REPORT.md"
cat "artifacts/multi_agent/${EXPERIMENT_ID}/tpcc_to_twitter/performance.csv"
cat "artifacts/multi_agent/${EXPERIMENT_ID}/tpcc_to_twitter/CAUSAL_GRAPH.md"
```

主要结果文件还包括：

- `hypothesis_pool.jsonl`：三个专家的结构化竞争假设；
- `round_NNN_decision.json`：交叉审查和协调器选择；
- `experiment_ledger.jsonl`：冻结预测、动作、实际生效值、原始测量及判定；
- `pending_graph.json`、`tested_graph.json`、`effective_graph.json`：三层增量因果图；
- `final_rollback.json`：实验结束后的配置恢复结果。

## 保留的脚本

- `setup_and_run_multi_agent.sh`：正式的一键配置并运行入口；
- `bootstrap_test_environment.sh`：环境、数据和快照准备；
- `install_postgres_benchbase.sh`：底层系统组件与 BenchBase 安装；
- `run_multi_agent_diagnosis.sh`：环境已经准备好时直接启动诊断；
- `run_benchmarks.sh`：单独运行三个真实负载；
- `analyze_bayesian_vs_andromeda.py`：读取已有结果生成对照报告。

方法、验证门控和公平对比要求见 [`docs/multi_agent_diagnosis.md`](docs/multi_agent_diagnosis.md)。
