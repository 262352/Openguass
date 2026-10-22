# 增量因果图

```mermaid
flowchart LR
  n_r001_concurrency_maintenance_node_intervention["Raise max_pred_locks_per_page/relation"]
  n_r001_concurrency_maintenance_node_mechanism["Fewer coarse SIRead promotions, less serialization"]
  n_r001_concurrency_maintenance_node_objective["Successful transaction TPS"]
  n_r002_query_execution_node_intervention["buffer_cache_capacity bundle"]
  n_r002_query_execution_node_mechanism["Cache retention + cache-aware planner costs"]
  n_r002_query_execution_node_objective["Successful transaction TPS"]
  n_r002_memory_storage_node_intervention["Raise shared_buffers/effective_cache_size, lower random/seq_page_cost, raise effective_io_concurrency"]
  n_r002_memory_storage_node_mechanism["Hot working set retained in cache; planner prefers cached index paths"]
  n_r002_memory_storage_node_objective["Successful transaction TPS"]
  n_r002_concurrency_maintenance_node_intervention["Tighten max_pred_locks_per_page/relation"]
  n_r002_concurrency_maintenance_node_mechanism["Coarser SIRead locking reduces per-txn lock churn"]
  n_r002_concurrency_maintenance_node_objective["Successful transaction TPS"]
  n_r003_query_execution_node_intervention["Raise default_statistics_target 100->400"]
  n_r003_query_execution_node_mechanism["Finer selectivity estimates for skewed user_id reduce GetUserTweets pages per call"]
  n_r003_query_execution_node_objective["Successful transaction TPS"]
  n_r003_memory_storage_node_intervention["WAL commit-path bundle: wal_buffers 2048, wal_writer_delay 20, wal_writer_flush_after 0, commit_delay 100, commit_siblings 2, wal_compression on"]
  n_r003_memory_storage_node_mechanism["Pre-flushed WAL + group commit + compressed FPI reduce per-commit fsync frequency and queueing"]
  n_r003_memory_storage_node_objective["Successful transaction TPS"]
  n_r003_concurrency_maintenance_node_intervention["WAL commit-path bundle (buffers, writer cadence, flush batch, group commit, compression)"]
  n_r003_concurrency_maintenance_node_mechanism["Batched WAL syncs reduce commit-path queueing"]
  n_r003_concurrency_maintenance_node_objective["Successful transaction TPS"]
  n_r004_query_execution_node_intervention["Raise default_statistics_target 100->400"]
  n_r004_query_execution_node_mechanism["Finer user_id selectivity estimates reduce GetUserTweets pages per call"]
  n_r004_query_execution_node_objective["Successful transaction TPS"]
  n_r004_memory_storage_node_intervention["Reverse WAL group-commit window: commit_delay 0, commit_siblings 5, wal_writer_flush_after 64"]
  n_r004_memory_storage_node_mechanism["Remove fixed per-commit stall; restore writer flush batching while keeping WAL buffers/compression"]
  n_r004_memory_storage_node_objective["Successful transaction TPS"]
  n_r004_concurrency_maintenance_node_intervention["Raise max_pred_locks_per_page 2->8 and max_pred_locks_per_relation -2->4"]
  n_r004_concurrency_maintenance_node_mechanism["Coarser SIRead locking reduces per-txn lock bookkeeping"]
  n_r004_concurrency_maintenance_node_objective["Successful transaction TPS"]
  n_r005_query_execution_node_intervention["Raise default_statistics_target 100->300"]
  n_r005_query_execution_node_mechanism["Finer user_id selectivity estimates reduce GetUserTweets pages per call"]
  n_r005_query_execution_node_objective["Successful transaction TPS"]
  n_r005_memory_storage_node_intervention["WAL commit-path bundle: commit_delay 500, commit_siblings 3, wal_writer_flush_after 256"]
  n_r005_memory_storage_node_mechanism["Wider group-commit window plus larger writer flush batch coalesce fsyncs"]
  n_r005_memory_storage_node_objective["Successful transaction TPS"]
  n_r005_concurrency_maintenance_node_intervention["Raise max_pred_locks_per_page 2->8 and max_pred_locks_per_relation -2->4"]
  n_r005_concurrency_maintenance_node_mechanism["Coarser SIRead locking reduces per-txn lock bookkeeping"]
  n_r005_concurrency_maintenance_node_objective["Successful transaction TPS"]
  n_r006_query_execution_node_intervention["Raise default_statistics_target 100->300"]
  n_r006_query_execution_node_mechanism["Finer user_id selectivity estimates tighten GetUserTweets index path"]
  n_r006_query_execution_node_objective["Successful transaction TPS"]
  n_r006_memory_storage_node_intervention["WAL commit-path bundle: commit_delay 500, commit_siblings 3, wal_writer_flush_after 256"]
  n_r006_memory_storage_node_mechanism["Wider group-commit window plus larger writer flush batch coalesce fsyncs"]
  n_r006_memory_storage_node_objective["Successful transaction TPS"]
  n_r006_concurrency_maintenance_node_intervention["Raise max_pred_locks_per_page 2->32 and max_pred_locks_per_relation -2->16"]
  n_r006_concurrency_maintenance_node_mechanism["Delayed SIRead promotion reduces per-txn predicate-lock bookkeeping"]
  n_r006_concurrency_maintenance_node_objective["Successful transaction TPS"]
  n_r007_query_execution_node_intervention["Lower random_page_cost 1.5->1.1 and seq_page_cost 0.8->0.7"]
  n_r007_query_execution_node_mechanism["Cache-consistent cost model keeps GetUserTweets on stable Index Scan path"]
  n_r007_query_execution_node_objective["Successful transaction TPS"]
  n_r007_memory_storage_node_intervention["WAL commit-path bundle: commit_delay 500, commit_siblings 3, wal_writer_flush_after 256"]
  n_r007_memory_storage_node_mechanism["Wider group-commit window plus larger writer flush batch coalesce fsyncs"]
  n_r007_memory_storage_node_objective["Successful transaction TPS"]
  n_r007_concurrency_maintenance_node_intervention["Tighten max_pred_locks_per_page 2->0 and max_pred_locks_per_relation -2->0"]
  n_r007_concurrency_maintenance_node_mechanism["Coarsest SIRead granularity minimizes per-txn predicate-lock bookkeeping"]
  n_r007_concurrency_maintenance_node_objective["Successful transaction TPS"]
  n_r008_query_execution_node_intervention["Raise random_page_cost 1.5->2.5 and seq_page_cost 0.8->1.0"]
  n_r008_query_execution_node_mechanism["Reversed cost model favors cheaper access paths for GetUserTweets"]
  n_r008_query_execution_node_objective["Successful transaction TPS"]
  n_r008_memory_storage_node_intervention["WAL commit-path bundle: commit_delay 500, commit_siblings 3, wal_writer_flush_after 256"]
  n_r008_memory_storage_node_mechanism["Wider group-commit window plus larger writer flush batch coalesce fsyncs"]
  n_r008_memory_storage_node_objective["Successful transaction TPS"]
  n_r008_concurrency_maintenance_node_intervention["Set max_pred_locks_per_page=0 and max_pred_locks_per_relation=0"]
  n_r008_concurrency_maintenance_node_mechanism["Coarsest SIRead granularity minimizes per-txn predicate-lock bookkeeping"]
  n_r008_concurrency_maintenance_node_objective["Successful transaction TPS"]
  n_r009_query_execution_node_intervention["Raise default_statistics_target 300->500"]
  n_r009_query_execution_node_mechanism["Finer user_id selectivity estimates reduce GetUserTweets pages per call"]
  n_r009_query_execution_node_objective["Successful transaction TPS"]
  n_r009_memory_storage_node_intervention["commit_delay 100->0, commit_siblings 2->5, wal_writer_flush_after 0->64"]
  n_r009_memory_storage_node_mechanism["Remove fixed per-commit stall; restore writer flush batching"]
  n_r009_memory_storage_node_objective["Successful transaction TPS"]
  n_r009_concurrency_maintenance_node_intervention["Set max_pred_locks_per_page=0 and max_pred_locks_per_relation=0"]
  n_r009_concurrency_maintenance_node_mechanism["Coarsest SIRead granularity minimizes per-txn predicate-lock bookkeeping"]
  n_r009_concurrency_maintenance_node_objective["Successful transaction TPS"]
  n_r010_query_execution_node_intervention["Raise default_statistics_target 300->500"]
  n_r010_query_execution_node_mechanism["Finer user_id selectivity estimates tighten GetUserTweets index path"]
  n_r010_query_execution_node_objective["Successful transaction TPS"]
  n_r010_memory_storage_node_intervention["WAL commit-path bundle: commit_delay 500, commit_siblings 3, wal_writer_flush_after 256"]
  n_r010_memory_storage_node_mechanism["Wider group-commit window plus larger writer flush batch coalesce fsyncs"]
  n_r010_memory_storage_node_objective["Successful transaction TPS"]
  n_r010_concurrency_maintenance_node_intervention["Raise max_pred_locks_per_transaction 8192->16384"]
  n_r010_concurrency_maintenance_node_mechanism["Larger per-txn SIRead budget reduces promotion churn"]
  n_r010_concurrency_maintenance_node_objective["Successful transaction TPS"]
  n_r011_query_execution_node_intervention["Raise random_page_cost 1.5->3.0 and seq_page_cost 0.8->1.0"]
  n_r011_query_execution_node_mechanism["Re-priced random vs sequential reads may select cheaper GetUserTweets path"]
  n_r011_query_execution_node_objective["Successful transaction TPS"]
  n_r011_memory_storage_node_intervention["commit_delay 100->0, commit_siblings 2->8, wal_writer_flush_after 0->128"]
  n_r011_memory_storage_node_mechanism["Remove fixed commit sleep; engage writer-side flush batching"]
  n_r011_memory_storage_node_objective["Successful transaction TPS"]
  n_r011_concurrency_maintenance_node_intervention["Restore max_pred_locks_per_page=2, max_pred_locks_per_relation=-2"]
  n_r011_concurrency_maintenance_node_mechanism["Default SIRead granularity reduces promotion churn and lock bookkeeping per GetUserTweets"]
  n_r011_concurrency_maintenance_node_objective["Successful transaction TPS"]
  n_r012_query_execution_node_intervention["Lower random_page_cost 1.5->1.0 and seq_page_cost 0.8->0.6"]
  n_r012_query_execution_node_mechanism["Cache-consistent cost model keeps GetUserTweets on cheapest Index Scan path"]
  n_r012_query_execution_node_objective["Successful transaction TPS"]
  n_r012_memory_storage_node_intervention["commit_delay 100->200, commit_siblings 2->3, wal_writer_flush_after 0->64"]
  n_r012_memory_storage_node_mechanism["Group-commit window gated to real sibling groups plus writer-side flush batching coalesce fsyncs"]
  n_r012_memory_storage_node_objective["Successful transaction TPS"]
  n_r012_concurrency_maintenance_node_intervention["Restore max_pred_locks_per_page=2, max_pred_locks_per_relation=-2"]
  n_r012_concurrency_maintenance_node_mechanism["Default SIRead granularity caps page-level locks and reduces relation-level promotion churn"]
  n_r012_concurrency_maintenance_node_objective["Successful transaction TPS"]
  n_r013_query_execution_node_intervention["Raise default_statistics_target 300->500"]
  n_r013_query_execution_node_mechanism["Finer user_id selectivity estimates tighten GetUserTweets index path"]
  n_r013_query_execution_node_objective["Successful transaction TPS"]
  n_r013_memory_storage_node_intervention["commit_delay 100->0, commit_siblings 2->20, wal_writer_flush_after 0->256"]
  n_r013_memory_storage_node_mechanism["Remove fixed per-commit stall; engage writer-side flush batching"]
  n_r013_memory_storage_node_objective["Successful transaction TPS"]
  n_r013_concurrency_maintenance_node_intervention["Raise max_pred_locks_per_transaction 8192->16384"]
  n_r013_concurrency_maintenance_node_mechanism["Larger per-txn SIRead budget delays promotion, reducing predicate-lock churn"]
  n_r013_concurrency_maintenance_node_objective["Successful transaction TPS"]
  n_r014_query_execution_node_intervention["Lower default_statistics_target 300->150"]
  n_r014_query_execution_node_mechanism["Reduced histogram/MCV build and planner estimate cost on skewed user_id"]
  n_r014_query_execution_node_objective["Successful transaction TPS"]
  n_r014_memory_storage_node_intervention["commit_delay 100->0, commit_siblings 2->20, wal_writer_flush_after 0->256"]
  n_r014_memory_storage_node_mechanism["Remove fixed per-commit stall; engage writer-side flush batching"]
  n_r014_memory_storage_node_objective["Successful transaction TPS"]
  n_r014_concurrency_maintenance_node_intervention["Set max_pred_locks_per_page=1, max_pred_locks_per_relation=1"]
  n_r014_concurrency_maintenance_node_mechanism["Interior SIRead granularity avoids degenerate zero-cap promotion churn"]
  n_r014_concurrency_maintenance_node_objective["Successful transaction TPS"]
  n_r015_query_execution_node_intervention["Lower default_statistics_target 300->150"]
  n_r015_query_execution_node_mechanism["Reduced histogram/MCV resolution lowers per-call estimate cost on dominant GetUserTweets while keeping Index Scan"]
  n_r015_query_execution_node_objective["Successful transaction TPS"]
  n_r015_concurrency_maintenance_node_intervention["Restore max_pred_locks_per_page=2, max_pred_locks_per_relation=-2"]
  n_r015_concurrency_maintenance_node_mechanism["Default SIRead granularity reduces promotion churn and coarse-lock footprint per GetUserTweets"]
  n_r015_concurrency_maintenance_node_objective["Successful transaction TPS"]
  n_r001_concurrency_maintenance_node_intervention -->|"tested_refuted; experiment:001:06a5cbbe"| n_r001_concurrency_maintenance_node_mechanism
  n_r001_concurrency_maintenance_node_mechanism -->|"stale; experiment:001:06a5cbbe"| n_r001_concurrency_maintenance_node_objective
  n_r002_query_execution_node_intervention -->|"tested_refuted; experiment:002:4a794433"| n_r002_query_execution_node_mechanism
  n_r002_query_execution_node_mechanism -->|"stale; experiment:002:4a794433"| n_r002_query_execution_node_objective
  n_r002_memory_storage_node_intervention -->|"parked; unvalidated"| n_r002_memory_storage_node_mechanism
  n_r002_memory_storage_node_mechanism -->|"parked; unvalidated"| n_r002_memory_storage_node_objective
  n_r002_concurrency_maintenance_node_intervention -->|"parked; unvalidated"| n_r002_concurrency_maintenance_node_mechanism
  n_r002_concurrency_maintenance_node_mechanism -->|"parked; unvalidated"| n_r002_concurrency_maintenance_node_objective
  n_r003_query_execution_node_intervention -->|"parked; unvalidated"| n_r003_query_execution_node_mechanism
  n_r003_query_execution_node_mechanism -->|"parked; unvalidated"| n_r003_query_execution_node_objective
  n_r003_memory_storage_node_intervention -->|"tested_refuted; experiment:003:4bdecc37"| n_r003_memory_storage_node_mechanism
  n_r003_memory_storage_node_mechanism -->|"stale; experiment:003:4bdecc37"| n_r003_memory_storage_node_objective
  n_r003_concurrency_maintenance_node_intervention -->|"parked; unvalidated"| n_r003_concurrency_maintenance_node_mechanism
  n_r003_concurrency_maintenance_node_mechanism -->|"parked; unvalidated"| n_r003_concurrency_maintenance_node_objective
  n_r004_query_execution_node_intervention -->|"parked; unvalidated"| n_r004_query_execution_node_mechanism
  n_r004_query_execution_node_mechanism -->|"parked; unvalidated"| n_r004_query_execution_node_objective
  n_r004_memory_storage_node_intervention -->|"stale; experiment:004:0f3190ec"| n_r004_memory_storage_node_mechanism
  n_r004_memory_storage_node_mechanism -->|"tested_refuted; experiment:004:0f3190ec"| n_r004_memory_storage_node_objective
  n_r004_concurrency_maintenance_node_intervention -->|"parked; unvalidated"| n_r004_concurrency_maintenance_node_mechanism
  n_r004_concurrency_maintenance_node_mechanism -->|"parked; unvalidated"| n_r004_concurrency_maintenance_node_objective
  n_r005_query_execution_node_intervention -->|"parked; unvalidated"| n_r005_query_execution_node_mechanism
  n_r005_query_execution_node_mechanism -->|"parked; unvalidated"| n_r005_query_execution_node_objective
  n_r005_memory_storage_node_intervention -->|"parked; unvalidated"| n_r005_memory_storage_node_mechanism
  n_r005_memory_storage_node_mechanism -->|"parked; unvalidated"| n_r005_memory_storage_node_objective
  n_r005_concurrency_maintenance_node_intervention -->|"stale; experiment:005:14e0e3b9"| n_r005_concurrency_maintenance_node_mechanism
  n_r005_concurrency_maintenance_node_mechanism -->|"tested_refuted; experiment:005:14e0e3b9"| n_r005_concurrency_maintenance_node_objective
  n_r006_query_execution_node_intervention -->|"tested_refuted; experiment:006:1d00c9fe"| n_r006_query_execution_node_mechanism
  n_r006_query_execution_node_mechanism -->|"stale; experiment:006:1d00c9fe"| n_r006_query_execution_node_objective
  n_r006_memory_storage_node_intervention -->|"parked; unvalidated"| n_r006_memory_storage_node_mechanism
  n_r006_memory_storage_node_mechanism -->|"parked; unvalidated"| n_r006_memory_storage_node_objective
  n_r006_concurrency_maintenance_node_intervention -->|"parked; unvalidated"| n_r006_concurrency_maintenance_node_mechanism
  n_r006_concurrency_maintenance_node_mechanism -->|"parked; unvalidated"| n_r006_concurrency_maintenance_node_objective
  n_r007_query_execution_node_intervention -->|"stale; experiment:007:64eb2fdf"| n_r007_query_execution_node_mechanism
  n_r007_query_execution_node_mechanism -->|"tested_refuted; experiment:007:64eb2fdf"| n_r007_query_execution_node_objective
  n_r007_memory_storage_node_intervention -->|"parked; unvalidated"| n_r007_memory_storage_node_mechanism
  n_r007_memory_storage_node_mechanism -->|"parked; unvalidated"| n_r007_memory_storage_node_objective
  n_r007_concurrency_maintenance_node_intervention -->|"parked; unvalidated"| n_r007_concurrency_maintenance_node_mechanism
  n_r007_concurrency_maintenance_node_mechanism -->|"parked; unvalidated"| n_r007_concurrency_maintenance_node_objective
  n_r008_query_execution_node_intervention -->|"parked; unvalidated"| n_r008_query_execution_node_mechanism
  n_r008_query_execution_node_mechanism -->|"parked; unvalidated"| n_r008_query_execution_node_objective
  n_r008_memory_storage_node_intervention -->|"stale; experiment:008:c3935d5f"| n_r008_memory_storage_node_mechanism
  n_r008_memory_storage_node_mechanism -->|"tested_refuted; experiment:008:c3935d5f"| n_r008_memory_storage_node_objective
  n_r008_concurrency_maintenance_node_intervention -->|"parked; unvalidated"| n_r008_concurrency_maintenance_node_mechanism
  n_r008_concurrency_maintenance_node_mechanism -->|"parked; unvalidated"| n_r008_concurrency_maintenance_node_objective
  n_r009_query_execution_node_intervention -->|"parked; unvalidated"| n_r009_query_execution_node_mechanism
  n_r009_query_execution_node_mechanism -->|"parked; unvalidated"| n_r009_query_execution_node_objective
  n_r009_memory_storage_node_intervention -->|"parked; unvalidated"| n_r009_memory_storage_node_mechanism
  n_r009_memory_storage_node_mechanism -->|"parked; unvalidated"| n_r009_memory_storage_node_objective
  n_r009_concurrency_maintenance_node_intervention -->|"stale; experiment:009:ecdadd0b"| n_r009_concurrency_maintenance_node_mechanism
  n_r009_concurrency_maintenance_node_mechanism -->|"stale; experiment:009:ecdadd0b"| n_r009_concurrency_maintenance_node_objective
  n_r010_query_execution_node_intervention -->|"parked; unvalidated"| n_r010_query_execution_node_mechanism
  n_r010_query_execution_node_mechanism -->|"parked; unvalidated"| n_r010_query_execution_node_objective
  n_r010_memory_storage_node_intervention -->|"stale; experiment:010:375efa09"| n_r010_memory_storage_node_mechanism
  n_r010_memory_storage_node_mechanism -->|"tested_refuted; experiment:010:375efa09"| n_r010_memory_storage_node_objective
  n_r010_concurrency_maintenance_node_intervention -->|"parked; unvalidated"| n_r010_concurrency_maintenance_node_mechanism
  n_r010_concurrency_maintenance_node_mechanism -->|"parked; unvalidated"| n_r010_concurrency_maintenance_node_objective
  n_r011_query_execution_node_intervention -->|"stale; experiment:011:2b211c30"| n_r011_query_execution_node_mechanism
  n_r011_query_execution_node_mechanism -->|"tested_refuted; experiment:011:2b211c30"| n_r011_query_execution_node_objective
  n_r011_memory_storage_node_intervention -->|"parked; unvalidated"| n_r011_memory_storage_node_mechanism
  n_r011_memory_storage_node_mechanism -->|"parked; unvalidated"| n_r011_memory_storage_node_objective
  n_r011_concurrency_maintenance_node_intervention -->|"parked; unvalidated"| n_r011_concurrency_maintenance_node_mechanism
  n_r011_concurrency_maintenance_node_mechanism -->|"parked; unvalidated"| n_r011_concurrency_maintenance_node_objective
  n_r012_query_execution_node_intervention -->|"parked; unvalidated"| n_r012_query_execution_node_mechanism
  n_r012_query_execution_node_mechanism -->|"parked; unvalidated"| n_r012_query_execution_node_objective
  n_r012_memory_storage_node_intervention -->|"parked; unvalidated"| n_r012_memory_storage_node_mechanism
  n_r012_memory_storage_node_mechanism -->|"parked; unvalidated"| n_r012_memory_storage_node_objective
  n_r012_concurrency_maintenance_node_intervention -->|"tested_refuted; experiment:012:d0840109"| n_r012_concurrency_maintenance_node_mechanism
  n_r012_concurrency_maintenance_node_mechanism -->|"tested_refuted; experiment:012:d0840109"| n_r012_concurrency_maintenance_node_objective
  n_r013_query_execution_node_intervention -->|"tested_refuted; experiment:013:23aba06a"| n_r013_query_execution_node_mechanism
  n_r013_query_execution_node_mechanism -->|"tested_refuted; experiment:013:23aba06a"| n_r013_query_execution_node_objective
  n_r013_memory_storage_node_intervention -->|"parked; unvalidated"| n_r013_memory_storage_node_mechanism
  n_r013_memory_storage_node_mechanism -->|"parked; unvalidated"| n_r013_memory_storage_node_objective
  n_r013_concurrency_maintenance_node_intervention -->|"parked; unvalidated"| n_r013_concurrency_maintenance_node_mechanism
  n_r013_concurrency_maintenance_node_mechanism -->|"parked; unvalidated"| n_r013_concurrency_maintenance_node_objective
  n_r014_query_execution_node_intervention -->|"parked; unvalidated"| n_r014_query_execution_node_mechanism
  n_r014_query_execution_node_mechanism -->|"parked; unvalidated"| n_r014_query_execution_node_objective
  n_r014_memory_storage_node_intervention -->|"stale; experiment:014:dd8c7785"| n_r014_memory_storage_node_mechanism
  n_r014_memory_storage_node_mechanism -->|"stale; experiment:014:dd8c7785"| n_r014_memory_storage_node_objective
  n_r014_concurrency_maintenance_node_intervention -->|"parked; unvalidated"| n_r014_concurrency_maintenance_node_mechanism
  n_r014_concurrency_maintenance_node_mechanism -->|"parked; unvalidated"| n_r014_concurrency_maintenance_node_objective
  n_r015_query_execution_node_intervention -->|"parked; unvalidated"| n_r015_query_execution_node_mechanism
  n_r015_query_execution_node_mechanism -->|"parked; unvalidated"| n_r015_query_execution_node_objective
  n_r015_concurrency_maintenance_node_intervention -->|"tested_refuted; experiment:015:b9432dd2"| n_r015_concurrency_maintenance_node_mechanism
  n_r015_concurrency_maintenance_node_mechanism -->|"tested_refuted; experiment:015:b9432dd2"| n_r015_concurrency_maintenance_node_objective
```

边标签中的实验 ID 可在 `experiment_ledger.jsonl` 中查到冻结预测、动作与原始测量。
