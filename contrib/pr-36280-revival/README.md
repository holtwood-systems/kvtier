# sglang PR #36280 复活补丁：prefetch outcome metrics 动态导出

- 日期：2026-11-21
- 基线：sglang main `e183c415bdfc5995f6288b1651e1c320c6a74015`（2026-11-20 15:17 +0800，
  浅克隆 `/tmp/sglang-main-audit`，验证后已 reset 保持干净）
- 上游材料：PR #36280（open，yikun-c，2026-08-25 开，最后更新 2026-09-06，12 commits，
  +73/−1，0 评论 0 review，CI 全红；元数据与 diff 经 api.github.com 于 2026-11-21 重新核实）；
  背景与维护者信号见 `docs/upstream/2026-11-20-bench-surface-audit.md` §3（xiezhq-hermann
  2026-11-10 在 #36474 评审中点名 "coordinate with #36280, which does exactly that"）。
- 产出：`0001-export-storage-prefetch-outcome-stats.patch`（git format-patch 风格，
  作者/日期为占位，投递前由所有者改定；`git apply` 与 `git am` 均可吃）。
- **只做不投**：本目录全部为草稿，任何 GitHub 写操作需所有者批准。

---

## 1. 原版 vs 复活版

| 维度 | #36280 原版（2026-08-25） | 复活版（本补丁） |
|---|---|---|
| 指标名 | `sglang:hicache_prefetch_outcomes_total{outcome}` | `sglang:storage_prefetch_outcome_events_total{outcome}`（事件/请求维度）+ `sglang:storage_prefetch_outcome_tokens_total{outcome}`（`*_tokens` 键） |
| 键集合 | **硬编码 6 键**（attempts/issued/declined_too_short/declined_rate_limited/revoked_insufficient/revoked_full_miss），main 已 14 键，8 键丢失 | **遍历快照键**，零枚举；新键自动导出 |
| 单位 | 6 键混在同一 counter（原 6 键恰好都是事件维度，无冲突；照搬则会把 2 个 token 键混入） | 按 `_tokens` 后缀路由到 tokens counter，家族内单位一致 |
| 命名对齐 | 独立 `hicache_*` 族 | 与 #37503 族前缀对齐（`storage_prefetch_hit_tokens_total` / `..._unfulfilled_...{reason}` / `..._deferred_...{reason}`），呼应维护者 "build on the #37503 metrics" |
| `prefetch_stats` 属性 | `getattr(storage_metrics, "prefetch_stats", {})` 踩未声明动态属性（上游若正式化为默认 None 即 `AttributeError`，即 #36474 评审点） | `StorageMetrics` **声明** `prefetch_stats: Optional[Dict[str, float]] = None`；`check_hicache_events` 喂点改为无条件赋值（声明字段后 `hasattr` 探测恒真会断流，两处必须联动改）；collector 侧 `None`/缺属性均为 no-op |
| epoch 语义 | 快照变小=新纪元，导出当前值，绝不导出负增量 | **原样保留**（这是原版最有价值的部分） |
| 遗忘键处理 | 未处理 | 快照中消失的键保留 last 值不剪枝——剪枝会让键重现时从零重导整段（双计） |
| 测试 | 1 个测试（6 键场景） | 4 个：单调增量/新纪元（原场景扩展）、键动态性（用原版 6 键之外的 7 个新键证明无硬编码）、单位路由、None/缺属性 no-op |
| 体量 | 12 commits、基线过期、CI 红 | 单 commit、+197/−2、3 文件 |

设计取舍备忘（预期评审问题）：

- **不预建 label series**（同类的 `unfulfilled{reason}` 在 `__init__` 预建 12+2 个 reason 序列）：
  预建需要 14 键清单，重新引入过期风险；且键属 `UnifiedRadixCache`（它 import 本文件，反向
  import 成环）。序列在首个非零增量时出现，与原版 #36280 行为一致。若评审坚持预建，后备方案是
  把初始键元组放 observability 侧、cache 侧 `dict.fromkeys` 反向取用（依赖方向成立）。
- token 键（`l3_miss_tokens`/`l1l2_miss_tokens`）不是 #37503 的重复：那是 attempt 级传输结果，
  这是请求需求侧视图（`_account_prefetch_outcome` 的 requested−hit 口径），上游未导出过。

## 2. `_prefetch_outcome_stats` 14 键全集（锚 e183c415b）

初始化 `unified_radix_cache.py:401-416`；快照 `prefetch_outcome_stats_snapshot()`（:3575）；
由 `check_hicache_events`（:4568-4574）挂到 `StorageMetrics.prefetch_stats` 喂
`log_storage_metrics`（唯一喂点，`enable_storage_metrics and storage_metrics_collector is not None` 门控）。

| 键 | 维度 | 含义 | 更新点（锚行号） |
|---|---|---|---|
| `attempts` | 事件 | 命名空间匹配后有正长度的 prefetch 候选 | urc:2734 |
| `issued` | 事件 | 实际发起的 L3 读 | urc:2812 |
| `issued_assumed_stored` | 事件 | 以 assume-stored 截短后发起 | urc:2814 |
| `declined_too_short` | 事件 | 低于 prefetch 阈值拒发 | urc:2737 |
| `declined_rate_limited` | 事件 | 限流拒发（cache 模式）/ 暂存分配不足记账一次（buffer 模式 parked） | urc:2743, 3896 |
| `declined_anchor_lost` | 事件 | buffer 模式锚前缀丢失 | urc:3752 |
| `declined_device_covered` | 事件 | 活树已覆盖该 span，跳过读 | urc:3780 |
| `declined_host_oversize` | 事件 | host 暂存超限 | buffer_mode/pipeline.py:1313 |
| `declined_unservable_prefix` | 事件 | device 前缀驻留但不可读 | urc:3762 |
| `revoked_insufficient` | 事件 | 发出后被撤销且部分命中（hit>0） | urc:3566（`_account_prefetch_outcome`） |
| `revoked_full_miss` | 事件 | 发出后被撤销且零可用命中 | urc:3568 |
| `l3_demand_requests` | 事件（请求） | 记账到 query outcome 的需求请求 | urc:3570 |
| `l3_miss_tokens` | **token** | 需求中 L3 miss 的 token（requested−hit） | urc:3572 |
| `l1l2_miss_tokens` | **token** | 需求全量 token | urc:3571 |

导出落点：前 12 键 → `storage_prefetch_outcome_events_total{outcome}`；后 2 键 →
`storage_prefetch_outcome_tokens_total{outcome}`（`_tokens` 后缀路由）。

## 3. 与 #37503 指标族的互补关系

#37503 族（main 已在，token 维度，attempt 级）：`storage_prefetch_hit_tokens_total`、
`storage_prefetch_unfulfilled_tokens_total{reason}`（发射点 reason 12 个）、
`storage_prefetch_deferred_tokens_total{reason}`。

| kvtier 失败类别（validate_results.py） | #37503 族（token） | 本补丁（事件） |
|---|---|---|
| storage_read_failed | `unfulfilled{reason=storage_transfer}` | — |
| transfer_anomaly | `unfulfilled{reason=deadline}` | — |
| index_evicted | 部分（`below_threshold`/`unservable_prefix` 为 token 数） | `revoked_full_miss`（查到却零可用）、`declined_unservable_prefix` |
| not_backed_up | `hicache_dropped_tokens_total{reason}` + `backuped` 增量 | — |
| 分配不足被静默撤销（B 臂） | `unfulfilled{reason=host_capacity}`（token） | `revoked_insufficient`（事件，直接对应"撤销"）+ `declined_rate_limited`（buffer 模式 parked 计） |

分工表述（PR 描述可用）：#37503 回答"多少 token 没变成可用 prefetch、为什么"；本补丁回答
"发生了多少次尝试/发出/拒绝/撤销"——撤销与限流只有事件计数才能归因，且 `attempts`/`issued`
给出了分母。`*_tokens` 两键补的是请求需求侧视图，上游此前无任何导出。

## 4. apply 与验证记录（2026-11-21，均在 e183c415b 克隆上）

**原版 #36280 diff**（api.github.com 取回，5262 字节）：

- `metrics_collector.py` 3 hunk：`git apply --check --include=<该文件>` **通过**；
- `test_stat_loggers_di.py` 2 hunk：**不通过**（import hunk 上下文缺后加的 `SchedulerStats`；
  测试插入 hunk 尾上下文假定 `TestHiCacheMetrics` 是文件最后一个类，main 其后还有 3 个类）。
  **更正审计 §3.3 的"测试文件 hunk 可 apply"结论**——原版不是可整体 apply 的复活基底。

**复活版**（`0001-export-storage-prefetch-outcome-stats.patch`，即最终产物逐字节验证）：

1. `git apply --check` 通过；`git apply` 干净应用（干净树 → 应用 → 验证 → `git checkout -- .` 复原，两轮）；
2. `python3 -m py_compile` 3 个改动文件通过；
3. 4 个新单测以独立 harness（`/tmp/pr36280-revival-verify/run_outcome_tests.py`，测试体逐字
   复刻、仅 import `metrics_collector`）在临时 venv（torch 2.5.1 + msgspec/prometheus_client 等）
   实跑：**4 passed**；
4. **待环境验证**：完整测试文件收集需完整 sglang 依赖链（本机缺 tvm_ffi 等含编译产物包，
   直连 PyPI 部分包不可得），全量 `test_stat_loggers_di.py` 与 CI 套件未跑。验证 venv 留在
   `/tmp/sgl-verify-venv` 供复用；
5. 克隆 working tree 已 reset，`git status` 干净；未做任何 commit/push。

## 5. 投递策略建议

**推荐路径：评论协调 → 新 PR 引用旧 PR**（不是 squash 重投到 #36280 分支——该分支属
yikun-c，kvtier 无推送权；也不是静默开新 PR——维护者注意力挂在 #36280 线程上，先认领再投
既保留信号又避免撞车）：

1. 在 #36280 发协调评论（草稿见下），说明复活版已就绪、列出与原版的差异，问 yikun-c 是否
   自行接纳或同意由我方开新 PR；
2. 等 24–48h：yikun-c 接手 → 把本补丁内容移交（他 force-push squash 后 CI 才能重跑）；
   无响应/同意 → 从 kvtier fork 开新 PR，标题 `Revive "#36280": export storage prefetch
   outcome stats dynamically`，正文引用 #36280、#35808、#36474 评审，`Co-authored-by:
   yikun-c <...>` 保留原 epoch 逻辑功劳；
3. 新 PR 单 commit（patch 文件已是单 commit 形态），只动观测面 + 单测，符合"纯覆盖率 PR
   冷置"教训的例外条件（维护者点名协调点 + 单一聚焦改动）；
4. CI：测试挂在已注册的 CPU 套件（`register_cpu_ci(est_time=11, suite="base-a-test-cpu")`
   沿用，新增 4 测试如评审问及可把 est_time 提到 ~13）；上轮 #36474 的教训是先请求 run-ci
   label 而非默认等排程；
5. 若评审要求预建 label series：启用 §1 备忘的反向键元组方案，勿回退到硬编码清单。

**评论话术草稿（#36280，英文，勿直接发布——按 contrib 规则发布前需重核当日 main 与讨论状态）**：

> Hi @yikun-c — picking this up per @xiezhq-hermann's note on #36474 ("coordinate with
> #36280, which does exactly that").
>
> I have a revival ready against current main. Same export discipline as your branch
> (monotonic deltas, lower-snapshot-as-new-epoch), three changes to keep it alive there:
>
> - outcomes are no longer enumerated: the collector now walks the
>   `_prefetch_outcome_stats` snapshot keys, so all 14 current outcomes (and any added
>   later) export; token-denominated keys (`l3_miss_tokens`, `l1l2_miss_tokens`) route
>   to a separate `storage_prefetch_outcome_tokens_total` so the event counter keeps a
>   single unit;
> - renamed to `sglang:storage_prefetch_outcome_events_total{outcome}` to sit with the
>   #37503 `storage_prefetch_*` family (event counts here, token counts there);
> - `prefetch_stats` is now a declared `StorageMetrics` field and the
>   `check_hicache_events` feed assigns it unconditionally, so a formalized/None field
>   can't break the export.
>
> Would you like to take the rework into this PR (happy to hand over the patch), or
> prefer a fresh PR referencing this one? Either way you'd keep credit for the
> delta/epoch design — it's the part that survived unchanged.

**新 PR 描述骨架**：Summary = 本文件 §3 末段分工表述 + §1 变更点列表（英文）；
Test plan = `test/registered/unit/observability/test_stat_loggers_di.py` 四个新用例
（单调增量/新纪元、键动态、单位路由、None no-op），附本地验证记录（§4）。
