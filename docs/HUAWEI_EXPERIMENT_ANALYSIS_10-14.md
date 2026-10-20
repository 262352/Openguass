# 华为 SCOW TPC-C → Twitter 实验分析（10/14）

> 分析日期：2026-10-14  
> 结果分支：`results/huawei-tpcc-twitter-20261013T121707Z-10-14`  
> 结果提交：`6bae5132b74efc9244e393f503ca2cbe56cb6306`  
> Suite：`huawei-tpcc-twitter-20261013T121707Z`

## 1. 最终判断

本次程序正常结束，数据库配置也成功恢复，但它不是一次完成了 12 轮的诊断调优实验。诊断阶段只完成了 3 次重复基线、2 次无参数观察和 3 轮模型决策，实际旋钮干预为 **0 次**。三轮共 9 个专家提案全部为 `observation-only`，最终停止原因为：

```text
no_executable_or_discriminating_followup
```

因此，本次结果不能用来判断多智能体方法是否能找到有效旋钮、是否能提高 TPS、是否比 BO 更快。它只能证明：

1. 华为 aarch64 服务器上的完整环境、快照、TPC-C 源配置投影、Twitter 基线、模型调用、证据图和回滚流程已经实际运行。
2. 在当前配置和 8 terminals 下，系统没有观察到锁、存储、WAL、临时文件、缓存或查询计划方面的明确瓶颈。
3. 当前诊断系统遇到“数据库表现健康、没有已知机制异常”的状态时，会停止而不是进行探索性优化。
4. 本次 BO reference 选择存在严重统计错误，导致所谓“Twitter 最佳参考配置”复测后反而明显弱于 TPC-C 源配置。

最关键的结论是：**这次实验并不存在一个有效的“源配置较差、Twitter BO reference 较好”的待关闭性能差距。** TPC-C 源配置切换到 Twitter 后已经比当前 Twitter BO reference 更快，所以诊断智能体没有合理目标可以追赶。

## 2. 实验是否跑完

程序从工程状态看已经跑完，而不是崩溃或被手动停止：

- `diagnosis_stage_summary.json` 已生成；
- `FINAL_COMPARISON.json` 已生成；
- 最终停止原因已经记录；
- `final_rollback.json` 显示恢复成功；
- nohup 日志最后输出完整 JSON，没有 Python traceback；
- PostgreSQL 最终恢复为 `True`。

但从实验预算看，它提前停止：

| 项目 | 计划 | 实际 |
|---|---:|---:|
| 最大诊断轮数 | 12 | 3 次决策 |
| 最大目标负载测量 | 60 | 5 |
| 初始重复基线 | 3 | 3 |
| 无参数 observation | 未固定 | 2 |
| 旋钮干预 | 最多 12 轮 | 0 |
| control/treatment 配对实验 | 每个候选四次 | 0 |
| 有效因果路径 | 期望至少 1 条 | 0 |

实际成本为：5 次正式负载测量、5 次 warmup、2 次 PostgreSQL 重启、10 次模型调用和 485,383 tokens。

## 3. 诊断阶段的真实性能

诊断使用 TPC-C BO reference 作为源配置，并为了当前 SERIALIZABLE 安全空间投影了两个值：

| 旋钮 | TPC-C BO reference | 诊断实际值 |
|---|---:|---:|
| `max_locks_per_transaction` | 160 | 256 |
| `max_pred_locks_per_transaction` | 105 | 4096 |

所以诊断的源配置并非与 TPC-C reference 完全逐位相同，但投影过程有明确记录。

Twitter 上三次相同配置基线为：

| 测量 | TPS | 平均延迟 | P95 | P99 |
|---|---:|---:|---:|---:|
| baseline 1 | 30,213.635 | 264 µs | 691 µs | 2,523 µs |
| baseline 2 | 29,762.641 | 269 µs | 696 µs | 2,560 µs |
| baseline 3 | 31,906.716 | 249 µs | 646 µs | 2,349 µs |

基线中位数为 **30,213.635 TPS**，保守值为 **29,762.641 TPS**，跨度为 7.096%，最大/最小比为 1.072。系统按 25% 阈值将其判为稳定。两次额外 observation 为：

| 测量 | TPS | 相对基线中位数 |
|---|---:|---:|
| round 1 observation | 28,257.493 | -6.474% |
| round 2 observation | 29,589.256 | -2.067% |

这些变化没有达到系统的同配置崩溃阈值，也没有伴随明确的服务器机制异常。

## 4. 三轮为什么都没有调整旋钮

### 4.1 查询计划与执行专家

该专家认为 Twitter 的主要事务 `GetUserTweets` 已经是缓存内的 Index Scan，没有物理读取、临时文件或计划切换证据。当前采集的计划只是：

```sql
SELECT * FROM tweets WHERE id = 1
```

它不是 BenchBase 中真实占主导的完整 `GetUserTweets` SQL。模型因此明确要求采集真实 SQL 的 `EXPLAIN (ANALYZE, BUFFERS)` 和等待事件，但当前执行器无法在下一轮完成这种证据采集，只能再次运行相同配置。

### 4.2 内存与存储专家

五次测量中 buffer hit ratio 约为 99.992%，没有 temp spill，Linux PSI I/O stall 为 0，WAL write 平均约 0.129–0.138 ms，WAL waiter 的 median/p95 都为 0、max 仅为 1。专家据此判断 `wal_wait` 告警只是单个采样点，不能支持修改 WAL、checkpoint、bgwriter 或 memory 旋钮。

### 4.3 并发与维护专家

实验只有约 9 个数据库 backend，而 `max_connections=137`；`locks_waiting` 和 `lock_waiting_backends` 始终为 0，deadlock/conflict 为 0，autovacuum 活动也很低。专家认为没有可以通过并发、锁或维护旋钮排空的服务器队列。

### 4.4 协调器行为

三位专家每轮都提交空 `knob_changes`。协调器按规则把所有 proposal 标记为 `observation-only proposal`，没有选择任何一个执行。允许的无参数观察轮数为 2，因此程序完成两次 observation 后，在第三轮再次没有可执行提案时停止。

这一行为符合当前安全规则，但也暴露了结构性死结：专家明确要求“采集真实 GetUserTweets 计划和按查询归因的等待事件”，而执行器只有“再跑一次相同工作负载”的 observation 动作。新一轮没有增加专家需要的新证据，所以结论必然重复。

## 5. 事务级证据说明了什么

最后一次 observation 中：

| 事务 | 调用数 | Mean | P95 | P99 | Terminal 时间占比 |
|---|---:|---:|---:|---:|---:|
| GetUserTweets | 3,186,707 | 265.51 µs | 637 µs | 2,626 µs | 88.13% |
| GetFollowers | 247,348 | 191.04 µs | 370 µs | 2,185 µs | 4.92% |
| GetTweetsFromFollowing | 35,456 | 1,058.25 µs | 3,684 µs | 7,352 µs | 3.91% |
| GetTweet | 35,289 | 177.56 µs | 285 µs | 2,081 µs | 0.65% |
| InsertTweet | 35,472 | 604.97 µs | 1,919 µs | 4,059 µs | 2.24% |

与旧的 2-terminal 失败实验不同，本次 `InsertTweet` 没有数百毫秒的 COMMIT 长尾，也没有占满 terminal。它的 p50 约 0.38–0.55 ms，terminal 时间占比只有约 2.1–2.2%。所以旧实验的 WAL/COMMIT 故障没有在当前 8-terminal、120 秒协议中复现。

本次 `GetUserTweets` 占据 88% terminal 时间，主要因为它约占全部调用的 88%–90%，而不是因为单次执行异常缓慢。当前确定性预诊断只要发现某类事务消耗超过 50% terminal 时间，就生成高严重度 `client_terminal_saturation` 告警。这会把“工作负载本来就以某类查询为主”误写成性能异常。

更合理的判定应同时比较：

- 事务调用占比；
- terminal 时间占比；
- 相对历史稳定基线的 per-call 延迟变化；
- 同类查询的等待事件和执行计划变化。

如果一个事务占 90% 调用、占 88% terminal 时间，它实际上比平均事务更轻，不应仅凭绝对占比触发高严重度瓶颈告警。

## 6. BO reference 存在的严重问题

### 6.1 Twitter BO 搜索结果

Twitter BO 完成 100/100 轮，所有候选均被标记为 valid：

- BO 初始基线：**26,959.473 TPS**；
- 搜索阶段最高单次观测：**36,670.302 TPS**，第 75 轮；
- 报告的单次提升：36.02%；
- 第 75 轮三次独立复测：25,103.556、24,138.796、24,234.569 TPS；
- 复测中位数：**24,234.569 TPS**；
- 复测保守值：**24,138.796 TPS**。

第 75 轮复测中位数比搜索时的单次高值低 **33.912%**，并且比 BO 自己的初始基线低 **10.107%**。这说明 36,670 TPS 不是该配置的稳定性能，可能混入了时段、缓存、存储状态或其他运行噪声。

### 6.2 reference 选择代码的问题

当前 `_build_reference()` 的逻辑是：

1. 按搜索阶段的单次 TPS 从高到低排序候选；
2. 选择第一个能够成功完成三次复测、没有抛异常的候选；
3. 不检查复测 TPS 是否仍高于 BO baseline；
4. 不继续复测其他 Top-K 候选；
5. 不按复测中位数或保守值重新排序。

所以第 75 轮虽然复测性能明显退化，只要三次运行没有 SQL 错误，就仍然成为 Twitter reference。`rejected_reference_candidates=[]` 只表示没有候选因运行异常被拒绝，不表示这个配置在性能上通过验证。

因此当前 `definition: best measured configuration` 和 `verified_tps` 的语义不成立。它是“搜索阶段单次最高且复测未报错的配置”，不是“复测后稳定最优配置”。

### 6.3 这如何破坏诊断对比

TPC-C 源配置在 Twitter 上的诊断基线：

- 保守 TPS：29,762.641；
- 中位 TPS：30,213.635。

Twitter BO reference：

- 保守 TPS：24,138.796；
- 中位 TPS：24,234.569。

源配置保守值比目标 reference 保守值高 **23.298%**，源配置中位数比目标 reference 中位数高 **24.672%**。`FINAL_COMPARISON.json` 因此记录 `initial_gap_to_bo_reference=-5623.845`。负 gap 表示 episode 一开始已经超过对照目标。

在这种情况下，即使诊断方法什么都不做，也会“优于 Twitter BO reference”。这不能证明诊断方法有效，只能证明 BO reference 选错了。

## 7. 当前实验还暴露了哪些方法问题

### 7.1 目标是最大化 TPS，但方法只会响应异常

当数据库没有明显锁、I/O、WAL、缓存或计划异常时，当前方法不会执行旋钮敏感性探测。它是故障诊断器，不是完整的性能最大化器。若论文目标仍是“在固定预算内尽快提高 TPS”，系统需要增加健康状态下的机制驱动探索：选择一个有可测预测的安全旋钮组做小范围探测，而不是要求必须先出现异常。

### 7.2 8 terminals 很可能限制了服务器压力

服务器有 192 个 CPU 和约 2 TB 内存，实验只使用 8 个闭环 terminal。TPS 基本满足 `terminals / weighted mean latency`，专家也多次判断 terminal 是有限预算。当前证据没有主机 CPU 利用率、run queue 和每核饱和度，无法证明数据库已经达到资源瓶颈。

如果客户端并发本身是上限，大多数 PostgreSQL 资源旋钮自然不会产生明显收益。下一次应先做 4/8/12/16 等并发容量曲线，在扩大谓词锁空间并排除 53200 后，找到 TPS 开始变平且错误率仍可接受的稳定并发，再固定正式比较参数。

### 7.3 查询证据仍未闭环

当前计划证据是人为固定的代表查询，而不是按 `pg_stat_statements.total_exec_time` 发现的真实 Top-N SQL。等待事件也只有实例级计数，没有按 `queryid` 归因。三轮专家都要求同一类新证据，但系统无法执行该证据动作。这正好验证了 10/14 报告提出的查询级证据模块是必要的。

### 7.4 模型输入中的时间口径存在歧义

`terminal_capacity_fraction` 使用正式测量窗口 `duration × terminals = 120 × 8 = 960 terminal-seconds`。但 measurement 的 `elapsed_seconds` 约为 162–168 秒，包含 BenchBase 之外的额外运行开销。模型在第一轮文字中曾把 843.5 秒除以 `8 × 168.4`，同时又引用程序计算的 0.8787，两个口径在算术上不一致。

模型输入应明确提供 `measurement_duration_seconds=120` 和 `terminal_capacity_seconds=960`，并将端到端运行耗时改名为 `wall_clock_elapsed_seconds`，避免模型混用。

### 7.5 两次 observation 没有增加信息

两次 observation 仍然采集相同的实例级指标和同一条代表性 EXPLAIN。它们只证明相同结论可以重复，没有完成专家请求的 per-query wait 或真实 SQL plan。未来 observation 必须是具体的证据采集动作；如果系统无法完成请求，应立即以 `missing_instrumentation` 停止，而不是消耗模型 tokens 和负载测量预算。

## 8. 必须优先修改的地方

### P0：修复 BO reference 选择

1. 把 BO baseline 和搜索 Top-K 候选都纳入相同的重复复测。
2. 按复测的保守 TPS或置信下界重新排序，而不是按搜索阶段单次最高值排序。
3. 候选复测中位数未超过 baseline 噪声阈值时拒绝该候选并继续测试下一名。
4. 如果所有候选都不能稳定超过 baseline，正式 reference 应回退到 baseline。
5. 将 `search_peak_tps` 与 `verified_reference_tps` 分开报告，不能再把单次峰值称为验证最优。

### P0：增加诊断实验前置条件

运行多智能体诊断前，先用完全相同协议复测：

- 源配置在目标负载上的 TPS；
- 目标 BO reference 的 TPS。

只有当目标 reference 的保守值显著高于源配置，且差距超过同配置噪声阈值时，才进入“关闭性能差距”的诊断实验。本次应被前置检查标记为 `no_verified_tuning_gap`，而不是消耗三轮模型调用。

### P1：把 observation 改成可执行证据动作

支持以下动作类型：

- 重置并读取测量窗口内的 `pg_stat_statements` Top-N；
- 按 `queryid` 高频采样 wait event；
- 对选中的只读 SQL 执行受控 EXPLAIN；
- 对写 SQL 使用 `auto_explain` 或只做不执行的 EXPLAIN；
- 采集 CPU 利用率、run queue、上下文切换和 cgroup throttling。

专家提出缺失证据后，协调器应选择一种证据动作；新证据回到黑板后再生成下一轮假设。

### P1：修正事务瓶颈告警

不能再用 `terminal_capacity_fraction >= 0.5` 单独判定瓶颈。建议至少计算：

```text
call_share = transaction_count / total_transaction_count
time_share = transaction_total_latency / total_terminal_time
latency_inflation = current_mean_latency / stable_baseline_mean_latency
excess_time_share = time_share - call_share
```

只有 time share 明显超过 call share、per-call latency 相对基线恶化，或者查询级等待/计划证据异常时，才触发高严重度告警。

### P2：明确方法边界

如果研究目标是负载切换后的故障恢复，当前“有证据才干预”的规则是合理的。如果研究目标是与 BO 比较 TPS 最大化效率，则必须加入健康状态下的小范围机制探测，否则没有异常时方法必然停止，无法与持续搜索的 BO 对比。

## 9. 下一次正式实验建议

1. 修复 reference 选择后，重新从现有 100 轮结果的 Top-K 开始复测；无需立即重新跑全部 BO，但至少要复测前 5–10 个候选和 baseline。
2. 完成 Twitter 4/8/12/16 terminals 容量扫描，确保数据库承受压力且不发生 SQLSTATE 53200。
3. 重新验证 TPC-C 源配置在 Twitter 上确实低于新的 Twitter reference；若仍高于目标，则更换源配置或负载切换场景。
4. 接入真实 Top-N SQL、按 queryid 等待归因和计划摘要。
5. 再运行 12 轮诊断；每个 observation 必须产生新证据，每个 intervention 必须冻结机制预测。
6. 最终用同一独立复测协议比较源配置、诊断最终配置和 BO reference。

## 10. 本次可以与不可以得出的结论

可以确认：

- 新协议在华为 aarch64 服务器上能够实际运行并安全结束。
- 当前源配置在 Twitter 上稳定达到约 29.8k–31.9k TPS。
- 旧实验的 InsertTweet COMMIT 长尾没有在本次协议中复现。
- 当前实例级证据没有支持任何旋钮组干预。
- 当前 BO reference 选择算法会接受复测后性能低于 baseline 的配置。
- 查询级计划和等待归因缺失会让 observation-only 无法转化为下一步实验。

不能声称：

- 多智能体调优提高了 TPS；本次没有修改任何旋钮。
- 多智能体方法优于 BO；当前 BO 对照 reference 无效。
- 诊断找到了有效根因；有效图、测试图和支持边均为空。
- 12 轮调优已经完成；系统在第 3 次决策时提前停止。
- 8 terminals 已使数据库饱和；现有结果缺少 CPU 饱和与并发容量证据。

本次实验最有价值的结果不是一个性能提升数字，而是暴露了评估流程的两个关键断点：**BO reference 没有按复测性能选择，以及诊断智能体提出的新证据需求无法由执行器实际完成。** 在修复这两个断点之前，继续增加诊断轮数或让模型更积极地修改旋钮都不会形成可信的对比结论。
