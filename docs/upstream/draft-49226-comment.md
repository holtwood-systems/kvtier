# 草稿：#49226 证据评论（英文，待所有者批准后发布）

> 用途：发布到 https://github.com/sgl-project/sglang/pull/49226
> 状态：**未发布**。所有者批准 + 过目后方可发出。
> 语气校准：给在飞 PR 补弹药，不抢功，数据说话，末尾轻轻带出 4× 新问题面。

---

Hi — I ran an independent reproduction of the admission-shortfall behavior from #49221 on current main (`e183c415b`, single RTX 4090 48GB, `file` storage backend), and the data seems to strengthen the motivation for the pin-until-admission approach in this PR. Sharing in case it's useful:

**Workload**: 32 requests × ~6,080 tokens, per-request distinct `cache_salt`, phased W/E/R/P-style bench (write → evict → reload → cold-prefill probes); audited result packages with per-reason counters in the linked repo.

- **Serial phase**: 28/32 requests retain full L3 hits (~168K tokens); all unfulfilled-reason counters ≈ 0.
- **Concurrent burst (16)**: cached tokens drop to **28% of serial** (47,680 vs 168,256); `cache_admission_shortfall` accounts for **96–97% of unfulfilled tokens** (82,688 vs `host_capacity` 3,392). TTFT interference: 4.7× (Qwen3-4B) / 6.3× (0.5B).
- **Dose–response**: conc4 → full hits; conc8 → collapse. Inflection arrives earlier than the issue's c*≈11, consistent with write-back staging and prefetch staging sharing the host pool in `cache` mode.
- **Pool amplification** (host pool ×12 → 393,216 tokens): **32/32 full hits, every unfulfilled reason back to zero** — capacity-mediated, matching the admission-window hypothesis this PR closes.
- **`buffer_only` + `wait_complete` control arm**: fully immune — shortfall is `cache`-mode specific, which lines up with this PR's fix surface.
- Repeated across two independent server restarts with reseeded content (same direction both times).

One observation slightly beyond this PR's scope, offered for triage: at **4× host-pool capacity** the `host_capacity` signature disappears but a compound signature emerges that traces to the L3 storage I/O path rather than the admission window: `deadline` unfulfilled (~8.3K), drain non-convergence (240s; enqueued 204,686 / acked 118,670 — the single backup thread writes ~12KB/page via tmp+rename at ~400-780 tok/s), and exists-queries that exhaust their timeout budget surface as zero-length misses with **no counter, no log, and retries canceled** (TTFT clusters at budget+prefill across all 12 R requests; on-disk file counts match acked counts exactly, ruling out actual data loss). Happy to file a separate issue with the dose-arm data and a source-level breakdown if maintainers think it's worth tracking.

Full evidence (results JSON, server logs, per-reason counter deltas, GPU baselines) in the linked repo; happy to rerun any arm on request.
