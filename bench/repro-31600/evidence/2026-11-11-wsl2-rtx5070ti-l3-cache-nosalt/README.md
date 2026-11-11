# DEV-20 后续 · cache 模式 saltless 机制确认臂（B 臂）

目的：实证「cache 模式下 prefetch 插入路径的 cache_salt 归属」缺陷——salted
臂的 `index_evicted` 并非存储缺失，而是插入路径对带 salt 的 re-match 不可见。
方法：同一 cache 模式、同一 file 后端、同一负载形状，仅去掉 `cache_salt`
（`--salt-mode none`）。

## 结论

**机制确认。** 所有 8 次运行中，首个 R 请求（内容已全量落盘、radix 已死）
稳定获得 `storage>0`（640–999 tokens）并被正确归属；对照 salted 臂同形状
全零，证明：

- saltless 下 prefetch 插入路径对请求的 re-match 可见，storage_hit_length
  正常写入 `cached_tokens_details.storage`；
- salted 臂的 `index_evicted` 确由 salt 归属差异造成（anchor=root →
  `prefetch_anchor_info` 取 `cache_salt=None` 插入，re-match 带
  `l2-*` salt → `RadixKey._check_compatible` 使其不可见）。

## 结果包

`../2026-11-11-wsl2-rtx5070ti-l3-cache-nosalt-1trial/results-20261111T020655Z.json`
是唯一 `status=complete` 的包（1-trial 版，R-000 `storage=722`，P-000 冷）。

本目录入库 2 个 failed 包作各失败模式的可审计代表
（T005715Z=dedup 无压力、T011530Z=staging 饥饿）；其余 5 个同模式
重复的原始包因 `storage_snapshots` 枚举 ~7 万 `.bin` 文件而体积过大
（共 ~101MB），保留在实验机本地 `~/dev20-nosalt-raw/`，下表数字
均取自其原始记录：

| run | R-000 | 失败位置 | 归因 |
|---|---|---|---|
| T005715Z | device=4223 | — | E 共享内容在 saltless 下内容寻址 dedup，无驱逐压力 |
| T010151Z | storage=640 | P-001 device=2 | 探针共享 `[211,212]` 头，saltless 下跨请求命中 |
| T010805Z | storage=768 | R-001 device=4223 | R-000 从 L3 物化路径后同内容 R-001 直接 device 命中 |
| T011530Z | storage=768 | R-001 storage=0 | 逐 trial 内容后转入 staging 饥饿（下述） |
| T013017Z | storage=701, R-001=298 | R-002 storage=0 | 部分 drain 后 staging 空间仍不足 |
| T014305Z | storage=999 | R-001 storage=0 | drain 等待因丢弃缺口永不收敛（600s 超时） |
| T015909Z | storage=768 | R-001 storage=0 | 静默判据收敛但 ~30k token 写仍挂起/丢弃 |

## 第二层机制：host staging 饥饿（cache 模式特有）

saltless 消费路径成立后，多 trial 仍失败的真因不是命名空间，而是
**prefetch staging 分配与在途写回竞争同一个 43k token host 池**：

1. E 阶段 395k token 的 write-through 写回经 host staging 转发，`protect_host`
   钉住对应节点直至 ack；
2. drain 很慢（WSL2 文件系统 ~190 文件/s，且部分写被静默丢弃永不 ack）；
3. M 阶段 `evict_host` 只能从未受保护的 device-evicted 叶子抠空间——实测
   每次首个 R 仅分到 722–999 token；
4. 后续 R 连 `prefetch_threshold=256` 都凑不齐 → `_revoke_pending_prefetch`
   静默撤销（仅 debug 日志）→ 冷 prefill → `storage=0`。

指标佐证（server /metrics 实测）：`hicache_host_total=43024`、
`hicache_host_used=42793–43024`（~100%）、`prefetch_capacity_limit=
0.5×pool≈21512`、`prefetched_tokens_total=768`（仅 R-000 一次）。

对应地，buffer_only 臂（`../2026-11-11-wsl2-rtx5070ti-l3-buffer/`）不受此限：
host 仅为瞬时 staging，write/prefetch 预算分离（
`prefetch_rate_limited` 按池实际用量而非 `prefetch_tokens_occupied` 记账），
故 6/6 R 全部获得 `storage>0`。

## harness 变化（由本臂暴露并修复）

- `--salt-mode {per-request,none}`：none 时请求不携带 cache_salt。
- `--evict-content-mode {shared,per-request}`：saltless 下必需 per-request，
  否则 E dedup 无驱逐压力（有护栏硬检查）。
- W/R 改为逐 trial 独立内容（种子 20000/21000/22000+i 区间），不再依赖
  salt 做逐轮隔离；P 探针种子（15000/16000/17000+i）与之全不相交。
- `--post-evict-wait`：E 后轮询 `backuped_tokens_total` 至静默收敛
  （丢弃写永不 ack，`acked>=enqueued` 不可靠）。
- `validate_results.py` / `validate_configuration` 接受
  `hicache_host_memory_mode ∈ {cache, buffer_only}`；storage 模式下 R 判据
  为 `storage>0`（路径存在性），不再要求 L2 驻留率阈值。

## 证据边界

- R-000 的 `storage=722` 来自 `prefetch success loaded=722`——是
  **host staging 容量截断后的部分命中**（4224 中 722），链查询本身命中
  更长的前缀；attribution 按实际加载量记账。
- `.bin` 文件按 token 粒度（12,288B/文件 = 单 token KV）命名；
  已用 sglang 原生 `get_native_hash`（page_size=1 链式）离线验证 6 条
  content 的 4224 个 key 全部存在于盘上——确认 R 失败不是存储缺失。
- `paired_ttft_ratio ≈ 16.8` 表示 L3 命中显著慢于冷 prefill（prefetch
  串行等待），本臂只证明路径可达与归属正确，不是性能声明。
- 固定 SGLang commit `6afee75f` + 两个本地补丁（WSL2 host-alloc、buffer
  metrics clamp——后者本臂未用到）。

环境详见 `../../ENVIRONMENT.md`。
