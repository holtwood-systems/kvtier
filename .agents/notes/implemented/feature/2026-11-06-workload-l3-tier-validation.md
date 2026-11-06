# Agent Note: workload.py 增加 L3 storage 层命中校验契约

Status: implemented

## Problem

`validate_configuration` 硬性要求 `hicache_storage_backend=None`，
`validate_cache_result` 的 R 阶段只把 `cached_tokens_details.host` 计为
有效命中。这反映 L2-only 时代的正确语义——当时 storage 命中不存在。
在 pinned commit `6afee75f9` 上 `--hicache-storage-backend file` 已可用，
2026-11-05 实测 file 后端确实向 `SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR`
写入页数据，但现有契约使任何 L3 配置直接拒跑，三层行为无法被审计。

## Decision

1. 新增 `--expected-storage-backend` 参数（默认 None）：传入后端名时，
   配置校验改为要求 `hicache_storage_backend` 恰等于该值，其余约束不变。
2. R 阶段命中校验在 storage 后端开启时改为
   `host + storage >= min_host_hit_ratio`（两层都是合法缓存命中；
   参数名保持不变，语义扩展为"分层命中"）。P 阶段的"全字段为 0"冷
   prefill 校验不变。
3. 结果 JSON 的 `args` 自动记录新参数，`cached_tokens_details` 逐 trial
   原样保留三层分布——host/storage 拆分交给 evidence README 解读，
   workload 不替读者下结论。

## Alternatives considered

- 只放宽 `--min-host-hit-ratio 0`：会把 storage 命中与彻底 miss 混为一谈，
  且参数校验要求 (0,1]，否决。
- 直接删去 storage_backend=None 的硬检查：破坏既有 L2 基准契约的显式性，
  且让未声明 L3 的旧配置静默变义，否决。
- 用 `--min-tier-hit-ratio` 新参数：与现有参数语义重叠，迁移成本高、
  收益小，否决。

## Risks

- 开启 storage 后 `host` 与 `storage` 的命中归因由上游实现决定，
  两字段拆分可能随 sglang 版本变化；本工具只保证"非冷启"判定，
  不保证两层拆分跨版本稳定。
- file 后端命中延迟与 host 差异巨大，R/P 比值解释需要在 evidence
  README 层声明，工具数字本身不区分。

## Consequences

- `--expected-storage-backend file` + host pool < evict 负载时，
  R 阶段出现 `storage>0` 命中的 trial 能通过校验并落盘。
- 不传新参数时行为与现状逐字节一致（既有 L2 基线可复跑）。

## Verification

2026-11-06 RTX 4090 实测：契约改动正确生效——L3 file 配置通过了
配置门禁并完成 W/E/M 流程，R 阶段校验按 `host+storage` 语义执行，
因两层均为 0 按契约失败（负结果包
`evidence/2026-11-05-linux-rtx4090-l3-file/`，validate 结构合法）。
L2-only 复跑语义不变（storage 恒 0）。离线单测 14/14 通过。

落地与提议有一处简化：`validate_cache_result` 的 host+storage 求和
是无条件的（而非"开启 storage 时才生效"）——L2-only 下 storage 字段
不存在时为 0，行为等价。
