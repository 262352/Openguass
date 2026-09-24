# PostgreSQL 14 tuning-knob catalog

All 60 requested knobs exist on the test server.

| Knob | Context | Execution | Type | Current | Unit |
|---|---|---|---|---:|---|
| [autovacuum_analyze_scale_factor](https://www.postgresql.org/docs/14/runtime-config-autovacuum.html) | sighup | reload | real | 0.1 |  |
| [autovacuum_freeze_max_age](https://www.postgresql.org/docs/14/runtime-config-autovacuum.html) | postmaster | restart | integer | 200000000 |  |
| [autovacuum_max_workers](https://www.postgresql.org/docs/14/runtime-config-autovacuum.html) | postmaster | restart | integer | 3 |  |
| [autovacuum_naptime](https://www.postgresql.org/docs/14/runtime-config-autovacuum.html) | sighup | reload | integer | 60 | s |
| [autovacuum_vacuum_cost_delay](https://www.postgresql.org/docs/14/runtime-config-autovacuum.html) | sighup | reload | real | 2 | ms |
| [autovacuum_vacuum_cost_limit](https://www.postgresql.org/docs/14/runtime-config-autovacuum.html) | sighup | reload | integer | -1 |  |
| [autovacuum_vacuum_scale_factor](https://www.postgresql.org/docs/14/runtime-config-autovacuum.html) | sighup | reload | real | 0.2 |  |
| [backend_flush_after](https://www.postgresql.org/docs/14/runtime-config-resource.html) | user | session | integer | 0 | 8kB |
| [bgwriter_delay](https://www.postgresql.org/docs/14/runtime-config-resource.html) | sighup | reload | integer | 200 | ms |
| [bgwriter_flush_after](https://www.postgresql.org/docs/14/runtime-config-resource.html) | sighup | reload | integer | 64 | 8kB |
| [bgwriter_lru_maxpages](https://www.postgresql.org/docs/14/runtime-config-resource.html) | sighup | reload | integer | 100 |  |
| [bgwriter_lru_multiplier](https://www.postgresql.org/docs/14/runtime-config-resource.html) | sighup | reload | real | 2 |  |
| [checkpoint_completion_target](https://www.postgresql.org/docs/14/runtime-config-wal.html) | sighup | reload | real | 0.9 |  |
| [checkpoint_flush_after](https://www.postgresql.org/docs/14/runtime-config-wal.html) | sighup | reload | integer | 32 | 8kB |
| [checkpoint_timeout](https://www.postgresql.org/docs/14/runtime-config-wal.html) | sighup | reload | integer | 300 | s |
| [checkpoint_warning](https://www.postgresql.org/docs/14/runtime-config-wal.html) | sighup | reload | integer | 30 | s |
| [commit_delay](https://www.postgresql.org/docs/14/runtime-config.html) | superuser | superuser | integer | 0 |  |
| [commit_siblings](https://www.postgresql.org/docs/14/runtime-config.html) | user | session | integer | 5 |  |
| [deadlock_timeout](https://www.postgresql.org/docs/14/runtime-config-locks.html) | superuser | superuser | integer | 1000 | ms |
| [default_statistics_target](https://www.postgresql.org/docs/14/runtime-config-query.html) | user | session | integer | 100 |  |
| [effective_cache_size](https://www.postgresql.org/docs/14/runtime-config-resource.html) | user | session | integer | 524288 | 8kB |
| [effective_io_concurrency](https://www.postgresql.org/docs/14/runtime-config.html) | user | session | integer | 1 |  |
| [from_collapse_limit](https://www.postgresql.org/docs/14/runtime-config-query.html) | user | session | integer | 8 |  |
| [hot_standby_feedback](https://www.postgresql.org/docs/14/runtime-config-replication.html) | sighup | reload | bool | off |  |
| [huge_pages](https://www.postgresql.org/docs/14/runtime-config-resource.html) | postmaster | restart | enum | try |  |
| [jit](https://www.postgresql.org/docs/14/runtime-config.html) | user | session | bool | on |  |
| [join_collapse_limit](https://www.postgresql.org/docs/14/runtime-config-query.html) | user | session | integer | 8 |  |
| [log_checkpoints](https://www.postgresql.org/docs/14/runtime-config-wal.html) | sighup | reload | bool | off |  |
| [log_connections](https://www.postgresql.org/docs/14/runtime-config-logging.html) | superuser-backend | new_backend_superuser | bool | off |  |
| [log_disconnections](https://www.postgresql.org/docs/14/runtime-config-logging.html) | superuser-backend | new_backend_superuser | bool | off |  |
| [log_min_duration_statement](https://www.postgresql.org/docs/14/runtime-config-logging.html) | superuser | superuser | integer | -1 | ms |
| [log_rotation_size](https://www.postgresql.org/docs/14/runtime-config-logging.html) | sighup | reload | integer | 10240 | kB |
| [log_temp_files](https://www.postgresql.org/docs/14/runtime-config-logging.html) | superuser | superuser | integer | -1 | kB |
| [logging_collector](https://www.postgresql.org/docs/14/runtime-config-logging.html) | postmaster | restart | bool | off |  |
| [maintenance_work_mem](https://www.postgresql.org/docs/14/runtime-config-resource.html) | user | session | integer | 65536 | kB |
| [max_connections](https://www.postgresql.org/docs/14/runtime-config.html) | postmaster | restart | integer | 100 |  |
| [max_locks_per_transaction](https://www.postgresql.org/docs/14/runtime-config-locks.html) | postmaster | restart | integer | 64 |  |
| [max_parallel_maintenance_workers](https://www.postgresql.org/docs/14/runtime-config-resource.html) | user | session | integer | 2 |  |
| [max_parallel_workers](https://www.postgresql.org/docs/14/runtime-config-resource.html) | user | session | integer | 8 |  |
| [max_parallel_workers_per_gather](https://www.postgresql.org/docs/14/runtime-config-resource.html) | user | session | integer | 2 |  |
| [max_pred_locks_per_transaction](https://www.postgresql.org/docs/14/runtime-config-locks.html) | postmaster | restart | integer | 64 |  |
| [max_replication_slots](https://www.postgresql.org/docs/14/runtime-config-replication.html) | postmaster | restart | integer | 10 |  |
| [max_standby_streaming_delay](https://www.postgresql.org/docs/14/runtime-config-replication.html) | sighup | reload | integer | 30000 | ms |
| [max_wal_senders](https://www.postgresql.org/docs/14/runtime-config-wal.html) | postmaster | restart | integer | 10 |  |
| [max_wal_size](https://www.postgresql.org/docs/14/runtime-config-wal.html) | sighup | reload | integer | 1024 | MB |
| [max_worker_processes](https://www.postgresql.org/docs/14/runtime-config-resource.html) | postmaster | restart | integer | 8 |  |
| [min_wal_size](https://www.postgresql.org/docs/14/runtime-config-wal.html) | sighup | reload | integer | 80 | MB |
| [random_page_cost](https://www.postgresql.org/docs/14/runtime-config-query.html) | user | session | real | 4 |  |
| [seq_page_cost](https://www.postgresql.org/docs/14/runtime-config-query.html) | user | session | real | 1 |  |
| [shared_buffers](https://www.postgresql.org/docs/14/runtime-config-resource.html) | postmaster | restart | integer | 16384 | 8kB |
| [temp_buffers](https://www.postgresql.org/docs/14/runtime-config-resource.html) | user | session | integer | 1024 | 8kB |
| [track_activity_query_size](https://www.postgresql.org/docs/14/runtime-config.html) | postmaster | restart | integer | 1024 | B |
| [vacuum_cost_delay](https://www.postgresql.org/docs/14/runtime-config-resource.html) | user | session | real | 0 | ms |
| [vacuum_cost_limit](https://www.postgresql.org/docs/14/runtime-config-resource.html) | user | session | integer | 200 |  |
| [wal_buffers](https://www.postgresql.org/docs/14/runtime-config-wal.html) | postmaster | restart | integer | 512 | 8kB |
| [wal_compression](https://www.postgresql.org/docs/14/runtime-config-wal.html) | superuser | superuser | bool | off |  |
| [wal_sync_method](https://www.postgresql.org/docs/14/runtime-config-wal.html) | sighup | reload | enum | fdatasync |  |
| [wal_writer_delay](https://www.postgresql.org/docs/14/runtime-config-wal.html) | sighup | reload | integer | 200 | ms |
| [wal_writer_flush_after](https://www.postgresql.org/docs/14/runtime-config-wal.html) | sighup | reload | integer | 128 | 8kB |
| [work_mem](https://www.postgresql.org/docs/14/runtime-config-resource.html) | user | session | integer | 4096 | kB |
