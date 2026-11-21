# SGLang HiCache 子系统版本间静态 diff：6afee75f → origin/main (789abb9be)

- 日期：2026-11-20
- 基线：`6afee75f918f3d59bdae7d619ba26b0ca22b1354`（2026-08-24，本地浅克隆 `third_party/sglang`，分支 `docs-slurm-examples`）
- 对照：`origin/main` = `789abb9be`（2026-11-20 07:08 UTC，经 ghfast.top 镜像 `--depth 1` 强制抓取；直连 GitHub 因 TLS 失败，本机代理 127.0.0.1:7892 已死）
- 方法：`git diff 6afee75f..origin/main`（commit 到 commit，不含工作区改动）。本地工作区在 `python/sglang/srt/mem_cache/pool_host/common.py` 有一个 kvtier 自有补丁（`SGLANG_HICACHE_HOST_ALLOC=pin` / cudaHostAlloc pinned memory），**不在 upstream main 中**，本文所有结论均基于两端 commit，未受其影响。
- 规模：`python/sglang/srt/mem_cache/` 全目录 233 files，+77795/−21883；连同 scheduler/schedule_policy/schedule_batch/cache_controller 共 237 files，+85142/−25583。本区间无逐 commit 历史可用（浅克隆），结论全部来自两端静态 diff。

> 状态标记：**已修**（基线缺陷在 main 已修复）/ **已变**（行为或结构改变）/ **未变**（两版语义一致）/ **新增**（main 新出现的机制）。行号未注明版本的均为 origin/main。

## 0. 总览表

| # | 主题 | 状态 | 关键证据（文件:行） |
|---|------|------|---------------------|
| 1 | cache_salt 归属（prefetch/load-back key） | **已修** | base `managers/scheduler.py:2779-2791`（不传 salt）+ base `mem_cache/unified_radix_cache.py:1558/1600`（salt 取自 anchor）；main `managers/scheduler.py:3818-3831`（显式传 `req.cache_salt`）+ `unified_radix_cache.py:2666-2727`（断言 anchor 与 request 命名空间一致）+ `buffer_mode/pipeline.py:1888` |
| 2 | Host staging 池容量管理 | **新增+已变** | 新文件 `hicache_auto_size.py`；`pool_host/base.py:42,112,127,198`（cgroup 预算+10GiB reserve）；新文件 `capacity.py`（`reclaim_for_load_back`、`windows_reclaimed_first`）；新文件 `prefill_budget.py`；prefetch 暂存改为命中时分配（`unified_radix_cache.py:2753-2760` vs base 1636-1666） |
| 3 | 异步回载路径 / copy stream | **已变**（异步本身两版都有） | `l2_transfer.py`：新增第三条 `index_stream`、`on_current_stream()`、direct 后端 DMA 阻塞 forward 的告警；`unified_radix_cache.py:2112-2180`（retraction restore 走 copy_stream）、`2366-2494`（load_back receipts）、新文件 `storage_prefetch.py`（重试梯） |
| 4 | 背压（write_through/write-back 限流、队满行为） | **部分已变**（无界队列两版一致；账目与重试已变） | 两版均无 `maxsize`（grep 无命中）；main `hybrid_cache_controller.py:733-762`（write 带 reclaim）、`943-951`（buffer 模式按真实占用限流、写暂存不占 prefetch 预算）；新参数 `--hicache-storage-prefetch-retry-{poll-interval,max-attempts}`（默认 8/8） |
| 5 | L2/L3 驱逐策略 | **新增+已变** | `evict_policy.py`：新增 `TLRUStrategy`、`StateCostStrategy`（choices 由 4 → 7，`arg_groups/choices.py:255-263` vs base `server_args.py:330`）；`capacity.py:81-92`（有 L3 后端时 SWA window 先于 FULL 被逐）；`lru_file_evictor.py` 所有权模型重写 |
| 6 | io-backend（kernel/direct） | **已变** | `pool_host/_kvcacheio.py`（新，XPU op 绑定 + page-first 变体 op 表）；`l2_transfer.py`（`_warn_direct_without_metadata_kernels`、`SGLANG_ENABLE_MLP_SYNC_METADATA_FILL`、`device_translate`、physical 后缀改名）；参数 choices 未变（`arg_groups/fields/memory.py:139`） |
| 7 | L3 storage 后端（file） | **已变+新增**（kvtier 负结果未确诊修复） | `hicache_storage.py:815-903`（key 后缀/unified scheme 重写）、`75-81`（`is_storage_writer` 写者选举）、`1156-1184`（stale metadata 失效）；`lru_file_evictor.py`（`owned_suffixes`）；新后端 `storage/fast_file/`（vectored IO + 原子发布 + 水位驱逐） |
| 附 | 参数/数据结构/UnifiedRadixCache 集成 | **重大重构** | `hiradix_cache.py`（2026 行）删除；新增 `unified_cache_linker.py`（无 host 层直连 L3）；server args 重组进 `arg_groups/`；6 个新 hicache 参数；4 个新 storage 后端 |

---

## 1. cache_salt 归属（Prefetch/load-back key）——**已修**（diff 明确可见）

**基线行为（6afee75f）**：
- `managers/scheduler.py:2779-2791` `_prefetch_kvcache` 调 `tree_cache.prefetch_from_storage(req.rid, last_host_node, new_input_tokens, last_hash, prefix_keys, matched_prefix_tokens=...)` —— **不传请求自身的 extra_key/cache_salt**。
- `mem_cache/unified_radix_cache.py:1558、1600`：salt 唯一来源是 `extra_key, cache_salt = self.tree_core.prefetch_anchor_info(last_host_node_id)`，即 **host 树 anchor 节点上存的 key 命名空间**。root anchor（host 无命中）没有命名空间 → salt 为 None → 带 salt 的请求在无 host 命中时会以 **无 salt 命名空间** 发起 L3 prefetch/load-back，与写入侧（events 已按 salt 区分 hash，base `mem_cache/events.py:88-156`）不一致。
- base `hiradix_cache.py:1487/1789` 同样只取 `last_host_node.key.cache_salt`。

**main 行为（789abb9be）**：
- `managers/scheduler.py:3818-3831`：调用点显式传 `extra_key=req.extra_key, cache_salt=req.cache_salt, storage_hit_end=...`。
- `unified_radix_cache.py:2666-2727` `prefetch_from_storage` 签名新增 `extra_key`/`cache_salt`/`storage_hit_end` 形参，并加了硬断言与注释：
  ```python
  # Key the span by the request's namespace, not the anchor's (a root
  # anchor has none): a span published under the wrong namespace gets
  # re-owned by the request's own insert (double free).
  assert (anchor_extra_key is None or anchor_extra_key == extra_key) and (
      anchor_cache_salt is None or anchor_cache_salt == cache_salt), ...
  ```
- buffer 模式：`buffer_mode/pipeline.py:1526-1531` prefetch 上下文保存 salt；`:1888` 回载完成断言 `(f.extra_key, f.cache_salt) == (req.extra_key, req.cache_salt)`。
- PP 预取票据保留 salt：`hybrid_cache/hybrid_cache_controller.py:1346-1352`（`PPPrefetchTicket.prefetch_key = RadixKey(..., cache_salt=prefetch_key.cache_salt, ...)`）、`:1359`。
- 写入侧（L3 events）base 已按 salt 区分（`compute_node_event_hash_values`），main 增补：事件合并要求 salt 相同（`events.py:299`）、`_namespace_seed` 对 dynamo 格式纳入 salt（`events.py:347-357`）。

**判定**：kvtier 怀疑的"prefetch/load-back key 不带请求自身 cache_salt"在基线确实存在（anchor-derived，root anchor 退化为 None）；main 已改为请求自带命名空间 + 一致性断言。**值得 GPU 重测：是**（salted 请求的 L3 命中率/正确性场景）。

---

## 2. Host staging 池容量管理 ——**新增（自动 sizing）+ 已变（竞争仲裁）**

**新增：host 池自动定容**
- 新文件 `mem_cache/hicache_auto_size.py`（174 行）：`auto_size_hicache()` —— 以 `--hicache-host-memory-fraction`（新参数，`arg_groups/fields/memory.py:128-130`）乘以"cgroup 可见可用内存（memory.max/memory.high 或 v1 limit，扣除 10 GiB 保留）"得到预算，把默认 hicache_ratio 向下折到装得下为止；跨 rank `all_reduce(MIN)` 一次（PP 各 stage 池数不同，逐池同步会死锁）。
- `pool_host/base.py:42` `HICACHE_HOST_MEMORY_RESERVE_BYTES = 10*1024**3`；`:112` `host_memory_budget_scope`；`:127` `ranks_per_host`；`:198-219` `host_memory_budget_plain_hugetlb`（cgroup 感知）。
- `hybrid_cache/host_pool_config.py`（新）：host 池组在分配前的容量/传输绑定打包（`HostPoolConfig`/`HostPoolGroupConfig`）。

**已变：容量竞争仲裁（与 W/E/R/P 饥饿直接相关）**
- 新文件 `mem_cache/capacity.py`（330 行）：调度器容量决策层。要点：
  - `windows_reclaimed_first()`（81-92 行）：**启用 HiCache storage backend（或 PD decode）时，reclaim 先逐 SWA window 表项、后逐 FULL token 表项** —— FULL 前缀（W 类写入的对象）在设备池回收时被显式保护。
  - `reclaim_for_load_back`（132 行）：为 L2→device 回载显式回收设备容量（base 无此入口，回载与 prefill 抢占同一可用字节数）。
  - `_SharedMemory` vs `_OwnPools`：共享字节 arena 的池不允许在决策内读"标量可用量"（`observe_only_read`，违反即 STRICT 断言）。
- 新文件 `mem_cache/prefill_budget.py`（1234 行）：单次 prefill 的内存预约制；"Shared-pool load preparation realizes the selected reservation before a host transfer pins device rows" —— host 回载固定 device 行之前先落实预约， admission 检查反映被锁定前缀/被抢占请求。
- **prefetch 暂存改到命中时分配**：base `unified_radix_cache.py:1636-1666` 在查询阶段预分配各组件 host 暂存，任一失败即放弃整个 fetch（`log_prefetch_aux_alloc_failed_tokens`）；main `unified_radix_cache.py:2753-2760` 注释改为 "Size the component's staging now; **it is allocated at hit time**, next to the KV staging, so **the query holds no host memory**" —— 存储往返期间不再占用 host 池。
- 命中时分配支持部分命中：`hybrid_cache_controller.py:992-1039` `allocate_storage_hit(..., allow_partial, min_tokens, evict_host)` —— 分配失败先 `evict_host(hit_tokens)` 再试；共享 arena 上二分找可装下的页数。
- 限流账目修正：base `managers/cache_controller.py:1083-1099` `prefetch_rate_limited` 以 `prefetch_tokens_occupied`（请求 span，偏大）为准；main `hybrid_cache_controller.py:943-951` buffer 模式改为按池真实占用，且 **写暂存（write staging）不占 prefetch 预算**（"charging it here would park hits behind its storage drain"）。
- write() 满时行为：base `hybrid_cache_controller.py:309-333` host alloc 失败直接返回 None（该节点不备份）；main `:733-762` `allocate_host_transfers(..., reclaim=...)` —— 先回收再重试，另加 `flush=` 批量提交与 `start_owner=`（逻辑页 KV 分片）。

**判定**：sizing（cgroup 预算/自动降 ratio）与容量仲裁（load-back 预约、window-first 回收、查询不占 host 内存）都是 main 的新机制，直接对应 kvtier 观察到的 host 池容量竞争与 W/E/R/P 饥饿。**值得 GPU 重测：是**（host 池高压下的 W 写入存活率与 R 命中延迟）。

---

## 3. 异步回载路径 ——**已变**（异步回载两版均有：base `managers/cache_controller.py:342` `load_queue`、`:780` `def load` 即异步；main 是重叠与流编排的增强）

- `mem_cache/l2_transfer.py` 重写（+205 行净增）：
  - 新增第三条流 `self.index_stream`："Index preparation runs here, **never queued behind earlier copies**"（direct 后端要在 host 上读回 device indices）。
  - 新增 `on_current_stream()` 上下文：H2D 提交可改在调用方当前流上按序执行（engine 自己的流闲置）。
  - 新增 direct 后端风险告警 `_warn_direct_without_metadata_kernels()`：direct 用 copy-engine DMA "can queue ahead of the forward's small metadata host-to-device copies and **stall the forward**; set `SGLANG_ENABLE_MLP_SYNC_METADATA_FILL=1`" —— 直接承认 direct 路径会拖慢 forward。
  - `submit_host_to_device` 增加 `SGLANG_TEST_HICACHE_H2D_DELAY_CYCLES` 测试旋钮；`layer_num` → `transfer_layer_id_max`。
  - `L2Transfer` 增加 `device_translate`（统一子池 id 空间换算）与 DCP 宽度处理。
- `unified_radix_cache.py:2112-2180` `start_restore_kv_cache(..., copy_stream)`：retraction 恢复的 H2D 可跑在指定 copy_stream，"Order later work on this stream after the copy **instead of blocking the scheduler**"；`restore_kv_cache` 只 `wait_event`（非阻塞）。
- `unified_radix_cache.py:2366-2494`：`load_back`/`_load_back_transfers` 引入 `_OngoingLoadBack` receipt（host-only mamba checkpoint 允许同 anchor 多 receipt）；`:4421-4472` `init_load_back` 返回 `None` 表示"不提交 load、admission 重试"；`:4357+` ack 回收走 rank 一致路径；`scheduler.py:6176` idle 判定纳入 `ongoing_load_back`。
- 新文件 `mem_cache/storage_prefetch.py`：`StoragePrefetchRetries` —— L3 miss 的重试梯（"A speculative miss must never delay the queue head"），配新参数 `--hicache-storage-prefetch-retry-poll-interval/-max-attempts`（默认 8/8）；另有 `StagedPrefetchPlan`/`WindowLoadBackPlan`（admission 前算好的组件级回载计划）。

**判定**：回载与 prefill 的重叠手段（专用 index 流、可选 copy_stream、非阻塞 restore、重试梯）显著增强；direct 后端的 forward 阻塞被明示且给了环境变量缓解。**值得 GPU 重测：是**（load-back 与 prefill 重叠的 TTFT、direct 后端 stall 复测）。

---

## 4. 背压 ——**部分已变**（核心事实：**两版队列均无界**）

- grep `maxsize` 在两版 `cache_controller.py`/`hicache_storage.py`/`hybrid_cache/` 均无命中：write_queue/prefetch 队列不受长度限制。**队满丢弃/阻塞机制不存在于两版**（未变）。
- main 的实际背压来源（已变/新增）：
  1. host 池分配失败 + reclaim 重试（见主题 2）；
  2. `prefetch_rate_limited`（两版都有；main 修了 buffer 模式账目并把写暂存移出 prefetch 预算）；
  3. write_back 下 `writing_check(write_back=True)` 阻塞驱逐直至在途写完成（两版都有；main 位置 `unified_radix_cache.py:1061/1353/1412/2202`）；
  4. **新增** prefetch 引发的 admission 延迟上限：miss 重试梯 `poll-interval/max-attempts`，超限 "admitted with whatever the device holds" —— 防止 R 类请求被 speculative prefetch 无限拖住；
  5. **新增** `write_back_jit` 路径（`hybrid_cache_controller.py:1063-1074、1759-1769`，kernel 后端 + page_first 布局时按池合并搬运）。
- `hicache_write_policy` choices 与默认值（`write_through`）两版一致（main `fields/memory.py:132-138`，base `server_args.py:2735`）。

**判定**：无界队列未变（若 kvtier 曾因 L3 写队列堆积观察到位移，main 不会改变该形态）；但 prefetch→admission 的反压上限和 buffer 模式记账是新的。**值得 GPU 重测：中**（写风暴下 host 池满时的服务延迟曲线）。

---

## 5. L2/L3 驱逐策略 ——**新增 + 已变**

- `mem_cache/evict_policy.py` 新增两个策略（diff 全量可见）：
  - `TLRUStrategy`（Tail-Optimized LRU，arXiv:2510.15152）：TEL-safe 节点按"无限老"上报，被既有驱逐循环优先排干；
  - `StateCostStrategy`（FlashStates 启发）：recurrent-state checkpoint 按重建成本（d/e 二次拟合）排序，FULL KV 仍按 recency。
  - `--radix-eviction-policy` choices：base `["lru","lfu","slru","priority"]`（`server_args.py:330`）→ main `+["tlru","joint_lru","state_cost"]`（`arg_groups/choices.py:255-263`）。
- L2（host 树）驱逐：main `unified_radix_cache.py:1916-1926` `evict_host` → `tree_core.drive_host_eviction(component_type, ...)`（按组件逐出）；buffer_only 模式无树驻留值，"nothing is evictable"。base 经 `hiradix_cache` 的 host LRU（`memory_pool_host.py:967-971` 的 `host_evict_fn` 回调在两版都存在）。
- **回收顺序变化（对 E 类敏感）**：`capacity.py:81-92` `windows_reclaimed_first()` —— 有 HiCache storage backend 或 PD decode 时，设备池 reclaim **先逐 SWA window 再逐 FULL**；FULL（W 前缀的载体）被保护。
- L3 file 驱逐：`storage/file/lru_file_evictor.py` 所有权模型重写（见主题 7）；新 `storage/fast_file/lru_file_evictor.py`（588 行）：高/低水位（默认 0.95/0.85）后台批量驱逐 + 可选 TTL 正向 metadata cache。

**判定**：策略族扩容、L2 回收顺序显式化。对 kvtier 的 W/E 混布场景，window-first 回收与 TEL-safe 语义都可能改变驱逐分布。**值得 GPU 重测：中**（E 风暴后 W 前缀的 L2/L3 存活率）。

---

## 6. io-backend（kernel/direct）——**已变**

- `--hicache-io-backend` choices 两版一致（`direct|kernel|kernel_ascend`，main `fields/memory.py:139`，base `server_args.py:2748`）；base 已有按后端自动切 layout 的逻辑（base `server_args.py:7982-8025`），main 保留。
- 新文件 `pool_host/_kvcacheio.py`（132 行）：XPU 下从 `sgl-kernel-xpu` wheel 绑定 `sgl_kernel.kvcacheio` 传输 op，缺 op 生成具名 stub 并在 host 池构造时拒绝；op 表新增 **page-first 变体**：kernel 侧 `transfer_kv_per_layer_mla_pf_lf`/`transfer_kv_all_layer_mla_lf_pf`，direct 侧 `transfer_kv_per_layer_direct_pf_lf`/`transfer_kv_all_layer_direct_lf_pf`。
- `l2_transfer.py`：见主题 3（index_stream、direct DMA 阻塞 forward 告警、`SGLANG_ENABLE_MLP_SYNC_METADATA_FILL`、`device_translate`）；传输调用由 `load_to_device_per_layer` 改名 `load_to_device_per_layer_physical`/`backup_from_device_all_layer_physical`（物理 id 语义）。
- `prepare_transfer_indices` 新钩子：host pool 按 io_backend 预处理索引（`l2_transfer.py` `_prepare_transfers`）。

**判定**：direct 路径存在被上游明示的 forward 阻塞问题（copy-engine DMA 越过小元数据 H2D），main 提供元数据 kernel 化的开关；kernel 路径新增 page-first MLA op。**值得 GPU 重测：是**（若 kvtier 用 direct：换 kernel 或开 `SGLANG_ENABLE_MLP_SYNC_METADATA_FILL=1` 的 A/B）。

---

## 7. L3 storage 后端（file 等）——**已变 + 新增**；kvtier 2026-11-06 负结果（8 个 W 前缀未在 L3 索引幸存，storage-backend=file）**未见确针对性修复，但路径大幅重构**

**file 后端（HiCacheFile）**：
- key 后缀派生重写（`hicache_storage.py:815-903` `_init_config_suffix`）：legacy `_{tp_rank}_{tp_size}`/`_{pp}_{pp}`/`_cp{r}_{s}` 保留；新增 `unified_suffixes`（拓扑无关 chunk 坐标，见附 1）；MLA rank-replicated 共享单文件；DCP 分片后缀。
- **写者选举**（`hicache_storage.py:75-81` `is_storage_writer`）：共享对象只选一个写者（unified+CP 只写 CP rank0；MLA 每 DCP 组 rank0；non-MLA 每 TP rank 都写）。base 无此概念。
- **exists 语义修复**：base `hicache_storage.py:561-571` `exists()` 若 metadata cache 命中即返回 True 且**从不清理陈旧条目**；main `_file_exists`（1156-1184）文件不存在时 `metadata_cache.remove(key)`，unified scheme 下干脆不信 metadata cache（别的部署的 evictor 无法失效本实例缓存，直接 `os.stat` 并校验 chunk 大小）。
- `storage/file/lru_file_evictor.py`：`is_mla_model`+rank0 集中记账模型 → **`owned_suffixes` 每秩所有权**（"Files ending in one of these suffixes are created and evicted by this rank; other files in the shared directory are only read"）；`reserve()` admission 语义随之按后缀判断。若 kvtier 负结果与 MLA 多秩下"非 owner 秩不建文件/不驱逐"的旧规则有关，此重构改变了该行为。
- 原子写（tmp + `os.replace`）两版都有（base :526-530，main :1123-1125）——**部分写可见性不是负结果的原因**（diff 明确可见）。

**新后端 `fast_file`**（`storage/fast_file/fast_file_store.py` 830 行 + `lru_file_evictor.py` 588 行，`backend_factory.py:92` 接线）：与 `file` 同盘上格式，但：
- vectored `readv/writev` 直接在 page-first host 池缓冲与页文件之间搬运（跳过 staging tensor 拷贝）；
- 有界 worker 池并行读/existence 检查；
- 原子发布（temp+rename，"a reader never observes a partially written page"）；
- 双水位后台 LRU + 可选 TTL 正向 metadata cache + 启动清理陈旧 temp；
- 按 model/layout 的命名空间目录，启动扫描/驱逐/clear 不碰别的部署的文件。
- extra-config：`read_workers`、`enable_metadata_cache`、`metadata_ttl`、`max_size`、`min_free_space`、`evict_high_watermark`(0.95)、`evict_low_watermark`(0.85)、`preevict_interval_ms`(10)、`evict_batch_size`(128) 等。

**负结果归因核对（区分可见事实与推断）**：
- 可见：`write_through_selective` 的 `hit_count >= threshold` 门控与"父节点未备份则子节点跳过"的 contiguity 不变量在 main 依然存在（`unified_cache/unified_tree_core.py:645`、`:1261-1278`；base `hiradix_cache.py:849-853、989-996`）。默认策略是 `write_through`（threshold=1），但**若 kvtier 当时跑 selective（threshold=2），单次 W 写入不落 L3 是策略而非 bug，main 未改此语义**。
- 可见：stale metadata cache 正向误报（base exists 永不清理）在 main 已修——若负结果的"索引有但查不到/查到但文件没了"属于这类陈旧信念，main 会改善。
- 可见：buffer_only 模式的 `storage_existence_cache.py`（存在性信念 LRU，512K 条上限）文档明确 stale positive 的自愈路径是"prefetch hit-query shortfall 失效该池条目"。
- **推断（非 diff 直接可见）**：8 个 W 前缀未幸存最可能是 (a) selective 门控 + 父链 gap 阻断，或 (b) evictor `reserve()` 在容量上限处拒收，或 (c) base 的 stale metadata 负向/正向错位。main 对 (c) 有修复，对 (a)(b) 语义未变；`fast_file` 提供了更强的可观测性与驱逐控制，适合作为复测载体。

**判定**：**值得 GPU 重测：是**（file 与 fast_file 双后端重跑 W 幸存实验；确认 write_policy 与 threshold 的实际取值）。

---

## 附 1：HiCache ↔ UnifiedRadixCache 集成方式变化（重大重构）

- **删除**：`hiradix_cache.py`（2026 行，HiRadixCache 整体移除）、`swa_radix_cache.py`、`mamba_radix_cache.py`、`chunk_cache.py`、`radix_cache_cpp.py` + `cpp_radix_tree/`（C++ 树）、`multi_ended_allocator.py`（2563 行）、`buffer_mode/buffer_page_cache.py`、`layout/page_major.py`。
- **替代/吸收**：`unified_radix_cache.py`（+4022 行变化）承接全部 HiCache 语义（prefetch/load_back/evict_host/retraction），`unified_cache/` 组件化（`components/{base,full,swa,mamba}.py` 重命名自 `*_component.py`）；树核心换成 `rust_tree_core/`（新 Rust adapter，1441 行）。
- **新增"无 host 层直连 L3"**：`unified_cache/unified_cache_linker.py`（994 行）——"Links the cache's device pools straight to an external KV store, **with no host tier in between**"；树侧只需 5 个受控钩子（`match_prefix`/`_prefetch_kvcache`/`init_load_back`/`check_hicache_events`/`BackupKV`→offload_nodes）；逐层 load 落在请求私有 device 槽、失败只影响自身请求。配套 `linker_factory.py`（支持 `dynamic` out-of-tree linker 经 importlib 加载）、`linker_pool_assembler.py`（897 行）、`mooncake_store/mooncake_direct_linker.py`（691 行，新）、`umbp/umbp_direct_linker.py`（1011 行，新）。
- **统一内存栈**（新目录 `kv/`，约 15K 行）：`--enable-unified-memory` 下 full-attention KV、SWA KV、recurrent state 共享一个字节 arena（`kv/memory/arena.py`、`kv/alloc/chain.py`、`kv/alloc/band.py`、`kv/alloc/demand.py::KVDemand`）；"Freeing is logical at once; physical reuse is not"、`capacity.py` 的 demand 式问答与之配套。

## 附 2：新增服务器参数 / flag / 环境变量（main vs base）

**新增 server args**（base 9 个 hicache 参数全部保留）：
| 参数 | 默认 | 说明 |
|---|---|---|
| `--hicache-host-memory-fraction` | None | cgroup 感知的 host 池预算分数（10 GiB reserve 后），仅当未显式给 ratio/size 时生效，自动降 ratio |
| `--hicache-storage-prefetch-retry-poll-interval` | 8 | L3 miss 后隔多少个调度 pass 重查 |
| `--hicache-storage-prefetch-retry-max-attempts` | 8 | 重查上限，超限按设备现有内容放行 |
| `--hicache-storage-key-scheme` | rank-suffix | `unified`＝拓扑无关 L3 key（file/mooncake 支持；隐含 page_unified layout + kernel io backend） |
| `--hicache-storage-head-partition` | None | unified scheme 的 fleet 级 head 分块约定 |
| `--hicache-storage-layer-partition` | None | fleet 级 layer 分块约定（PP 对手方） |

- `--radix-eviction-policy` 新 choices：`tlru`、`joint_lru`、`state_cost`。
- `--hicache-storage-backend` 新 choices：`fast_file`、`npu_memcache`、`seaweedfs`、`tensorcast`（base 无）。
- 新 env（`environ.py`）：`SGLANG_DISAGG_PREFILL_POLL_HICACHE_WHILE_WAITING`、`SGLANG_ENABLE_HICACHE_SWA_TAIL_ONLY_BACKUP`、`SGLANG_ENABLE_WAITING_PREFIX_REFRESH`、`SGLANG_HICACHE_HOST_PARALLEL_ALLOC_THREADS`、`SGLANG_HICACHE_HOST_REGISTER_CHUNK_GB`、`SGLANG_HICACHE_MAMBA_HOST_RATIO`、`SGLANG_HICACHE_MEMCACHE_CONFIG_PATH`、`SGLANG_HICACHE_SKIP_HOST_DUPLICATE_RECLAIM`；移除 `SGLANG_ENABLE_HICACHE_BUFFER_ANCHOR_LOCK`。
- server args 体系重组：`server_args.py` 单体 → `arg_groups/`（`fields/memory.py`、`hicache_hook.py` 等）——自动化脚本若按字段位置/名称解析需跟进。

**新数据结构（择要）**：`KVCacheNamespace`/`namespace_digest`（`hicache_key_scheme.py`）、`PPPrefetchTicket`/`PPPrefetchState`、`_OngoingLoadBack`、`_StoragePrefetchWait`、`RetractionBackup`、`StagedPrefetchPlan`/`WindowLoadBackPlan`（`storage_prefetch.py`）、`KVDemand`（`kv/alloc/demand.py`）、`PoolExistenceBeliefs`（`buffer_mode/storage_existence_cache.py`）、`HostPoolConfig`/`HostPoolGroupConfig`、`L2Transfer.device_translate`。

## 附 3：文件级 diffstat 摘要

- 总量（mem_cache + scheduler 四件套）：237 files，+85142/−25583。
- mem_cache 内部按目录份额（dirstat by files）：`mem_cache/` 根 34.7%、`storage/` 21.0%、`unified_cache/` 4.7%+components 3.0%、`pool_host/` 7.2%、`kv/` 及其子包 ~19%、`allocator/` 3.4%、`cpp_radix_tree/`（删除）3.8%。
- 重点单文件：`unified_radix_cache.py` +4022 变更行、`hybrid_cache/hybrid_pool_assembler.py` 2695、`kv/alloc/band.py` 3666（新）、`hybrid_cache/hybrid_cache_controller.py` 1865、`deepseek_v4_memory_pool.py` 1884、`memory_pool.py` 3311、`kv_cache_configurator.py` 3001、`buffer_mode/pipeline.py` 2335。
- 删除大件：`hiradix_cache.py` 2026、`multi_ended_allocator.py` 2563、`swa_radix_cache.py` 1440、`mamba_radix_cache.py` 1427、`cpp_radix_tree/`（约 1100）、`storage/lmcache/lmc_radix_cache.py` 523。
- mem_cache 之外强相关：`managers/scheduler.py`（prefetch 调用点、idle 判定、storage_prefetch 重试）、`managers/schedule_policy.py`、`managers/cache_controller.py`、`disaggregation/decode_hicache_mixin.py`（PD decode 侧 L3 prefetch：`query_storage_hit_length`/`prefetch_from_storage` 调用于 :97/:140）。

## 附 4：重测清单（按优先级）

1. **cache_salt 正确性/命中率**（主题 1）：salted 请求在无 host 命中时的 L3 prefetch/load-back 是否命中且不串数据 —— main 修复明确，收益直接。
2. **host 池高压下的容量竞争**（主题 2+4）：`--hicache-host-memory-fraction` 自动 sizing、命中时分配 + window-first 回收 + load-back 预约对 W/E/R/P 饥饿曲线的影响。
3. **file/fast_file 的 W 前缀 L3 幸存**（主题 7）：复跑 2026-11-06 实验，双后端对比，并核对 `hicache_write_policy`（selective threshold 与父链 contiguity 不变量仍在，需用 `write_through` 排除策略性不落盘）。
4. （次优先）direct io-backend 的 forward 阻塞 A/B：`kernel` vs `direct`+`SGLANG_ENABLE_MLP_SYNC_METADATA_FILL=1`（主题 3/6）。

## 附 5：本地克隆状态备忘

- 本地 `third_party/sglang` 工作区：分支 `docs-slurm-examples`，`pool_host/common.py` 有未提交的 kvtier 补丁（`SGLANG_HICACHE_HOST_ALLOC=pin`，cudaHostAlloc 方案）；upstream main 无此机制 —— rebase 时该文件需手工合并。
- `origin/main` ref 已被本次分析强制更新为 789abb9be（旧值是 8 月下旬的陈旧 tip）；抓取方式：`git fetch --depth 1 https://ghfast.top/https://github.com/sgl-project/sglang.git +main:refs/remotes/origin/main`（需清空 `http(s)_proxy` 环境变量，本机 7892 代理已死）。
