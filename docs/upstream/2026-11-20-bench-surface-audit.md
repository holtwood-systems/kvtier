# kvtier bench 对 SGLang 外部接口面的依赖审计：6afee75f → main（2026-11-20）

- 日期：2026-11-20
- 基线：`6afee75f918f3d59bdae7d619ba26b0ca22b1354`（2026-08-24，bench/repro-31600 锚定 commit）
  —— 对照源为 `/home/shane/github/open-infra-ai/kvtier/third_party/sglang` 工作区文件（只读），
  该工作区含 kvtier 自有未提交补丁（`pool_host/common.py` 的 `SGLANG_HICACHE_HOST_ALLOC=pin`），本审计只读取了
  `server_args.py` / `metrics_collector.py` / `io_struct.py` / `http_server.py` / `scheduler.py` 等与补丁无交集的文件；
- 对照：`e183c415bdfc5995f6288b1651e1c320c6a74015`（2026-11-20 15:17 +0800，独立浅克隆
  `/tmp/sglang-main-audit`，ghfast.top 镜像）。与同日 mem_cache diff 报告
  （`docs/upstream/2026-11-20-memcache-diff-6afee75f-to-main.md`，锚 `789abb9be`）为同日不同 tip：
  本克隆晚约 8 小时，接口面结论与该报告一致处不再重复论证；
- 方法：枚举 `bench/repro-31600/`（`run_server.sh`、`workload.py`、`validate_results.py`）实际消费的
  CLI flag / HTTP 端点 / Prometheus 指标，逐项在新克隆源码中定位并比对语义。
  GitHub 元数据经 gh API / curl 直连获取（本机 7892 代理已死，需 `env -u http_proxy ...`）；
- 本文区分两类断言：**【可见】**＝两版源码文本直接可见；**【推断】**＝基于源码迹象的判断，未实测。

---

## 0. 结论速览

| 分类 | 数量 | 一句话 |
|---|---|---|
| A. 会直接报错 | 2（+1 风险项） | `--disable-cuda-graph` 被移除；WSL2 host-alloc 本地补丁因文件重构失效 |
| B. 会静默改变语义 | 5 | 上游修了 cache_salt 归属、重写了 staging 分配与 L3 metadata——bench 的机制结论需重校准 |
| C. 无影响 | 34 | 其余全部接口（含 12/12 指标名、全部 HTTP 端点与字段）原样可用 |

PR #36280：**建议有条件复活**（维护者 10 天前刚点名它是 prefetch 事件计数的协调点；但 diff 的硬编码键已过期，必须按其建议重构重投）。详见 §3。

---

## 1. 依赖面枚举与逐项核实

### 1.1 服务器 CLI 参数（run_server.sh）

| flag（bench 用法） | 基线 6afee75f 位置 | main e183c415b 位置 | 判定 |
|---|---|---|---|
| `--model-path` | `srt/server_args.py` | `srt/arg_groups/fields/model.py:50` | C |
| `--port` | 同上 | `fields/serving.py:66` | C |
| `--mem-fraction-static` | 同上 | `fields/schedule.py:27` | C |
| `--kv-cache-dtype auto` | 同上 | `fields/model.py:203` | C |
| `--enable-hierarchical-cache` | `server_args.py:2719` | `fields/memory.py:109`（新增别名 `--enable-hicache`） | C |
| `--hicache-host-memory-mode cache\|buffer_only` | `server_args.py:2722`（choices 同） | `fields/memory.py:113`，choices 不变 | C |
| `--hicache-ratio 4.0` | `server_args.py:2730` | `fields/memory.py:120` | C（注：新 `--hicache-host-memory-fraction` 默认 0.8 仅在未显式给 ratio/size 时生效，help 明示；bench 显式传 ratio，不受影响【可见】） |
| `--hicache-mem-layout page_first` | `server_args.py:2756` | `fields/memory.py:146`，`page_first` 仍在 choices（新增 `layer_first_kv_split`、`page_unified`） | C |
| `--hicache-write-policy write_through` | `server_args.py:2740` | `fields/memory.py:132`，choices 与默认值均不变 | C |
| `--hicache-io-backend kernel` | `server_args.py:2748` | `fields/memory.py:139`，choices `direct\|kernel\|kernel_ascend` 不变 | C |
| `--enable-metrics` | 同上 | `fields/observability.py:72` | C |
| `--enable-cache-report` | 同上 | `fields/serving.py:191` | C（两版语义一致：作用于 OpenAI usage 字段；native `/generate` 的 `meta_info.cached_tokens_details` 不受它门控【可见】） |
| `--log-level info` | 同上 | `fields/observability.py:30` | C |
| `--hicache-storage-backend file\|shm`（可选） | `server_args.py:2770` | `fields/memory.py:161`，`file`/`shm` 仍在 choices（新增 `fast_file`、`npu_memcache`、`tensorcast`、`seaweedfs`） | C |
| `--disable-cuda-graph`（DISABLE_CUDA_GRAPH=1 时） | `server_args.py`（有效 flag） | **已移除**：`server_args.py:223-227` `REMOVED_FLAGS` 表登记该 flag，传入即停机并提示；替代 `--disable-decode-cuda-graph --disable-prefill-cuda-graph`（`fields/exec_.py:764/760`；`disable_cuda_graph` 字段本身 `no_cli=True`，`fields/exec_.py:768`） | **A** |
| env `SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR` | `environ.py` | `srt/environ.py:928`，消费点 `mem_cache/hicache_storage.py:903`（`fast_file` 亦读它，`storage/fast_file/fast_file_store.py:21`） | C |

**A1（会直接报错）**：`run_server.sh:22-25` 的 `--disable-cuda-graph` 分支在 main 上会启动失败
（不是静默忽略——`REMOVED_FLAGS` 的设计就是"stop a launch that still passes one"【可见】）。
修改：分支改为追加 `--disable-decode-cuda-graph --disable-prefill-cuda-graph`。

### 1.2 HTTP 端点与响应字段（workload.py SGLangClient）

| 端点 / 依赖字段 | 基线 | main | 判定 |
|---|---|---|---|
| `GET /health` | `http_server.py:655` | `:768` | C |
| `GET /server_info`：`version/model_path/...`、`hicache_ratio/hicache_host_memory_mode/hicache_write_policy/hicache_io_backend/hicache_storage_backend/enable_hierarchical_cache/enable_metrics/enable_cache_report/mem_fraction_static/kv_cache_dtype/tp/dp/pp/attn_cp_size/speculative_algorithm` | `http_server.py:797` | `:901`；响应 = `build_server_info` → `server_args.resolved_dict()`（`entrypoints/server_utils.py:206-215`），上述字段均为存活 server args 字段 | C |
| `internal_states[0].memory_usage.token_capacity` | scheduler internal state | `managers/scheduler_components/internal_state.py:84` + `scheduler_components/memory_usage.py:36`（`"token_capacity": int(...)`） | C |
| `GET /model_info`：`model_path/tokenizer_path/is_generation/weight_version` | `http_server.py:739` | `:859`，四字段全部在场（另有新 `served_model_name`、`load_format` 等） | C |
| `POST /tokenize` `{prompt, add_special_tokens:false}` → `{tokens, max_model_len}` | 原生 tokenize 端点 | `http_server.py:1919` 改为 OpenAI 兼容端点：`TokenizeRequest`（`entrypoints/openai/protocol.py:1883`：`prompt`、`add_special_tokens` 默认 True，extra allow）→ `TokenizeResponse`（`:1935`：`tokens/count/max_model_len`）。bench 读取的两个字段都在 | C【字段可见；运行行为一致为推断】 |
| `POST /flush_cache?timeout=30` | `http_server.py` | `:1063`，`timeout: float = Query(0.0, ge=0.0)` 兼容；两版 handler 均做 `tree_cache.reset()` + `metrics_reporter.reset_metrics()`（main `managers/scheduler.py:6303-6345` vs 基线 `scheduler.py:4381+`，逐行同构） | C |
| `GET /metrics` | prometheus ASGI | `entrypoints/server_utils.py:67` `Mount("/metrics", make_asgi_app(registry))` | C |
| `POST /generate`：payload `input_ids/rid/sampling_params{max_new_tokens,temperature,ignore_eos}/stream:true/cache_salt` | `GenerateReqInput`（io_struct） | `managers/io_struct.py:220`：`rid`(:223)、`input_ids`、`sampling_params`、`stream`、`cache_salt`(:422) 全在；新增 Rust 生成契约 gate `entrypoints/api_contract.py:12`（`proto/sglang/api/v1/generate.proto` 含 `rid/input_ids/stream/cache_salt`，bench 载荷可过）；`sampling/sampling_params.py:167/191` `max_new_tokens`/`ignore_eos` 在 | C |
| SSE `meta_info`：`completion_tokens/prompt_tokens/cached_tokens/cached_tokens_details{device,host,storage}` | MetaInfo | `managers/tokenizer_manager.py:2888`（prompt_tokens）、`:2916-2920`（completion_tokens/cached_tokens）、`:2942`（cached_tokens_details）；`CachedTokensDetails`（`openai/protocol.py:210`）`device/host/storage` 不变，`storage` 为 None 时序列化时省略（bench `cache_details()` 用 `.get(...,0)` 兜底）；`get_cached_tokens_details` 无 cache_report 门控（`scheduler_components/output_streamer.py:151-176`），零命中返回 None → bench 判 P 全冷逻辑不受影响 | C |

### 1.3 Prometheus 指标（workload.py `_METRIC_NAMES`/`_COUNTER_NAMES` + validate_results.py `_REQUIRED_COUNTERS`）

main 位置：`srt/observability/metrics_collector.py`（基线同路径，行号对照如下）。

| 指标名 | 基线行 | main 行 | 语义比对 | 判定 |
|---|---|---|---|---|
| `sglang:hicache_host_used_tokens` | 637 | 819 | Gauge，仅 `enable_hierarchical_cache` 时创建（两版同） | C |
| `sglang:hicache_host_total_tokens` | 643 | 825 | 同上 | C |
| `sglang:hicache_backup_tokens_total` | 2148 | 2693 | D2H backup token 计数，`pool` 标签两版都有；文档语义一致 | C |
| `sglang:hicache_backup_bytes_total` | 2128 | 2669 | 一致 | C |
| `sglang:hicache_backup_duration_seconds` | 2118 | 2659 | 一致 | C |
| `sglang:hicache_dropped_tokens_total` | 2157 | 2704 | `reason` 标签两版都有；main 的 reason 文档化取值 `host_pressure`/`write_through_unbacked_eviction`（`unified_radix_cache.py:1183/1149`【可见】） | C |
| `sglang:hicache_backup_dropped_tokens_total` | 1884 | 2358 | 一致 | C |
| `sglang:backuped_tokens_total` | 1877 | 2299 | L3 写 ack 计数；main 仍在 L3 写路径调用 `log_backuped_tokens`（`unified_radix_cache.py:4079`）——bench 的 drain 轮询判据（acked vs enqueued）继续可用【可见】 | C |
| `sglang:evicted_tokens_total` | 2095 | 2632 | "device KV token slots freed by eviction, regardless of backup/drop"——文档与基线一致；发射点移到 `mem_cache/base_prefix_cache.py:511`（`update_eviction_metrics`），断言正增量的能力不变 | C |
| `sglang:load_back_tokens_total` | 2111 | 2650 | `pool` 标签两版都有；语义一致 | C |
| `sglang:load_back_bytes_total` | 2138 | 2681 | 一致 | C |
| `sglang:load_back_duration_seconds` | 2104 | 2641 | 一致 | C |

12/12 存活，名字、标签结构（含 `pool`）、counter/gauge 类型不变。bench 的 `metric_total()` 按
样本名前缀求和的解析方式不受影响（单 rank、dense 模型只有 `pool="kv"` 一个序列【推断：Qwen2.5 无 SWA/mamba 池】）。

main 新增的相关指标（bench 未消费，建议采纳，见 B4/建议区）【可见】：

- `sglang:storage_prefetch_hit_tokens_total`（`:2306`）——L3 查询命中的 tokens（分配前）；
- `sglang:storage_prefetch_unfulfilled_tokens_total{reason}`（`:2314`）——命中却未成为可用 prefetch 的 tokens，reason 实际取值：
  `storage_transfer`、`deadline`、`already_cached`、`dropped`、`anchor_lost`、`device_covered`、`below_threshold`、`host_capacity`、`unservable_prefix`（`unified_radix_cache.py:3079/3121/3149-3152/3391-3414/3771/3807/3883`）；
- `sglang:storage_prefetch_deferred_tokens_total{reason}`、`sglang:prefetch_aux_alloc_failed_tokens_total`、`sglang:storage_backup_failed_tokens_total`（`metrics_collector.py:2446-2476` 一带，L2→L3 写失败族）；
- `sglang:prefetched_tokens_total` 语义已变（#37503：改为"实际从 L3 读入 host 的 tokens"）——bench 不在名单内，无影响。

### 1.4 构建环境面

| 项 | 判定 | 说明 |
|---|---|---|
| 本地补丁 `sglang-43375-local.patch`（WSL2 host-alloc，torch pinned memory） | **A2** | 上游 PR #43375 "[HiCache] Fix host-pointer handling on WSL2" **仍是 open、未合入**（gh API 2026-11-20 查证）【可见】；main 的 `pool_host/` 已重构（common/mha/mla 模块化 + 新 base.py），`common.py` 中 `cudaHostRegister` 机制仍在（`pool_host/common.py:249`，基线 :124），补丁上下文必不匹配。【推断】WSL2 + `kernel` IO 后端在 main 上仍会触发 illegal access（机制未变、修复未进），复跑前必须手工把 pin 方案移植到 main 的 `pool_host/common.py`。 |
| 本地补丁 `sglang-buffer-metrics-clamp-local.patch`（buffer_only `num_used` 可负 → metrics 断言崩溃） | **A3（风险）** | main 的 pool 统计已重构（`metrics_collector.py:111-114` `num_used_tokens` 注释明确"max(full_num_used, every window's num_used)"），原缺陷是否复现未知；若复现，旧 clamp 补丁同样套不上。需实测确认【推断】。 |
| env `SGLANG_ENABLE_STRICT_MEM_CHECK_DURING_IDLE` | C | 仍在：`environ.py:531`（默认 True），`scheduler_components/state_check.py:52-69` 消费——idle leak 断言的降级开关继续可用【可见】。 |
| Rust/native 构建（rustc 版本、`<openssl/sha.h>`） | 未核 | 超出接口面审计范围；`rust_tree_core/` 为新依赖（companion 报告附 1），首次构建按新 README 重走【推断】。 |

---

## 2. 三级分类适配清单

### A. 会直接报错（2 项 + 1 风险项）

1. **`--disable-cuda-graph`**（`run_server.sh:22-25`，仅 `DISABLE_CUDA_GRAPH=1` 触发）→ 换
   `--disable-decode-cuda-graph --disable-prefill-cuda-graph`（`fields/exec_.py:760/764`；
   `REMOVED_FLAGS` 在 `server_args.py:224` 给出同一指引）。
2. **WSL2 host-alloc 补丁失效**：`sglang-43375-local.patch` 打不进 main 的 `pool_host/common.py`
   （文件重构），而 #43375 未合入；不移植则 WSL2 上 `kernel` IO 后端预计启动即崩（A2）。
3. （风险）buffer_only 的 metrics clamp 补丁同废，缺陷是否仍存在需实测（A3）。

### B. 会静默改变语义（5 项）

这些不报错，但会改变 bench 的实验结论或证据解释——复跑前后必须知情：

1. **cache_salt 归属已修**（companion 报告主题 1；`managers/scheduler.py:3818-3831` 显式传
   `req.cache_salt`，`unified_radix_cache.py:2666-2727` 加一致性断言）：bench 的 saltless B 臂
   （"prefetch 以 anchor=root 的 salt=None 插入、带 salt 的 re-match 不可见"）在 main 上机制不复存在；
   salted 模式的 L3 命中率预期显著改善。`--salt-mode none` 对照臂的设计初衷失效，需重新定义机制假设。
2. **prefetch 暂存改命中时分配 + host 容量仲裁**（`unified_radix_cache.py:2753-2760` 注释
   "query holds no host memory"；新 `capacity.py`/`prefill_budget.py`；`reclaim_for_load_back`）：
   E 阶段写回 backlog 钉住 host 池、R 被"分配不足静默撤销"的旧失败模式（ENVIRONMENT.md B 臂实测）
   在 main 上是上游已动的机制——`--post-evict-wait` 的动机与收敛判据（`hicache_backup` vs `backuped`
   对比轮询）仍可运行【可见：两计数器都在】，但等待时长与必要性需重调。
3. **storage prefetch 重试梯**（`--hicache-storage-prefetch-retry-poll-interval/-max-attempts`，默认 8/8；
   `storage_prefetch.py`）：L3 miss 的 R 请求不再无限期等 prefetch，超限"admitted with whatever the
   device holds"——R 的 TTFT 分布形态与部分命中行为改变。
4. **file 后端 metadata/所有权重写**（`hicache_storage.py:1156-1184` 陈旧 metadata 失效；
   `lru_file_evictor.py` `owned_suffixes` 每秩所有权）：2026-11-06 负结果（8 个 W 前缀未在 L3 索引幸存，
   `index_evicted`）的复现条件改变；`write_through_selective` 门控与父链 contiguity 不变量未变
   （companion 报告主题 7），复跑时 write_policy 取值要显式核对。
5. **`/tokenize` 端点换壳为 OpenAI 兼容**：请求/响应字段兼容（§1.2），但实现路径不同
   （`openai_serving_tokenize.handle_request`）——功能等价为推断，dry-run 首跑时先单测该端点。

### C. 无影响（34 项）

- CLI：`--model-path/--port/--mem-fraction-static/--kv-cache-dtype/--enable-hierarchical-cache/
  --hicache-host-memory-mode/--hicache-ratio/--hicache-mem-layout(page_first)/--hicache-write-policy(write_through)/
  --hicache-io-backend(kernel)/--enable-metrics/--enable-cache-report/--log-level`（13）
  + 可选 `--hicache-storage-backend file|shm`（1）+ env `SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR`（1）；
- HTTP：`/health`、`/server_info`（含全部白名单字段与 `internal_states[].memory_usage.token_capacity`）、
  `/model_info`（含 `weight_version`）、`/tokenize`、`/flush_cache?timeout=`、`/metrics`、
  `/generate`（payload、SSE、`meta_info` 四字段、`cached_tokens_details` 三键）（7 端点，约 25 个字段全部核过）；
- Prometheus：12/12 指标名与标签结构；
- flush_cache 的 reset 行为（两版都 reset radix + metrics，bench 的 before 快照逻辑不变）。

### Top 5 必改项（复跑 main 的最小动作集）

1. `run_server.sh`：`--disable-cuda-graph` 分支 → `--disable-decode-cuda-graph --disable-prefill-cuda-graph`。
2. WSL2 host-alloc：把 pin 方案手工移植到 main 的 `pool_host/common.py`（或跟踪 #43375 rebase）。
3. 机制预期重校准：saltless/B 臂假设、`--post-evict-wait`、R 部分命中判据（B1-B3）——
   先以一次小 N 试跑对比 `storage_prefetch_unfulfilled_tokens_total{reason}` 再定正式参数。
4. `workload.py` `_METRIC_NAMES`/`_COUNTER_NAMES` 增补 main 新指标
   （`storage_prefetch_unfulfilled_tokens_total`、`storage_prefetch_hit_tokens_total`、
   `storage_backup_failed_tokens_total`），否则结果包丢失失败归因证据（建议级，但对审计连续性近于必须）。
5. 构建与文档：`ENVIRONMENT.md` 锚定新 commit；rust_tree_core/native hash 构建步骤按 main 重走；
   buffer_only 两处断言缺陷状态实测（A3）。

---

## 3. PR #36280 复活可行性评估

### 3.1 PR 与 RFC 概况（gh API，2026-11-20 查证）

- **#36280** `feat(hicache): export prefetch outcome metrics`，作者 yikun-c（CONTRIBUTOR），
  2026-08-25 开，**open**，最后更新 2026-09-06，0 条评论 / 0 条 review，CI 全红（base 当时的三个
  PR Test 均 :x:），12 commits，+73/−1，改 2 个文件。`Closes #35808`。
- **#35808**（观测性 RFC，hhy-seven）：2026-08-21 开，2026-10-25 被 github-actions 机器人以不活跃关闭
  （"automatically closed due to inactivity"）。评论区关键脉络：
  - moonboyknm 表示有趣；
  - **holtwood**（即本仓库一侧的调研，2026-08-24）：复核后确认大部分传输遥测已存在
    （#32388 的 L2 计数、StorageMetricsCollector 的 L3 带宽、`cached_tokens_total{cache_source}`），
    剩余缺口是**失败分类**："UnifiedRadixCache records some prefetch outcomes internally, but the
    default collector does not export them"；建议低基数 `op`/`result` 计数器，rid 留在日志；
  - yikun-c 随后认领"聚焦切片"并开出 #36280。

### 3.2 #36280 的指标与 kvtier 审计器需求的重合度

PR 新增 `sglang:hicache_prefetch_outcomes_total{outcome}`，硬编码 6 个枚举：
`attempts / issued / declined_too_short / declined_rate_limited / revoked_insufficient / revoked_full_miss`
（epoch 化的单调导出：快照变小视为新纪元，绝不导出负增量）。

对照两边的需求：

| kvtier 失败类别（validate_results.py `_FAILURE_CLASSES`） | main 已有（#37503 族，token 维度） | #36280 独有（事件维度） |
|---|---|---|
| `storage_read_failed`（读失败） | `unfulfilled{reason=storage_transfer}` 已覆盖 | — |
| `transfer_anomaly`（传输异常） | `unfulfilled{reason=deadline}` 已覆盖 | — |
| `index_evicted`（索引不在） | 部分：`unfulfilled{reason=below_threshold/unservable_prefix}` 是 token 计数 | `revoked_full_miss`（查到却零可用→读侧归零）、`declined_rate_limited` |
| `not_backed_up`（没写进去） | `hicache_dropped_tokens_total{reason}` + `backuped` 增量部分覆盖 | — |
| B 臂实测的"分配不足被静默撤销" | `unfulfilled{reason=host_capacity}`（token 维度） | `revoked_insufficient`（事件维度，直接对应"撤销"） |

**结论**：标签级重合约 1/3–1/2（`declined_too_short`≈`below_threshold`）；`revoked_insufficient /
revoked_full_miss / declined_rate_limited` 三个事件计数是 main 上**存在但未导出**的独有信息
（`_prefetch_outcome_stats` 字典在 `unified_radix_cache.py:401` 初始化、`:3556` 记账、`:4573` 快照挂到
StorageMetrics，但 `log_storage_metrics` 不导出【可见】）。kvtier 的即时需求大部分可由 #37503 族满足，
#36280 补的是"事件计数 + 撤销/限流归因"这一层。

### 3.3 diff 在当前 main 上还能 apply 吗

对照 `/tmp/sglang-main-audit`（e183c415b）逐 hunk 核对【可见】：

- **hunk 1**（`StorageMetricsCollector.__init__` 里 `self.labels = labels` 之后、
  `self.prefetched_tokens_total` 之前插入）：main `metrics_collector.py:2293-2295` 结构逐行匹配，可 apply；
- **hunk 2**（`log_prefetch_outcomes` 方法插在 `log_prefetched_tokens` 前）：锚点
  `buckets=bucket_bandwidth, )` + `def log_prefetched_tokens(...)` 在 main :2424-2435 原样存在，可 apply；
- **hunk 3**（`log_storage_metrics` 尾部追加 `self.log_prefetch_outcomes(getattr(storage_metrics,
  "prefetch_stats", {}))`）：main :2496-2501 尾部四行直方图循环 + 空行 + `class ExpertDispatchCollector`
  与 hunk 上下文完全一致，可 apply；
- **测试文件** `test/registered/unit/observability/test_stat_loggers_di.py` 在 main 存在，
  `_RecordingStorageMetricsCollector`（:91）与 `TestHiCacheMetrics`（:180）fixture 都在，hunk 可 apply。

**但功能上有三处过期**（均为【可见】，且与维护者在 #36474 的评审一致）：

1. 硬编码 6 键 vs main 的 14 键 `_prefetch_outcome_stats`（多出 `issued_assumed_stored`、
   `declined_anchor_lost/device_covered/host_oversize/unservable_prefix`、`l3_demand_requests`、
   `l3_miss_tokens`、`l1l2_miss_tokens`）——须改为遍历快照键；
2. `StorageMetrics`（main :2269-2274）**没有声明** `prefetch_stats` 字段，它由调用点动态注入
   （`unified_radix_cache.py:4573` `if not hasattr(...): storage_metrics.prefetch_stats = ...`）；
   PR 的 `getattr(..., {})` 防御在当前 main 上恰好安全（要么是 dict 要么缺省空 dict），但这是踩在
   未声明的动态属性上，上游若把字段正式化并默认 None，就会变成 #36474 评审里提到的
   `AttributeError: 'NoneType' object has no attribute 'get'`；
3. main 的 `log_storage_metrics` 只在 `enable_storage_metrics and storage_metrics_collector is not None`
   的 cache 路径被喂（`unified_radix_cache.py:4570-4574`），语义没变，但周边新增了
   `storage_prefetch_deferred_tokens_total` 等并行指标——PR 的命名与 epoch 策略需与之对齐。

另：PR 基于 2026-08-25 的 main，CI 全红；12 个 commit 需要 squash 重投。

### 3.4 生态位与维护者信号

- **#36474**（hhy-seven 的同域大 PR，open）：2026-11-10 维护者 **xiezhq-hermann** 给出长评，
  逐条指出 main 已有 #37503/#43946/#47626/#48577 覆盖了大部分，并明确说：
  > "(c) For outcome counters, please build on the #37503 metrics rather than adding a parallel
  > counter. If you still want event-count export of `_prefetch_outcome_stats`,
  > **please coordinate with #36280, which does exactly that**."
  
  这是维护者对 #36280 的**正面点名**（10 天前），也是"上游仍缺这个导出"的直接证词【可见】。
- 已知教训（维护者"只欢迎挂在具体 bug 修复/行为变更上的贡献，纯覆盖率 PR 冷置"）依然成立——
  #36474 的遭遇（拆分后才肯 run-ci）就是最新例证；但 #36280 恰好是维护者指定的协调点，
  属于该教训的例外信号。

### 3.5 结论：**建议复活（有条件），但与 kvtier bench 复跑解耦**

理由：

1. **正信号**：维护者 2026-11-10 刚点名 #36280 是事件计数导出的载体；RFC 虽被机器人关闭，
   PR 仍 open 且 `Closes #35808` 挂着——复活有明确的上游承接人语境；
2. **必做重构**（否则必死）：squash 成单 commit；6 键硬编码改为遍历 `prefetch_stats` 快照键
   （并在 PR 描述里说明与 #37503 token 维度的分工：本 PR 只做事件计数）；处理 `prefetch_stats`
   动态属性/可能的 None 默认；修 CI；基于当前 main 重写测试；
3. **对 kvtier 的实际价值有限且不紧迫**：main 已有的 `storage_prefetch_unfulfilled_tokens_total{reason}`
   等（#37503 族）已覆盖审计器六类失败里的大多数（token 维度）；#36280 独有的
   `revoked_*`/`declined_rate_limited` 事件计数只影响归因精度。因此 **bench 复跑不应等它**——
   先改用 main 已有指标（Top5 第 4 条），#36280 作为独立上游贡献并行推进；
4. 若不想维护一个纯观测 PR，替代路径是把 `revoked_*` 事件导出绑进下一个实际 bug 修复 PR
   （例如 bench 复跑中发现的新缺陷），按教训这更稳——但会失去 #36280 已有的维护者注意力。

---

## 4. 证据状态汇总

- 【可见】（两版源码/GitHub 元数据直接可见）：§1 全部表格行级结论、12/12 指标存活、
  REMOVED_FLAGS、#43375 未合入、#37503 指标族与 reason 枚举、#36280 三 hunk 锚点匹配、
  14 键 outcome 字典、维护者 2026-11-10 点名评论、RFC 被机器人关闭；
- 【推断】：WSL2 illegal access 在 main 仍复现（机制同、修复未进，未实测）；A3 buffer_only
  断言缺陷是否残留；`/tokenize` 换壳后行为逐字段等价；dense 模型下 pool 标签只有 "kv" 一个序列；
  "上游仍缺事件计数导出" 以维护者评论为准（我未逐条排除其它导出路径，但 `log_storage_metrics`
  现文本确无 outcome 导出）。

## 5. 复跑前置动作（浓缩版）

1. 改 `run_server.sh` cuda-graph 分支；2. 移植 WSL2 host-alloc 补丁；3. 小 N 试跑校准
B1-B3 机制预期并扩充 workload.py 指标名单；4. 显式核对 `--hicache-write-policy`（排除 selective
策略性不落盘）；5. `--sglang-commit` 换新 SHA、按 main 重走 native 构建。#36280 与复跑解耦、
可并行按 §3.5 重构重投。
