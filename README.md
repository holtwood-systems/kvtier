# kvtier — KV cache tiering for LLM inference（推理 KV 分层存储）

> LLM 推理 KV 在 L1 (GPU HBM) / L2 (host DRAM) / L3 (storage) 间的分层存储、
> 回载与策略实验。对应 SGLang 的 HiCache（`--enable-hierarchical-cache`）
> 与 vLLM KV offloading；HPC 的 memory hierarchy 功力在 AI 推理场景的直接迁移。

> **项目状态**：**active**（研究演进中；已有完成审计的单卡 L2/L3 结果包矩阵，
> 尚无已合入的上游 PR）。权威状态注册表见
> [holtwood-systems/portfolio](https://github.com/holtwood-systems/portfolio)。

## 亮点速览（30 秒版）

**一句话定位**：把 HPC memory hierarchy 的方法论迁移到 LLM 推理——围绕
SGLang HiCache 的 KV cache 三层存储（GPU HBM → host DRAM → storage）做
可复现微基准、负结果留档与上游贡献。

**核心实测（全部通过 `validate_results.py` 审计，绑定 SGLang commit / 模型 SHA / 启动参数）**：

- **L2 回载明确快于等长冷 prefill**：RTX 4090 上 8/8 轮 100% host 命中，
  回载 TTFT 中位 **24.4 ms** vs 冷 prefill **65.1 ms**，配对 R/P 中位比 **0.375**
  （[`2026-11-05-l2-baseline`](bench/repro-31600/evidence/2026-11-05-linux-rtx4090-l2-baseline/README.md)）。
- **IO 后端消融**：`hicache-io-backend=direct` 的 R/P 比值 **0.988**（回载几乎无收益）
  且写入 TTFT 1631.9 ms vs kernel 后端 65.6 ms——把「为什么 HiCache 要做 kernel IO」
  反向量化为约 **2.6× 回载优势**（[`io-direct`](bench/repro-31600/evidence/2026-11-05-linux-rtx4090-io-direct/README.md)）。
- **FP8 KV 消融**：`kv-cache-dtype=fp8_e4m3` 使 device 容量 1.56M→**3.11M tokens 翻倍**，
  HiCache 链路功能无损，R/P 仅劣化到 0.392（约 +4%）
  （[`kv-fp8`](bench/repro-31600/evidence/2026-11-05-linux-rtx4090-kv-fp8/README.md)）。
- **CUDA graph 消融**：关 graph 后 R/P 0.375→0.416（约 +11% 劣化）、写入慢 22%，
  机制结论不依赖 graph 配置
  （[`no-graph`](bench/repro-31600/evidence/2026-11-05-linux-rtx4090-no-graph/README.md)）。
- **L3 负结果如实归档**：`storage-backend=file` 在该负载形状下 8 个 W 前缀未在
  L3 索引中幸存，三层全 miss，基准按契约标 `failed` 并保留——这是方法论的一部分，
  不是失败被藏起来（[`l3-file`](bench/repro-31600/evidence/2026-11-05-linux-rtx4090-l3-file/README.md)）。
- **跨平台根因定位**：WSL2 上 HiCache 回载必现 `cudaErrorIllegalAddress`，
  定位到 `cudaHostRegister` host/device 指针非恒等，按上游 PR #43375 打本地补丁绕过；
  原生 Linux 无需补丁开箱可用（[`WSL2 诊断`](bench/repro-31600/evidence/2026-10-26-wsl2-l2-baseline/README.md)）。

本仓是研究孵化器：把上游源码阅读收敛为可运行实验、可审计结果和上游贡献。
2026-10-25 经所有者决定提前转为公开并登记入组织状态注册表（`active`）；
下方「公开门槛与现状」诚实记录各条达成状态，公开决策记录见
[portfolio 仓笔记](https://github.com/holtwood-systems/portfolio/blob/master/.agents/notes/implemented/process/2026-10-25-kvtier-radar-publication.md)。

## 当前目录结构

- `docs/` — SGLang/vLLM 架构分析、上游 PR/issue 工作记录
- `bench/repro-31600/` — 单卡 HiCache L2 微基准（客户端、结果审计器、离线测试）
- `patches/` — 按上游 commit 绑定的补丁与差异文件
- `contrib/` — 上游贡献的最小复现、测试或草案，当前包含 minisgl
- `third_party/` — 只读研读的上游源码浅克隆（sglang 等），不入库

## 战略主线

1. **上游贡献**：主攻 SGLang（切入点为 HiCache 子系统），vLLM 作为第二熟悉对象。
2. **差异化项目**：CPU 侧 KV 分层存储优化，将 HPC 内存层级优化经验迁移到推理场景。
3. **知识体系**：推理原理 + 引擎源码 + 适量 CUDA（会读、会 profile、会写中等 kernel）。

阶段任务、验收条件和停止条件见 [`PLAN.md`](PLAN.md)。

## 完成边界

- 源码阅读只能支持"实现如何工作"或"值得测量的假设"，不能替代性能数据；
- 单卡 L2 微基准只能说明该模型和配置下的 host 回载表现，不能自动复现多卡、L3、
  EAGLE 场景；
- 只有真实 GPU 运行、缓存路径核验和结果包审计完成后，才可以对外给出性能结论；
- 上游 issue/PR 的状态会变化，公开评论前必须再次核对当前 `main` 和重复工作。

## 公开门槛与现状

本仓原计划满足以下条件后才转公开；2026-10-25 经所有者决定提前公开，
各条现状诚实标注：

1. 完成至少一组真实 GPU 实验，并通过结果包审计——**已达成，且已扩展为矩阵**：
   - 2026-10-26：WSL2 / RTX 3060 Laptop / Qwen2.5-0.5B，6 轮，L2 回载 72.3 ms
     vs 冷 prefill 338.0 ms，R/P 中位比 0.215（需 PR #43375 本地补丁）；
   - 2026-11-05/06：原生 Linux / RTX 4090 五组——kernel L2 基线（R/P 0.375，
     8/8 轮 100% host 命中，**无补丁**）、io-backend=direct（0.988）、
     kv fp8_e4m3（0.392，device 容量翻倍）、disable-cuda-graph（0.416）、
     L3 file（负结果，status=failed 保留）；
   证据索引见上文「亮点速览」，全部通过 `validate_results.py`。
   这些结果只描述各自的环境与配置，不是 #31600 复现。
2. 固定上游 commit、模型、硬件、启动参数和原始日志——**已就绪**：结果包记录
   40 位 SGLang commit、模型哈希、GPU、启动参数、原始响应与 Prometheus 指标；
   本次运行另含一个未合入上游 WSL2 修复的本地补丁，已在证据中标注。
3. 至少一个外部可验证的上游结果——**部分达成**：
   [PR #36116](https://github.com/sgl-project/sglang/pull/36116) 处于 OPEN、
   [PR #36115](https://github.com/sgl-project/sglang/pull/36115) 未合入关闭、
   [#35808 评论](https://github.com/sgl-project/sglang/issues/35808#issuecomment-5396206747)
   已发布；尚无已合入的 PR。
4. 移除个人绝对路径——2026-10-25 公开批次已清理（发布命令草稿保留在
   `docs/` 中作为流程记录，路径已相对化）。

## 当前技术基线

SGLang 自 [PR #35081](https://github.com/sgl-project/sglang/pull/35081) 起默认使用
`UnifiedRadixCache`。启用 `--enable-hierarchical-cache` 后，HiCache 作为 L2/L3
能力附着在这棵统一前缀树上。`HiRadixCache` 仍在代码中，但不应再写成默认架构。

建议按以下顺序阅读：

1. `mem_cache/unified_radix_cache.py`：前缀树与 HiCache 的统一入口；
2. `mem_cache/unified_cache/`：树组件、FULL/SWA/MAMBA 状态及 storage attachment；
3. `managers/cache_controller.py` 与 `mem_cache/l2_transfer.py`：L2/L3 I/O 编排；
4. `mem_cache/pool_host/`：不同 KV 布局的 host pool 与传输分支；
5. `observability/metrics_collector.py`：已有指标，避免重复提案。

## 分层架构与基准数据流

本仓测量的是下面这条链路（以 SGLang `UnifiedRadixCache + HiCache` 为对象）：

```
                    ┌────────────────────── token 前缀树（UnifiedRadixCache）────────┐
                    │                                                                │
  W (write_through) │  L1  GPU HBM (device KV pool, mem-fraction-static)             │
 ─────────────────► │      └─ 容量满 → E (eviction) ──► L2 host DRAM (host pool)     │
                    │                                    └─ L3 storage (file 等)     │
  R (reload)        │                                          │                     │
 ◄───────────────── │ ◄── L2 回载（hicache-io-backend = kernel / direct）◄────────┘
                    │                                                                │
  P (paired cold)   │  与 R 等长 input_ids、独立 cache_salt 的冷 prefill（对照组）   │
 ◄───────────────── │                                                                │
                    └────────────────────────────────────────────────────────────────┘
```

基准的 W/E/R/P 四阶段与审计器契约（`bench/repro-31600/`）：

| 阶段 | 动作 | 审计要求 |
|---|---|---|
| W | 写入前缀（write_through 备份到 host） | Prometheus backup counter 正增量 |
| E | 制造超容量驱逐流量（按 `/server_info` 实际 token_capacity 生成） | eviction counter 正增量 |
| R | 回载：host 命中必须可验证 | `cached_tokens_details.host` ≥ 阈值（如 4223/4224） |
| P | 独立 cache_salt 的等长冷 prefill | host 命中必须为 0 |

**核心指标**：配对 R/P TTFT 中位比——<1 表示 L2 回载快于等长冷 prefill，
比值即分层收益的直接度量。所有原始响应与 counter 增量写入 JSON 结果包，
`validate_results.py` 逐条复核，`failed` 结果保留并如实标注。

## 面试考点映射

| 高频考点 | 本仓落点（文档 / 证据 / 代码） |
|---|---|
| **KV Cache 显存公式** `2 × layers × kv_heads × head_dim × seq × batch × dtype_bytes` | 基准按 `/server_info` 实测 `token_capacity` 反推 device 容量，并据此生成精确的驱逐流量（E 阶段）；fp8 组直接展示 dtype_bytes 减半 → 容量翻倍（1.56M→3.11M tokens） |
| **prefix caching / radix tree**（前缀共享与命中计费） | [`docs/02-hicache-dataflow.md`](docs/02-hicache-dataflow.md)：`UnifiedRadixCache` 前缀树、cache namespace/salt 隔离、命中如何计入 `cached_tokens_details` |
| **KV offloading 与分层**（SGLang HiCache ↔ vLLM KV offloading/connector） | [`docs/01-sota-survey.md`](docs/01-sota-survey.md) 对比两引擎的分层与 offloading 边界；本仓 L2/L3 消融是其实测面 |
| **host↔device 带宽为什么重要**（pinned memory、kernel copy vs 张量搬运） | io-direct 消融：direct 后端 R/P≈0.988（回载无收益）、写入慢 25×，量化「kernel IO 后端存在的理由」 |
| **FP8 KV cache**（容量 vs 精度/带宽权衡） | kv-fp8 组：容量翻倍、回载比仅 +4% 劣化；未覆盖 e5m2/mxfp4 与更大模型（边界如实标注） |
| **CUDA graph 与调度开销** | no-graph 组：关 graph 使 R/P 0.375→0.416、写入慢 22%——graph 影响的是每步固定开销，不改变分层机制结论 |
| **跨平台/驱动层问题定位** | WSL2 `cudaHostRegister` host/device 指针非恒等 → 回载 illegal address 的根因链与 PR #43375 对照（诊断脚本与失败运行全部留档） |
| **负结果方法论** | l3-file 组：结构合法但 status=failed 的结果包原样保留，附「不能据此断言 file 后端不保留数据」的解读边界 |
| **PD 分离 / EAGLE 等长上下文场景** | [`PLAN.md`](PLAN.md) 的 #31600 复现任务（GLM-5.2 + FP8 + EAGLE + L2/Mooncake + TP/CP）明确列为未做，不假装已复现 |

## 可执行任务

| 优先级 | 任务 | 当前边界 |
|---|---|---|
| 1 | 单卡 HiCache L2 回载微基准 | 已完成：2026-10-26 WSL2（6 轮，R/P 0.215）+ 2026-11-05/06 原生 Linux 4090（kernel/direct/fp8/no-graph/L3-file 五组矩阵）；不是 #31600 复现 |
| 2 | 按原配置复现 #31600 | 需要 GLM-5.2、FP8、EAGLE、L2+Mooncake、TP/CP，并比较 accept rate、decode throughput 和总时长 |
| 3 | HiCache 多卡同步与 host 内存局部性 profiling | 目前只是测量方向；不能先验宣称 all-reduce 或 NUMA 是瓶颈 |
| 4 | L3 prefetch 失败分类可观测性 | 已有大量传输指标；剩余候选是有限枚举的失败原因和结构化日志。L3 file 首组为负结果（前缀未在 L3 索引幸存），正向证据需重新设计（小 evict 量、明确 backup 触发点或 buffer_only host 模式） |
| 5 | KV 换出压缩 | 需先建立 correctness 与带宽基线，再判断是否值得实现 |

## 文档

- [`docs/01-sota-survey.md`](docs/01-sota-survey.md)：SGLang/vLLM 当前架构与 issue
  边界；
- [`docs/02-hicache-dataflow.md`](docs/02-hicache-dataflow.md)：当前
  `UnifiedRadixCache + HiCache` 数据流及 #31600 分析；
- [`docs/03-nano-vllm-walkthrough.md`](docs/03-nano-vllm-walkthrough.md)：nano-vLLM
  调度、块管理与 SGLang 对照；
- [`bench/repro-31600/README.md`](bench/repro-31600/README.md)：单卡 L2 微基准说明。

## 状态

- [x] 2026-08-25：重新核对 SGLang `main`，修正默认缓存架构、#31600 根因范围、
  HiCache 指标和 nano-vLLM 映射；
- [x] 2026-08-25：重构单卡 L2 微基准，加入精确 token、cache namespace 隔离、
  L2 命中验证、指标交叉验证和 JSON 结果包；
- [x] 2026-08-28：建立根级 Git 边界、客户端依赖与 `make test` 入口；
- [x] 2026-08-28：加入结果包审计器，正式运行要求记录 40 位 SGLang commit；
- [x] 2026-10-26：新增 CPU-only CI（`make test`，14 项离线测试）；
- [x] 2026-10-26：完成并审计单卡 L2 基线（WSL2 / 3060 / Qwen2.5-0.5B，6 轮，
  R/P 中位比 0.215）；定位 WSL2 `cudaHostRegister` 指针非恒等导致的 HiCache
  回载崩溃，并按上游 PR #43375 应用本地补丁；
- [x] 2026-11-05/06：原生 Linux / RTX 4090 五组消融矩阵全部审计——kernel L2
  基线（R/P 0.375，8/8 轮 4223/4224 host 命中，无需任何补丁）、io-backend=direct
  （0.988，写入慢 25×）、kv fp8_e4m3（0.392，device 容量 1.56M→3.11M tokens）、
  disable-cuda-graph（0.416）、L3 file（负结果 status=failed 保留）；另留档
  L3 file 首轮启动失败的 openssl 系统依赖缺失记录；
- [ ] 若要回应 #31600，另行搭建尽量匹配原报告的多卡 EAGLE/L3 实验；
- [x] 已提交 [PR #36115](https://github.com/sgl-project/sglang/pull/36115)；本轮未重复审核；
- [x] 已提交 [PR #36116](https://github.com/sgl-project/sglang/pull/36116)；本轮未重复审核；
- [x] 已发布并更正
  [#35808 评论](https://github.com/sgl-project/sglang/issues/35808#issuecomment-5396206747)，
  本地证据见 `docs/issue-35808-comment-draft.md`。

## 持续开发入口

主线按"可复现实验 → 结果审计 → 数据驱动优化"推进。单卡 HiCache 基准的客户端依赖、
测试和结果包审计说明位于 `bench/repro-31600/`；完整的阶段计划、任务清单和验收条件
见 [`PLAN.md`](PLAN.md)。

```bash
make test                                   # 离线测试（客户端隔离环境）
make audit RESULT=results/results-<UTC>.json  # 结果包审计
```

真实 GPU 运行前，必须固定 SGLang commit、模型、GPU 和启动参数；真实结果通过结果审计
后，才进入下一项 CPU 侧 KV 分层优化或上游贡献评估。
