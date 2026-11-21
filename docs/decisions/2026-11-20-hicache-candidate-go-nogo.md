# HiCache 候选问题 Go/No-Go 报告（骨架 v0.5，2026-11-20）

> 状态：**v1.0（完整证据版，2026-11-20）**。五路证据齐备：上游调研（§3）、静态
> diff（§4）、兼容审计（§5）、实验设计包（§7）、迁移基线（§1）。提交所有者做
> Go/No-Go 决策。main 环境预建仍在服务器进行，不阻塞决策。

## 0. 决策问题

从候选池中选**至多一个**问题进入源码修复阶段（D4–9：复现 → 定位 → 最小修复 →
回归 + A/B），还是全部 No-Go、收敛为根因分析报告路线。

## 1. 环境与硬件事实（已定）

| 项 | 值 | 证据 |
|---|---|---|
| GPU | RTX 4090 48GB 改装（49140 MiB，sm_89），驱动 550.107.02 | nvidia-smi |
| 显存稳定性 | **ALL PASS**：43×1GiB 图案填充 + GEMM 混合压力 25 min（22147 轮），2 次中途校验 + 终验零位损坏；D2D 937 GB/s；峰值 74°C / 352.7W / 无异常降频 | `/data/stability/vram_stress.log` + `monitor.csv`（服务器） |
| 平台约束 | Ubuntu 22.04 容器：无 dmesg（Xid 不可观测）、`/` 40G、`/data` 500G NVMe（L3 实验唯一合法路径）、宿主内存 1TB 但共享（L2 带宽实验需邻居噪声评估） | 本机盘点 |
| 已修复 | 容器 CUDA Error 804：镜像将 `/usr/local/cuda-12.8/compat`（570 前向兼容库）置于 ldconfig 首位，屏蔽 550 匹配库；已禁用该条目（`.bak-disabled`）。**任何新 env 不得把 compat 目录加回 LD_LIBRARY_PATH** | 本机修复记录 |
| 工具链 | nvcc 12.8（V12.8.61）在 `/usr/local/cuda-12.8`；服务器 mihomo 代理 `127.0.0.1:8894`（HK 出口，境外资源优先） | 本机验证 |
| 既有模型 | Qwen2.5-0.5B-Instruct（迁移基线用）、Qwen2.5-7B-Instruct-AWQ、Qwen3.5-27B-FP8（**linear-attention 混合架构，不用于 full-attention 结论**） | `/root/models`、`/model` |
| 迁移基线（11-05 对照 R/P 0.375） | **通过**：R/P **0.4817**（0.3~0.5 区间），host 命中 8/8×4223/4224 与 11-05 完全一致，`make audit` 通过；绝对 TTFT 偏慢（52.8/109.4 ms vs 24.4/65.1 ms）主候选归因=同机并发租户活动，如实记录未重跑。⚠️ 实测 **3,568 MiB 宿主侧不可见显存占用**（device pool 1,434,030 vs 1,555,940 tokens）——**实验 0 每次测量前必须核验 GPU 独占** | `bench/repro-31600/evidence/2026-11-20-4090-migration-l2baseline/` |

## 2. 准入标准（不可妥协）

来源：上游 PR #36115 关闭评论（"只欢迎挂在具体 bug 修复或行为变更上的贡献"）
+ 任务书第 5 条决策门槛。

| # | 门禁 | 判定方式 |
|---|---|---|
| G1 | 问题可稳定复现 | 设计包给出量化判定门禁 + 复现成功率 |
| G2 | 能定位到源码/时间线具体原因 | 静态 diff 报告 + 复现时的计数器证据链 |
| G3 | 当前上游未解决 | issue 状态 + main 代码核对（静态 diff 报告） |
| G4 | 存在可验证的最小改进方案 | 修复草图 ≤ 200 行 + 回归测试方案 |
| G5 | 实施成本符合时间盒 | 剩余预算 ≤ 11 天内可完成 A/B |

## 3. 候选池与初筛（上游调研已定）

完整清单见本目录引用的调研记录。按"证据强度 × 修复可行性 × 占坑状态"初筛：

| 候选 | 一句话 | 占坑 | 初筛 |
|---|---|---|---|
| #49221 | L3 prefetch 命中的 host pages 在 admission 前被 L2 驱逐（并发命中 188K→64K，shortfall +88K） | **报告者已开修复 PR #49226**（pin until admission + FIFO 容量排队，stack 于 #48848） | 降级为**独立复现证据线**（G3 破：上游在修，不抢修复） |
| **L3 链路 main 验证** | 11-11 DEV-20 双臂已**机制确认**双层根因：① cache 模式 prefetch 插入以 anchor=root 的 `cache_salt=None` 命名，salted re-match 不可见（nosalt 臂对照证实）；② prefetch staging 与写回竞争同一 host 池 → 饥饿 + `_revoke_pending_prefetch` 静默撤销。main 已修 ①（Agent-3 证实）、重构 ② 区域（命中时分配 + 仲裁层）→ **在 main 复跑双臂验证修复完整性；若 salted 臂仍失败或饥饿仍现，即天然满足 G1–G3 的真 bug** | 自家 DEV-20 harness | **升为首选低成本线**（与 #49221 同域） |
| #47481 | 容量压力下命中率骤降 87%→64% | 维护者参与，明确未解决 | 备选（依赖 bench 设计） |
| #50790 | write_back evictable_size 计入不可驱逐 KV → scheduler OOM | 已有 2 个修复 PR 在飞 | **不介入**（G3 存疑） |
| 无界队列 | write/prefetch 队列无 maxsize（main 仍如此） | 无 issue 症状记录 | 暂缓：缺 G1 用户可见症状 |
| #46952 | write_back 驱逐阻塞 scheduler 数百 ms | #47126 可能部分缓解 | 备选（需 GPU 验证残留） |
| cudaHostRegister 集群 | #50015/#39744/#46799/#43375 | 3 个竞争 PR | 暂缓（除非 #51731 失败） |
| #45400 | 混合 GDN host mamba 池耗尽 | 活跃 | 与 full-attention 主线不可比，暂缓 |

## 4. 代码路径状态交叉（静态 diff 已定）

完整报告：`docs/upstream/2026-11-20-memcache-diff-6afee75f-to-main.md`（main tip `789abb9be`）。

对候选池的直接含义：

- **cache_salt 归属已修**（scheduler 显式传 `req.cache_salt` + 命名空间断言）→ 相关旧议题关闭，不重复提交。
- **#49221 的代码区域刚被 main 大改**：prefetch 暂存"命中时分配"、capacity/prefill_budget 仲裁层、`--hicache-host-memory-fraction` 自动 sizing、L3 miss 重试梯（默认 8 次，超限放行 admission）→ **"main 上是否仍复现"是首个必答实验，也是最高信号量动作**。
- ~~selective 策略门控假说~~ **已被 11-11 证据否证**：DEV-20 各臂均为 `write_through` 仍 `index_evicted`，真根因是 salt 归属 + staging 饥饿（双臂机制确认）。selective 门控（unified_tree_core.py:645/1272）仍作为一般性陷阱记入实验设计约束。
- **静默撤销可观测性缺口**：`_revoke_pending_prefetch` 仅 debug 日志——与搁浅的 PR #36280（prefetch outcome metrics）直接对接，也是把观测性贡献"绑到 bug 上"的天然挂钩（满足 #36115 教训的准入形态）。
- 新 `fast_file` 后端（readv/writev、原子发布、双水位驱逐）→ 确诊实验顺带产出 file vs fast_file 增量对比。
- `hiradix_cache.py` 已删除、HiCache 并入 UnifiedRadixCache → kvtier docs/02-hicache-dataflow.md 需标注过时，面试链路图必须按 main 重画。

## 5. 兼容性与重基线风险

bench 依赖面审计（**已完成**，报告：`docs/upstream/2026-11-20-bench-surface-audit.md`，
锚点 main `e183c415b`，与 `789abb9be` 结论一致）：

- 三级分类：**A 直接报错 2 项**（`--disable-cuda-graph` 已入 REMOVED_FLAGS → 换
  `--disable-{prefill,decode}-cuda-graph`；WSL2 host-alloc 补丁对 main 失效——
  4090 原生 Linux 服务器不受影响，仅 WSL2 机器需要）+1 风险项；**B 静默变义 5 项**
  （salt 已修、暂存命中时分配、重试梯 8/8 等——即 §4 的机制重校准）；**C 无影响
  34 项**（12/12 Prometheus 指标名与 pool 标签、7/7 端点与 `cached_tokens_details`
  字段全部存活）。
- **重要增量**：main 新增 `storage_prefetch_unfulfilled_tokens_total{reason}`
  （#37503 族；**预注册 12 值且有两处 label 失配**——`unservable_prefix`/`shrunk`
  有发射无预注册，`aux_window_trim`/`device_overlap` 反之，详见 03 号链路文档附 B）
  ——workload.py 指标名单必须扩充，否则结果包丢失失败归因证据；staging 饥饿验证
  可直接用 `reason=host_capacity` 计数，不再需要间接推断。

已登记风险：
- open-infra-ai 克隆工作区含 kvtier 自有未提交补丁（`SGLANG_HICACHE_HOST_ALLOC=pin`），
  rebase 需手工合并，引用基线代码时不得混入；
- 服务器可被重置（11-06 后已发生一次）：实验产物当日离机；
- GPU 与 ComfyUI 生产共用：测量窗口双向独占。

## 6. 预算与时间盒

- 总时间盒至 2026-12-03（D14 硬停止）；今日为 D1。
- GPU 窗口：稳定性已过（~35 min 已耗）；基线 + 复现实验预算合计 ≤ 20 GPU·h。
- 求职动作并行，不为本报告阻塞。

## 7. 建议（草案，待三路数据后定稿）

**倾向方案（v0.6 修订，吸收 11-11 DEV-20 证据与 #49226 情报）**：
1. **主攻：L3 链路 main 双臂验证**（DEV-20 复用：salted vs saltless × cache vs
   buffer_only，write_through，file/fast_file）——salt 归属 main 已修、staging
   区域 main 已重构，双臂复跑直接判定修复完整性；任一臂仍现 index_evicted/
   饥饿 = 天然满足 G1–G3 的修复候选（自家 harness 现成，预算 ~2h GPU）。
2. **次选：#49221 独立复现证据**（~4h GPU，Qwen3-4B 判定 + 0.5B 快扫）——为
   #49226 提供独立复现/不复现证据与剂量刻画，不做修复。
3. **观测性挂钩（范围收窄）**：main 的 #37503 已提供 token 维度
   `unfulfilled{reason}`（9 种），缺口收窄为 revoke/decline 的**事件计数导出**
   （revoked_insufficient / revoked_full_miss / declined_rate_limited）——#36280
   重投（squash + 以 #37503 为基）有维护者点名信号，可作独立小贡献推进。

**No-Go 条款**：若双臂在 main 全通过且 #49221 不复现，则收敛为
"HiCache 演进分析 + 修复验证报告"路线，不强行改码。

## 8. 证据索引

- 上游调研（8 候选 + #36115 复盘）：本会话调研记录（将随 v1.0 归档入 docs/upstream/）
- 静态 diff：`docs/upstream/2026-11-20-memcache-diff-6afee75f-to-main.md`
- 稳定性测试：服务器 `/data/stability/vram_stress.log`、`monitor.csv`（待归档离机）
- 复现实验设计：`docs/bench-design/2026-11-20-repro-main-dualarm.md`（**实验 0，首选**）、`...-49221.md`、`...-47481.md`
- 硬件稳定性证据：`bench/repro-31600/evidence/2026-11-20-4090-hw-stability/`（ALL PASS，含监控边界声明）
- 11-11 机制证据：`bench/repro-31600/evidence/2026-11-11-wsl2-rtx5070ti-l3-cache-nosalt/`、`...-l3-buffer/`、`...-l3-file/`
- vLLM 对比：`docs/interview/sglang-vs-vllm-kv-offload.md`
- 数字弹药库：`docs/interview/numbers-bank.md`、war story：`docs/interview/war-story-01-cuda-804.md`
- bench 依赖面审计：`docs/upstream/2026-11-20-bench-surface-audit.md`（main `e183c415b`）
- 迁移基线：`bench/repro-31600/evidence/2026-11-20-4090-migration-l2baseline/`（R/P 0.4817，audit 通过，含环境复建偏离项记录）
