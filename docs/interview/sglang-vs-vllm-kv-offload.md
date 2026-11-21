# SGLang vs vLLM：KV Cache 分层/卸载架构对比（面试准备）

> 调研日期 2026-11-20，纯桌面调研。SGLang 侧采用本仓已核实事实库（UnifiedRadixCache / Cache Linker 体系）；vLLM 侧来自官方 docs/blog/RFC（截至 dev 文档 v0.33.0rc3）与 DeepWiki 代码问答。查不到的维度明确标 **未核实**。
> 版本速查：vLLM async Connector API v0.9.0 → OffloadingConnector v0.11.0（2026-01 博客）→ 物理块布局重构 v0.12.0 → CLI flag/抢占回载 v0.14.0 → 多级 TieringOffloadingSpec v0.22（2026-09 博客）→ 当前 dev v0.33.0rc3。

## 一页对比表

| 维度 | SGLang（HiCache → UnifiedRadixCache / Cache Linker） | vLLM（KVConnector → OffloadingConnector / TieringManager） |
|---|---|---|
| 总体范式 | **树内分层**：L2/L3 能力直接附着在统一前缀树上，前缀语义一等公民 | **树外契约**：GPU APC（hash 索引）为 L1，以下全部抽象为 KVConnector 外部缓存，best-effort |
| 分层模型 | L1=device KV；L2=host DRAM；L3=storage（file/fast_file/mooncake/hisparse/GCS），统一在 radix 树视图下 | L1=GPU HBM；L2=CPU pinned（primary tier，唯一有 GPU 访问权）；L3=secondary tiers（fs/obj(S3)/p2p），**全部经 host 中转**（host-centric） |
| 前缀感知 | radix 树原生分层（Rust TreeCore/sglang-radix-tree），节点级 prefix 语义、逐层驱逐 | 无 radix 树：`BlockHashToBlockMap` 平面 hash 表 + 链式块哈希（父哈希+tokens+LoRA/mm extra keys）；offload 层以内容哈希寻址，chunk 默认=1 GPU block |
| 写策略 | 显式三态：write_through / write_back / selective；prefetch 带 cache_salt 归属 | 原生=**块完成即写穿**（offload_prompt_only 默认 true，decode 块跳过）+ cascade 到所有 secondary tier；准入靠 store_threshold（≥2 次查询才写，仅单级）；无原生 write-back。LMCache/Mooncake 后端另提供 write-back 变体 |
| 一致性/共享 | 跨实例靠 L3 后端语义（GCS/mooncake）；cache_salt 隔离 | 内容寻址 + 固定 NONE_HASH seed（PYTHONHASHSEED 可覆盖）→ 同内容同名；canonical_layout 提供 TP 秩无关规范页（跨 TP 规模共享、MLA/GQA 去重）；xxhash 算法 seed 随机需手工统一 |
| 异步/背压 | 已知问题家族：**无界 write/prefetch 队列**、admission 前驱逐（#49221）、write_back 驱逐阻塞 scheduler（#46952，0.6ms→260-340ms）；新仲裁层 capacity/prefill_budget | async DMA 与计算重叠；`wait_for_layer_load` 逐层同步；sync_load_budget_bytes 同步加载预算；secondary 层 submit/poll + promotion 期间 RETRY；p2p 层需 poll 线程（tier_poll_interval_s=1ms，失败 30s 退避）；request_finished 可延迟释放块 |
| 数据面工程 | `--hicache-io-backend kernel\|direct`，kernel 路径回载优势 **~2.6×**（本仓实测 R/P 0.375 vs 0.988） | 0.12.0 起物理块=全层连续（KB→0.5-2.5MB），卸载吞吐**数量级**提升；DMA(cudaMemcpyAsync) vs 自定义 kernel：H100 双向 83.4 vs 68.5 GB/s，大块 DMA 胜、kernel 干扰计算（0% 命中时比不卸载还差 6%） |
| 存储后端生态 | file / fast_file / mooncake / hisparse / GCS + out-of-tree linker | 原生 CPU/fs/obj(NIXL, 含 S3-over-RDMA)/p2p(NIXL RDMA) + 连接器族：LMCache、NixlConnector(P/D)、MooncakeConnector(P2P P/D)、MooncakeStoreConnector(共享存储)、MultiConnector、FlexKV、MoRIIO、UMBPStore；旧 SharedStorageConnector 疑被吸收（**未核实**） |
| 扩展机制 | Unified Cache Linker：契约 + device pool 装配 + out-of-tree linker | 三层皆可外挂：module_path 自定义 OffloadingSpec / SecondaryTierManager / CachePolicy（驱逐策略也 out-of-tree），无需 fork |
| 观测性 | 事实库未含系统级指标清单（**未核实**）；issue 数据可作行为证据 | `vllm:kv_offload_config_info` 配置 gauge；per-tier 命中率/延迟/传输吞吐；KV events（--kv-events-config，BlockStored/BlockRemoved，self-describing 含哈希/token_ids/LoRA/组元数据 + locality 标签）→ llm-d/Dynamo 消费做 cache-aware 路由 |
| 多卡/CP/PD | 事实库未含 CP×HiCache 组合结论（**未核实**） | TP：offload worker 必须恰为 TP 组；canonical_layout 仅支持 TP（禁 PP/PCP/DCP，scatter/gather 开销）；**多节点引擎 secondary tier 启动即失败**；PD 走 p2p 层（host-to-host，llm-d 编排）或 Nixl/Mooncake 连接器；DP 每 replica 独立端口 |
| 混合模型 | 事实库未含（**未核实**） | OffloadingConnector 实现 SupportsHMA：sliding window 只回载窗口内 token，Mamba 状态随 KV 卸载（固定 chunk 字节大小，各层打包不同 token 数） |
| 隔离/安全 | prefetch cache_salt 归属（已核实） | 请求级 cache_salt 进块哈希链（LoRA/租户隔离，源自 #3264 multi-LoRA 串缓存 bug）；引擎级 `--kv-cache-salt` 存在性高置信但**未逐字核对** |
| 性能结论 | 本仓：R/P 中位 0.375（L2 命中 4223/4224）；fp8 KV 容量翻倍 R/P 仅 +4%；12.3KB/token（BF16） | 官方：CPU 命中 TTFT 快 2-22×、吞吐至多 9×；0.12.0 相对 0.11.0 TTFT 4×/吞吐 5×；分层卸载 >128 并发会话时 NVMe 层吞吐翻倍（Qwen3.6-35B-A3B, 2×H100）；LMCache 宣称多轮/RAG TTFT 3-10× |
| 成熟度/坑 | write_back evictable 计账→scheduler OOM；无界队列；admission 前驱逐 | API 标注 experimental；社区报告 v0.19.1 时部分模型（Gemma 4）CPU 卸载不稳（**未核实**）；canonical_layout 启动 fail-fast 而非静默降级（好评） |

## 面试 Talking Points（有观点、有 trade-off）

### 1. 树内统一 vs 树外契约：两种"把 L2/L3 挂上来"的哲学
SGLang 把分层能力直接并入 UnifiedRadixCache——前缀树本身就懂分层，驱逐/准入/匹配在同一个语义域里做，天然支持 write_back 这类需要"驱逐时再写"的策略；代价是**计账耦合进 scheduler**，write_back 的 evictable 计账 bug 直接炸成 scheduler OOM。vLLM 反其道：GPU 侧 APC 仍是平面 hash 表，L2/L3 全部躲在 KVConnector 契约后面，engine core 只看见"额外匹配的 token 数"——核心稳定、生态开放（fs/S3/P2P/任意 out-of-tree），但分层语义被压扁成"按内容哈希的 best-effort 缓存"，块粒度对齐、部分块尾部丢弃这些毛刺都暴露在契约缝合处。**一句话：SGLang 用核心复杂度换语义完整性，vLLM 用语义降级换核心与生态解耦。** 两家的 bug 分布恰好证明这一点：SGLang 的坑在计账与队列（树内代价），vLLM 的坑在块对齐与"丢掉保存=未来 miss"（契约代价）。

### 2. Host-centric 网关 vs 端到端直通
vLLM 2026-09 的分层框架核心原则是"所有 KV 过 host"：secondary tier 永不碰 GPU，好处是二级层极简（POSIX I/O/S3 SDK/RDMA verbs，单进程）、TP 分片在 host 侧合并成少数大 I/O（consolidated I/O）、canonical layout 让异构节点直接共享；代价是**每一跳都多一次中转**。反面证据恰好都在对手阵营：SGLang 的 kernel io_backend 实测 ~2.6× 回载优势，LMCache 的 GDS（GPU Direct Storage）让 NVMe DMA 绕过 host DRAM。所以正确的说法是："host 作为强制网关"是存储生态碎片化下的 v1 正确解，随着 RDMA/NVMe-direct 普及会被端到端路径蚕食——但 consolidated I/O 和规范布局的价值不依赖这个前提。面试时可以把这条讲成"架构决策的生命周期"而不只是对错。

### 3. 为什么两家都往 connector/linker 方向走？（必考）
三个层次：(a) **工程现实**——存储后端（Mooncake/Redis/S3/GCS）不可能都进 engine core，out-of-tree 扩展（vLLM 的 module_path 三连：spec/tier/policy；SGLang 的 out-of-tree linker）是不 fork 前提下的唯一解；(b) **契约收敛**——vLLM 把交互压缩到 KVConnectorBase_V1 十来个 hook + SecondaryTierManager 4 个方法，SGLang 收敛到 linker 契约 + device pool 装配，都是在给"引擎 × 存储"这个 N×M 问题造标准接口，类比 K8s CSI；(c) **真正的驱动力是集群化**——KV events 被 llm-d/Dynamo 消费做 cache-aware 路由后，KV cache 从单实例内部数据结构升格为**集群级调度资源**，路由器需要跨引擎的标准事件与寻址语义，这比"多支持一个后端"重要一个量级。谁能把自己的契约变成路由层的默认方言，谁就拿到生态位。

### 4. 写策略光谱：write-back 是所有尖锐 bug 的家
SGLang 提供显式三态（write_through/write_back/selective），语义最强、坑也最硬：write_back 需要"驱逐时同步/异步写穿 + 可驱逐计账"，SGLang 的三个已知 issue 家族（evictable 计账 OOM、admission 前驱逐、驱逐阻塞 scheduler 500×）全部落在 write_back 一侧。vLLM 原生干脆**不做 write-back**：块完成即异步写穿（DMA，用户无感），GPU 驱逐时数据已经在下层的"write-through 语义"里活着，准入用 store_threshold（查询 ≥N 次才值得写）。这是用容量效率（多写了不热的块）换正确性简单性——对 best-effort 缓存是完全理性的选择，因为"丢一次保存"的代价只是未来一次 miss。答法要点：先讲清 write-back 省的是什么（下层写带宽 + 热度未知时的投机的容量），再讲它逼你解决什么（evictable 计账、驱逐路径阻塞、准入竞争），最后给出你的仲裁层设计（SGLang 的 capacity/prefill_budget 正是补这一课）。

### 5. 决胜负的是数据面，不是架构图
两家 2026 年最大的性能跃迁都来自布局/传输而非架构：vLLM 0.12 把每层碎片化的物理块（几 KB）合并为全层连续块（0.5-2.5MB），卸载吞吐数量级提升，且让 DMA 打平/超过自定义 kernel（大块 DMA 双向 83.4 GB/s vs kernel 68.5，kernel 还挤占计算：0% 命中时吞吐反降 6%）；SGLang 的 kernel io_backend 拿到 ~2.6× 回载优势；LMCache 走 GDS 直接绕 CPU。共同教训：**卸载系统的第一性问题是"单位 I/O 有多大、走哪条物理路径"**——chunk 尺寸（blocks_per_chunk）、pinned/huge page、规范布局的 scatter/gather 开销，这些决定 hit 时候的收益能不能兑现。本仓 roofline 数字（12.3KB/token、Gen4 ~25GB/s、~100K tokens 才 PCIe-bound）可以现场用来推演拐点。

## 面试官可能的追问 + 答法要点

**Q1："两家为什么都往 connector/linker 方向走？"**
→ Talking Point 3 的三层：out-of-tree 不 fork → 契约收敛（N×M 问题）→ 集群级 cache 路由（KV events → llm-d/Dynamo）。补一句差异：vLLM 的契约挂在 scheduler 生命周期 hook 上（get_num_new_matched_tokens / update_state_after_alloc / request_finished 延迟释放），SGLang 的 linker 契约挂在 device pool 装配上——前者是"时序契约"，后者是"拓扑契约"。

**Q2："统一 radix 树内分层 vs hash 表+connector，取舍是什么？"**
→ 语义（树天然支持 write_back、节点级驱逐、selective）vs 解耦（hash 内容寻址天然跨实例、跨 TP 规模共享——canonical layout 是 TP 秩无关的，radix 树做不到跨引擎）。再补：vLLM 的链式哈希同样编码前缀（父哈希入链），所以"前缀感知"并不是树的专利，差别在于**驱逐与准入能不能看见前缀结构**（SGLang 可以按子树/路径策略驱逐，vLLM 的 LRU/ARC 只看 chunk 独立热度，但其自定义 CachePolicy 用 get_offload_key_position 也能拿到位置序）。诚实点：DeepWiki 明确未见 vLLM 引入 radix/UtahTree，勿编造。

**Q3："write-back 难在哪？vLLM 为什么原生不做？"**
→ 见 Talking Point 4。具体机制三连答：evictable 计账（哪些块"在途"不能算空闲——SGLang OOM 根因）、驱逐路径阻塞（0.6ms→260-340ms 的 scheduler stall）、admission 前驱逐（#49221：串行 188,416 vs 并发 64,768 命中 tokens）。vLLM 的替代：完成即写穿 + best-effort + store_threshold 准入 + request_finished 延迟释放块。

**Q4："vLLM 为什么坚持 all-through-host？什么时候这是错的？"**
→ 收益：二级层简单（不碰 GPU API）、consolidated I/O（TP8 八个小流并成一个大流）、canonical layout 共享、GPU 侧 just-in-time（host 拷完即释放显存）。错的场景：延迟敏感的回载（多一跳）、GDS/RDMA 直通可用的硬件（LMCache GDS 证明）、超大 KV（host DRAM 成为必经瓶颈）。对照 SGLang：io_backend 两条路径可选 + fast_file 后端，说明它没把 host 设为强制网关。

**Q5："跨实例/跨 TP 共享怎么保证一致？"**
→ vLLM：内容寻址命名 + 固定 NONE_HASH 种子（PYTHONHASHSEED 可统一；xxhash 例外需显式设）+ 规范布局命名空间（版本+KV 布局族入 key，canonical 与 direct 永不混）；P2P 握手时校验双方 seed 一致，不一致拒绝。SGLang：靠 L3 后端自身（GCS/mooncake）+ cache_salt 归属。坑点：vLLM 每 rank 写 `_r<rank>` 目录、canonical_layout 下轮流写单副本（MLA/GQA 各 rank KV 相同）——这是"去重"也是"一致性依赖"。

**Q6："异步与背压怎么设计？无界队列怎么办？"**
→ 摆谱系：vLLM 用 submit/poll + RETRY（promotion 未完返回 None，scheduler 下轮重查）+ sync_load_budget_bytes（同 step 同步加载预算，用等待换立即执行）+ unbound_store_timeout_s（P2P 生产者停泊超时 60s，防止无限 pin CPU 槽）+ poll 线程 30s 退避；代价是 p2p 在场时引擎不能真正 idle。SGLang 已知短板正是无界 write/prefetch 队列 + capacity/prefill_budget 仲裁层作为新补丁。答法：背压的本质是把"下层慢"翻译成"上层少生产"，三个翻译器：预算（bytes/请求数）、队列上界 + 拒绝/降级、超时释放。

**Q7："混合模型（Mamba/sliding window）下分层还成立吗？"**
→ vLLM 的 SupportsHMA 是目前最完整的公开答案：固定字节大小的 chunk、各层组打包不同 token 数（Mamba 状态一个 chunk 覆盖远多于 attention）、sliding window 只回载窗口内、混合前缀只有所有组都驻留的边界内可复用；KV events 里 mamba 状态用单一哈希+空 token_ids 表达。SGLang 侧**未核实**，别编。延伸：RFC #48203 把"卸载"再推一层——layerwise（prefill 逐层借还）+ sparse（decode 只回载 topk，2×topk buffer 命中 80-90%），KV 显存压到 1/16，说明卸载正在从"缓存持久化"分化出"容量换算力"的第二语义。

**Q8："生产选型：什么时候选谁？"**
→ 框架：先问 prefix 重叠度与工作集大小（HBM 装得下就都别开），再问跨实例共享是否是一等需求（是→vLLM fs/obj/P2P + llm-d 路由，或 SGLang GCS/mooncake），再问拓扑（多节点引擎/PP/CP→vLLM secondary tier 直接不支持，启动即失败；CP×HiCache 未核实），再问写策略需求（要 write_back/selective→目前只有 SGLang 显式提供），最后看运维（vLLM 观测性成体系：config_info gauge/per-tier 命中率/KV events；两家 API 均标 experimental，锁定版本+灰度）。诚实收尾：社区对 vLLM CPU 卸载稳定性有抱怨（v0.19.1/Gemma 4，未核实），生产上前先做 recompute-vs-reload 盈亏平衡测算（用 12.3KB/token + 链路带宽即可估）。

## 信息源清单

**vLLM 官方**
- 博客：Inside vLLM's New KV Offloading Connector（2026-01-08，Ozeri/Harnik, IBM Research）— OffloadingConnector v0.11/0.12/0.14 演进、DMA vs kernel 实测、TTFT 2-22×/吞吐 9×
- 博客：Tiered KV Cache Offloading in vLLM（2026-09-10）— host-centric 设计、cascade/LRU/ARC、P2P+llm-d、KV events 消费方、>128 会话吞吐翻倍
- docs.vllm.ai：KV Offloading Usage Guide（latest/dev v0.33.0rc3）— CPUOffloadingSpec/TieringOffloadingSpec 全参数、fs/obj/p2p 细节、canonical layout、自定义驱逐、info 指标、self-describing KV events、多节点限制
- docs.vllm.ai：Automatic Prefix Caching — 用法与边界
- GitHub issue #38260（RFC: Multi-tier KV offloading，2026-03）— TieringManager/SecondaryTierManager 接口、primary 独占 GPU 访问、TP1 canonical 形态
- GitHub issue #48203（RFC: Layerwise and Sparse KV offloading，2026-07，引 #33398/#33980）— GLM-5.2/DeepSeek-V3.2 稀疏注意力卸载、1/16 显存
- vLLM docs 侧栏连接器清单：Mooncake/MooncakeStore/ECMooncake/MoRIIO/UMB PStore/Nixl(兼容矩阵)/FlexKV/LMCache 示例、HiSparse local KV offload 设计文档（存在性）

**DeepWiki（vllm-project/vllm，2026-11-20 查询）**
- KVConnectorBase_V1 接口与角色分离、hook 语义、KVConnectorOutput、SupportsHMA、best-effort 语义、LMCacheConnectorV1 存在性
- V1 APC 内部：BlockHashToBlockMap、链式哈希+extra keys、FreeKVCacheBlockQueue、hash 算法四选、本地命中→connector 查询的对齐规则
- Mooncake(P2P P/D) vs MooncakeStore(共享存储) vs MultiConnector vs NixlConnector、kv_role 语义

**LMCache**
- docs.lmcache.ai：Architecture Overview（分层/两模式/Controller/256-token chunk）
- docs.lmcache.ai：Integration（vLLM/SGLang/TRT-LLM、lookup→inject→async store 流程、TTFT 3-10×）
- WebSearch 汇总：后端清单（Redis/Valkey/Mooncake/InfiniStore/S3/NIXL/Weka/GDS）、write-back buffer 模式、Mooncake 自身 WT/WTS/WB 三态

**SGLang 侧（本仓已核实，直接采用）**
- 用户事实库：UnifiedRadixCache 合并、Cache Linker 体系、--hicache-host-memory-fraction、capacity/prefill_budget、L3 后端族、io_backend 2.6×、三写策略、cache_salt prefetch、issue 家族
- docs/interview/numbers-bank.md：R/P 0.375、12.3KB/token、#49221/#47481/#46952 数字
- docs/02-hicache-dataflow.md、docs/05-issue-35438-analysis.md（本仓上下文）

**未核实清单（面试中主动声明）**
- vLLM KV events 的 Kafka/HTTP 传输细节（--kv-events-config 内部）
- LMCacheConnectorV1 当前异步加载深度（DeepWiki 所见方法返回 False，疑同步或未启用）
- "v0.19.1 时 Gemma 4 CPU 卸载不稳"——社区问答转述
- vLLM SharedStorageConnector 移除时间线（latest docs 已不见）
- SGLang CP×HiCache 组合、混合模型（HMA）支持
- vLLM 引擎级 `--kv-cache-salt` flag 的精确语义（请求级 cache_salt 已多源确认）
