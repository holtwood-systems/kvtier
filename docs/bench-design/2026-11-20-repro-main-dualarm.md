# 实验 0：L3 链路 main 双臂验证（repro-main-dualarm）

- 日期：2026-11-20（设计，未执行）
- 定位：**两份主实验（`2026-11-20-repro-49221.md` / `2026-11-20-repro-47481.md`）的前置闸门**
  ——用最便宜的负载判定 main 对 DEV-20 双根因的修复完整性，同时核实
  可观测性缺口，为主实验的假设面和上游再介入（#36280 / #49226）提供依据。
- 运行对象：SGLang main；**锚定说明**：本设计的源码核对基于两个同日 tip——
  `789abb9be9d987e0b7fc950aa33f842f569168e5`（mem_cache diff 报告）与
  `e183c415bdfc5995f6288b1651e1c320c6a74015`（bench 依赖面审计，晚 ~8h，
  接口面结论一致）；正式运行锚定当日最新 tip 并记录 40 位 SHA。
  两个 tip 均**不含 open 的 PR #49226**，验证的是它合入前的基线状态
- 历史基线：DEV-20（2026-11-11，WSL2 / RTX 5070 Ti 16GB / SGLang
  `6afee75f9`，Qwen2.5-0.5B）三份证据：
  [`l3-file`](../../bench/repro-31600/evidence/2026-11-11-wsl2-rtx5070ti-l3-file/README.md)（cache+salted 负结果）、
  [`l3-cache-nosalt`](../../bench/repro-31600/evidence/2026-11-11-wsl2-rtx5070ti-l3-cache-nosalt/README.md)（机制确认）、
  [`l3-buffer`](../../bench/repro-31600/evidence/2026-11-11-wsl2-rtx5070ti-l3-buffer/README.md)（正向证据）
- 目标硬件：RTX 4090 48GB（sm_89）、1TB 宿主内存、`/data` 500G NVMe、原生 Linux
- 不修改上游源码；脚本草稿见附录 A（Go 之后落 `bench/repro-main-dualarm/`）

## 0. 结论先行

DEV-20 在 6afee75f 上确认了 L3 file 负结果的双层根因。静态 diff 表明
main 对两层都有动作（①salt 显式传递+断言；②staging 命中时分配+容量
仲裁），但**没有人做过 GPU 级的行为验证**。本实验用 DEV-20 同形状负载
在 main 上重跑 2×2 双臂 × 双后端，回答四个问题：

1. 根因 ①（salt 归属）在 main 是否真修（salted 臂应从"全 0"变为"可见"）；
2. 根因 ②（staging 饥饿）是**结构性修复**还是只是被大内存掩盖（等压
   43k 池 + 4× 剂量臂分离）；
3. main 新后端 `fast_file` 是否改变结论（顺带的增量对比）；
4. 静默撤销在 main 的可观测性成色（`reason="dropped"` 已出口，但
   revoked_insufficient/full_miss 分类仍未导出）——决定 #36280/#49226
   的观测性证据能否绑上 bug。

任何一格结果都直接改写主实验的假设面（§11），故本实验优先级最高、
预算最小。

## 1. DEV-20 事实回顾（全部来自本仓 evidence README，已核对原文）

**基线负结果（l3-file 臂，cache + salted）**：
`--num-trials 6 --prefix-tokens 4096 --tail-tokens 128
--evict-factor 1.3 --expected-storage-backend file`；服务端
`MEM_FRACTION=0.35`（device pool **358,532 tokens**）、
`HICACHE_RATIO=0.12`（host pool **43,024 tokens / 0.53 GB**——16GB RAM
下 10GiB 硬保留逼出的上界）、write_through、kernel、page_first、
page-size 默认（`.bin` 为单 token 粒度 12,288 B）。结局
`status=failed / index_evicted`：R-000 三层全 miss；E=456 请求 /
466,944 tokens（> device+host 合计 401,556）。

**根因 ①（salt 归属，B 臂 nosalt 证实）**：cache 模式下 L3 prefetch
的 host 插入路径以 anchor=root 的 `cache_salt=None` 命名
（`prefetch_anchor_info` 取 anchor 的 key），带 `l2-*` salt 的 re-match
经 `RadixKey._check_compatible` 不可见 → 命中无法归属。nosalt 臂 8/8
运行 R-000 `storage>0`（640–999）而 salted 臂同形状全 0，机制封闭。

**根因 ②（staging 饥饿，cache 模式特有）**：E 的 395k token
write-through 写回经 host staging 转发并 `protect_host` 钉住至 ack；
drain 慢（WSL2 ~190 文件/s）且部分写被**静默丢弃永不 ack**；M 阶段
`evict_host` 只能从未保护叶子抠空间——首个 R 仅分到 722–999 token；
后续 R 连 `prefetch_threshold=256` 都凑不齐 →
`_revoke_pending_prefetch` 静默撤销（当时仅 debug 日志）→ 冷 prefill
→ `storage=0`。指标：`hicache_host_used=42793–43024`（~100%）、
`prefetch_capacity_limit=0.5×pool≈21512`、`prefetched_tokens_total=768`。

**正向对照（l3-buffer 臂）**：buffer_only 下 6/6 R
`storage ∈ [1373,1536]`，P 全冷；但驻留率部分（~1400–2900/4224，
疑因 `batch_exists` 首失截断——与 #47481 的 H_chain 同谓词）、
`dropped reason=overlap`、TTFT 比 16.8–32.6×、且需两个 6afee75f 专属
workaround（buffer metrics clamp、`SGLANG_ENABLE_STRICT_MEM_CHECK_
DURING_IDLE=0`）。

**可复用的 harness 能力（已在 repro-31600 workload.py）**：
`--salt-mode {per-request,none}`、`--evict-content-mode {shared,per-request}`
（nosalt 下 per-request 必需，有护栏硬检查）、`--post-evict-wait`（静默
收敛轮询）、逐 trial 独立内容的 W/R/P 种子区间、`failure_classification`、
`storage_snapshots`、L3 臂 R 判据 `storage>0`（路径存在性）。

**环境差异清单（4090 服务器 vs 5070 Ti/WSL2）**：无需 WSL2 host-alloc
补丁（原生 Linux）；NVMe 快（drain 不再是 190 文件/s 的瓶颈——这正是
要控制的变量，见 §4）；**openssl 头文件在 4090 服务器也曾缺失**
（`evidence/2026-11-05-linux-rtx4090-l3-file-failed-ssl/` 留档）——
libssl-dev 是前置；6afee75f 的两个 buffer workaround 是否仍需要是
**观测点而非前提**（若 main 仍崩，本身即 main 侧 bug 发现，如实留档）。

## 2. main 静态核对结论（本设计已完成；源码锚 `789abb9be`，与
`e183c415b` 依赖面审计一致处已随行标注）

| # | 核对项 | main 状态 | 证据（commit 789abb9be） |
|---|---|---|---|
| 1 | 根因 ① 修复 | **静态已修**：prefetch 调用显式传 `req.cache_salt`，插入按请求命名空间，附一致性断言 | `managers/scheduler.py:3818-3831` + `mem_cache/unified_radix_cache.py:2666-2727`（"a span published under the wrong namespace gets re-owned … (double free)" 断言）；本仓 diff 报告主题 1 |
| 2 | 根因 ② 修复 | **静态重构**：staging 改命中时分配（"the query holds no host memory"）、`allocate_storage_hit(allow_partial, min_tokens, evict_host)` 失败先逐 host 再试、`capacity.py`/`prefill_budget.py` 仲裁、cache 模式 enqueue 即预留 requested span | `unified_radix_cache.py:2753-2760`、`:3802`（`min_tokens=self.prefetch_threshold if allow_partial`）、`:3584+`（`_prefetch_occupied_span` docstring）；diff 报告主题 2 |
| 3 | revoke 符号 | `_revoke_pending_prefetch` → **`revoke_pending_prefetch`**（公开方法，仍在）；cache 模式 revoke / buffer 模式 park-and-retry 的分化**未变** | `unified_radix_cache.py:3675`、`:3727`（"buffer mode parks and retries; cache mode revokes"） |
| 4 | 撤销/未履约可观测性 | **token 维度已出口**：revoke → `_finish_storage_prefetch(reason="dropped")` 汇入已导出的 `sglang:storage_prefetch_unfulfilled_tokens_total{reason}`——发射点实际取值 9 种（含 **`host_capacity`**、`unservable_prefix`；collector 预建 11 个 label 值）；但 `_prefetch_outcome_stats` **14 键**事件分类（`revoked_insufficient/full_miss`、`declined_anchor_lost/device_covered/host_oversize/unservable_prefix`、`issued_assumed_stored`、`l3_demand_requests/l3_miss_tokens/l1l2_miss_tokens` 等，rank-synced）**仍为内部 dict，未导出 /metrics** | urc `:3676/:3556-3574/:411`；`metrics_collector.py:2311-2344`；依赖面审计 §1.3/§3.2【可见】 |
| 5 | prefetch 阈值 | `prefetch_threshold = 256`（默认，file/fast_file 无独立阈值）——与 DEV-20 实测同值 | `unified_radix_cache.py:385/569`、`:3867`（`below_threshold` 原因绑定） |
| 6 | PR #49226 关系 | main **不含** #49226（open，head `7b920fe2`，base `3830fb07`，叠 #48848）；其 diff（13 文件）加的是**机制**——host 树 pin 至 admission（`_pin_hicache_host_prefix`/release）、`PrefetchAdmissionBudget` FIFO 容量排队（`prefetch_admission_budget.py` 新文件）、PD 侧回退、9 个单元测试（`test_completed_prefetch_survives_eviction_until_admission` 等）；**不新增 Prometheus 计数器**（测试 mock 复用 `log_storage_prefetch_unfulfilled_tokens`） | 本设计抓取的 patch-diff 全文核对 |

**推论（本设计观点）**：实验 0 验证的是 **#49226 合入前的 main 基线**。
若 ② 的饥饿签名在等压池上复现，则 #49226 的容量排队与 pin 正是对症
方向，实验 0 数据 = 该 PR 动机的 GPU 级行为证据；若 ② 已被"命中时
分配 + 仲裁"结构性消除，则 #49226 只剩 admission 窗口那一半问题
（即 #49221 本体）。计数器面在 main→#49226 间稳定（同 counter 不同
值），实验 0 的观测设计对两者通用。

## 3. 假设与预测矩阵

**H①**：main 已修复 salt 归属——salted 请求的 L3 命中在 re-match 中可见。
**H②**：main 已（结构性）修复 staging 饥饿——等压 host 池下 prefetch
staging 不再被写回钉死到低于阈值。

诊断变量（每臂 6 trial，R 判据沿用 DEV-20）：

- **D1（测 ①）**= salted-cache 臂 vs saltless-cache 臂的 R `storage>0`
  覆盖率（trial 数）。**main 上两臂预期同-hit**（salt 归属静态已修，
  saltless 臂从"机制对照"重定义为"修复验证"：若 salted 仍系统性低于
  saltless，差异本身即 main 的 bug 证据——依赖面审计 B1 同此结论）；
- **D2（测 ②）**= cache 臂（取 ① 成立者）的 R 驻留水平 + M 阶段
  `unfulfilled{reason=host_capacity}`（staging 分配失败，**主签名**，
  直接读计数器不再靠水位间接推断）、`{reason="dropped"}`（revoke）、
  `{reason=below_threshold}`（截断至 <256）三类增量 + host 水位（降为
  佐证通道）；
- 驻留分级：**L1 路径存在**（`storage>0`）/<b>L2 过半</b>（中位
  `storage ≥ 0.5×4224`）/<b>L3 全量</b>（中位 `≥ 0.9×4224`）。

**"命中时分配"之后饥饿为何仍可能（机制通路，矩阵解读依据）**：
main 把 staging 从查询期预分配改为命中时分配，消除了"查询占用"这层；
但——
(a) E 阶段 write-through 的**写暂存 + `protect_host` 钉住至 ack** 的
消费形态未变：drain 未收敛时 host 池仍被写回钉死；
(b) 命中时分配的仲裁是"先 `evict_host` 再试、允许部分命中
（`min_tokens=prefetch_threshold`，urc:3802）"——当未保护可逐出页不足
时分配仍失败，其出口即 `unfulfilled{reason=host_capacity}` 或 revoke 的
`{reason="dropped"}`；cache 模式另有 **enqueue 期 span 预留**
（`_prefetch_occupied_span` docstring：cache mode reserves the requested
span at enqueue），预留失败同走容量类出口；
(c) 分配成功并插入 host 树的页，在 admission 排队/重试梯期间**仍无
防逐引用**（= #49221/#49226 的 admission 窗口；本实验串行 M 相位预期
不触发，burst 化的主实验会）。
因此 ② 在 main 的残留形态预期从"静默撤销"变为**计数器可见的
host_capacity 降级 / revoke / 部分命中**——判定主信号就是该计数器。

| 组合 | 观察（cache 臂，file 后端） | 预期计数器签名（M 阶段增量） | 结论与动作 |
|---|---|---|---|
| ①修 ②修 | salted 与 saltless 的 R 均 ≥5/6 trial `storage>0`，中位驻留 ≥L2，M 开始时 host_used ≤90% | `host_capacity`/`dropped`/`below_threshold` 各 <256；`storage_prefetch_hit_tokens_total` ≈ R 前缀总量 | main 双根因闭合。主实验 #49221 直接进 burst 判定；#49226 动机收窄为 admission 窗口；#36280 仍有价值（14 键事件分类未导出） |
| ①修 ②未修 | 两臂 R-000 均 `storage>0` 但驻留 <L2、或 ≥2 trial `storage=0`，M 开始 host_used 贴满 | **`host_capacity` ≥256（主签名，机制通路 b）**或 `dropped` ≥256（revoke 路径）；`below_threshold` 伴随部分命中；`hit_tokens_total` 明显小于前缀总量 | salt 修复确认（vs 6afee75f 的 salted 全 0）；饥饿仍在且**被 1TB 内存掩盖的风险被等压设计排除**。→ #49226 容量排队的 GPU 级动机证据；#36280 的 `revoked_*` 事件导出需求坐实 |
| ①未修 ②修 | salted 全 trial `storage=0`，saltless 全量驻留（≥L3）且无饥饿签名 | salted 臂 `hit_tokens_total≈0` 且 unfulfilled 无增量（查询即 miss，未进入分配）；saltless 臂签名同第一行 | main 的 salt 修复未覆盖"root anchor + salted"场景（断言只保证 anchor 有命名空间时一致）→ **潜在新上游 issue**，负结果留档并考虑最小化复现 |
| ①未修 ②未修 | salted 全 0；saltless 呈 6afee75f 饥饿签名（722 级截断/撤销） | salted 同上；saltless `host_capacity`/`dropped` ≥256 | main 未闭合任何一层（先复查 commit 与启动参数再下结论）——与静态 diff 预期矛盾，本身是重大发现 |

**剂量臂（D2 的因果补强）**：saltless-cache + host 池 4×（172,095
tokens）。若等压臂呈 ② 签名而 4× 臂恢复 ≥L2 驻留**且 `host_capacity`
增量随容量消失（<256）** → 饥饿是**容量边界**问题（#49226 的排队是
对症的）；若 4× 臂仍呈签名（`host_capacity`/`dropped` 仍 ≥256）→
存在与容量无关的结构缺陷（更严重，优先上游）。

**buffer 臂的附加假设（H③，顺带测量）**：buffer_only 臂在 main 的
驻留率是否仍部分（首失截断）。若 main 已全量驻留 → #47481 的
H_chain 在 file/fast_file 侧已被消除；若仍 ~1/3 驻留 → 为 #47481 文档
的 H_chain 预测提供本地前导证据。预期 buffer 臂不受 ①② 影响
（staged prefetch 不依赖 radix 路径，写/预取预算分离）——若 buffer
臂也失败，先查 6afee75f 两个 workaround 是否在 main 仍必要。

## 4. 环境换算（等压重建推导）与臂表

**关键换算**：DEV-20 的 43,024-token host 池 = `HICACHE_RATIO 0.12 ×
device 358,532`（host 池按 ratio × device 池 token 数换算；0.53 GB
@12,288 B/token）。16GB RAM 的硬约束在 1TB 服务器上消失，**必须显式
钉住两个 token 数**才能等压：

- `--max-total-tokens 358532`（device 池 token 数与 DEV-20 逐 token 相等；
  0.5B 下仅 4.4 GB KV，4090 显存无压力，`MEM_FRACTION=0.35` 沿用以
  保留其余行为可比性）
- `--hicache-ratio 0.12` → host = **43,024 tokens**（与 DEV-20 相等；
  0.53 GB，占 1TB 的 0.05%——不设此参数则 main 默认 ratio 2.0 会给出
  ~717k tokens，压力完全消失，实验无效）
- main 无 `--hicache-size-ratio` 之名：现行参数为 `--hicache-ratio`
  （float，`arg_groups/fields/memory.py`）；`--hicache-size`（int GB）
  最小粒度 1 GB ≈ 87k tokens，**无法表达 43k**，故只能走 ratio 路线
- `--hicache-host-memory-fraction` 仅在 ratio/size 均未显式设置时参与
  自动 sizing——显式给 ratio 后无干扰（预期；G0 用启动日志核对）
- 剂量臂：`--hicache-ratio 0.48` → host = 172,095 tokens ≈ 4.00×
- 负载逐字沿用：`--num-trials 6 --prefix-tokens 4096 --tail-tokens 128
  --evict-factor 1.3`（E 由 `/server_info` 的 358,532 推导 → 456 请求 /
  466,944 tokens，device+host 超订 1.16×、host 单独对 E 写回超订 ~10.9×
  ——与 DEV-20 相同的压力结构）；page-size 默认（=1，单 token `.bin`）

**臂表（9 次运行）**：

| 臂 | salt | host mode | ratio | 后端 | 检验 |
|---|---|---|---|---|---|
| A1 | per-request | cache | 0.12 | file | D1（①）主判 |
| A2 | none（per-request evict 内容） | cache | 0.12 | file | D2（②）主判 |
| A3 / A4 | per-request / none | buffer_only | 0.12 | file | 对照 + H③ 链截断 |
| B1–B4 | 同 A1–A4 | 同 | 0.12 | **fast_file** | 后端增量对比 |
| C2 | none | cache | **0.48** | file | 剂量臂（4×） |

harness 变更（客户端侧，Go 后实施）：新增 `--expect-mode diagnostic`
——R `storage=0` 不再触发 failed，而是记录逐臂判定输入；结果包新增
`arm_verdict` 字段（①/② 判定 + 驻留分级 + 计数器签名）。审计器同步
接受该模式。

## 5. 判定门禁（G0/G1/G2/G3）

**G0 路径前提（每臂，任一不满足 → 该臂无法判定）**

1. 启动日志 `Allocating ... hierarchical KV host pool: N tokens`：
   等压臂 N ∈ [42,164, 43,884]（43,024±2%），剂量臂 ∈ 172,095±2%；
   `/server_info` device token_capacity ∈ 358,532±2%；
2. W 路径证据：post_write `sglang:backuped_tokens_total` 正增量
   （≥6×4,224 的新内容部分；dedup-hit 语义见 l3-file README 注意事项，
   与 `.bin` 增长二选一满足即可）；
3. P 探针全冷（`sum(cached_tokens_details)==0`）；nosalt 臂护栏：
   `--evict-content-mode per-request` 由 harness 硬检查；
4. 服务快照：`hicache_write_policy=="write_through"`、
   `hicache_storage_backend` 与臂一致、host mode 与臂一致、TP=1、
   `enable_metrics`、`enable_cache_report`；40 位 commit；
5. 每臂 storage 目录 run 前清空；E 后 drain 等待上限 240 s
   （静默收敛判据沿用；超时不判失败，如实记录 unconverged——
   DEV-20 实测丢弃写可致永不收敛）。

**G1（修复确认，对应矩阵第一行）**：A1 与 A2 均 ≥5/6 trial
`storage>0` 且两者中位驻留 ≥L2，且 M 阶段 `host_capacity`/`dropped`/
`below_threshold` 增量各 < 256（一阈值），M 开始时 host_used ≤ 90%
total。

**G2（分层判定，对应矩阵第 2/3 行）**：
- ①修：A1 ≥5/6 `storage>0`（与 A2 无显著差：两者覆盖率差 ≤1 trial）；
  ①未修：A1 全 6 trial `storage=0` 且 A2 ≥5/6 `storage>0`；
- ②未修（在 ① 修的前提下）：A1/A2 中位驻留 <L2，或 ≥2 trial
  `storage=0` 伴 M 阶段 `host_capacity`（主）或 `dropped`+
  `below_threshold` 合计增量 ≥256，或 M 开始时 host_used >95%（佐证）；
  剂量臂 C2 恢复 ≥L2 且 `host_capacity` 增量 <256 → "容量边界型"，
  C2 仍 <L2 → "结构型"；
- ②修：不满足上述任一签名且 A1/A2 达 G1 驻留要求。

**G3（无法判定）**：`failure_classification` 命中 `not_backed_up` /
`storage_read_failed` / `config`；或服务崩溃（buffer 臂若复现
6afee75f 的 pool-memory-leak 断言 → 记 main 侧发现，臂标 G3 并附
crash log）；或 fast_file 目录布局使 `storage_snapshots` 不可解析
（此时 fast_file 臂仅用 counter 判定并如实降级标注）。

**可重复性规则（不再决策）**：仅当某臂判定落在边界（覆盖率恰为
4/6，或驻留中位落在 [0.45, 0.55]×4224）时，重跑该臂一次取两次一致
方向；预算内最多 2 次重跑。

## 6. 可观测性核对清单（决定观测性贡献能否绑 bug）

跑前静态已核对（§2 表）；跑时逐臂采集并回答：

1. **revoke 出口**：cache 臂 M 阶段 `unfulfilled{reason="dropped"}`
   是否有增量（main 新出口）；与 `--log-level debug` 下的 revoke 日志
   行数对照——若 counter=0 而日志有 revoke 行 → 出口仍不完整（新发现）；
2. **分类缺口（#36280 绑定点）**：`/metrics` 全文 grep
   `hicache_prefetch_outcomes`（预期**不存在**；依赖面审计 §3 已证
   `log_storage_metrics` 无 outcome 导出、`prefetch_stats` 是动态注入
   属性）＋记录 14 键 `_prefetch_outcome_stats` 只能从 debug 日志侧写
   ——"事件维度分类在 main 只存在于进程内 dict"这一事实写入结果包，
   作为 #36280 重投的运行时证据（审计 §3.5 的复活条件与维护者点名
   见 §11；本实验只产出证据，不依赖该 PR）；
3. **#49226 计数器面**：确认本实验全部判定信号在 #49226 diff 中
   未改名/未新增（已静态核对：它复用 `unfulfilled` counter）——
   实验结果可直接映射到 #49226 前/后对比而无需改造客户端；
4. **阈值语义**：`below_threshold` 增量出现时，对照该 trial 的
   `storage` 值是否 < 256（`prefetch_threshold=256`，`urc:3867`）——
   验证 reason 与阈值绑定关系未漂移；
5. **buffer 臂丢弃映射**：6afee75f 的 `dropped reason=overlap`（内部
   staged-prefetch 丢弃）在 main 对应到 `already_cached` /
   `device_overlap` 哪个 reason——建立新旧事件名映射表（结果包附表），
   供两份主实验的解读复用；
6. **flush_cache 重置 metrics**：main 与 6afee75f 的 `/flush_cache`
   均 reset radix + metrics（依赖面审计 §1.2 行级核对）→ 阶段边界
   counter 增量天然是相位内增量，`host_capacity` 归属不受跨相位累积
   污染；harness 的 `counters_before`（flush 后快照）逻辑沿用，Go
   实施时确认无回归。

## 7. 服务器启动参数全集（main 参数名）

A1（salted-cache-file，等压）：

```bash
SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR=/data/hicache-l3/dualarm/A1 \
python -m sglang.launch_server \
  --model-path /home/shane/models/Qwen2.5-0.5B-Instruct \
  --host 127.0.0.1 --port 30031 \
  --mem-fraction-static 0.35 \
  --max-total-tokens 358532 \
  --enable-hierarchical-cache \
  --hicache-host-memory-mode cache \
  --hicache-ratio 0.12 \
  --hicache-io-backend kernel \
  --hicache-mem-layout page_first \
  --hicache-write-policy write_through \
  --hicache-storage-backend file \
  --enable-metrics --enable-cache-report \
  --log-level debug
```

- 臂间替换：A2/A3 同命令但 `HICACHE_HOST_MEMORY_MODE`、salt 由
  workload 侧 `--salt-mode` 控制（服务端不变）；A3/A4
  `--hicache-host-memory-mode buffer_only`；B* 把后端两行换成
  `--hicache-storage-backend fast_file` + extra-config
  `'{"storage_dir":"/data/hicache-l3/dualarm/B1","max_size":20}'`
  （键名以 main `storage/fast_file/` 接线为准——跑前审计点；
  `max_size` 取 20 GB：9 臂总写入 ≤ E 466,944×12,288 B ≈ 5.7 GB，无驱逐干扰）；
  C2 `--hicache-ratio 0.48`；
- **log-level 规则**：cache 臂（A1/A2/B1/B2/C2）`debug`（revoke 日志
  侧写分类缺口，0.5B 短运行日志量可控）；buffer 臂 `info`；
- **main flag 适配**（依赖面审计 A1/C 类）：`--disable-cuda-graph`
  已移除（REMOVED_FLAGS，传入即停机）——本实验默认开 graph 不受影响；
  若需无 graph 对照须拆为 `--disable-prefill-cuda-graph
  --disable-decode-cuda-graph`。`--enable-cache-report` 两版均只作用于
  OpenAI usage 字段，native `/generate` 的 `cached_tokens_details` 不受
  其门控（保留该 flag 仅为与 DEV-20 配置一致）；
- workload 侧：A2/B2/C2 加 `--salt-mode none --evict-content-mode
  per-request`（护栏硬检查）；A1/B1 `--salt-mode per-request`（默认）；
  全臂 `--num-trials 6 --prefix-tokens 4096 --tail-tokens 128
  --evict-factor 1.3 --expected-storage-backend <file|fast_file>
  --storage-dir <对应目录> --post-evict-wait 240 --expect-mode diagnostic`；
- 其余与 DEV-20 的差异只有：原生 Linux、无补丁、NVMe 目录、
  `--max-total-tokens` 显式钉住（DEV-20 靠 16GB 内存隐式达成同值）。

## 8. 观测信号清单

| 信号 | 来源 | 用途 |
|---|---|---|
| 每 trial R `cached_tokens_details{device,host,storage}` | workload 结果包 | D1/D2 主判据（覆盖率 + 驻留分级） |
| `sglang:storage_prefetch_unfulfilled_tokens_total{reason=...}`（collector 预建 11 label，发射点实取 9 种——依赖面审计 §1.3） | /metrics，post_write/post_evict/post_measure 分阶段快照（harness 已有） | **`host_capacity`＝②主签名（staging 分配失败，直接判据）**；`dropped`＝revoke；`below_threshold`＝截断 <256 |
| `sglang:hicache_host_used_tokens/_total`（gauge） | /metrics，E 与 M 阶段 1 Hz 采样（harness 采样器扩展） | host 水位钉死证据（②签名）；M 开始时水位门禁 |
| `sglang:backuped_tokens_total` / `hicache_backup_tokens_total` | /metrics | W 落盘证据 + drain 收敛/丢弃缺口（`backuped` 恒小于 enqueued 的差值即丢弃量） |
| `sglang:storage_prefetch_hit_tokens_total`、`prefetched_tokens` 日志行 | /metrics + server.log | prefetch 实际加载量（对照 DEV-20 的 768） |
| revoke/`prefetch success loaded=`/`fill committed`/`dropped` 日志行 | server.log（debug） | 事件栅格（V2）；新旧事件名映射（§6-5） |
| `Allocating ... host pool: N tokens` | 启动日志 | G0-1 换算核对 |
| `.bin` 快照（文件数/字节/mtime） | `storage_snapshots`（fast_file 布局审计点见 G3） | 落盘与孤儿化对照（DEV-20 的 dedup 注意事项沿用） |

harness 适配（依赖面审计 Top5 第 4 条）：workload.py 的
`_METRIC_NAMES`/`_COUNTER_NAMES` 增补 main 新指标——
`storage_prefetch_unfulfilled_tokens_total`（按 `name{reason="..."}`
逐 label 解析）、`storage_prefetch_hit_tokens_total`、
`storage_backup_failed_tokens_total`；既有 12 个指标名已核实两版存活、
`cached_tokens_details{device,host,storage}` 三键不变（审计 §1.2/§1.3）。

## 9. 可视证据采集方案（答辩用：指时间线讲根因）

**V1 分阶段水位时序（每臂必产）**

- 工具：harness 采样器（workload.py 的 `counter_snapshot` 扩展为
  1 Hz 连续采样 gauge+counter）+ matplotlib 离线出图。
- 采样范围：W 开始前 10 s → 最后一个 P 结束后 10 s（全程，含 E 的
  456 请求与 drain 等待）。
- 图面规格：x=墙钟；五条固定纵向虚线 = W 开始/结束、E 开始/结束、
  M 开始（harness 阶段边界）；通道 1 = host_used/total 水位（total
  虚线）；通道 2 = `backuped_tokens_total` 与 `hicache_backup_tokens_
  total` 双累计线（间隙 = 丢弃/在途）；通道 3 = `unfulfilled` 的
  `dropped`/`below_threshold`/`host_capacity` 阶梯；通道 4 = 每请求
  散点（W/E 灰、R 绿=storage>0 红=0、P 蓝）。
- 预期特征：②未修时 E 段水位贴满 + 双累计线出现永不闭合的间隙 +
  M 段 `dropped` 阶梯与红 R 散点同窗；②修时 E 段水位周期回落、
  M 段绿 R 散点 + 无阶梯。答辩讲法：一张图同时给出"写回钉住水位、
  drain 缺口、prefetch 被撤销、R 冷掉"的因果链。
- 出图脚本入口 `plot_v1.py <results.json>`（附录 A），零 GPU 时间。

**V2 日志事件栅格（cache 臂必产）**

- 工具：离线解析 server.log（debug 级），事件类型 = `prefetch
  success loaded=`、revoke 行、`fill committed`、`dropped`、
  `Write page ... failed`（若出现）；按时间排成栅格图（每类一行，
  事件为点），与 V1 共享 x 轴。
- 预期特征：②未修签名 = revoke 点簇紧跟 M 段开始、`loaded=` 值
  ≤999；对照 §6-1 的 counter 侧写。E 段写回 ack 稀疏（NVMe 快则
  密集——与 DEV-20 的 190 文件/s 对比本身是环境差异的量化记录）。

**V3 条件触发 kernel 剖面（规则内建，执行者不决策）**

- 触发规则：首个出现 G2"②未修"签名的臂，在其**重跑**时于 M 阶段
  开始前 `POST /start_profile {"output_dir":"/data/prof/dualarm/<arm>",
  "activities":["CPU","GPU"]}`，M 结束后 `/stop_profile`（字段名以
  main http_server.py 为准，跑前核对一次）。
- 看什么：R 请求有无 L3 回载 copy 段（copy stream）vs 纯冷 prefill
  compute 段；被撤销的 prefetch 是否表现为"查询 kernel 后无加载"。
- 预算计入总账（≈8 min，只触发一次）。

**全程旁证**：`nvidia-smi dmon -s pucmt -d 1 -o T -f dmon-<arm>.log`
每臂后台常开；H2D 流量在 M 段的有无与 V1 红绿散点互证。

## 10. GPU 时间预算与前置

| 项 | 估算 |
|---|---|
| 9 臂运行：每臂 = 启动 ~1.5 min（0.5B）+ W/E ~2–4 min（NVMe 下 E 的 456 请求显著快于 WSL2）+ drain ≤4 min + M ~2 min ≈ 8–10 min | ≈ 1.4 h |
| 烟测（G0 逐条 + fast_file 键名审计 + debug 日志量确认） | ≈ 0.3 h |
| 边界重跑 ≤2 次 + V3 条件剖面 1 次 | ≈ 0.3 h |
| **合计 GPU** | **≈ 2.0 h**（上限 2.3 h；超限砍 B3/B4，保 A1/A2/A3/C2/B1/B2） |

前置（不占 GPU）：

1. main `789abb9be` 构建（与两份主实验共享，一次构建三实验用）；
2. **libssl-dev 安装**（4090 服务器此前因缺 `<openssl/sha.h>` 启动
   失败，有留档；fast_file 臂同样依赖 native hash 扩展）；
3. 模型 `Qwen2.5-0.5B-Instruct` 已在库（DEV-20 同款，保内容寻址
   语义可比）；`/data/hicache-l3/dualarm/` 目录规划（每臂独立子目录）；
4. 客户端 harness 改造（`--expect-mode diagnostic`、`arm_verdict`、
   1 Hz 采样器、plot 脚本）——纯客户端，Go 后实施；
5. 无需任何 SGLang 补丁（原生 Linux；6afee75f 的 WSL2/buffer
   workaround 不携带，若 main 复现崩溃按 G3 记录）。

## 11. 与两份主实验及上游的关系

- **→ repro-49221**：若 G2 判"②未修"，#49221 的 burst 判定臂将继承
  更尖锐的背景（等压下 staging 饥饿与 admission 窗口叠加），其 §6
  隔离设计需启用"host_capacity/dropped 分流"分支；若"②修"，#49221
  可放心把 burst shortfall 归因于 admission 窗口本体。
- **→ repro-47481**：buffer 臂（A3/A4/B3/B4）的驻留分级是 #47481
  H_chain（首失截断）在 file/fast_file 侧的前导测量——若 main 已全量
  驻留，#47481 主实验的 H_chain 预期要下调；若仍 ~1/3，主实验的
  V2 时间线设计获得本地校准数据。
- **→ #36280（open，CI 红；审计结论：有条件复活、与 bench 复跑解耦）**：
  实验 0 的 §6-2 产出（14 键事件分类只存在于进程内 dict 的运行时证据
  + `revoked_*`/`host_capacity` 签名的实际诊断价值）是该 PR 重投的
  弹药；维护者 xiezhq-hermann 2026-11-10 在 #36474 评审中点名 #36280
  为事件计数导出的协调点（"please coordinate with #36280, which does
  exactly that"）。注意其 diff 硬编码 6 键已过期（main 为 14 键），
  重投须按审计 §3.3/§3.5 重构（squash、遍历快照键、与 #37503 token
  维度分工、修 CI）；本实验不依赖该 PR。
- **→ #49226（open，main 未含）**：实验 0 验证其合入前基线；"②未修
  + 剂量臂容量边界型"的组合 = 其 PrefetchAdmissionBudget FIFO 排队的
  GPU 级动机证据；"②修"则其价值收窄为 admission 窗口（#49221 本体），
  可在评论中提供减灶依据。公开评论前按 PLAN.md P4 重查两 PR 状态。

## 12. 边界与风险

1. main 的 cache 模式"enqueue 即预留 requested span"（`_prefetch_
   occupied_span`）可能使 43k 池在 M 段单 prefetch（4,224）也放不下
   之外的情况——若 A2 出现 `host_capacity` 主导而非 `dropped`，判定
   按 §6-4 记录 reason-阈值绑定并归入"②未修（分配路径形态）"，不与
   6afee75f 签名强行对齐。
2. NVMe 使 drain 大幅快于 WSL2 → ② 的 E 段钉住可能自然减弱。这是
   **环境不可完全复刻**的部分：等压设计控制了容量变量，控制不了
   ack 时延。若因此 G1 成立，结论措辞为"在 NVMe 时延下未复现"，
   并把 WSL2 慢盘作为已知触发条件写入边界（必要时加一个
   `--post-evict-wait 0` 的无 drain 对照臂，预算允许时）。
3. fast_file 页布局可能非单 token `.bin`（vectored IO），`storage_
   snapshots` 语义变化 → G3 降级路径已备；其 `max_size` 水位驱逐在
   本实验数据量（≤5.7 GB vs 20 GB）下不应触发，若触发即配置错误。
4. `--max-total-tokens` 与 mem-fraction 的相互作用以 `/server_info`
   实测 token_capacity 为准（G0-1）；若 main 对 `max-total-tokens`
   有下限校验导致 358,532 被抬高，臂表全部按实测容量重算 E
   （evict-factor 机制自动适应），只保 host ratio=0.12 的等压关系。
5. debug 日志量：E 段 456 请求 × per-page 日志可能达 GB 级——跑前
   在烟测中实测单请求日志体积，若磁盘压力大则仅对 M 段窗口保留
   debug（重启服务切 info 的替代方案记入 runbook，不临时决策）。
6. 本实验不回答 #49221 的 admission 窗口本体、不测 mooncake、
   不做性能结论（DEV-20 的 16.8–32.6× TTFT 比是路径延迟描述，
   main 上重测仅记录不判定）。
7. `host_capacity` 的发射点覆盖"命中时分配失败"与"enqueue 期 span
   预留受限"两类路径（urc 多点）——烟测阶段先以人为缩池
   （`--hicache-ratio 0.05` 单次启动）确认该 reason 可触发且与 debug
   日志对得上，再进入正式臂（规则内建，执行者不临时决策）。

## 附录 A：runbook 骨架（草稿，不执行）

```python
# bench/repro-main-dualarm/run_matrix.py（Go 之后创建）
ARMS = [
  # (arm, salt_mode, host_mode, ratio, backend, log_level)
  ("A1", "per-request", "cache",      0.12, "file",      "debug"),
  ("A2", "none",        "cache",      0.12, "file",      "debug"),
  ("A3", "per-request", "buffer_only",0.12, "file",      "info"),
  ("A4", "none",        "buffer_only",0.12, "file",      "info"),
  ("B1", "per-request", "cache",      0.12, "fast_file", "debug"),
  ("B2", "none",        "cache",      0.12, "fast_file", "debug"),
  ("B3", "per-request", "buffer_only",0.12, "fast_file", "info"),
  ("B4", "none",        "buffer_only",0.12, "fast_file", "info"),
  ("C2", "none",        "cache",      0.48, "file",      "debug"),
]
# 每臂：起服务（§7 模板 + 臂参数）→ /health → G0 核对 →
#   workload.py --num-trials 6 --prefix-tokens 4096 --tail-tokens 128
#     --evict-factor 1.3 --expected-storage-backend <backend>
#     --salt-mode <salt> [--evict-content-mode per-request]
#     --storage-dir <dir> --post-evict-wait 240 --expect-mode diagnostic
#   （1 Hz 采样器并行写 samples-<arm>.jsonl，供 V1）
# → arm_verdict 计算（G1/G2 规则内置于审计器）→ 关服务 → 下一臂。
# 边界重跑与 V3 触发规则见 §5/§9（自动判定，无人工决策）。
# 产物：results-<arm>-<UTC>.json ×9 + samples ×9 + server logs +
#   plot_v1.py / plot_v2.py 出图 + arm_matrix.md 判定矩阵汇总。
```

---

## 2026-11-21 补充约束（来自 main 链路文档 docs/03-hicache-dataflow-main.md 的核对发现）

执行本设计前必须确认以下三点，均为 e183c415b 实测源码结论：

1. **`--hicache-host-memory-fraction` 默认已是 0.8（非 None）**（arg_groups/fields/memory.py:128-131）：
   main 上 host 池默认走自动定容（本机 1TB RAM → 池会非常大）。压力臂的等效换算
   （§5 的 ratio 压缩）**必须显式设置 `--hicache-size-ratio` 或 `--hicache-size`**——
   显式给值时 hicache_hook.py:132-141 会折回 None 走旧路径，正是我们需要的控制方式。
   剂量对照臂同理。启动日志须记录实际生效的池容量（换算 ±2% 核对）。
2. **fast_file 与 unified key scheme 互斥**（fast_file_store.py:105-108）：fast_file 臂
   不得启用 unified key scheme；若 harness 默认带出该 flag，fast_file 臂需显式关闭。
   file 臂不受限。
3. **unfulfilled reason 预注册为 12 值且有两处 label 失配**（metrics_collector.py:2336 附近）：
   `unservable_prefix`、`shrunk` 有发射点但不在预注册集合（抓取时勿被预注册名单误导，
   harness 的动态解析已兼容）；`aux_window_trim`、`device_overlap` 预注册但无发射点
   （预期恒零，不应作为判据）。
