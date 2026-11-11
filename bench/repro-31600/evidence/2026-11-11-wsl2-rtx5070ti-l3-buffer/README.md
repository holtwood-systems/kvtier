# 2026-11-11 · WSL2 / RTX 5070 Ti · L3 file 后端 buffer_only 正向证据

`results-20261111T004850Z.json`：`status=complete`，审计
`validate_results.py` 通过（trials=6，paired_ttft_ratio_median=24.538）。

## 结论

在 pinned commit `6afee75f9` + file 后端 + `buffer_only` host 模式下，
**L3 存储命中链端到端可用**：6/6 次 R 测量全部出现
`cached_tokens_details.storage ∈ [1373, 1536]`（>0），P 探针全冷。
命中可跨 `cache_salt` 命名空间成立——storage 键是无 salt 的内容 hash
链（`get_hash_str(tokens, last_hash)`），与 radix 命名空间无关。

与同日 cache 模式负结果（`index_evicted`，R 全 0）形成机制对照：
cache 模式下 prefetch 也会加载，但插入的 host 路径以 anchor=root 的
`cache_salt=None` 命名，带 salt 的 re-match 不可见，命中无法被消费；
buffer_only 的 staged prefetch 不依赖 radix 路径存活，命中直接计入
`storage_hit_length`。

## 重要限定（如实记录）

1. **meta 的 storage 计数是"从 storage 加载的 token 数"，不等于被
   请求消费的 KV**。本次 6 个 R 的 staged prefetch 全部被记
   `dropped reason=overlap`：fill commit 时请求自身的 cold prefill
   已写入 571 token 的 live 路径，重叠即丢弃。正向证据链为
   「storage 查询命中 → 加载至 host → meta 归属」，消费（fill
   committed）在本 workload 下未发生；W-002 的
   `fill committed filled=531` 与另一运行中 P-001 的
   `fill committed filled=1024` 证明消费路径可达。
2. **驻留率是部分的**：storage 中可命中的链长 ~1400-2900 token，
   远小于 4224 前缀；原因未完全定位（链上首个缺失页提前截断
   batch_exists）。
3. **TTFT 比率 16.8–32.6×**：L3 reload 在本配置下远比冷 prefill 慢
   （prefetch 查询+加载计入 TTFT）。该数字描述路径延迟，非加速收益。
4. **E 阶段每个请求的 staged prefetch 同样 overlap 丢弃**
   （`tokens_wasted=1023`）——内容寻址使 E 命中 W 的共享前缀页。
5. **运行用了两处 workaround，均不改变缓存语义**：
   - `sglang-buffer-metrics-clamp-local.patch`：buffer 模式下
     `evictable_size` 计入不占设备池的节点，`num_used` 可为负触发
     metrics 断言；本地补丁仅钳制展示值。
   - `SGLANG_ENABLE_STRICT_MEM_CHECK_DURING_IDLE=0`：同一账目失真
     触发 idle 巡检 "pool memory leak" 崩溃；降为 warning（告警行
     留在 server.log 中）。
   另沿用 `evidence/2026-10-26-wsl2-l2-baseline/sglang-43375-local.patch`
   （WSL2 host 分配）。

## 配置

- 服务器：third_party/sglang `6afee75f9`（editable，含上述本地补丁），
  `MEM_FRACTION=0.35`（device pool 358,532 tokens）、
  `HICACHE_RATIO=0.12`、`buffer_only`、write_through、kernel IO、
  page_first、file backend（`/tmp/sglang-l3-buffer6`，run 时新建）。
- workload 参数：`--num-trials 6 --prefix-tokens 4096 --tail-tokens 128`
  `--expected-storage-backend file --salt-mode per-request`（默认）。
- 模型 Qwen2.5-0.5B-Instruct HF BF16。

## 文件

- `results-20261111T004850Z.json`——complete 结果包（审计通过）。
- `server.log`——对应运行日志（prefetch dropped/committed、leak
  warning 均在）。
- `server-prefill-crash.log`——未打 metrics clamp 补丁时首次崩溃
  （`usage=-0.0018` 断言）记录。
- `server-p-noncold.log`——P 冷基线语义修复前运行：P-000 `storage=2896`
  命中 W 内容、P-001 `storage=1024` 命中 P-000 内容，佐证内容寻址。
