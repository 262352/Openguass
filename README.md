# Andromeda

Andromeda serves as a natural surrogate of DBAs to answer a wide range of natural language (NL) questions on DBMS configuration issues, and to generate diagnostic suggestions to fix these issues. Nevertheless, directly prompting LLMs with these professional questions may result in overly generic and often unsatisfying answers. To this end, we propose a retrieval-augmented generation (RAG) strategy that effectively provides matched domain-specific contexts for the question from multiple sources. 

## Requirements

- Python >= 3.10

### 一键安装完整测试环境

在 Ubuntu 22.04 上以一个命令安装 Python 隔离环境、PostgreSQL 14、Java 23、
固定版本的 BenchBase，创建三个测试数据库，装载 TPC-C、Twitter、YCSB，生成
可重复使用的快照，并执行全部单元测试与真实负载冒烟测试：

```bash
cd /root/Andromeda
DEEPSEEK_API_KEY=你的密钥 ./scripts/bootstrap_test_environment.sh
```

在中国大陆环境可以让 apt、pip 和 Maven 使用清华源或阿里云源：

```bash
./scripts/bootstrap_test_environment.sh --mirror tsinghua
# 或
./scripts/bootstrap_test_environment.sh --mirror aliyun
```

清华模式使用 TUNA 的 Ubuntu 与 PyPI 镜像；由于 TUNA 当前没有 Maven Central
镜像，BenchBase 的 Maven 依赖使用阿里云公共仓库。镜像配置仅对本次脚本进程生效，
不会覆盖 `/etc/apt/sources.list`、用户的 pip 配置或 Maven 全局配置。

`DEEPSEEK_API_KEY` 未提供时，单元测试、数据库和 BenchBase 仍可完成安装，调用
DeepSeek 的诊断实验需要之后把密钥写入 `.env`。脚本会自动生成本地数据库密码，
且不会打印密码。重复运行会复用已有负载快照。

只检查已经安装的环境：

```bash
./scripts/bootstrap_test_environment.sh --check-only
```

强制重新装载三个负载并重建快照：

```bash
./scripts/bootstrap_test_environment.sh --force-reload
```
  
You can install multiple packages:

```
pip install -r requirements.txt
```

## 1. Quick start

### run example to generate hybrid-pipeline


```
python example.py
```

The file `example.py` is an example. Modify `query` according to your configuration.

- The **input** contains a user's NL question about DBSM configuration issue.
- The **output** is the troubleshooting configuration recommendation.

### You can find our code in `./core`.

## 2. Dataset

### You can find our benchmark in `./dataset` with link 

[https://1drv.ms/f/c/140409cb8fe0acca/Eo-VI-dPwVRNkald2yEdngIBJ0RZ616df-ZDtxXrITd2mg?e=WhrapQ ](https://1drv.ms/f/c/140409cb8fe0acca/Eo-VI-dPwVRNkald2yEdngIBLb-EM4i5vVVh_R718XQZRA?e=vFf6hd)

```python
-dataset
├── augment_train
│   └── mysql_forum_train_augment.json
│   └── mysql_so_train_augment.json
│   └── pg_so_train_augment.json
│   └── mysql_run_train_augment.json
├── historical_questions
│   └── mysql_forum_retrieval_data.json
│   └── mysql_so_retrieval_data.json
│   └── pg_so_retrieval_data.json
│   └── mysql_run_retrieval_data.json
├── manuals
│   └── mysql_manuals_data.json
│   └── mysql_manuals_data.json
├── test
│   └── mysql_forum_test_data.json
│   └── mysql_so_test_data.json
│   └── pg_so_test_data.json
│   └── mysql_run_test_data.json
├── train
│   └── mysql_forum_train_data.json
│   └── mysql_so_train_data.json
│   └── pg_so_train_data.json
│   └── mysql_run_train_data.json
-sbert_embeds
├── mysql_forum_retrieval_data.npy
├── mysql_forum_train_augment.npy
├── mysql_run_retrieval_data.npy
├── mysql_run_train_augment.npy
├── mysql_so_retrieval_data.npy
├── mysql_so_train_augment.npy
├── pg_so_retrieval_data.npy
├── pg_manuals_data.npy
├── mysql_manuals_data.npy
-sentence-transformers
├── all-mpnet-base-v2
```

The vectors in sbert_embeds are directly ebedded by model in sentence-transformers.

## 3. Results

You can find our generated prompts in `./results/prompt.json`.

You can find the results of LLM reasoning in `./reasoning_results`.

You can find the manual evaluation results in the Runnable setting in `./results/manual_evaluation_on_runnable_setting.json`.

## 4. Experiments

Please refer `./main.py` to see our experiment results.


## 5. PostgreSQL BenchBase 实测环境

当前实验环境使用 PostgreSQL 14 与 BenchBase，负载为 TPC-C、Twitter 和 YCSB。
这一层参考了 MCTuner 与 GPTuner 的设计：BenchBase 负责建表、装载、执行和生成
throughput、goodput、延迟分布、原始事务记录与 DBMS 指标。BenchBase 固定到提交
`33c00473807ebd49304d114a6d769d2d2b2bbb34`，其当前版本要求 Java 23。

首次安装与构建（Ubuntu 22.04，以 root 运行）：

```bash
cd /root/Andromeda
./scripts/install_postgres_benchbase.sh
```

首次装载三个数据库并分别执行 10 秒冒烟测试：

```bash
./scripts/run_benchmarks.sh prepare 10 2
```

后续调参前后应复用同一份数据，只执行负载：

```bash
./scripts/run_benchmarks.sh run 30 2
```

也可直接运行最终 Python 命令：

```bash
python3 -m app.benchmark.cli all --duration 30 --terminals 2 --reuse-data
```

`duration` 是每个负载的测量秒数，`terminals` 是并发终端数。每次运行写入
`artifacts/runs/<run_id>/`，其中包含 BenchBase 的 `summary.json`、`raw.csv`、
`metrics.json`、完整 stdout 和本项目生成的 `verified-result.json`。只要子进程失败、
缺少 summary，或 `Unexpected SQL Errors` 非空，命令就以失败退出。

本机已用 PostgreSQL 14.24、Temurin 23.0.2、2 个终端和每负载 10 秒完成实测。
三种负载均返回成功，合并结果保存在
`artifacts/runs/latest-benchbase-results.json`。该短测仅验证执行链，不代表正式性能结论；
正式对比应提高持续时间、终端数和 scale factor，并在前后测试中保持完全一致。

## 6. LHS+GP reference tuning and bidirectional switching

Inspect and freeze the actual environment:

```bash
python3 -m app.lab.cli environment inspect
python3 -m app.lab.cli migration report
python3 -m app.lab.cli transition matrix
```

Run the formal experiment. This defaults to exactly 15 valid LHS plus 35 valid GP trials for both TPC-C and Twitter, then selects repeated references and executes both active transitions:

```bash
cd /root/Andromeda
./scripts/run_phase1_lhs_gp_two_transitions.sh
```

Run a short end-to-end engineering validation without claiming the formal 50-trial result:

```bash
MODE=quick LHS=2 GP=1 REPEATS=1 DURATION=5 TERMINALS=1 \
  ./scripts/run_phase1_lhs_gp_two_transitions.sh
```

Individual commands support checkpoint resume:

```bash
python3 -m app.lab.cli tune reference tpcc --experiment-id formal-001
python3 -m app.lab.cli reference select tpcc --experiment-id formal-001
python3 -m app.lab.cli transition run tpcc twitter --experiment-id formal-001 --strict-llm
python3 -m app.lab.cli workload run ycsb --duration 30 --terminals 2
```

The formal/quick distinction, PostgreSQL adaptation, isolation boundary, leakage rules, telemetry approximation, and original-paper boundary are documented in `docs/current_system_audit.md`.

### Interrupted formal runs

Snapshot restore is transactional and prints start/completion timing. On this machine the 42 MB TPC-C snapshot restores in about 10 seconds; this pause is expected. Pressing Ctrl+C terminates the current run. Resume the same checkpoint with the experiment ID printed at startup or stored in `artifacts/current_experiment_id.txt`:

```bash
EXPERIMENT_ID=$(cat artifacts/current_experiment_id.txt) ./scripts/run_phase1_lhs_gp_two_transitions.sh
```

The script forces the portable `C` locale, reuses an existing complete snapshot, and does not overwrite it during resume. Snapshot creation writes a temporary file and renames it only after `pg_dump` succeeds.

## 7. Live progress and result layout

The experiment terminal prints one concise line per start/result event. Full BenchBase stdout is written to the corresponding run artifact and is not streamed to the terminal.

After starting an experiment, read its ID and follow the human-readable report:

```bash
EXPERIMENT_ID=$(cat artifacts/current_experiment_id.txt)
watch -n 2 "cat artifacts/experiments/$EXPERIMENT_ID/live_progress.md"
```

A machine-readable current-state query is also available:

```bash
python3 -m app.lab.cli experiment status --id "$EXPERIMENT_ID"
```

Each experiment uses this layout:

```text
artifacts/experiments/<id>/
├── live_progress.md    # concise table for people
├── live_status.json    # current state and best result per workload
├── progress.csv        # one row per baseline/trial/transition step
├── tpcc/
│   ├── baseline.json
│   ├── trials.jsonl
│   └── checkpoint.json
└── twitter/
    ├── baseline.json
    ├── trials.jsonl
    └── checkpoint.json
```

Transition results are split into a concise `transition_report.json`, raw one-second samples in `telemetry_samples.json`, and model calls under `llm/`. Pressing Ctrl+C marks `live_status.json` as `paused`; rerunning with the same `EXPERIMENT_ID` resumes from the checkpoint.

# Openguass
