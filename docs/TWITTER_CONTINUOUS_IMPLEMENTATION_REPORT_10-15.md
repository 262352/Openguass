# Twitter 持续诊断调优实现与验证报告（10-15）

## 已实现

本版新增独立的 Twitter 持续调优入口，不经过 TPC-C，不读取 BO/GP 配置、历史最优 TPS 或跨负载结果。基线从 `pg_settings.boot_val` 建立，并保存当前值、默认值、来源、上下文、配置文件和 pending_restart。观测项与 scale-100/SERIALIZABLE 所需锁表容量被列为固定例外。

每次有效测量同步产生 BenchBase 总体/分事务指标、PostgreSQL 窗口指标、主机 CPU/内存/磁盘、WAL 计时、`pg_stat_statements` 差分、queryid 等待采样和有上限的非执行 EXPLAIN。查询统计不推造分位数；缺失字段为 null 并附带 missing/disabled/unsupported 原因。

三个领域专家只决定机制和候选参数组。确定性调度器从当前 incumbent 产生实际参数值，记录 active/refining/dormant、步长、方向和已测配置。明确资源问题进入瓶颈修复；有效方向进入附近探索；连续低收益进入跨组或同组组合探索。休眠组定期恢复。专家只观察、指标正常或没有慢查询均不会结束循环。

每个候选采用两次 control 与两次 treatment 的交错次序。性能判定与机制判定分开；性能提高但机制不成立可以更新 incumbent 并标记 `unexplained_performance`。SQL 和索引建议只记录，不执行。

## 自动测试

`python -m pytest -q`：97 项全部通过。新增测试覆盖：

- boot 默认与实际配置、固定例外的对应；
- pg_stat_statements 重置、queryid 消失和缺失字段；
- 无异常时仍产生候选；
- 参数组休眠后重新激活；
- 新入口不依赖 BO reference 或 source config。

原有测试继续覆盖冻结预测、性能/机制分离、unexplained performance、交错验证、失败恢复接口、联合动作不自动声明交互和参数模式边界。

## 真实小预算冒烟

已实际启动 `twitter-continuous-smoke-20261015T122136Z`。流程在首个默认基线 warmup 被阻断，尚未执行候选：

- raw run：`20261015T122145644451Z-twitter`；
- SQLSTATE：`23503`；
- 866 次非预期 SQL 错误，比例 1.1025%；
- `InsertTweet` 写入的 uid 不存在于 `user_profiles`；
- 当前快照只有 20,000 个用户，而 XML scale factor 100 对当前 BenchBase 常量要求 50,000 个用户；
- 最终配置恢复成功，耗时 5.339 秒。

这次冒烟只证明错误策略和 finally 恢复生效，不证明方法有效。入口现已增加两层前置检查：快照 profile 必须匹配当前 XML；数据库用户数必须匹配 BenchBase scale factor。需要先执行 `sudo bash scripts/bootstrap_test_environment.sh --force-reload` 重建快照，再重新运行冒烟。

## raw run 清理

清理前共有 767 个目录。按“run ID 未被任何 `artifacts/runs` 外的实验记录引用”的严格规则，删除 340 个孤立目录（580,429,413 bytes），保留 427 个被引用目录。随后真实冒烟新增并引用了 1 个失败 run，因此当前为 428 个。聚合审计保存在 `artifacts/run_prune_deletion_summary.json`，后续执行会追加 `artifacts/run_prune_history.jsonl`。
