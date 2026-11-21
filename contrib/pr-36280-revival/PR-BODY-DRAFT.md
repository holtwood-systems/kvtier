# 草稿：#36280 复活 PR 正文（英文，待所有者批准后按流程发布）

> 流程：先在旧 PR #36280 发协调评论（草稿见 contrib/pr-36280-revival/README.md）→
> 24-48h → 从 fork 开新 PR，正文如下，Co-authored-by yikun-c。
> 状态：**未发布**。

---

## Export storage prefetch outcome stats as Prometheus counters

Revives the stalled #36280 (original work by @yikun-c — full credit, co-authored).
Coordinates with the maintainer pointer in the #36474 review
("please coordinate with #36280, which does exactly that").

### Motivation

The 14 prefetch outcome counters (`attempts`, `issued`, `issued_assumed_stored`,
`declined_too_short`, `declined_rate_limited`, `declined_anchor_lost`,
`declined_device_covered`, `declined_host_oversize`, `declined_unservable_prefix`,
`revoked_insufficient`, `revoked_full_miss`, `l3_demand_requests`,
`l3_miss_tokens`, `l1l2_miss_tokens`) currently live only in the in-process
`_prefetch_outcome_stats` dict — there is no Prometheus export.

Runtime evidence from a HiCache capacity-pressure benchmarking session
(11 full `/metrics` dumps, SGLang main @ `e183c415b`, single 4090):
`hicache_prefetch_outcomes` appears in **0/11** dumps, while the
token-dimension `storage_prefetch_unfulfilled_tokens_total{reason}`
(#37503 family) is present but cannot distinguish events — e.g.
`revoked_full_miss` vs `declined_rate_limited` collapse into the same
token counters. In the same session, `host_capacity` allocation failures
had no dedicated text-log line either; event counters would have cut
triage time substantially.

### What this PR does

1. `StorageMetricsCollector.log_prefetch_outcomes`: iterate the
   `prefetch_stats` snapshot **keys dynamically** (no hardcoded key list —
   robust to future key additions; the original PR hardcoded 6 of what is
   now 14 keys), preserving the monotonic-increment + snapshot-shrank =
   new-epoch semantics; keys that vanish keep their last value to avoid
   double counting on re-appearance.
2. Unit routing aligned with the `storage_prefetch_*` family:
   `*_tokens` → `sglang:storage_prefetch_outcome_tokens_total{outcome}`;
   everything else → `sglang:storage_prefetch_outcome_events_total{outcome}`.
3. `StorageMetrics.prefetch_stats` formally declared
   (`Optional[Dict[str, float]]`); the feed point in `check_hicache_events`
   becomes an unconditional assignment (the `hasattr` probing noted as
   fragile in the #36474 review is gone). Collector no-ops on None.
4. 4 unit tests: monotonic/new-epoch, dynamic keys, unit routing, None no-op.

### Relationship to #37503

Complementary, not overlapping: #37503 exports the **token dimension**
(how many tokens went unfulfilled, by reason); this PR exports the
**event dimension** (how many prefetch attempts were declined/revoked,
and why). Both are needed to answer "is this a capacity problem or a
lifecycle problem" without gdb.

### Testing

- `git apply` clean on `e183c415b`; `py_compile` ×3 files.
- Unit tests run green in an isolated harness (metrics_collector only).
- Full CI run requested (`/tag-and-rerun-ci`).
