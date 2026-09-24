# Phase 2 redesign: diagnostic tuning speed after workload switching

## Research question

For a directed switch such as TPC-C → Twitter, starting from the stable TPC-C configuration `C_A`, how quickly can diagnostic Andromeda recover Twitter TPS compared with a traditional GP tuner under the same PostgreSQL instance, workload snapshot, knob universe, evaluation duration, terminal count, seed set, and wall-clock budget?

The optimization objective is **target-workload TPS only**. Latency, locks, waits, buffers, WAL, checkpoints, tuple churn, autovacuum, CPU, memory, and disk measurements are diagnostic evidence. They are not additional objective terms.

## Why the completed phase-2 result is not the requested comparison

The completed implementation made one AI change and compared it with a hidden target reference. It did not run an iterative Andromeda search and a GP search from the same source configuration, so it cannot measure relative tuning speed. Its prompt also treated the absolute TPC-C/Twitter throughput difference as a degradation even though their transactions have different costs.

## Fair protocol for each direction

1. Restore one database snapshot and machine configuration.
2. Apply the robust source reference `C_A`; run source steady state.
3. Switch to target workload without changing `C_A`; collect a warm-up-free transition window followed by a steady measurement window.
4. Fork two identical target snapshots and configurations.
5. Run diagnostic Andromeda and traditional GP independently from exactly `C_A`.
6. Give both methods the same 60-knob catalog, constraints, benchmark duration, retry policy, maximum evaluations, and wall-clock limit.
7. After each evaluation, record best-so-far target TPS. Intermediate metrics may guide Andromeda but never enter the objective.
8. Keep the robust target reference distribution `D_B` hidden until both searches finish.
9. Compare best-so-far TPS curves, area under the curve, time/evaluations to reach 90% and 95% of median `D_B`, final normalized regret, invalid trials, reloads, restarts, and total wall time.

Run two tracks: a dynamic track containing session/reload knobs, and a full track containing all 60 knobs with restart time charged to the method. This prevents restart-heavy parameters from silently making one method appear slower.

## Required telemetry

Collect aligned one-second windows and retain raw samples. At minimum:

- objective: target TPS and transaction count;
- concurrency: active backends, waiting backends, wait-event classes, ungranted locks, lock modes, deadlocks, serialization aborts;
- buffer/I/O: hits, reads, read/write time, temporary files/bytes, backend/checkpoint/bgwriter buffers;
- WAL/checkpoints: WAL records/bytes/FPI, writes/syncs, checkpoint frequency/write/sync time;
- access paths: sequential/index scans and tuples fetched/returned;
- row churn: inserts/updates/deletes, live/dead tuples, vacuum/analyze activity;
- host: CPU utilization, run queue, RSS, disk throughput/latency and I/O pressure.

Use per-second rates and per-transaction normalized values where appropriate. Locks and active sessions are gauges and must be summarized by median/p95/max rather than differenced as counters.

## Question-composer contract

The composer receives the transition identity, `C_A`, target TPS objective, workload fingerprints, time-aligned source/transition/target-steady telemetry, normalized deltas, detected change points, hardware limits, and evidence IDs. It must:

- describe every material metric change with direction and magnitude;
- separate expected workload-semantic differences from suspected configuration bottlenecks;
- never call a TPS difference between unlike workloads a regression by itself;
- never claim lock, WAL, memory, or I/O pressure without the corresponding evidence;
- produce bottleneck hypotheses, not knob values;
- omit hidden `C_B`, `D_B`, GP history, labels, and future outcomes.

A suitable core instruction is:

> The sole optimization objective is target-workload TPS. All other measurements are diagnostic evidence. Describe the transition from source steady state to target steady state, distinguish workload-semantic changes from configuration-induced bottlenecks, quantify each supported change, and cite evidence IDs. Do not recommend parameters and do not infer unavailable metrics.

## Retrieval and diagnosis contract

Retrieve detailed entries from the PostgreSQL 14 official knob catalog using the metric narrative and bottleneck hypotheses. The diagnosis model sees all 60 knob names and execution constraints, plus detailed documentation for the retrieved candidates. It may recommend only evidence-supported changes and must return:

- decision: change, no safe change, or insufficient evidence;
- one or more knobs and values within validated bounds;
- metric evidence IDs and official knowledge IDs;
- why each knob can increase TPS for this observed bottleneck;
- expected directional changes in TPS and diagnostic metrics;
- execution class: session, reload, restart, superuser, or new backend;
- rollback values and a stop/verification condition.

`effective_cache_size` is a planner estimate, not allocated cache. `random_page_cost` should not be changed merely because block reads rose; the diagnosis also needs access-path and storage-latency evidence.

## Knob execution

The requested 60 knobs all exist on PostgreSQL 14.24. Their contexts are: 18 `user`, 22 `sighup`, 5 `superuser`, 2 `superuser-backend`, and 13 `postmaster`. The executor must use session options, `ALTER SYSTEM` plus reload, a new backend, or restart as required, verify `pg_settings`, and always restore prior values. Logging, replication, standby, and wraparound-prevention knobs remain in the universe but should be selected only when relevant evidence exists.

## Executable full-track experiment

The implemented full track is `app.phase2.cli`. It captures the live values of all 60
knobs, saves `postgresql.auto.conf`, runs both methods from that same configuration,
charges reload/restart time to the method that caused it, and restores the original
file on completion, failure, SIGINT, or SIGTERM. The default launcher evaluates the
two directed transitions `tpcc:twitter,twitter:tpcc`:

```bash
cd /root/Andromeda
BUDGET=20 DURATION=30 TERMINALS=2 \
  ./scripts/nohup_phase2_full_knob_comparison.sh
```

The launcher prints the experiment ID, PID, log path and live report path. Read the
compact per-direction report while the experiment runs:

```bash
cat artifacts/phase2/<experiment-id>/tpcc_to_twitter/LIVE_PROGRESS.md
tail -f artifacts/phase2/<experiment-id>/nohup.log
```

Include YCSB transitions by overriding the direction list:

```bash
DIRECTIONS=tpcc:twitter,twitter:tpcc,tpcc:ycsb,ycsb:tpcc \
  ./scripts/nohup_phase2_full_knob_comparison.sh
```

Use `kill -TERM <pid>` to stop the process and invoke the database rollback handler.
The detailed output consists of `progress.csv`, per-evaluation JSON and telemetry,
the exact LLM prompts/responses, `comparison_report.json`, and the top-level
`summary.json`.
