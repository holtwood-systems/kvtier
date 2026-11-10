# 2026-11-11 HiCache L3 file 后端探测（WSL2 / RTX 5070 Ti）—— 负结果复现

在第二台机器（不同 GPU/OS 形态）复现 `2026-11-05-linux-rtx4090-l3-file`
的负结果，并使用 DEV-20 新增验证器字段（`failure_classification`、
`stage_counters`、`storage_snapshots`）。

## 环境

- 机器：WSL2（Linux 6.18.33.2-microsoft-standard），RTX 5070 Ti 16GB
  （sm_120），driver 616.56，CUDA 13.3；系统 RAM 16GB
- 服务端：SGLang `6afee75f918f3d59bdae7d619ba26b0ca22b1354` editable 安装
  （`0.0.0.dev1+g6afee75f9`）+ **复打本地补丁**
  `../2026-10-26-wsl2-l2-baseline/sglang-43375-local.patch`
  （pinned commit 不含上游 WSL2 host-alloc 修复；`git apply` 干净通过，
  `SGLANG_HICACHE_HOST_ALLOC=auto` → 自动选 cudaHostAlloc）
- JIT 依赖：HiCache native hash 扩展需 `<openssl/sha.h>`；系统无
  libssl-dev，以 miniconda 的 openssl 3.6 头/库经 `CPLUS_INCLUDE_PATH` +
  `LIBRARY_PATH` 编译进 torch 扩展缓存
- 模型：`/home/shane/models/Qwen2.5-0.5B-Instruct`（HF BF16）
- 配置：`MEM_FRACTION=0.35`（device pool 358,532 tokens）、
  `HICACHE_RATIO=0.12`（host pool 43,024 tokens / 0.53GB —— 受限于
  10GiB 硬保留 + 16GB 总内存，ratio 上限 ~0.83GB）、`HICACHE_IO=kernel`、
  `HICACHE_LAYOUT=page_first`、`write_through`、`storage_backend=file`、
  `SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR=/tmp/sglang-l3-storage`
- workload：`--num-trials 6 --prefix-tokens 4096 --tail-tokens 128
  --expected-storage-backend file --storage-dir /tmp/sglang-l3-storage
  --evict-factor 1.3`（E=456 请求/466,944 tokens > device+host 容量）

## 结果

- `status=failed`，`failure_classification=index_evicted`：
  R-000 三层全 miss（device=0, host=0, storage=0，要求 ≥3801）。
  `validate_results.py` 判定包结构合法（负证据原样保留）。
- `.bin` 页文件确曾落盘（pre_write 快照 4252 个 / 51MB），
  `sglang:backuped_tokens_total` post_write→post_evict +61.6k 有增量。

## 本次新增证据（与 4090 负结果的关系）

1. **复现性**：同 `index_evicted` 结局出现在不同 GPU/内核/OS 形态，
   说明该现象是架构性的、与 WSL2 或 4090 特有环境无关。
2. **`.bin` 内容为内容寻址**：文件名携带 page 内容 hash——W/E 的
   token 序列跨 run/跨 cache_salt namespace 共享，run2 期间
   `backuped_tokens_total` 增长 6 万但 `.bin` 数不变（dedup hit）。
   E 阶段反复出现的 `HiCache prefetch success loaded=999` 也是
   命中内容相同的历史页，不是 namespace 索引幸存。
   → 验证器注意事项：`.bin` 增长只能证明"有新内容页落盘"，不能
   区分 dedup-hit；`backuped_tokens_total` 计数含 dedup 尝试。
3. **W TTFT 中位 2198.9ms**：file 后端下写入路径显著变慢
   （4090 基线 ~65.6ms 量级、同机 L2 run 应在 ~百 ms 级）。
   首轮（同 storage 残留时）为 1607ms。

## 解读边界

- 已证实：本负载形状下（host 索引被驱逐）L3 file 无法支撑 R 命中；
  storage 引用随 host 池条目同死，`.bin` 成为不可达孤儿文件。
- 未分离测量：是否存在"host 数据出局但索引幸存"的状态（本架构
  index=host 条目，推测不存在）；`buffer_only` host 模式是另一套
  契约，未测。
- 正向 L3 证据的候选路径（未做）：`hicache_host_memory_mode`
  buffer_only / 显式 backup 触发点 / 进程重启后索引恢复。

## 产物

`results-20261110T233023Z.json`（status=failed,
classification=index_evicted）、`server.log`。storage 目录为
`/tmp` 残留，未入库；`.bin` 列表见结果包 `storage_snapshots`。
