# 设计：4× 结构型变形的三症状根因定位与最小修复方案

> 日期：2026-11-21。纯源码分析 + 设计文档，**未改任何代码、未发布任何内容**。
> 分析基线：SGLang main `e183c415bdfc5995f6288b1651e1c320c6a74015`（`/tmp/sglang-main-audit`，HEAD 已核验）。
> 证据来源：`bench/repro-31600/evidence/2026-11-20-main-dualarm/`（下称"证据包"；重点 C2 臂），
> 机制地图 `docs/03-hicache-dataflow-main.md`，静态 diff `docs/upstream/2026-11-20-memcache-diff-6afee75f-to-main.md`。
> 路径前缀约定：`python/sglang/srt/` 简写 `srt/`。C2 臂实际运行在 **Python TreeCore**（server.log:121 前后
> "Using the Python TreeCore: the Rust TreeCore toolchain is unavailable"），下述行号均以 Python 侧实现为准，
> Rust adapter 为同语义移植，结论不受影响。

---

## 〇、TL;DR

实验 0 剂量臂（C2，4× host 池 172,096 tokens）报告的三个"结构型"症状，经源码与证据交叉定位后
**同根于一个平面：L3 file 后端的单线程存储 IO 与 timeout 停读策略**，而不是 L2/L3 索引管理：

| 症状 | 根因（一句话） | 源码可见 / 推断 |
|---|---|---|
| ① L3 "索引丢失"（4/6 轮查询 0 命中） | **不是索引被驱逐**——file 后端默认无 evictor、无 metadata cache，exists=纯 `os.path.exists`；真实机制是 exists 查询在单线程 prefetch 管线 + 写风暴下超过 ~2.03s deadline 预算被终止，且"终止时命中未入账"的查询**零记账、零日志、零重试**，对外表现为"查询未命中" | 机制链源码可见；"4 个 trial 的文件在盘上"为强推断（FIFO+计数器+2 个全 span 命中） |
| ② drain 不收敛（240s：enqueued 204,686 / acked 118,670） | L3 写路径是**单条 `hicache-backup` 线程**逐页写小文件（page_size=1 时每 token 一个 12KB tmp+`os.replace`），实测 ~400–780 tok/s（≈5–10 MB/s）；4× 池把 write_through 准入量放大 2.7×（75,662→204,686），超过 240s 观察窗能排干的量——**吞吐失配，不是静默丢写**（盘上文件数 124,427 ≈ acked 124,814，精确一致；无失败告警） | 线程结构与写路径源码可见；吞吐数字与"无丢弃"由证据包实证 |
| ③ deadline 未履约 8,318 tokens | timeout 停读策略预算 = `1.0s + 0.25s/Ki-token`（4,223 页 ≈ **2.03s**），且计时起点是**首次 blocked admission poll**——查询+host 分配+IO 读全部记在一个 ~2s 预算里；查询被写风暴拖慢后，读只剩 0–1 个 batch（128/4,223）就被切断 | 公式与计时起点源码可见；写风暴拖慢查询为推断（GIL/目录争用） |

**三个症状是同一条因果链**：4× 池 → write_through 放行更多 E 写 → 单线程 L3 写者积压（②）→ M 阶段读
查询与写线程争用（GIL + 同目录）→ 查询/装载超 2.03s 预算 → deadline 切断（③）→ 未入账的切断零痕迹（①）。
**证据**：全部 12 个 R 请求（A1/A2/C2 三臂）TTFT 聚在 2.09–2.59s ≈ deadline 预算 2.03s + prefill，
包括 6 个"未命中"的 C2 trial——它们不是没查，是**等满了超时窗口后被放行**。

与 #49226（pin-until-admission + FIFO 容量排队）：**互补、无冲突、零交集**。#49226 修的是 L2 admission
窗口（cache_admission_shortfall / host_capacity 面），C2 三症状全在 L3 存储 IO 面（C2 中 host_capacity=0，
#49226 的触发条件根本不出现）。#49226 合入后三个症状全部原样存在。

---

## 一、C2 臂时间线重建（证据包实证）

运行结构（workload-C2.log + samples-C2.jsonl 逐秒对齐）：

```text
01:55:46  warmup + flush_cache（pre_write 快照 142 个残留文件）
01:55:47  W：6 个隔离候选前缀写入（6×4,224 tokens = 25,344）
01:55:47  "等待 10.0s 让 write_through 备份完成"
          → post_write 快照仅 7,906 文件（~780 tok/s；W 的 L3 写只完成 ~31%）
01:55:57  E：456 请求 / 466,944 tokens（device 358,532，evict-factor 1.3）
          → host 池填满（host_used 172,032/172,096），W 的 host 条目被挤出
01:56:28  D2H 计数（hicache_backup_tokens_total）达 204,686 后平台
01:56:29  "等待 backup drain 至多 240s"
          → L3 ack（backuped_tokens_total）以每步恰好 1,024 tokens 爬升，~400–500 tok/s
02:00:30  超时警告；enqueued 204,686 / acked 118,670（差 86,016 仍在队列）
02:00:32  M：6 轮配对 R/P 测量（R=完整 W 前缀少 1 token，4,224-1=4,223）
02:00:35–48  逐 trial：R TTFT 2.17–2.59s；R cache 全部 device=0 host=0；
          仅 trial03/04 的查询入账（hit 4,223×2 = 8,446），装载被切断（128/0）；
          trial 00/01/02/05 storage=0 且**无任何计数器/日志痕迹**
          （server.log 全程只有 2 行 "HiCache prefetch success"，debug 级开着重试梯 0 痕迹）
02:00:51  结束。final：D2H 255,246；L3 acked 124,814；盘上文件 124,427；
          dropped{write_through_unbacked_eviction}=13,312；deadline 8,318；host_capacity 0
```

关键实证数字：

| 断言 | 数字 | 来源 |
|---|---|---|
| L3 写吞吐 | ~400–500 tok/s（空闲排干段）；~780 tok/s（W 段） | samples 逐秒差分 |
| L3 写有效带宽 | 12,288 B/页 × ~450 页/s ≈ **5.5 MB/s**（NVMe 盘） | 同上 + 文件 size |
| 盘上文件 == acked | post_drain 119,105 ≈ acked 118,670；post_measure 124,427 ≈ 124,814 | storage_snapshots vs metrics |
| 无写失败 | 0 条 "Write page to storage … failed" / "HiCache backup failed" | server.log grep |
| R TTFT 全聚 ~2s | C2: 2.17/2.41/2.43/2.45/2.59/2.41；A1/A2 同分布（2.09–2.49） | results-*.json trials |
| 重试梯未参与 | 0 条 "re-issue"（debug 级开启）/ "reissue cap" / "prefix shrank" | server.log grep |
| deadline 预算 | 1.0 + 4,223×0.25/1024 ≈ **2.03s** | 源码公式（下） |
| 账目恒等 | hit 8,446 = fulfilled 128 + deadline 8,318 | metrics-final |

---

## 二、症状①："L3 索引丢失" —— 排除索引驱逐，定位到 deadline 截断的零记账查询

### 2.1 先排除三个候选假说（任务书怀疑链逐项裁决）

**假说 A：file evictor 的 owned_suffixes 驱逐把 W 页文件删了。**
排除（源码可见）：evictor 仅在 `max_size>0 or min_free>0` 时启用
（`srt/mem_cache/storage/file/lru_file_evictor.py:90-92`，`reserve` 在未启用时直接放行 `:159-160`）；
`SGLANG_HICACHE_FILE_BACKEND_MAX_SIZE` 默认 `None`→0 字节、`MIN_FREE_SPACE` 默认 `"0"`
（`srt/environ.py:928-932`）。C2 未传 extra_config（server_args 确认 `hicache_storage_backend_extra_config: None`）
→ **evictor 惰性，磁盘无驱逐**。且快照文件数与 acked 计数全程一致，没有任何文件消失
（post_drain 119,105 ⊆ post_measure 124,427，只增不减）。

**假说 B：索引存 host radix、被 4× 池的 E 条目挤出。**
排除（源码可见）：file 后端 legacy rank-suffix 方案下"索引"就是**文件系统本身**——
`_file_exists` 在 metadata cache 关闭时退化为 `os.path.exists`
（`srt/mem_cache/hicache_storage.py:1159-1187`），而 metadata cache 默认关闭
（`SGLANG_HICACHE_FILE_BACKEND_ENABLE_METADATA_CACHE = EnvBool(False)`，`environ.py:934`；
开启时也只做正向 TTL 缓存，miss 后回源 stat）。host radix 树只提供 anchor/命名空间，不存任何
"storage 存在性"状态。README 中"host radix 容量大 → E 条目挤掉 W 前缀的 storage 引用
（DEV-20 index_evicted 复活）"是 6afee75f 时代启发式分类器的迁移表述（证据包偏离项 9 已自我标注），
在 main 上无对应机制。

**假说 C：写入用了不同 hash 链（salt/namespace 错位）。**
排除（运行时证据）：trial03/04 的查询以**同一条链**全 span 命中（4,223/4,223），说明 W 写入与 R 查询
的 namespace/链完全一致；C2 是 nosalt 臂，A1（salted）行为无差异（D1 判定）。

### 2.2 真实机制：deadline 票决终止 + 命中记账的时序不对称

查询生命周期（源码可见，行号=unified_radix_cache.py 除注明）：

1. R 入队 → `_prefetch_kvcache`（`scheduler.py:3775-3828`）→ `prefetch_from_storage`（`:2666`）；
   matched=0（device/host 都没有 W），anchor=root，全 span 4,223 tokens ≥ threshold 256，
   无 rate limit（occupancy≈0，日志 "occupied=0"）→ `submit_prefetch` 入 `prefetch_queue`。
2. **单条** `hicache-prefetch` 线程（`cache_controller.py:553-569` 只启动 1 条）串行消费，
   `_storage_hit_query` → `batch_exists`：基类默认实现**逐 key 串行** `exists`
   （`hicache_storage.py:549-558`），file 后端每个 key = 1 次 `os.path.exists`——4,223 次目录查找，
   与此同时**单条** `hicache-backup` 线程正在同一目录以 ~450 文件/s 的速率创建 tmp + `os.replace`
   （`_set_file`，`hicache_storage.py:1105-1142`），两线程争 GIL 与目录元数据。
3. 调度线程每个 step：`check_hicache_events`（`:4513`）→ `_vote_prefetch_deadlines`（`:4565→:2897-2925`）。
   admission 侧 `check_prefetch_progress`（`:3057-3085`）首次 blocked poll 时
   `_start_storage_prefetch_wait`（`:2844-2856`）起表，预算
   `timeout_s = prefetch_timeout_base(1.0) + pages × prefetch_timeout_per_page`
   （`prefetch_timeout_per_page = page_size/1024 × 0.25`，init `:570-578`、`:494`）。
4. 到点 → `_mark_prefetch_deadline_expired`（`:2870-2877`）→ **取消该请求的全部 L3 重试**
   （`storage_prefetch_retries.cancel`，`:2872`）→ terminate。
5. 记账不对称（**本症状的核心**，源码可见）：
   - 命中只有被调度线程从 `prefetch_hit_queue` 取出时才入账：`_drain_and_alloc_storage_hit`
     → `_record_storage_prefetch_hit`（`:3860`，定义 `:3315`），此刻才建
     `_storage_prefetch_hit_remaining_by_reqid`；
   - 若 deadline 票决发生在**命中入账之前**：`check_prefetch_progress` 走 `:3077-3082` 分支
     （host_indices 尚为 None）→ `_finish_storage_prefetch(request, 0, reason="deadline")`
     → `_resolve/_finish` 因 remaining 为 None **直接 return，什么都不记**（`:3336-3343`、`:3364-3369`）；
     同时无 WARNING、无 unfulfilled、无重试（已 cancel）。
   - 若发生在入账**之后**：remaining=4,223 → 记 `unfulfilled{deadline}`（trial03/04 的 8,318）。
   - 对照注释 `:3866`："A storage miss can retry; a policy deadline cannot."

**结论**：4 个零命中 trial 的 R 请求都真实发起了查询（TTFT 2.17–2.59s ≈ 预算 2.03s + prefill 0.1–0.5s
——与命中 trial 的 TTFT 同分布，说明它们同样等满了 deadline 窗口），但查询结果没能在预算内
返回/入账，被票决静默终止。"W 前缀已落盘但索引没了"的正确表述是：
**W 前缀在盘上、索引（目录项）也在，是查询本身没能在 2.03s 内穿过被写风暴拖慢的单线程存储管线**。

### 2.3 "文件在盘上"的推断强度标注

- 源码可见：L3 写队列 FIFO（`Queue()`，`cache_controller.py:544`）；W 的 L3 写在 D2H ack 后同 tick 入队
  （`_finish_write_through_ack`→`write_backup_storage`），全部早于 E（W D2H 01:55:48 完成，E 01:55:57 开始）；
  无写失败路径触发。
- 实证：backuped 在 01:56:12 已达 21,134 > W 总量 25,344 的 83%，01:56:28 达 32,654 > 25,344+尾；
  trial04（第 5 个写入的 W）全 span 命中 → FIFO 对更早的 W0–W3、同批的 W5 同样成立。
- 推断（强，不可直接观测）：W0/W1/W2/W5 四个 span 在 M 时刻完整在盘。
  如需钉死：在修复臂上重跑 C2（见 §6 EXP-A），修复后这四 trial 应转为全 span 命中——
  这同时是该假说的判别实验。

---

## 三、症状②：drain 不收敛 —— 单线程 L3 写者的吞吐失配

### 3.1 写路径全景（源码可见）

```text
insert 步进 → BackupKV → _execute_and_commit_kv_backup（unified_radix_cache.py:2227）
  → _execute_kv_backup（:2283；host 不足先 evict_host）
  → controller.write(flush=False)（延迟合并，:2302）
  → 每 tick writing_check（:4293，只等 D2H——ongoing_write_through）
  → start_writing 合并提交 D2H（cache_controller.py:1101-1136）
  → D2H ack → _finish_write_through_ack（:2349）→ write_backup_storage（:2572-2605）
     · build_storage_backup_spec：not backuped → None → 静默跳过（不 enqueue、不 ack、无计数）
     · enqueue 到 backup_queue + ongoing_backup 持 host lock ref（:2599-2602）
  → 【单线程】hicache-backup（cache_controller.py:1716-1731）
     → _page_backup（:1683-1706）：按 STORAGE_BATCH_SIZE=128 页一批
     → page_set_func → HiCacheFile.batch_set（hicache_storage.py:1144-1154）
       → 逐 key set（:1089-1103）→ _set_file（:1105-1142）：
         先 _file_exists 快路径（1 次 stat）→ evictor.reserve（惰性 no-op）→
         tofile(tmp) → os.replace —— 每 token 3+ 次 syscall、两次目录变更
     → 失败语义：break + operation.completed_tokens 部分ack（:1698-1702，有 WARNING，本次未触发）
  → ack_backup_queue → 调度 tick _drain_backup（:3929-3973）→ _finish_backup_ack（:4032-4085）
     · shortfall 记 hicache_storage_backup_failed_tokens + WARNING（:4064-4085；本次为 0）
     · cache 模式注释（:4044-4047）："Nothing is retried here; the tail stays un-backed
       until a later write-through covers it."
```

### 3.2 为什么 4× 池才发作（结构型形态切换的力学）

- **1× 池（A1/A2，43,024 tokens）**：host 池在 E 早期就满，多数 E 节点在 `_execute_kv_backup` 的
  host 分配/evict 处被拒（staging 饥饿面）→ D2H 仅 75,662，L3 队列 240s 内排干（需求 ≤315 tok/s < 实际 ~450）。
- **4× 池（C2，172,096 tokens）**：host 准入放量 → D2H 204,686（2.7×）→ L3 单线程写者需求
  204,686/~450 tok/s ≈ **455s > 240s 观察窗**。瓶颈从 L2 准入面（host_capacity unfulfilled）**切换**到
  L3 写吞吐面（deadline + drain 积压）——这就是"结构型"的机制学表述。
- 附带伤害（源码可见 + 实证）：
  - in-flight L3 写持 host lock ref（`write_backup_storage:2601` → `_finish_backup_ack` 才 dec），
    86,016 tokens 的 host 槽位被钉住，进一步压缩可用 L2；
  - `dropped{write_through_unbacked_eviction}`=13,312：E 阶段设备驱逐追上了尚未完成 D2H 的
    write_through（发射点 `unified_radix_cache.py:1146-1164`），这部分 token **永久无备份**（有计数器）。

### 3.3 "42% 从未 ack"的性质裁决

- **不是静默丢弃**：盘上文件数与 acked 全程一致；无失败告警；backuped 在杀死前仍在爬升
  （最后 2s +1,024）→ 队列活着，只是慢。
- **存在一条源码可见的"永不 ack"路径但本次未量化触发**：`write_backup_storage` 的
  `build_storage_backup_spec → None` 静默跳过（`:2578-2579`）——D2H 已计数、L3 未入队、无差额计数器。
  本 run 中 W 段无 host 压力、E 段节点在 D2H ack 前被钉住，该路径贡献应为 0 或极小（推断）。
  修复方案 ③-c 顺带补上这个可观测缺口。
- D2H(255,246) 与 acked(124,814) 的终值差 = 杀死时仍在队列的 ~79.9k + M 阶段新入队的 ~46k 中未完成部分
  ——纯吞吐解释，无需引入丢弃假设。

---

## 四、症状③：deadline 未履约 —— 预算公式、计时起点与 drain 的交互

（机制已在 §2.2 展开，此处只列修复设计需要的要点。）

- 预算：`unified_radix_cache.py:570-578`（init 默认 base=1.0s、per_ki_token=0.25s）、`:494`（page 换算）、
  `:2844-2856`（起表：**首次 blocked admission poll**，注释 "Start at the first blocked admission poll;
  refetches keep this budget"）。预算隐含假设：L3 读吞吐 ≥ ~4 Ki-token/s 且查询本身快。
- 计时覆盖：查询（单线程 prefetch 线程，4,223×os.path.exists）+ 命中入账 + host 分配
  （满池时要 evict_host）+ IO 读（单线程 prefetch-io 线程，4,223 个 12KB 文件、每 128 页一批）——
  **全部记在一个 ~2.03s 预算里**。
- 切断语义：`_vote_prefetch_deadlines`（`:2897-2925`）到点 terminate；装载只完成 0–1 批
  （128/4,223）→ `unfulfilled{deadline}`（`:3117-3121`）。
- 与 drain 的交互：deadline 只在 4× 臂显性化，因为只有 4× 臂在 M 时刻仍有 ~86k tokens 的写积压
  在同目录、同 GIL 上与查询/装载竞争（A1/A2 臂 R TTFT 同样 ~2.1–2.5s——**预算失配在等压臂就存在**，
  只是那里 host_capacity 先把命中掐掉，轮不到 deadline 记账）。
- README 的"重试梯 8×8s 超限放行"表述需修正：重试梯（`storage_prefetch.py:22-78`，
  poll-interval/max-attempts=8/8）在本次 M 阶段**从未启动**（0 条 re-issue debug 日志）——
  因为 deadline 到点即 `retries.cancel`（`:2872`），且"miss 可重试、deadline 不可"（`:3866`）。
  真正放行机制是 timeout 票决，不是重试梯到顶。

---

## 五、修复设计（每症状 1–2 个最小方案）

设计纪律：单方案改动面 ≤200 行；不动存储格式与跨秩协议；每个方案给回归测试思路、副作用、
以及明确"不做的方案"。以下方案可独立合入、互不依赖（F1/F2 叠加效果最佳）。

### F1（症状①+③共治，推荐首选）：file 后端 batch_exists 的 scandir 快照化

- **改动**：`HiCacheFile` 重载 `batch_exists`（当前继承基类逐 key 串行，`hicache_storage.py:549-558`）：
  legacy 方案 + metadata cache 关闭 + key 数 ≥ 阈值（如 32）时，**一次 `os.scandir`** 建目录名集合，
  再按序数连续命中；小批量或 unified/metadata-cache 路径回退逐 key。evictor 启用时对命中的连续前缀
  逐 key 复核一次（防止快照后即刻被驱逐的正向误报）。
- **改动面**：~30–40 行，单文件，无协议变化。
- **机理**：把 4,223 次（每次可能被 GIL 打断）目录查找压成 1 次 O(目录) 扫描（~10–30ms @ 12 万项），
  查询阶段从 ~2s 降到 <50ms——deadline 预算几乎全额留给真正的 IO 装载；同时缓解与写线程的 GIL 争用。
- **回归测试**：
  - 单元（离线）：临时目录造 N 页文件，`batch_exists` 返回最长连续前缀；缺首页→0；快照后删除一页
    （evictor 开启）→ 复核后返回缩短值；unified_suffixes/metadata_cache 路径回退正确。
  - 集成：repro-31600 C2 臂重跑（§6 EXP-A），判据 = 零命中 trial 转 ≥4,223 全 span 命中。
- **副作用**：目录极大（百万页）且批量小时 scandir 反而贵 → 用 key 数阈值门控；快照与并发写的窗口
  语义 = "视为一次一致读"（新写入的页视为 miss，与现状一次 stat 的窗口语义等价或更保守）。
- **不做**：给 metadata_cache 默认开启（引入 TTL 正向误报面，且 unified 方案下明令不信 cache）；
  重写 file 后端为 fast_file（B1/B2 已证其在等压下 host_capacity=100%，换汤不换药且改动面大）。

### F2（症状③）：deadline 预算的相位拆分 / 自适应

- **F2-a 计时相位拆分（推荐）**：预算只覆盖"装载"相位——`_start_storage_prefetch_wait`（`:2844-2856`）
  改为在命中分配点（`_drain_and_alloc_storage_hit` → `_try_alloc_storage_hit` 成功、`prefetch_buffer.put`
  前后）起表；查询相位交给重试梯兜底（miss 重试本就存在，`poll_miss`）。查询相位另设一个宽松的
  独立上限（如 base×4）防退化后置死。
- **F2-b 自适应预算**：用 ack 观测维护 per-page 读写延迟 EWMA（storage 层已有 duration 直方图管道，
  `metrics_collector.py:2402-2433` 一族），`timeout_s = base + pages × max(0.25/1024, 2×ewma)`。
- **改动面**：F2-a ~15–25 行（unified_radix_cache.py 单文件）；F2-b ~30–40 行（+EWMA 状态与更新点）。
- **回归测试**：单元——用 `sim_storage` 的 `_inject_delay("batch_exists")`（`hicache_storage.py:796-803`
  已有故障/延迟注入钩子）把查询人为拖过预算，断言（a）装载不被切断、（b）查询相位独立上限触发时
  走 poll_miss 而非静默终止；集成——EXP-A/B 臂 `deadline` 增量 ≈ 0 且 R TTFT 中位 ≤ 1.5× P TTFT。
- **副作用**：admission 尾延迟上界变松（等待最慢查询）——用查询相位独立上限 + 队头不拖延规则
  （`storage_prefetch.py:49-53`）约束；F2-b 的 EWMA 在冷启动用名义值，不改变默认行为。
- **不做**：按后端类型硬编码预算表（mooncake/hf3fs 各自特性差异大，硬编码必错）；把 stop policy
  默认改回 best_effort（等于放弃防 hang 保护，A3W 已证 wait_complete 会挂起）。

### F3（症状②）：L3 写队列背压（有界 in-flight）

- **改动**：`HybridCacheController` 维护 `backup_outstanding_tokens`（`write_storage` `:1633-1649` 入队时 +，
  `_finish_backup_ack` 结账时 −）；`write_backup_storage`（`unified_radix_cache.py:2572-2605`）入口处
  超过高水位（如 `2 × (host_pool logical_size) / 8`，或暴露成 extra_config/env）时**跳过本次 L3 写**，
  计一个新计数器（如 `hicache_storage_write_deferred_tokens_total`）。安全性依据（源码可见）：
  cache 模式 L3 缺口的既有语义就是"host 副本被驱逐且再次命中时会重发"
  （`_finish_backup_ack` docstring `:4044-4047`）——被跳过的节点仍是 host-backed，后续 write-through
  周期可补写；不新增任何树状态。
- **改动面**：~40 行（controller + cache 两文件 + 计数器注册）。
- **回归测试**：单元——高水位下 `write_storage` 被拒并计数，节点 backuped 状态不变；ack 后水位回落、
  后续写恢复；恒等式 `D2H − dropped − deferred == acked + in-flight` 在任意 tick 成立。集成——EXP-C 臂
  240s 排干（enqueued−acked ≤ 1%），W 存活 ≥5/6，`dropped{write_through_unbacked_eviction}` 不恶化。
- **副作用**：burst 期部分前缀的 L3 落盘延迟到下一个 write-through 周期（若永不再命中则该前缀
  永不落 L3——这是**语义取舍**：有界资源 vs 最终落盘；水位默认值需保守）；TP>1 下各秩独立跳过会造成
  L3 页链跨秩空洞吗？——不会：页链按秩后缀分片，各秩只写自己的页（legacy 方案），跳过只影响本秩页的
  时序，不影响他人；但**会让 batch_exists 的跨秩 MIN 折叠看到不齐的边界**（现状本来就允许
  "写入在途→查不到"，语义等价）。
- **不做**：
  - 多条 backup 写线程（**明确拒绝**）：`_drain_backup` 的跨秩 MIN 结算假设"ack 按提交顺序成对"
    （`:3951-3960` 注释 "the acks pair by position"），乱序完成会破坏 rank 一致结算，需按 op-id
    做 collective 对齐——改动面远超 200 行且引入死锁面；
  - file 后端批量页合并写（改存储格式，破坏内容寻址页共享，拒绝）；
  - 调大 `STORAGE_BATCH_SIZE`（治标不治本：瓶颈在每页 3+ syscall 与 Python 循环，不在批量参数）。

### F4（症状①的归因面，纯可观测性）：deadline 截断的零记账出口补计数

- **改动**：`check_prefetch_progress` 的 `:3077-3082` 分支：当 `deadline_expired` 且该 op 从未入账
  （remaining 为 None）时，计 `storage_prefetch_deadline_cut_queries_total`（或复用
  `unfulfilled{reason=deadline_query_cut}` 非 token 维度计数器）+ 一条 WARNING（含 rid/span）。
  同样给 `declined_rate_limited`（`:2742-2746`）与 `declined_*` 族补 Prometheus 出口
  （§7.2 已证的"只在快照、无 counter"缺口，14 键 → 至少导出 declines）。
- **改动面**：~20–30 行（含 metrics_collector 注册）。
- **回归测试**：单元——构造"查询未返回即到点"序列，断言计数器 +1 且恒等式不被污染（它不是 token 维度）；
  集成——EXP-A 的 main 对照臂上，该计数 == 零命中 trial 数（4）。
- **副作用**：无行为变化；预注册 label 集合要同步（§7.1 已有两处 label 失配前科，避免再犯）。

### 优先级与叠加

推荐合入顺序：**F4（先可观测）→ F1（治本提速）→ F3（背压）→ F2-a（兜底）**。
F1 落地后症状①③的发病率应大幅下降，F2 变成防御性兜底；F3 独立解决②并顺带把 86k 钉住的
host 槽位解套。四方案合计 ~150–200 行，互相正交。

---

## 六、验证实验设计（简版，复用 kvtier harness / G 门禁风格）

基线臂一律 = 证据包原臂重跑（同 commit、同 seed 协议）；patch 臂 = main + 单一修复（不叠加，
除最终 EXP-D）。G0（环境闸）沿用证据包清单：池容量 ±2%、token_capacity、W 落盘正增量、
storage 目录清空、配置快照含 commit。

### EXP-A：F1（scandir batch_exists）——症状①的判别 + 症状③缓解

- 臂：`C2+F1` vs `C2-main`（cache/file/0.48/nosalt，6 trial）。
- 门禁：
  - G0 同基线；
  - **G1-A（判别性）**：`C2+F1` 的 R storage 命中 trial 数 ≥5/6 且每 trial hit_tokens ≥4,223
    （main 为 2/6）——同时证伪/证实"W 文件在盘"推断（若仍 0 命中 → §2.3 推断错，回头查盘）；
  - G2-A：`unfulfilled{deadline}` 增量较 main 下降 ≥80%；`host_capacity`、`dropped` 不增；
  - 恒等式（hit = fulfilled + Σunfulfilled）逐臂闭合。
- 观测补充：R/P TTFT 中位比（预期从 ~15× 降到 ≤3×）。

### EXP-B：F2-a（deadline 相位拆分）——症状③

- 臂：`C2+F2a` vs `C2-main`。
- 门禁：G1-B = `deadline` 增量 ≈0（≤128 tokens 容差）∧ 无请求挂起（沿用 A3W 的 600s 客户端超时探针）；
  G2-B = R TTFT 中位 ≤ 1.5× P TTFT（等满 2s 窗口的 stall 消失）。
- 注意与 F1 的交互：单独跑 F2-a 时查询仍慢，预期装载阶段被保护但整体 TTFT 改善有限——
  这正是要分开归因的原因。

### EXP-C：F3（写队列背压）——症状②

- 臂：`C2+F3` vs `C2-main`，另加 `A1+F3`（回归检查：小池不劣化）。
- 门禁：G1-C = 240s 排干（post_drain `enqueued−acked ≤ 1% × enqueued`）∧ W 存活 ≥5/6（storage>0）；
  G2-C = `hicache_backup_dropped/deferred` 有界且 `write_through_unbacked_eviction` 不高于 main ±10%；
  G3-C = M 阶段 `host_used` 高水位比 main 低 ≥30k tokens（钉住解除的直接读数）。

### EXP-D：叠加剂量复扫——"结构型"消失判定

- 臂：`main` vs `全修（F1+F2a+F3+F4）`，剂量 1×/2×/4×（A1/C1/C2 同构），如 #49226 届时已合入
  另加一对含/不含 #49226 的对照。
- 门禁（复刻原 G2 的判据方向）：G1-D = 各剂量下 R storage 命中 ≥5/6 且**命中数不随池容量反向变化**
  （形态切换消失）；G2-D = 1× 臂 host_capacity 面（staging 饥饿）是否仍需 #49226——分离
  "L2 准入面"与"L3 IO 面"两个修复平面的贡献。

### 通用纪律

- 每臂保留：server.log（debug 级）、samples 1Hz、metrics-final、storage_snapshots、dmon、结果包；
  patch 臂额外记录 patch diff 的 sha（结果包 `args` 注入）。
- F4 的计数器在所有 main 对照臂上采集（它就是"零命中 trial 数"的直接读数）。

---

## 七、与 PR #49226 的关系判定

**判定：互补（complementary），无冲突（no interference），零覆盖（zero overlap on these symptoms）。**

| 维度 | #49226 | 本设计三症状 |
|---|---|---|
| 失败平面 | L2 admission 窗口：prefetch 完成的 host 页在 admission 前被逐（`cache_admission_shortfall`）；瞬态 host 满时对已确认命中 FIFO 排队 | L3 存储 IO：单线程写者吞吐（②）、timeout 预算（③）、记账不对称（①） |
| 触发条件 | host 容量压力存在（C2 中 `host_capacity`=0，**条件不出现**） | L3 写积压 / 查询延迟超 ~2s 预算（C2 中必然出现） |
| 代码面 | host radix 生命周期 / admission 路径 | storage 后端 / storage 线程 / stop policy |

- **#49226 合入后仍存在的症状**：①②③全部。C2 的三症状没有一个落在"prefetch 完成→admission"窗口内
  ——命中根本没有完成装载（③切断）或完成查询（①切断），#49226 的 pin 无页可 pin。
- **潜在正向交互**：#49226 的 FIFO 容量排队会让 1×/2× 臂的 `host_capacity` 饥饿变成"排队等待"，
  这会增加**到达 L3 查询阶段的请求比例**——若无本设计，等价于把更多流量送进已经过载的单线程
  存储管线（下游放大）。建议 EXP-D 的含/不含 #49226 对照专门盯 `deadline` 与 drain 深度。
- **无冲突的技术理由**：本设计四方案不触碰 host radix 生命周期、admission 门、pin 语义；
  F3 的跳过点在 `write_backup_storage`（L3 入队前），与 pin-until-admission 正交。

---

## 八、新 issue 文案草稿（英文，只写不发）

> 状态：**DRAFT — 未发布**。发布前需：重跑 EXP-A 判别臂钉死 §2.3 推断、核对上游 main 是否已有
> 相关改动、按维护者模板调整。

**Title:** HiCache file backend: single-threaded L3 writer cannot drain write-through bursts; timeout policy cuts in-flight reads and silently drops the accounting (main @ e183c415b)

**Body:**

### Summary

On current main (`e183c415b`, single RTX 4090 48GB, `--enable-hierarchical-cache`, `file` storage backend,
`cache` host mode, `write_through`, default `page_size=1`, default storage prefetch policy `timeout`),
a phased prefix-cache microbenchmark (write 6 isolated 4,224-token prefixes → 456 filler requests /
466,944 tokens to evict device+host → 240s drain wait → 6 paired reload/cold-prefill trials) shows a
compound failure that only appears once the host pool is large enough to admit the full write-through
volume (4× pool = 172,096 tokens). With a small pool the dominant signature is
`storage_prefetch_unfulfilled_tokens_total{reason="host_capacity"}`; with the 4× pool that drops to
zero and three new symptoms appear:

1. **L3 lookups return nothing for prefixes that are on disk** (4/6 trials: `device=0, host=0, storage=0`),
   while the other 2 trials find the full 4,223-page span — so the pages and their content-addressed
   keys are demonstrably intact.
2. **Write drain does not converge**: after a 240s idle drain window, D2H-submitted tokens 204,686 vs
   L3-acked 118,670 (~42% still in flight). Files on disk exactly match the acked counter at every
   snapshot (119,105 ≈ 118,670; 124,427 ≈ 124,814); no write-failure warnings — the queue is alive,
   just slow (~400–500 tokens/s ≈ 5–10 MB/s effective on NVMe).
3. **`unfulfilled{reason="deadline"}` = 8,318 tokens**: the two lookups that did complete were cut by
   the timeout stop policy after loading 0 and 128 of 4,223 tokens.

Token accounting closes exactly on every arm (`hit = fulfilled + Σ unfulfilled`), so these are three
windows of the same pipeline, not bookkeeping drift.

### Key observations

- All 12 reload requests across three arms (1×, 1× nosalt, 4× pools) have TTFT 2.09–2.59s ≈
  `1.0s + 4223 × 0.25s/1024-token` ≈ 2.03s + prefill — including the six "missed" trials. They were
  not never-queried; they waited out the deadline budget and were admitted cold
  (`check_prefetch_progress` → `_mark_prefetch_deadline_expired` also cancels all L3 retries:
  "A storage miss can retry; a policy deadline cannot").
- The file backend's existence check is one `os.path.exists` per page in a serial loop
  (`hicache_storage.py` `batch_exists` base implementation), executed by the single
  `hicache-prefetch` thread, while the single `hicache-backup` thread is writing ~450 small files/s
  (tmp + `os.replace` per token at `page_size=1`) into the same directory. GIL + directory metadata
  contention plausibly explains queries exceeding the 2.03s budget.
- Defaults rule out index eviction as the cause: `SGLANG_HICACHE_FILE_BACKEND_MAX_SIZE` defaults to
  none (evictor inert) and `SGLANG_HICACHE_FILE_BACKEND_ENABLE_METADATA_CACHE` defaults to false,
  so `exists` is disk truth and nothing evicts files.
- Observability gaps that made triage harder, possibly worth their own fix:
  (a) a deadline-expired lookup whose hit was never drained from `prefetch_hit_queue` records
  **no counter, no log, and no retry** — externally indistinguishable from a storage miss;
  (b) the `declined_*`/`revoked_*` prefetch outcome stats are only exported in the
  `StorageMetrics` snapshot, not as Prometheus counters.

### Reproduction config

```bash
--mem-fraction-static 0.35 --max-total-tokens 358532 \
--hicache-ratio 0.48            # host pool 172,096 tokens (4× the 0.12 arm)
--hicache-io-backend kernel --hicache-mem-layout page_first \
--hicache-write-policy write_through \
--hicache-storage-backend file  # default extra_config: no evictor cap, no metadata cache
# storage prefetch policy left at default: timeout (budget = 1.0s + 0.25s per Ki-token)
```

Workload phases per run: warmup+flush → W (6 × 4,224 tokens) → 10s wait → E (456 requests, 466,944
tokens) → 240s drain wait → M (6 paired reload/cold-prefill trials). Full evidence packages (per-second
metric samples, server logs at debug level, storage directory snapshots, result JSONs) available on
request.

### Preliminary localization (main @ e183c415b)

- Single storage worker threads: `python/sglang/srt/managers/cache_controller.py`
  `_start_storage_worker_threads` (one `hicache-prefetch`, one `hicache-prefetch-io`, one
  `hicache-backup`).
- Per-page serial `batch_exists`: `python/sglang/srt/mem_cache/hicache_storage.py:549` (base) and
  per-key `set`/tmp/`os.replace` loop: `:1089-1154`.
- Deadline budget and start point: `python/sglang/srt/mem_cache/unified_radix_cache.py:570-578`
  (1.0s + 0.25s/Ki-token), `:2844-2856` (timer starts at first blocked admission poll; covers
  query + allocation + IO), `:2897-2925` (per-tick MAX vote terminates the op).
- Silent-zero accounting: `:3077-3082` (deadline + no host indices → no-op finish when the hit was
  never recorded at `:3860`); retry cancellation at `:2872`.
- Drain: unbounded `backup_queue` (`cache_controller.py:544`), single consumer `:1716-1731`,
  in-flight writes pin host lock refs until ack (`unified_radix_cache.py:2599-2602`).

### Suggested directions (not prescriptions)

1. Snapshot-based `batch_exists` for the file backend (one `os.scandir` per batch instead of
   per-key stats) — would move the query phase from seconds to milliseconds at `page_size=1`.
2. Start the timeout budget at load time (hit allocation) rather than the first blocked poll, so
   query latency is not charged to the load budget.
3. Bounded in-flight L3 write tokens (defer new `write_storage` submissions above a high-water mark;
   the existing cache-mode semantics already re-send a span when its host copy is evicted and re-hit).
4. Export a counter/log line for deadline-cut lookups that never recorded a verdict.

Happy to rerun any arm or share the harness. Not sure whether some of this is intended behavior of the
`file` backend at `page_size=1` (i.e., "use fast_file / a real page size"), so flagging for triage
rather than proposing a concrete patch.

---

## 九、"源码可见" vs "推断" 总表

| # | 命题 | 类别 | 依据 |
|---|---|---|---|
| 1 | file 后端默认无 evictor、无 metadata cache；exists=磁盘真相 | 源码可见 | `environ.py:928-934`、`lru_file_evictor.py:90-92/159-160`、`hicache_storage.py:1159-1187` |
| 2 | L3 查询/写各只有单线程 | 源码可见 | `cache_controller.py:553-569` |
| 3 | deadline 预算公式与计时起点（覆盖查询+分配+IO） | 源码可见 | `unified_radix_cache.py:570-578/2844-2856` |
| 4 | deadline 到点取消全部重试；"miss 可重试、deadline 不可" | 源码可见 | `:2872`、`:3866` |
| 5 | 命中未入账时被 deadline 终止 → 零记账零日志 | 源码可见 | `:3077-3082` + `:3336-3369`（remaining None 早退）+ `:3860`（入账唯一入口） |
| 6 | L3 写 FIFO、W 先于 E 入队；in-flight 写钉 host 槽 | 源码可见 | `cache_controller.py:544`、`unified_radix_cache.py:2349-2364/2572-2605` |
| 7 | 写失败有告警+部分 ack+shortfall 计数（本次未触发） | 源码可见 | `cache_controller.py:1683-1706`、`unified_radix_cache.py:4064-4085` |
| 8 | `build_storage_backup_spec→None` 静默跳过 L3 写（无差额计数） | 源码可见 | `:2578-2579` |
| 9 | M 时刻 W0/W1/W2/W5 的页文件完整在盘 | **推断（强）** | FIFO+计数器+trial04 全 span 命中；EXP-A 为判别实验 |
| 10 | 4 个零命中 = 查询未在 2.03s 内返回/入账（而非盘上缺文件） | **推断（强）** | §2.2 排除链 + TTFT 同分布（2.17–2.59s）+ 无任何 decline/重试/失败痕迹 |
| 11 | 查询变慢的具体机制 = GIL 争用 + 目录元数据争用 | **推断（中）** | 单线程写者 ~450 文件/s 同目录同期运行；未做 profiler 级取证 |
| 12 | L3 写吞吐 ~400–780 tok/s 是 page-per-token 小文件 + Python 循环的固有上限 | 实证（本机） | samples 差分；机理定位为推断 |
| 13 | #49226 合入后三症状仍全部存在 | 推断（强，基于平面分析） | §7；EXP-D 含/不含对照为验证实验 |
| 14 | README 原"L3 miss 重试梯 8×8s 超限放行"表述 | **修正** | 重试梯本次未启动（0 条 debug 痕迹）；放行机制是 timeout 票决（§四） |

---

## 附：本设计的边界

- 未运行任何新实验（纯静态 + 已有证据包二次分析）；所有"修复后预期"均为设计推演，待 §6 实验裁决。
- C2 单机单卡 TP=1：跨秩（TP>1/PP）行为仅做源码推演（`_drain_backup` 的按位配对、deadline 的
  MAX 票决），未实验验证。
- fast_file / buffer_only / mooncake 等其它后端与模式的面（B 臂、A3/A4、A3W）不在本设计范围，
  仅作为对照引用。
