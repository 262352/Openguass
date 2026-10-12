# Andromeda 诊断调优与基础贝叶斯优化对比报告

报告对象：Twitter 工作负载，目标为最大化 TPS（`throughput_rps`）
Andromeda 实验：`chain-group-20261009T020912Z/tpcc_to_twitter`
贝叶斯优化实验：`bo-twitter-20261009T033659Z/twitter`

## 1. 结论

当前结果不能证明 Andromeda 的诊断调优方法优于基础贝叶斯优化。现有绝对 TPS 证据反而支持贝叶斯优化：

- 在双方都只看前 11 个候选的口径下，Andromeda 最终保留值为 **2962.029 TPS**，贝叶斯优化已达到 **3236.695 TPS**。贝叶斯优化高 **274.666 TPS（9.27%）**。
- 贝叶斯优化完成 100 轮后，最优观测为 **3500.688 TPS**，比 Andromeda 的 2962.029 TPS 高 **538.660 TPS（18.19%）**。
- 贝叶斯优化在第 2 轮 LHS 就得到 **3222.733 TPS**，已经超过 Andromeda 最终保留值。
- 在与 Andromeda 相同的约 4924 秒墙钟时间内，贝叶斯优化完成 71 轮，最优值为第 22 轮的 **3491.130 TPS**，仍高于 Andromeda **17.86%**。

Andromeda 的工程闭环已经工作：它能够生成单条诊断链、选择旋钮组、执行 C1/T1/C2/T2 反事实测量、接受或回滚候选，并在结束时恢复源配置。但“诊断找到了真实性能机制”尚未得到证明：11 个候选中仅 2 个被性能规则接受；其中一个机制结论为 `refuted`，另一个为 `inconclusive`，没有任何被接受方案得到 `supported` 的机制结论。最终报告也明确记录 `unexplained_performance=true`。

因此，当前最准确的判断是：**系统执行能力有效，配置搜索获得过性能改善；诊断因果有效性未证实，搜索效率和最终绝对性能均未胜过本次基础贝叶斯优化。**

## 2. 两组实验实际做了什么

### 2.1 Andromeda

- 从 TPC-C 源配置切换到 Twitter 工作负载。
- 由单智能体根据吞吐量、中间遥测和未解决证据，每轮提出一个诊断及一个相关旋钮组。
- 每个候选使用独立配对复测：`control_1 → treatment_1 → control_2 → treatment_2`。
- 候选保守分数取两次 treatment 的较低值；只有相对两个局部 control 都为正且超过 incumbent 时，才接受配置。
- 计划最多 12 次干预，实际执行 11 次后由用户停止。
- 共执行 57 次工作负载测量、20 次 PostgreSQL 重启，耗时 **4923.848 秒（约 1 小时 22 分）**。
- 共调用模型 21 次，累计 1,772,918 tokens。
- 结束时源配置恢复成功。

### 2.2 基础贝叶斯优化

- 在同一 Twitter 数据快照、同一 30 秒测量时长、同一 2 terminals 和同一语义源配置上，对 60 个旋钮搜索。
- 前 8 轮为 LHS 初始采样，后 92 轮为 GP/EI，共 **100/100** 轮，100 轮均有效。
- 优化目标仅为最大化 `throughput_rps`。
- 总耗时 **6912.917 秒（约 1 小时 55 分）**。
- 初始基线 **2852.495 TPS**；第 90 轮取得最优 **3500.688 TPS**，相对本次基线提高 **22.724%**。
- 结束时源配置恢复成功。

## 3. 核心结果对比

| 比较项 | Andromeda | 贝叶斯优化 | 结果 |
|---|---:|---:|---|
| 工作负载 | Twitter | Twitter | 相同 |
| 目标 | 最大化 TPS | 最大化 TPS | 相同 |
| 候选旋钮空间 | 从 60 个旋钮中诊断选择旋钮组 | 同时搜索 60 个旋钮 | 同源空间，不同搜索方式 |
| 单次负载时长 / terminals | 30 秒 / 2 | 30 秒 / 2 | 相同 |
| 已完成候选 | 11 | 100 | 不同 |
| 实际负载测量次数 | 57 | 100 次候选测量，另有基线 | 计算成本不能只按候选数比较 |
| 耗时 | 4923.848 秒 | 6912.917 秒 | BO 多用约 1989 秒 |
| 前 11 个候选最优 | 2962.029 | 3236.695 | BO 高 9.27% |
| 相同墙钟时间内最优 | 2962.029 | 3491.130（71 轮内） | BO 高 17.86% |
| 全程最优 | 2962.029 | 3500.688 | BO 高 18.19% |
| 达到 2962.029 的时间 | 第 6 个 Andromeda 候选 | BO 第 2 轮 | BO 更早 |
| 候选复测 | 两次 treatment，取较低值 | 单次观测 | Andromeda 更保守 |
| 机制证据 | 0 个 accepted 候选获 supported | 不做机制诊断 | Andromeda 的因果主张未成立 |

贝叶斯优化 100 轮原始 TPS 的均值为 **1855.020**，中位数为 **1421.180**，样本标准差为 **975.523**，范围为 **175.400–3500.688**。这说明 60 维全空间中存在大量差配置，同时也说明单次最高值有被噪声抬高的可能。100 轮中有 18 轮单次观测不低于 Andromeda 的 2962.029 TPS。

## 4. Andromeda 每轮旋钮与测量结果

`验证 TPS` 为两次 treatment 的较低值。

| 轮次 | 修改的旋钮 | C1 | T1 | C2 | T2 | 验证 TPS | 性能结论 | 机制结论 | 接受 |
|---:|---|---:|---:|---:|---:|---:|---|---|---|
| 1 | `effective_io_concurrency=200`; `random_page_cost=1.1` | 754.699 | 656.331 | 822.066 | 633.565 | 633.565 | no_improvement | refuted | 否 |
| 2 | `autovacuum_analyze_scale_factor=0.02`; `autovacuum_naptime=20` | 1067.699 | 1079.599 | 989.863 | 3079.629 | 1079.599 | verified_improvement | refuted | 是 |
| 3 | `effective_cache_size=1048576`; `shared_buffers=65536` | 3002.266 | 3468.661 | 3061.464 | 1430.731 | 1430.731 | no_improvement | refuted | 否 |
| 4 | `wal_buffers=2048`; `wal_writer_delay=20` | 1254.299 | 971.600 | 937.997 | 993.664 | 971.600 | invalid | refuted | 否 |
| 5 | `bgwriter_delay=10`; `bgwriter_flush_after=256`; `bgwriter_lru_maxpages=1000`; `bgwriter_lru_multiplier=6` | 1508.297 | 2230.700 | 2719.666 | 2710.525 | 2230.700 | no_improvement | refuted | 否 |
| 6 | `default_statistics_target=500` | 2512.666 | 2962.029 | 3146.895 | 3177.032 | 2962.029 | verified_improvement | inconclusive | 是 |
| 7 | `checkpoint_completion_target=0.95`; `checkpoint_timeout=600`; `max_wal_size=4096`; `min_wal_size=512` | 2831.965 | 3068.163 | 3223.662 | 2714.597 | 2714.597 | no_improvement | refuted | 否 |
| 8 | `max_parallel_workers=16`; `max_parallel_workers_per_gather=4`; `max_worker_processes=16` | 2198.561 | 840.899 | 1886.430 | 1985.665 | 840.899 | no_improvement | refuted | 否 |
| 9 | `deadlock_timeout=3000`; `max_locks_per_transaction=256`; `max_pred_locks_per_transaction=256` | 2972.025 | 2593.731 | 2908.696 | 2917.461 | 2593.731 | no_improvement | refuted | 否 |
| 10 | `effective_cache_size=1048576`; `shared_buffers=32768` | 2958.797 | 3313.195 | 3378.195 | 3348.430 | 3313.195 | no_improvement | refuted | 否 |
| 11 | `random_page_cost=1.0`; `seq_page_cost=2.0` | 3182.962 | 2832.692 | 3424.296 | 3434.163 | 2832.692 | no_improvement | refuted | 否 |

最终接受并保留的变化只有：

- `autovacuum_analyze_scale_factor: 0.1 → 0.02`
- `autovacuum_naptime: 60 → 20`
- `default_statistics_target: 100 → 500`

第 10 轮值得单独说明：两次 treatment 为 3313.195 和 3348.430 TPS，保守值比 incumbent 高 11.856%；但第二次 treatment 低于对应 control 3378.195，所以严格配对规则将它拒绝。这反映了当前协议在强噪声下可能拒绝有潜力的配置，也说明单凭某一次高 TPS 不能建立因果关系。

## 5. 为什么 716.14% 的 Andromeda 改善不能直接采用

Andromeda 记录的初始 TPS 为 362.933，初始复核为 426.533；贝叶斯优化在同一语义源配置上的基线为 2852.495。两者相差约 6.7–7.9 倍。由 362.933 计算出的 **716.14%** 改善主要受异常低起点影响，不能用于证明方法优于贝叶斯优化。

更可信的跨方法比较是绝对 TPS，以及相同候选预算或相同墙钟时间下的 best-so-far。按照这两个口径，本次都由贝叶斯优化领先。

## 6. 当前方法暴露的问题

1. **诊断没有形成被证据支持的因果链。** 两个 accepted 候选的机制分别为 `refuted` 和 `inconclusive`；最终性能改善被系统自己标记为 unexplained。
2. **旋钮组命中率低。** 11 组中 9 组未被接受，多个诊断方向造成显著退化；基础 BO 第 2 轮随机空间覆盖就超过了 Andromeda 最终值。
3. **接受结果不等于诊断正确。** 第 2 轮虽然被接受，但两次 treatment 为 1079.599 与 3079.629，跨度极大，且机制被反驳。第 6 轮性能被接受，但机制仍不确定。
4. **候选预算不等于测量预算。** Andromeda 的 11 个候选使用了 57 次负载测量；BO 的前 11 个候选主要是一候选一次测量。若声称样本效率更高，应按总测量次数、总墙钟时间和模型成本共同计算。
5. **两种协议尚未做到论文级公平。** Andromeda 有 30 秒 warmup、固定的配对复测和保守取值；当前 BO 候选为单次观测，没有对最优配置做独立复测。BO 的 3500.688 TPS 因此是“最高单次观测”，不能直接当作稳定部署性能。
6. **Andromeda 实验未完成原计划。** 状态为 `stopped_by_user`，实际 11/12 次干预；这不影响现有 11 轮对比，但不能称为完整预算结果。

## 7. 可以和不可以得出的结论

现有实验可以支持：

- Andromeda 的单智能体诊断、旋钮组干预、反事实复测、接受/回滚和恢复流程可以实际运行。
- Andromeda 得到了一个经两次 treatment 测量的保守值 2962.029 TPS 的配置。
- 基础 BO 在这次运行中更快找到更高的绝对 TPS，并以更高的最终观测结束。

现有实验不支持：

- Andromeda 比基础 BO 更有效或更快。
- Andromeda 的 716.14% 是可信的真实提升比例。
- 已接受旋钮组对应的诊断机制成立。
- BO 的 3500.688 TPS 是稳定可复现的最终性能，因为最优配置尚未使用与 Andromeda 相同的独立重复协议复核。

## 8. 最终判断

若“方法有效”定义为能够运行完整闭环并找到比异常低初始测量更好的配置，则答案是 **工程上有效**。若定义为诊断准确、因果链被证据支持、并比基础 BO 更快或得到更高 TPS，则答案是 **当前实验没有证明有效**。本次可观测证据更支持基础贝叶斯优化，但尚需对双方最终配置采用完全相同的 warmup、随机种子和重复测量规则后，才能形成正式的统计结论。

## 9. 原始证据位置

- Andromeda 逐轮结果：`artifacts/v3_v4/chain-group-20261009T020912Z/tpcc_to_twitter/performance.csv`
- Andromeda 完整链报告：`artifacts/v3_v4/chain-group-20261009T020912Z/tpcc_to_twitter/chain_report.json`
- BO 逐轮进度：`artifacts/bayesian/bo-twitter-20261009T033659Z/twitter/progress.csv`
- BO 每轮完整配置与测量：`artifacts/bayesian/bo-twitter-20261009T033659Z/twitter/trials.jsonl`
- BO 总结：`artifacts/bayesian/bo-twitter-20261009T033659Z/twitter/summary.json`
- 机器可读对比：`artifacts/bayesian/bo-twitter-20261009T033659Z/twitter/final_comparison.json`
