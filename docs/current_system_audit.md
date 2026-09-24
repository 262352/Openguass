# Current system audit

## Scope decision

The supplied implementation prompt says MySQL, while the latest explicit project decision and the only installed, exercised DBMS are PostgreSQL 14 plus BenchBase. This implementation preserves the requested experimental semantics on PostgreSQL and does not install MySQL. PostgreSQL `user`-context settings are injected into each benchmark JDBC connection, so a trial needs no restart and leaves no global configuration behind.

## What existed

The public Andromeda repository contains document training/retrieval, prompt construction, and a legacy chat call. Its paper evaluates natural-language knob identification on MySQL and PostgreSQL datasets plus a small MySQL runnable setting. It does not provide an automated TPC-C/Twitter/YCSB LHS+GP optimizer or a workload-switch state machine. `main.ipynb` and result prose are retained as historical provenance.

The local workspace already had a PostgreSQL BenchBase adapter for TPC-C, Twitter, and YCSB, pinned at commit `33c00473807ebd49304d114a6d769d2d2b2bbb34`. All three workloads had been loaded and smoke-tested. An earlier unexecuted MySQL scaffold remains for provenance but is outside this PostgreSQL experiment path.

## Added experiment path

- A uniform adapter exposes `prepare`, `restore_snapshot`, `warmup`, `run`, `stop`, `parse_result`, `fingerprint`, and `health_check`.
- The formal reference phase fixes the budget at 15 valid maximin Latin-hypercube samples plus 35 valid GP/EI samples per workload for TPC-C and Twitter. Invalid trials are logged and do not consume that valid budget. YCSB has the same adapter and run command but is deliberately excluded from this formal 50-trial phase.
- The GP is implemented locally with a Matérn 5/2 ARD kernel, observation noise, Cholesky solve, and expected improvement. This avoids an undeclared NumPy/SciPy dependency.
- The predeclared objective is throughput ratio minus a 0.25 penalty for p95 latency regression relative to the untuned baseline.
- Trial JSONL, checkpoints, summaries, selected references, repeat distributions, raw BenchBase paths, model prompts/responses, telemetry, and transition reports are persisted under `artifacts/`.
- All six directed workload transitions are registered. Only `tpcc->twitter` and `twitter->tpcc` are active in phase 1.
- The target reference is not read until question composition, diagnosis, one-knob correction, and verification have completed. A fail-closed leakage guard rejects hidden-reference, target-optimum, label, and historical-trial tokens in model-visible payloads.

## Telemetry and paper boundary

The transition sampler records PostgreSQL database counters once per second: commits, rollbacks, blocks read/hit, temporary bytes, and deadlocks. It derives rates and buffer-hit ratio. For short windows it uses a centered robust trend idea with MAD residual screening, documented as a short-window approximation to the paper's STL plus generalized ESD style. It is not claimed to reproduce the paper's Prometheus 557-metric collection exactly.

## Isolation and rollback

Session settings disappear when BenchBase connections close; the state machine also calls `stop` on success and error. Formal mode creates a PostgreSQL custom-format snapshot with `pg_dump` and restores it with `pg_restore --clean` before every baseline, trial, and reference repeat. Quick mode deliberately reuses the preloaded scale-factor-1 databases to keep engineering validation short, so its transaction workloads mutate rows and its numbers are not publication results.

## Model migration

Executable model calls use the OpenAI-compatible client with `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL=https://api.deepseek.com`, and `DEEPSEEK_MODEL=deepseek-flash` from central settings. No key is written to source or artifacts. `artifacts/model_migration_report.json` inventories executable and historical references. Historical GPT names in papers/notebooks are preserved because changing them would falsify provenance.
