# TPC-C 多智能体诊断调优实验报告（10/16）

## 1. 实验对象与结论

本报告分析实验 `tpcc-only-20261015T125511Z`：先用 100 个候选配置执行贝叶斯优化，再从 PostgreSQL 默认配置启动 15 轮多智能体诊断调优。两种方法均使用同一个 TPC-C scale factor 5 快照、10 terminals、120 秒测量、30 秒预热、固定随机种子，并在每次测量前恢复快照。

结论是：本次多智能体方法没有产生任何被接受的配置变化，最终 incumbent 仍是默认配置。默认配置三次 TPS 为 `154.835 / 185.438 / 161.587`，中位数为 `161.587`；BO 第 76 个候选被选中，复测 TPS 为 `281.290 / 298.653 / 325.224`，中位数为 `298.653`。多智能体最终中位 TPS 比 BO 低 `45.895%`，或者说 BO 比多智能体最终值高约 `84.83%`。

诊断层确实找到了 WAL 刷盘、后台写回、组提交和计划成本等有依据的方向，但协调器没有忠实执行最强专家建议，搜索步长也明显偏小。尤其是查询执行专家连续 12 次建议启用 `wal_compression`，一次都没有被实际测试。内存存储专家持续建议成组调整 `backend_flush_after`、`bgwriter_delay`、`bgwriter_lru_maxpages` 和 `bgwriter_lru_multiplier`，实际执行器却把它们拆成零散、幅度较小、部分方向相反的动作。因此本次结果更准确地说明“诊断到干预的转换失败”，不能简单归结为诊断证据完全无效。

本次实验没有修改索引，没有执行任何 DDL，也没有改写 SQL。系统只修改 PostgreSQL 参数。

## 2. 实验协议和评价口径

| 项目 | BO | 多智能体诊断 |
|---|---:|---:|
| 起点 | PostgreSQL 默认配置 | PostgreSQL 默认配置 |
| 候选配置数 | 100（10 LHS + 90 GP/EI） | 15 |
| 正式工作负载测量 | 约 100 次搜索测量，另有基线和 3 次复测 | 63 次：3 次基线 + 15 × 4 次配对实验 |
| 每个诊断候选 | 单次搜索评价 | 两次 control + 两次 treatment |
| 最终值 | 最佳候选复测 3 次取中位数 | 被接受 incumbent 的 3 次基线中位数；本次无候选被接受 |
| TPS 目标 | BenchBase effective goodput | BenchBase effective goodput |

两个方法的单次测量条件一致，但搜索预算并不等价：BO 评价了 100 个不同配置；诊断方法只评价了 15 个不同配置，把较多预算用于同一候选的配对复测。这个差异是 BO 更容易发现高维联合配置的重要原因之一。

结果文件 `REPORT.md` 的标题字段显示 `154.835 TPS`，而对比文件使用 `161.587 TPS`。两者来自同一默认配置：前者是第一次基线测量，后者是三次基线的中位数。正式方法对比应使用 `161.587 TPS`，不应把这两个数理解为调优前后性能。

## 3. 三个智能体分别做了什么

15 轮中，第 2、5 轮没有得到可执行的专家提案，由确定性搜索器直接生成候选。其余 13 轮，每个专家各产生 13 条结构化提案，共 39 条。

### 3.1 查询执行专家（query_execution）

该专家负责计划、事务延迟、查询等待和执行路径。它观察到：

- WAL 平均同步延迟约 `26.67 ms`；
- WAL 同步耗时约 `1042.9 ms/s`，持久化通道接近饱和；
- `LWLock:WALWrite` 是最突出的采样等待之一；
- 每次提交约产生 `19.1 KB` WAL，其中约 `1.73` 个 full-page image；
- `Payment`、`NewOrder` 和 `Delivery` 占用了绝大部分 terminal capacity；
- 唯一可用的计划样例 `SELECT * FROM warehouse WHERE w_id = 1` 使用 Bitmap Index Scan + Bitmap Heap Scan。

它在 12 轮中提出 `wal_compression: off → on`，认为压缩 full-page image 可以缩短 WAL 持久化路径；第 14 轮改为建议 `random_page_cost: 4.0 → 1.1` 和 `effective_cache_size: 524288 → 1048576`，希望单行主键探测由 bitmap 路径转向普通 index scan。

限制是 `pg_stat_statements` 不可用，报告中的 `query_evidence.status` 为 `missing`。因此它没有完整的 SQL 排名、每条 SQL 的资源消耗和计划变化，只能依赖事务模板统计、等待采样和一个固定的 warehouse 查询计划。它虽然名为查询执行专家，本次大部分建议仍集中在 WAL 参数，而没有形成真正的查询级诊断。

### 3.2 内存存储专家（memory_storage）

该专家负责缓冲池、脏页、后台写回、检查点和存储队列。它持续观察到：

- buffer hit ratio 约 `95.38%`；
- backend 同步写回约 `1254 buffers/s`；
- dirty bytes 中位数约 `132 MB`、最大约 `393 MB`；
- host I/O full stall fraction 约 `15.8%`；
- WAL fsync 与后台页面写回共享设备路径。

13 条提案基本一致，核心组合为：

- `backend_flush_after: 0 → 64`；
- `bgwriter_delay: 200 → 50 ms`；
- `bgwriter_lru_maxpages: 100 → 400`；
- `bgwriter_lru_multiplier: 2 → 3/4`；
- 部分轮次再加入 `shared_buffers: 8192 → 32768`。

这是一条相对连贯的机制链：让后台写进程提前、持续地接管脏页写回，减少 backend 自己写脏页，从而降低 WAL fsync 前面的设备队列。但实际执行器从未完整测试这个组合，也没有测试专家提出的 `shared_buffers=32768`。

### 3.3 并发维护专家（concurrency_maintenance）

该专家负责提交并发、锁等待、序列化冲突和维护活动。它观察到：

- `wal_waiting_backends` 的 p95 为 10，接近全部 10 个 benchmark terminals；
- 存在 `transactionid` 锁等待；
- rollback ratio 约 `90.4%`，SERIALIZABLE 模式下存在大量重试/回滚；
- 提交者在 WALWrite/WALSync 上排队时，事务持有的行锁和 transactionid 依赖也随之延长。

13 条提案都围绕显式组提交窗口：

- `commit_delay: 0 → 200/500/1000 μs`；
- `commit_siblings: 5 → 2/3/4`。

实际执行器只在第 6 轮测试 `commit_delay=50, commit_siblings=4`，第 10 轮测试 `commit_delay=25`，第 11 轮只测试 `commit_siblings=4`。这些幅度远小于后期专家稳定建议的 `commit_delay=1000`，也小于 BO 最优配置的 `commit_delay=743`。

### 3.4 协调器和确定性搜索器

协调器根据异常标签选择参数组，再由确定性局部搜索器生成最终动作。记录中的最终候选归属为 memory_storage 12 轮、query_execution 2 轮、concurrency_maintenance 1 轮，但这些候选多数并不是对应专家原始提案，而是搜索器在同一参数组中生成的替代动作。

这导致了明显的“专家提出 A，执行器测试 B”：例如专家要求 `wal_compression=on`，执行器却测试 `wal_buffers` 或 `wal_writer_delay`；专家要求提高 `bgwriter_lru_maxpages`，第 9 轮实际把它从 100 降到 75；第 14 轮专家要求降低 `random_page_cost`，执行器却从 4.0 提高到 5.0。

## 4. 每轮实际修改的旋钮和结果

所有候选都从同一个默认 incumbent 出发，因为前面没有任何候选被接受。TPS 列保留两次 control 和两次 treatment 的原始值。

| 轮次 | 参数组 | 实际修改 | Control TPS | Treatment TPS | 有效性 | 机制结论 | 性能结论 |
|---:|---|---|---|---|---|---|---|
| 1 | WAL/提交 | `wal_buffers -1→409` | 114.198 / 148.496 | 139.479 / 200.380 | 不稳定 | 不确定 | 不确定 |
| 2 | 写回/检查点 | `checkpoint_completion_target 0.9→0.95` | 143.438 / 131.405 | 156.017 / 120.909 | 不稳定 | 不确定 | 不确定 |
| 3 | WAL/提交 | `wal_writer_delay 200→145`; `wal_writer_flush_after 128→230` | 150.075 / 194.488 | 130.240 / 163.050 | 不稳定 | 不确定 | 不确定 |
| 4 | 写回/检查点 | `checkpoint_timeout 300→229`; `max_wal_size 1024→1261` | 187.190 / 163.785 | 111.000 / 149.884 | 不稳定 | 不确定 | 不确定 |
| 5 | 维护 | `autovacuum_analyze_scale_factor 0.1→0.156`; `autovacuum_vacuum_scale_factor 0.2→0.124` | 102.587 / 183.876 | 179.190 / 184.066 | 不稳定 | 不确定 | 不确定 |
| 6 | WAL/提交 | `commit_delay 0→50`; `commit_siblings 5→4` | 170.637 / 148.223 | 159.422 / 164.653 | 有效 | 支持 | 无改善 |
| 7 | WAL/提交 | `wal_buffers -1→50`; `wal_writer_delay 200→217` | 167.256 / 114.984 | 155.744 / 160.959 | 不稳定 | 不确定 | 不确定 |
| 8 | 写回/检查点 | `min_wal_size 80→88`; `bgwriter_delay 200→164` | 161.984 / 155.513 | 167.389 / 162.265 | 有效 | 反驳 | 无改善 |
| 9 | 写回/检查点 | `bgwriter_lru_maxpages 100→75`; `bgwriter_lru_multiplier 2→2.1375` | 154.653 / 170.595 | 163.463 / 150.380 | 有效 | 支持 | 无改善 |
| 10 | WAL/提交 | `wal_writer_flush_after 128→154`; `commit_delay 0→25` | 182.521 / 162.471 | 111.058 / 158.678 | 不稳定 | 不确定 | 不确定 |
| 11 | WAL/提交 | `commit_siblings 5→4` | 151.281 / 131.182 | 152.430 / 164.851 | 有效 | 支持 | 无改善 |
| 12 | 写回/检查点 | `backend_flush_after 0→6`; `bgwriter_flush_after 64→58` | 147.570 / 134.355 | 100.934 / 101.868 | 有效 | 反驳 | 无改善 |
| 13 | 写回/检查点 | `checkpoint_flush_after 32→26`; `checkpoint_warning 30→33` | 98.711 / 105.438 | 165.975 / 109.579 | 不稳定 | 不确定 | 不确定 |
| 14 | 查询计划 | `random_page_cost 4→5`; `seq_page_cost 1→0.7` | 117.603 / 110.306 | 144.240 / 155.372 | 有效 | 支持 | 无改善 |
| 15 | 查询计划 | `effective_cache_size 524288→425854`; `effective_io_concurrency 1→27` | 155.843 / 148.537 | 139.504 / 118.686 | 有效 | 反驳 | 无改善 |

统计结果：

- 8/15 轮被判为不稳定，占全部候选的 `53.3%`；
- 7/15 轮有效，但全部判定为 `no_improvement`；
- 机制证据为支持 4 轮、反驳 3 轮、不确定 8 轮；
- 15 轮接受数为 0；
- 正式负载测量 63 次、PostgreSQL 重启 8 次、模型调用 45 次；
- 最终恢复成功。

“机制支持”不等于“配置值得接受”。例如第 11、14 轮的中间指标方向得到支持，但保守 treatment TPS 仍没有超过 incumbent，所以没有更新配置。现有 `REPORT.md` 把第 11、14 轮称为两条“有效路径”，容易让人误以为它们已带来有效性能提升；它们实际只是因果图边得到支持，两个候选都未被接受。

## 5. 与 BO 最优配置的对应关系

BO 第 76 个配置单次搜索 TPS 为 `290.852`，三次复测中位数为 `298.653`。该配置相对默认值修改了 60 个旋钮中的 54 个。高维配置中的单个差异不能被解释为独立因果效应，但它能用于检查诊断方法是否至少覆盖了相同方向和尺度。

诊断方法实际触碰了 23 个不同旋钮，其中 21 个也被 BO 修改。按相对默认值的方向比较，27 个实际旋钮动作中：

- 16 个与 BO 同方向；
- 9 个与 BO 方向相反；
- 2 个修改了 BO 保持默认的 `commit_siblings`。

最有解释力的对应关系如下：

| 机制 | 专家建议 | 诊断实际测试 | BO 最优配置 | 判断 |
|---|---|---|---|---|
| WAL 压缩 | `wal_compression on`，出现 12 次 | 从未测试 | `on` | 最强建议在执行层丢失 |
| 缓冲池 | `shared_buffers 8192→32768` | 从未测试 | `57646` | 方向一致，但诊断未执行 |
| backend 写回 | `backend_flush_after 0→64` | 只到 `6` | `56` | 步长约小一个数量级 |
| bgwriter 周期 | `bgwriter_delay 200→50` | 只到 `164` | `16` | 方向一致但幅度严重不足 |
| bgwriter 页数 | `100→400` | 实际 `100→75` | `140` | 实际方向相反 |
| bgwriter 倍率 | `2→3/4` | 只到 `2.1375` | `4.033774` | 方向一致但幅度不足 |
| 组提交 | `commit_delay 0→500/1000` | 只到 `25/50` | `743` | 幅度严重不足 |
| WAL buffer | 未作为专家首选 | `-1→50/409` | `770` | 方向一致但未到 BO 区域 |
| WAL writer flush | 未作为专家首选 | `128→154/230` | `1012` | 幅度严重不足 |
| 随机页成本 | `4→1.1` | `4→5` | `3.620721` | 实际方向相反 |
| 有效缓存 | `524288→1048576` | `524288→425854` | `601209` | 实际方向相反 |
| I/O 并发 | 未作为核心首选 | `1→27` | `183` | 方向一致但幅度不足 |

BO 还显著修改了诊断方法没有测试的 `jit`、`temp_buffers`、`work_mem`、`default_statistics_target`、并行 worker、vacuum cost、锁表容量等 33 个旋钮。不能据此认定这 33 个参数都有效，因为它们只作为一个联合配置通过复测；但它说明 15 个局部候选没有进入 BO 找到的高性能区域。

## 6. 为什么最终赶不上贝叶斯优化

### 6.1 专家建议没有被忠实转成实验

这是本次最直接的问题。三个专家的建议已经高度收敛到 WAL 压缩、后台写回和组提交，但协调器用固定参数组搜索替换了具体动作。结果是最有证据的 `wal_compression` 从未测试，写回组合被拆散，计划参数甚至反向调整。当前系统优化的是“在参数组内按预设网格走一步”，而不是“验证专家刚刚提出的机制动作”。

### 6.2 只从默认配置做小范围局部探测，无法形成联合配置

由于 15 轮全部未接受，每轮都重新从默认配置出发。即使若干小动作方向正确，也从未累积为 `wal_compression + shared_buffers + bgwriter + commit_delay` 的联合状态。数据库旋钮存在强交互：例如提高 bgwriter 能力而不扩大缓冲池，或者只改变 commit_siblings 而不打开足够的 commit_delay，可能不会产生可测 TPS 增益。BO 可以直接评价 60 维联合配置，因此更容易跨越这种局部平台。

### 6.3 搜索范围过小

专家和 BO 的尺度实际上相当接近：`commit_delay` 建议 500–1000、BO 为 743；`backend_flush_after` 建议 64、BO 为 56；`bgwriter_lru_multiplier` 建议 3–4、BO 为 4.03。但执行器只测试 25/50、6 和 2.1375。实验没有真正检验专家假设所指向的参数区域。

### 6.4 大量预算消耗在不稳定实验上

基线三次 TPS 的 spread 已达到 `18.94%`，仍被当前 25% 阈值标记为 stable。15 个候选中又有 8 个因 control/treatment 不稳定而无法下结论，消耗了 32 次正式测量。第 5、13 轮甚至出现一次 treatment 很高、另一次接近低位的情况，无法被可靠接受。配对验证避免了误收异常结果，但也使有限的 15 轮候选预算只剩 7 轮可判定。

### 6.5 查询证据不完整，三个专家逐渐收敛成同一种 WAL 诊断

`pg_stat_statements` 不可用，查询专家看不到最昂贵 SQL、调用次数、共享块读写、临时文件和单 SQL 计划。现有计划证据只覆盖一个 warehouse 主键查询，不能代表 NewOrder、Payment、Delivery 的完整语句链。因此三个专家虽名称不同，后期实际上都围绕同一 WAL/提交假设反复提案，竞争假设的多样性不足。

### 6.6 SERIALIZABLE 回滚风暴可能是更上游的限制

基线 `xact_rollback≈1514/s`、`xact_commit≈161/s`，rollback ratio 约 `90.4%`。这意味着大量 CPU、锁和 WAL 工作可能来自最终回滚并重试的事务。专家提案已经把它列为替代解释，但当前动作空间只允许 GUC 参数，不能改变 BenchBase 事务重试行为或隔离级别。如果主要限制来自 SERIALIZABLE 冲突，仅靠 WAL 和 bgwriter 参数很难达到 BO 配置的表现，且瞬时 WAL 指标可能只是回滚风暴的下游结果。

### 6.7 BO 预算更大，但 BO 配置不等于因果解释

BO 测试了 100 个联合配置，诊断方法只测试 15 个候选。BO 的优势部分来自更大的候选覆盖。另一方面，BO 最优配置同时变化 54 个旋钮，不能说明其中每个旋钮都有贡献。本报告只把 BO 当作经三次复测的性能参考，以及诊断搜索范围是否覆盖高性能区域的线索，不把 BO 配置逐项解释为真实因果结论。

## 7. 索引和 SQL 是否被修改

没有。

- manifest 明确设置 `ddl=false`、`sql_rewrite=false`、`index_build=false`；
- 15 个 `round_NNN_decision.json` 的 `action_boundaries` 均记录 `ddl_executed=false`、`sql_rewrite_executed=false`、`index_build_executed=false`；
- experiment ledger 中没有 `CREATE INDEX`、`DROP INDEX`、`REINDEX` 或 `ALTER INDEX`；
- 每次测量恢复的 TPC-C 快照包含 BenchBase 初始化时已有的表和索引，这些原有索引没有被实验改变；
- 第 14、15 轮只调整优化器成本参数，可能影响现有索引的选择方式，不会创建或删除物理索引。

因此本次实验不能评价“索引调优是否有效”。它评价的是 60 个 PostgreSQL GUC 参数中的局部干预。

## 8. 对当前方法的最终判断

本次实验中，多智能体证据采集产生了有价值的瓶颈描述，特别是 WAL fsync 饱和、后台写回竞争和组提交窗口。它没有把这些诊断可靠地落实为候选配置，也没有在 15 轮内获得 TPS 改善。方法当前的主要失败点依次是：

1. 专家具体建议被参数组搜索器替换；
2. 旋钮组被拆散，无法验证交互机制；
3. 实际步长远小于专家建议和 BO 高性能区域；
4. 查询级观测缺失，竞争假设逐渐同质化；
5. 超过一半候选因波动无法得出结论；
6. 可能存在 GUC 动作空间无法解决的 SERIALIZABLE 回滚风暴。

因此，这次结果不支持“当前多智能体方法已经有效调优 TPC-C”。它支持一个更窄的判断：诊断模块能够指出部分有意义的系统机制，但诊断到实验动作的编译、联合旋钮验证和查询级证据仍不足，导致最终性能停留在默认配置，明显落后于 BO 参考值。

## 9. 可复核原始材料

- `artifacts/tpcc_continuous/tpcc-only-20261015T125511Z-diagnosis/performance.csv`：逐轮原始 TPS；
- `round_001_decision.json` 至 `round_015_decision.json`：专家提案、最终动作和动作边界；
- `experiment_ledger.jsonl`：冻结候选、应用结果、配对测量和结论；
- `baseline_stability.json`：三次默认基线；
- `TPCC_BO_VS_DIAGNOSIS.json`：统一口径的最终对比；
- `artifacts/workload_switch/tpcc-only-20261015T125511Z/oracle/bo_references/tpcc/reference.json`：BO 最优配置及三次复测；
- `artifacts/workload_switch/tpcc-only-20261015T125511Z/oracle/runs/tpcc-only-20261015T125511Z-tpcc/tpcc/progress.csv`：100 轮 BO 搜索轨迹。

## 10. 10/16 追加核查：回滚来源与下一版修正

### 10.1 回滚来源已经确认

原报告把 PostgreSQL 的高 `xact_rollback` 只描述为“可能的 SERIALIZABLE
回滚风暴”。重新解析 BenchBase 的终端直方图后，这一点已经可以确认。
基线运行 `20261015T201557588197Z-tpcc` 的计数为：

- 成功完成事务：`19,552`；
- 最终放弃事务：`48`；
- BenchBase 内部 `Server Retry`：`189,528`；
- 总事务尝试：`209,128`；
- 内部重试占尝试次数：`90.6277%`；
- 每个成功事务平均发生 `9.6935` 次服务端重试；
- `Unexpected SQL Errors`：`0`。

同一次运行的 BenchBase reported throughput 约为 `661.265 requests/s`，但排除重试后的
Goodput 只有 `161.587 transactions/s`。这进一步说明旧协议主要制造了无效事务尝试，不能把
reported throughput 当作有效 TPS。

BenchBase 的 PostgreSQL retry 判定只把 SQLSTATE `40001` 识别为服务端可重试错误，
所以这些计数代表序列化失败，不是连接故障、普通 SQL 错误或解析器误报。旧解析器只读取
`Unexpected SQL Errors`，因此曾错误地把 `retryable_database_aborts` 记录为 0；这与
PostgreSQL 侧约 90.4% rollback ratio 表面矛盾。解析器现在同时读取 Completed、Aborted、
Server Retry 和 Retry Different 四个直方图，并把 `server_retry_ratio` 作为独立诊断指标。

### 10.2 终端数判断

10 terminals 对一般 TPC-C 并非天然错误；问题在于本实验同时使用 scale factor 5 和
`TRANSACTION_SERIALIZABLE`。日志显示 worker 000/001 同时绑定 W_ID 1，worker 002/003
同时绑定 W_ID 2，形成每个 warehouse 两个 home terminal 的直接热点。结合 90.63% 的
`40001` 重试率，可以判断当前并发相对这份小数据集过高，并且测到的主要是 SSI 冲突重试，
而不是参数改变后的成功事务处理能力。

新协议把 TPC-C 改为 scale factor 5、5 terminals，即每个 warehouse 一个 home terminal；
隔离级别仍为 SERIALIZABLE，时长仍为 120 秒，预热仍为 30 秒，rate 仍为 unlimited。
终端数变化不改变数据内容，已有 scale factor 5 快照可以复用。由于负载协议已经变化，旧的
10-terminal BO 最优值 `298.653 TPS` 不能作为新实验的正式对照；BO 和诊断方法都必须在
5 terminals 下重新运行。新实验仍应记录 `server_retry_ratio`，如果它依然很高，再根据
NewOrder/Payment 的查询级证据分析跨 warehouse 与谓词锁冲突，不能继续只靠降低并发掩盖问题。

### 10.3 查询证据修复

本机的 `shared_preload_libraries` 已包含 `pg_stat_statements`，但旧安装流程只保证 Twitter
库创建扩展，TPC-C 与 YCSB 库可能没有扩展对象。这正是旧 TPC-C 诊断中
`query_evidence.status=missing` 的直接原因。安装脚本现在会在 `tpcc`、`twitter`、`ycsb`
每个数据库中执行并验证 `CREATE EXTENSION IF NOT EXISTS pg_stat_statements`。本机三个库均已
验证可查询。该路径不需要先引入 pg_stat_monitor、pgBadger 或 auto_explain；只有目标服务器
无法安装/预加载 pg_stat_statements 时才需要使用这些降级方案。

### 10.4 DeepSeek 缓存未命中的原因

旧实验共有 65 份模型调用元数据，prompt token 合计 `7,160,280`，其中 cached token
`2,025,728`，整体命中率只有 `28.29%`；completion token 又达到 `1,717,978`。最大请求
为 `828,542` bytes，其中 `graph_frontier` 单项约 `674 KB`。每轮把不断增长的完整图节点、
边、原始 observation、完整 incumbent evidence 和 measurement 重复发送，并把变化的 round、
incumbent 等动态内容放在可缓存前缀内。DeepSeek 的缓存匹配相同前缀，因此后缀中出现过的
相同大图不能产生有效跨轮命中。重试调用命中率反而较高，是因为它只在同一请求末尾追加了
validation error，前缀几乎完全相同。

此外，旧调用没有限制 `max_tokens`，也没有显式关闭 `deepseek-flash` 的 thinking，导致部分
调用生成很长的推理输出，再因结构校验失败而重试。这不是 API 缓存服务损坏，主要是请求组织
和生成设置的问题。

新提示词与请求已做以下调整：

1. 固定 schema、允许旋钮目录、角色职责和执行规则放在稳定 system prefix；动态 JSON 使用
   稳定键排序和紧凑序列化。
2. 图输入改成最多 80 条边的摘要，删除递归增长的原始 observations；incumbent 只保留配置、
   验证性能和 evidence pointer，不再复制整份测量证据。用旧第 15 轮图回放时，pending graph
   从约 `856 KB` 降为约 `92 KB`。
3. 显式设置 `temperature=0`、`max_tokens=8192`、`thinking.type=disabled`，减少长推理导致的
   截断和结构校验重试。
4. 提示词明确区分 `client.server_retry_ratio` 与 PostgreSQL rollback ratio，并把序列化重试、
   分事务 terminal capacity、查询模板和等待归因设为高优先级证据。
5. 要求专家给出可直接执行的精确值或有依据的范围，禁止重复 tested action，限制文字长度；
   协调器在诊断阶段直接执行专家旋钮组，不再用固定参数组网格替换建议。

DeepSeek 后续仍可能报告少量 cache miss，因为 incumbent、测量证据和图摘要会随实验变化，
这是正常的动态输入。判断修复是否生效应看“同一专家跨轮的 system prefix 命中”和总输入
token 是否显著下降，不应要求动态请求 100% 命中。

### 10.5 后续实现补充

在本报告初稿之后，两阶段流程已经补齐。诊断阶段现在维护显式假设状态表，优先清空图中已有
`proposed` 边，再请求下一批专家建议；协调器只选择已有专家提案，不能生成旋钮或改变值。动作
一致性层在应用前后分别核对专家动作、冻结候选和数据库实际设置，任何差异均记录为
`action_substitution` 并拒绝或判无效。

诊断停止必须同时满足：所有假设均为 `tested_*` 或 `rejected_*`，且连续两次专家咨询没有新增
可执行假设。无论最终是诊断穷尽还是预算用完，系统都会输出 `DIAGNOSIS_EXHAUSTION.json`。

诊断结束后，搜索空间编译器按照机制和性能判定生成 `FROZEN_SEARCH_SPACE.json`，随后只在这个
不可变范围内进行第二阶段参数搜索。协调器不参与第二阶段数值生成，搜索器也不能加入诊断记录
之外的旋钮。正式运行前还会执行动作忠实性契约冒烟检查；正式每轮仍重复运行时校验。
