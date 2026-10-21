# Andromeda 10/21 方法更新与运行入口

本版将候选、champion 和实验父配置分开记录。候选只通过同期序贯配对晋级：默认最小对数收益 `0.03`，首块明显变差立即止损，最多执行三个块。完整候选保存在 `candidate_archive.jsonl`，当前推荐配置保存在 `champion.json`。

专家调度使用 EWMA 对数收益和 Softmax 概率，并强制每位专家至少执行一次；连续三次负收益会进入 `paused_low_yield`，新异常、交互证据或第五轮探索可重新激活。诊断之后可运行 30 秒的健康覆盖扫描，强制覆盖 `shared_buffers`、`wal_buffers`、`random_page_cost` 和 `work_mem` 等代表旋钮。

固定工作负载协议为 TPC-C scale factor 5、3 terminals，Twitter 5 terminals，YCSB 5 terminals，隔离级别 `TRANSACTION_SERIALIZABLE`。BenchBase 使用 10 次重试和 50 ms 基准的 full-jitter 指数退避。

## 冒烟实验：5 轮诊断 + 5 轮健康扫描

```bash
cd /root/Openguass
mkdir -p artifacts

EXPERIMENT_ID="tpcc-next-smoke-$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$PWD/artifacts/${EXPERIMENT_ID}.nohup.log"

nohup env \
  PATH="$PWD/.venv/bin:$PATH" \
  EXPERIMENT_ID="$EXPERIMENT_ID" \
  MAX_ROUNDS=5 \
  HEALTH_ROUNDS=5 \
  SEARCH_ROUNDS=0 \
  MAX_MEASUREMENTS=63 \
  MAX_MODEL_CALLS=15 \
  MAX_WALL_SECONDS=14400 \
  DURATION=30 \
  WARMUP_SECONDS=10 \
  TPCC_TERMINALS=3 \
  BASELINE_REPEATS=3 \
  BASELINE_STABILITY_RETRIES=0 \
  PAIRED_BLOCKS=3 \
  PAIRED_DELTA_MIN=0.03 \
  BENCHMARK_SEED=20261021 \
  bash scripts/run_tpcc_continuous.sh >"$LOG" 2>&1 &

PID=$!
printf '%s\n' "$PID" > artifacts/current_tpcc_continuous_pid.txt
printf 'PID=%s\nEXPERIMENT_ID=%s\nLOG=%s\nRESULT=%s\n' \
  "$PID" "$EXPERIMENT_ID" "$LOG" \
  "$PWD/artifacts/tpcc_continuous/$EXPERIMENT_ID"
```

## 正式实验：10 轮诊断 + 10 轮健康扫描

```bash
cd /root/Openguass
mkdir -p artifacts

EXPERIMENT_ID="tpcc-next-formal-$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$PWD/artifacts/${EXPERIMENT_ID}.nohup.log"

nohup env \
  PATH="$PWD/.venv/bin:$PATH" \
  EXPERIMENT_ID="$EXPERIMENT_ID" \
  MAX_ROUNDS=10 \
  HEALTH_ROUNDS=10 \
  SEARCH_ROUNDS=0 \
  MAX_MEASUREMENTS=126 \
  MAX_MODEL_CALLS=30 \
  MAX_WALL_SECONDS=43200 \
  DURATION=120 \
  WARMUP_SECONDS=30 \
  TPCC_TERMINALS=3 \
  BASELINE_REPEATS=3 \
  BASELINE_STABILITY_RETRIES=1 \
  PAIRED_BLOCKS=3 \
  PAIRED_DELTA_MIN=0.03 \
  BENCHMARK_SEED=20261021 \
  bash scripts/run_tpcc_continuous.sh >"$LOG" 2>&1 &

PID=$!
printf '%s\n' "$PID" > artifacts/current_tpcc_continuous_pid.txt
printf 'PID=%s\nEXPERIMENT_ID=%s\nLOG=%s\nRESULT=%s\n' \
  "$PID" "$EXPERIMENT_ID" "$LOG" \
  "$PWD/artifacts/tpcc_continuous/$EXPERIMENT_ID"
```

部署后若 BenchBase 尚未按本版重新构建，先运行现有的一键安装脚本。它会应用 `patches/benchbase-full-jitter.patch`，并配置或验证查询证据扩展。
