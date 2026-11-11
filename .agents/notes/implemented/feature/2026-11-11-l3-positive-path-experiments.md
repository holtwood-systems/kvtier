# Agent Note: L3 正向路径实验（分离索引存活、命中归属与 staging 容量）

Status: implemented

## Problem

2026-11-11 cache 模式实跑（`results-20261110T233023Z.json`）记录
`status=failed / index_evicted`（R 三层全 0），与 11-05 RTX 4090 负结果
同构。当时结论「L3 索引随 host 条目同死」掩盖了真实机制：server.log
显示 `prefetch success loaded=999`——**数据确实从 storage 加载过**，
只是没被消费归因。需要实验分离三层因素：索引存活、命中归属
（namespace）、staging 容量。

## 机制分解（源码 + 实测交叉验证）

### M1 · cache 模式的 salt 归属缺陷（已被 B 臂实证）

cache 模式 prefetch 的 `prefetch_key.cache_salt` 取自
`prefetch_anchor_info(last_host_node)`；anchor=root 时 salt=None →
加载页以 saltless 命名空间插入 radix → 带 `l2-*` salt 的 re-match 经
`RadixKey._check_compatible` 判不可见 → `host_hit=0` →
`split_cached_prefix_by_tier(0,0,999)` 全零 → meta 报告 `index_evicted`，
实际是一层 namespace 缺陷，不是索引/存储缺失。

实证对照：B 臂仅去掉 `cache_salt`（同一 cache 模式、同一负载形状），
所有 8 次运行首 R 稳定 `storage>0`（640–999）并被归属；salted 臂同
形状全零。缺陷成立。

### M2 · buffer_only 模式绕过整个问题（A 臂正向）

`hicache_host_memory_mode=buffer_only` 下 host 池仅作瞬时 staging，
树不持 host 值、`evict_host` 恒 0；storage 命中经
`staged_prefetch_tokens` 直接拼成 `host_hit_length`，不依赖 radix 路径
可见性。A 臂（同一 workload 含 salt）拿到 clean 6/6：
R `storage=1373–1536`，P 全冷，`status=complete`，审计通过。

边界：R 的 staged fill 在 commit 时与请求自身冷 prefill 竞争，全部
`dropped reason=overlap`——命中归属按加载量记账，但不能证明 staged
KV 被用于计算；W-002 的 `fill committed` 证明消费路径可达。

### M3 · host staging 饥饿（B 臂发现的新失败模式）

cache 模式下 prefetch staging 分配与在途写回竞争同一个 host 池
（`prefetch_capacity_limit = 0.5 × pool`；`evict_host` 只释放
device-evicted 且未 `protect_host` 的叶子）：

- E 阶段 395k token write-through 经 host staging 转发，钉住节点至 ack；
- WSL2 文件后端 drain ~190 文件/s，且部分写被静默丢弃永不 ack
  （实测 enqueued=84945 时 acked 停滞于 55220）；
- M 阶段每次首 R 仅分到 722–999 token（`loaded` 如实截断），后续 R
  连 `prefetch_threshold=256` 都凑不齐 → `_revoke_pending_prefetch`
  静默撤销（debug 级日志，info 不可见）→ 冷 prefill → `storage=0`。

`hicache_host_used ≈ 43024/43024`（L2 驻留占满），可用空间 = 当前
未驻留且未钉住的碎屑。1-trial B 臂（单 R）拿到 clean
`status=complete`（R-000 `storage=722`），因为单请求只需一次分配。

### 附：buffer_only 在 pinned commit 的账目缺陷（本地处置）

- `pool_stats_observer`：`num_used = total − (available + evictable)`
  可为负 → metrics 断言崩溃。本地 clamp 补丁
  （`evidence/sglang-buffer-metrics-clamp-local.patch`）。
- idle 巡检 `available+evictable > total` 报 "pool leak" →
  `SGLANG_ENABLE_STRICT_MEM_CHECK_DURING_IDLE=0` 降为 warning。

## Decision

1. DEV-20 结论更新：原 `index_evicted` 不是「索引随 host 同死」单一
   机制，而是 M1（salt 归属）+ M3（staging 饥饿）叠加；4090 与
   5070 Ti 同构源于 M1 的架构性原因，与环境无关。
2. 正向证据采用 A 臂：buffer_only + file backend 给出 clean 6/6
   complete 包，作为「L3 路径可达且命中可归属」的主证据。
3. B 臂保留为机制对照：1-trial complete 包 + 6 个结构化 failed 包，
   证明 M1 与 M3；不追求 6-trial cache 阳性（容量问题非本任务边界）。
4. harness 修正（全部带注释注明实测来源）：
   - `run_server.sh`：`HICACHE_HOST_MEMORY_MODE` 参数；
   - `workload.py`：`--salt-mode {per-request,none}`、
     `--evict-content-mode {shared,per-request}`（saltless+shared 有
     护栏硬检查）、`--post-evict-wait`（静默收敛轮询）、W/R 逐 trial
     独立内容、P 探针逐 trial 独立且与 W/E 区间全不相交；
   - `validate_configuration`/`validate_results.py`：host_memory_mode
     白名单化；storage 模式 R 判据 `storage>0`。

## Alternatives considered

- **改 sglang 让 prefetch 继承 req.cache_salt**：修 M1 的正解，但 pin
  upstream commit 非本仓代码，超出边界；记录为缺陷候选（anchor=root
  时 salt 应回退到 req.cache_salt）。
- **调大 host 池**（hicache_ratio 0.12→~0.18）：受 10GiB 硬保留约束
  上限 ~0.8GB，对 59k token backlog 仍是碎屑量级，不解决 M3。
- **减小 E 驱逐量**：device 池 358k token 是硬约束，E 需 >360k，
  backlog 无法显著缩小。
- **drain 判据用 `acked>=enqueued`**：丢弃写永不 ack，该条件不收敛
  （实测 600s 超时）；改为静默收敛轮询。
- **调大 prefetch_timeout**：`loaded` 由 alloc 容量决定而非超时，无关。

## Risks / 证据边界

- R 的 `storage=N` 是 host staging 截断后的**部分命中**（链查询命中的
  前缀更长，alloc 只装得下 N）；`paired_ttft_ratio ≈ 16.8` 表示 L3
  命中显著慢于冷 prefill——本组证据只证明路径可达与归属正确，
  不是性能声明。
- buffer_only 的 overlap-drop 意味着不能证明 staged KV 被用于计算；
  已用 W-002 `fill committed` 佐证消费路径存在。
- 缺陷候选（未修、未向上游报告）：M1 salt 归属、M3 小池下 prefetch
  饿死、buffer 模式两处账目断言。

## Consequences

- 证据包：A 臂 complete（`evidence/2026-11-11-wsl2-rtx5070ti-l3-buffer/`）、
  B 臂 1-trial complete + 6 个结构化 failed 包
  （`evidence/2026-11-11-wsl2-rtx5070ti-l3-cache-nosalt*/`），各自
  README 记录边界；全部通过 `validate_results.py`。
- `.bin` 内容寻址核验：用 sglang 原生 `get_native_hash`（page_size=1
  链式）离线确认 6 条 content 的 4224 个 key 全部存在于盘——R 失败
  不是存储缺失。
- 验证：29/29 unittest；`npm run verify-notes`。
- DEV-20 在 career-prep 维持「已实施待复核」，结论按上述三层机制修正。
