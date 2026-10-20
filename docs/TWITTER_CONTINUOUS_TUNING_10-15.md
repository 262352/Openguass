# Twitter 默认配置起步、诊断引导持续调优（10-15）

## 本版方法

本入口固定使用 Twitter 数据集、workload 混合、隔离级别和发压配置，从 PostgreSQL `pg_settings.boot_val` 建立可审计基线。当前配置只作为恢复对象；不会被称作默认值。系统保存 60 个设置的 `setting/boot_val/reset_val/unit/context/source/sourcefile/sourceline/pending_restart`，随后将调优参数恢复到启动默认值。观测参数与 Twitter scale-100 在 SERIALIZABLE 下所需的锁表容量作为显式例外固定。

每次 benchmark 测量并行采集总体与分事务性能、数据库计数器和等待、WAL、主机 CPU/内存/磁盘、`pg_stat_statements` 窗口差分及按 queryid 的等待事件。统计重置、queryid 消失、样本不足、版本不支持字段和未开启的计时都单独标记；缺失值不会写成 0。查询分位数只来自 BenchBase 原始事务记录，不从累计 SQL 统计推算。

三个专家读取同一 incumbent、同一测量快照和同一版本证据图，输出异常解释或优化机会。专家负责机制和参数组，`AdaptiveSearchScheduler` 负责可复现的具体值：先按明确资源/等待路径修复瓶颈；有收益时在同组缩放步长继续附近探索；连续低收益后转向未充分探索的组，并在跨组阶段尝试同组双参数组合。组连续失败后进入 `dormant`，经过固定轮次自动恢复为 `active`。健康指标、没有慢查询或专家只返回观察都不会结束实验。

每个候选执行交错的两次 control 与两次 treatment。性能判定和机制判定分别输出：TPS 可靠提升且延迟/错误约束满足时可以更新 incumbent；机制预测不成立时标记 `unexplained_performance`。机制变化但 TPS 无收益不会更新。联合动作只代表联合效果，不声明单旋钮因果或参数交互。

本轮只执行参数动作。SQL、索引、应用和事务机会写入 `sql_opportunities.jsonl`，不会执行 DDL、建索引或 SQL 改写。

## 固定例外

- `logging_collector`、连接/断开/checkpoint 日志、慢语句与临时文件日志、日志轮转大小和 `track_activity_query_size` 固定为进入实验前的观测设置。
- `max_locks_per_transaction=256`、`max_pred_locks_per_transaction=4096` 是 Twitter scale-100、SERIALIZABLE 的环境安全设置，不进入搜索。
- `shared_preload_libraries/pg_stat_statements`、`track_io_timing`、`track_wal_io_timing`、端口、角色、数据目录属于 60 旋钮以外的基础设施或观测设置。
- 初始 `postgresql.auto.conf` 保存在 `system/postgresql.auto.conf.before`，退出时无论成功或异常都会恢复并重启 PostgreSQL。

## 输出

结果位于 `artifacts/twitter_continuous/<EXPERIMENT_ID>/`：

- `default_baseline_plan.json`：原始配置、boot 默认、分类、固定例外和恢复方法。
- `default_baseline_effective.json`：请求值、实际生效值和 pending_restart 检查。
- `measurements/*.json`：每次原始测量、查询窗口、等待采样、主机与数据库指标。
- `round_NNN_decision.json`：专家建议、当前阶段、调度原因、实际冻结候选和动作边界。
- `search/search_state.json`：每组步长、方向、历史候选、active/refining/dormant 状态。
- `graph_versions/`、`pending_graph.json`、`tested_graph.json`、`effective_graph.json`：唯一证据图的版本快照。
- `experiment_ledger.jsonl`、`performance.csv`、`incumbent.json`、`report.json`：完整执行次序和性能/机制判定。
- `sql_opportunities.jsonl`：记录但不执行的查询与索引机会。
- `final_rollback.json`：恢复结果。

## 环境检查

从零部署时运行：

```bash
cd /root/Openguass
sudo bash scripts/bootstrap_test_environment.sh --mirror tsinghua
```

安装脚本会建立 Python 虚拟环境、PostgreSQL、固定角色和密码、BenchBase、三个 workload 快照，并预加载和创建 `pg_stat_statements`。已有环境只检查时运行：

```bash
cd /root/Openguass
sudo bash scripts/bootstrap_test_environment.sh --check-only
```

## 小预算冒烟

冒烟只验证默认基线、查询证据、一个候选、判定和最终恢复，不用于证明方法有效：

```bash
cd /root/Openguass
EXPERIMENT_ID="twitter-smoke-$(date -u +%Y%m%dT%H%M%SZ)" \
MAX_ROUNDS=1 MAX_MEASUREMENTS=5 MAX_WALL_SECONDS=1800 MAX_MODEL_CALLS=3 \
DURATION=30 WARMUP_SECONDS=10 TWITTER_TERMINALS=8 BASELINE_REPEATS=1 \
bash scripts/run_twitter_continuous.sh
```

## 正式后台运行

密钥继续从已有 `.env` 或环境读取。下面的命令不会把密钥写入命令历史：

```bash
cd /root/Openguass
mkdir -p artifacts
EXPERIMENT_ID="twitter-continuous-$(date -u +%Y%m%dT%H%M%SZ)"
LOG="artifacts/${EXPERIMENT_ID}.nohup.log"

nohup env \
  EXPERIMENT_ID="$EXPERIMENT_ID" \
  MAX_ROUNDS=12 \
  MAX_MEASUREMENTS=60 \
  MAX_WALL_SECONDS=21600 \
  MAX_MODEL_CALLS=36 \
  DURATION=120 \
  WARMUP_SECONDS=30 \
  TWITTER_TERMINALS=8 \
  BASELINE_REPEATS=3 \
  DRIFT_PERCENT=25 \
  MIN_TPS_GAIN_PERCENT=5 \
  BENCHMARK_SEED=20261015 \
  bash scripts/run_twitter_continuous.sh > "$LOG" 2>&1 &

PID=$!
printf 'PID=%s\nEXPERIMENT_ID=%s\nLOG=%s\nRESULT=%s\n' \
  "$PID" "$EXPERIMENT_ID" "$LOG" "artifacts/twitter_continuous/$EXPERIMENT_ID"
```

查看进度：

```bash
tail -f "$LOG"
cat "artifacts/twitter_continuous/$EXPERIMENT_ID/performance.csv"
cat "artifacts/twitter_continuous/$EXPERIMENT_ID/search/search_state.json"
```

## 清理孤立 raw run

脚本只将“run ID 没有出现在任何 `artifacts/runs` 外实验记录中”的目录判为孤立；默认只预览，`--apply` 才删除，并生成 `artifacts/run_prune_report.json`：

```bash
.venv/bin/python scripts/prune_orphan_runs.py
.venv/bin/python scripts/prune_orphan_runs.py --apply
```
