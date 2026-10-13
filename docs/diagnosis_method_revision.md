# 诊断调优方法修订：事务关键路径与可验证旋钮组

## 修订原因

旧方法主要把总体 TPS、总体 p95 和 PostgreSQL 累计计数交给专家。Twitter 中约 1% 的
`InsertTweet` 可以长期阻塞两个 BenchBase 终端，但频繁的读事务仍会把总体 p95 保持在
亚毫秒级。因此，模型看到的是“总体尾延迟正常”，没有看到真正决定吞吐量的事务关键路径。
旧协调器还把 `effective_cache_size` 归到内存机制，把 `random_page_cost` 和
`seq_page_cost` 归到查询机制，导致同一规划代价模型的三旋钮组被错误拒绝。

## 新诊断链

每次测量现在按以下顺序执行：

1. 恢复同一数据库快照并执行固定预热。
2. 强制 `CHECKPOINT`，再等待 `SNAPSHOT_SETTLE_SECONDS`，减少快照恢复产生的后台写回混入计时窗口。
3. 同时采集 BenchBase 原始事务记录、PostgreSQL WAL/等待事件和 Linux PSI I/O 压力。
4. 按事务类型计算 count、mean、p50、p95、p99、最大值、累计占用秒数和终端容量占比。
5. 规则层先生成高信号告警，再由三个专家围绕同一证据图提出竞争解释。
6. 专家可以提出一个机制对应的最小旋钮组；协调器仍一次只执行一个候选组，用成对
   control/treatment 重复验证。
7. 只有 TPS 达标、重复有效且总体与事务级延迟护栏均通过时，候选配置才成为 incumbent。

`track_io_timing` 和 `track_wal_io_timing` 在保存回滚副本后自动开启，属于观测设置，
不属于 60 个被调旋钮；实验结束恢复原始 `postgresql.auto.conf`。

## 新证据及解释规则

- `client.transaction_latency.<事务名>.*`：直接验证哪类事务占用了终端时间。
- `client.bottleneck_summary`：当单类事务占用至少一半终端容量时标记关键事务。
- `wal_timing.sync_average_ms`、`wal_timing.write_average_ms`：区分 WAL 次数与单次落盘慢。
- `wal_waiting_backends`、`io_waiting_backends`、`lock_waiting_backends`：区分等待机制。
- `host_io.some_stall_fraction`、`host_io.full_stall_fraction`：识别主机块设备压力。
- `dirty_bytes`、`writeback_bytes`：识别快照恢复或检查点后的后台写回。

总体 p95 只保留为一个指标，不再被当作低频事务尾延迟的替代。专家提示词明确要求：
WAL 字节数少不能排除慢 fsync；若事务关键路径、WAL 等待和主机 I/O 同时异常，应优先提出
同一 WAL/提交机制内的旋钮组及可证伪预测。

## 对旧实验的离线回放

运行：

```bash
cd /root/Andromeda
.venv/bin/python scripts/replay_transaction_evidence.py \
  artifacts/workload_switch/bo-switch-formal/episodes/repeat_01/tpcc_to_twitter
```

回放的 9 次测量都识别出 `InsertTweet` 是关键事务。前三次基线的终端容量占比为
0.598–0.647；吞吐量崩落后的测量为 0.919–0.955。崩落后的 `InsertTweet` p50 为
323–566 ms，而总体 p95 仅为 427–535 us。这证明新增指标能暴露旧方法遗漏的吞吐量限制路径。

回放文件：

- `transaction_evidence_replay.csv`
- `transaction_evidence_replay.json`

旧实验没有开启 WAL I/O timing，因此无法从旧产物补出 fsync 时延。需要用新协议重跑，
才能区分提交同步、主机 I/O 和其他 InsertTweet 路径问题，并据此验证 WAL/提交旋钮组。
