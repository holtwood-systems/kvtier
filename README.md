# kvtier — KV cache tiering for LLM inference（推理 KV 分层存储）

> LLM 推理 KV 在 L1 (GPU HBM) / L2 (host DRAM) / L3 (storage) 间的分层存储、
> 回载与策略实验。对应 SGLang 的 HiCache（`--enable-hierarchical-cache`）
> 与 vLLM KV offloading；HPC 的 memory hierarchy 功力在 AI 推理场景的直接迁移。

> **项目状态**：**active**（研究演进中；已有完成审计的单卡 L2 结果包，
> 尚无已合入的上游 PR）。权威状态注册表见
> [holtwood-systems/portfolio](https://github.com/holtwood-systems/portfolio)。

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

1. 完成至少一组真实 GPU 实验，并通过结果包审计——**已达成（2026-10-26）**：
   WSL2 / RTX 3060 Laptop / Qwen2.5-0.5B，6 轮，L2 回载 72.3 ms vs 冷
   prefill 338.0 ms，R/P 中位比 0.215，审计通过；证据见
   [`bench/repro-31600/evidence/2026-10-26-wsl2-l2-baseline/`](bench/repro-31600/evidence/2026-10-26-wsl2-l2-baseline/)。
   该结果只描述该机、该模型、该配置，不是 #31600 复现。
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

## 可执行任务

| 优先级 | 任务 | 当前边界 |
|---|---|---|
| 1 | 单卡 HiCache L2 回载微基准 | 2026-10-26 已完成 6 轮审计结果（0.5B / WSL2）；不是 #31600 复现 |
| 2 | 按原配置复现 #31600 | 需要 GLM-5.2、FP8、EAGLE、L2+Mooncake、TP/CP，并比较 accept rate、decode throughput 和总时长 |
| 3 | HiCache 多卡同步与 host 内存局部性 profiling | 目前只是测量方向；不能先验宣称 all-reduce 或 NUMA 是瓶颈 |
| 4 | L3 prefetch 失败分类可观测性 | 已有大量传输指标；剩余候选是有限枚举的失败原因和结构化日志 |
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
