# 草稿：4× 结构型新 issue（英文，待所有者批准后发布）

> 用途：发布到 https://github.com/sgl-project/sglang/issues/new
> 状态：**未发布**。发布前置（Agent-12 建议）：重跑 EXP-A 判别臂钉死"文件在盘"推断、
> 核对 main 是否已有相关改动。定位为"flagging for triage"而非提交补丁。
> 完整根因分析：`docs/bench-design/2026-11-21-structural-morph-fix-design.md`（§8 原文）

---

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
