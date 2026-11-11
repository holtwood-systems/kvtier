# HiCache L2 单卡微基准

> 目录名 `repro-31600` 是早期命名，为避免破坏已有路径暂时保留。这个实验**不是**
> [SGLang #31600](https://github.com/sgl-project/sglang/issues/31600) 的复现。

## 能回答什么

本基准只比较同一台单卡服务器上的两条路径：

- `R`：前缀已从 GPU 驱逐、随后从 host DRAM（L2）回载；
- `P`：token 数完全相同、但 cache namespace 独立的冷 prefill。

它能判断“在这次配置下，L2 回载 TTFT 相对冷 prefill 是快还是慢”。它不能代表
#31600，因为原问题使用 GLM-5.2、FP8、EAGLE、L2+L3、TP/CP 多卡；报告中的主要
退化也出现在 decode/accept rate/总耗时，而不是 store-hit TTFT 本身。

## 证据约束

脚本不会仅凭请求顺序猜测缓存命中：

- 通过 `/tokenize` 构造精确等长的 `input_ids`，不再用字符数估算 token 数；
- 默认 `per-request` salt：每轮 W/R 共用一对独立 `cache_salt` 且逐轮使用
  不同 prompt 内容（不依赖 salt 做逐轮隔离）；P/E 使用独立 salt 与内容；
  `--salt-mode none` 可全部省略 salt（验证 L3 命中归属的机制对照臂）；
- 从 `/server_info` 读取实际 device `token_capacity`，据此生成超过容量的驱逐流量；
- R 请求必须报告命中：开启 storage 后端时要求 `cached_tokens_details.storage > 0`
  （L3 路径存在性），否则要求 `host + storage ≥ min_host_hit_ratio × 输入`；
  P 请求必须为零命中，否则整次实验标记为失败；
- Prometheus 的 backup、eviction、load-back counter 都必须出现正增量；
- **L3（`--expected-storage-backend` 非空）额外要求路径证据**：结果包须含
  `metrics.stage_counters`（post_write/post_evict/post_measure 分阶段 counter 快照），
  且 `sglang:backuped_tokens_total` 有正增量或 `storage_snapshots` 显示 `.bin`
  增长；不能仅凭 `cached_tokens_details.storage` 声称经过了 L3；
- `--storage-dir`（仅本机服务）在 W/E/M 各边界枚举 `.bin` 页文件
  `{name,size,mtime}`，把"数据是否真正落盘"与"索引是否幸存"拆开；
- 失败包带 `failure_classification`：`config / not_backed_up / index_evicted /
  storage_read_failed / transfer_anomaly / unknown`，由客户端可采集证据归因，
  匹配不到规则如实记 unknown；
- R/P 采用固定随机种子的配对、平衡顺序，所有原始响应、配置白名单和相关指标写入
  JSON 结果包；
- SSE 解析错误、异常结束或没有输出 token 事件都会失败，不会用总耗时冒充 TTFT。

## 运行

### 开发环境与结果审计

基准客户端只依赖 Python 3.10+ 和固定版本的 `requests`。建议为客户端建立隔离环境：

```bash
cd bench/repro-31600
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

正式运行必须传入构建服务所用的 40 位 SGLang commit SHA；这样结果包不会把未标记的
`latest` 环境当成可追溯证据。运行结束后，先用审计器检查完整结果：

```bash
python3 validate_results.py results/results-<UTC>.json
```

审计器会重新核对服务边界、W/E/R/P 阶段数量、L2/冷路径命中条件、配对 TTFT 比值和
Prometheus counter 增量。`failed` 结果仍可保留原始错误，但审计命令会以非零状态退出。

仓库根目录的 `make test` 运行所有离线测试，不启动服务，也不会写入 Python 字节码。

建议从明确的 SGLang commit 构建环境，并把 commit、镜像、GPU 型号和启动命令与
结果包一同保存。不要只记录 `latest` 或“最新 main”。

```bash
# 终端 A
MODEL_PATH=/path/to/Qwen2.5-7B-Instruct bash run_server.sh

# 终端 B；先检查离线试验计划
python3 workload.py --dry-run

# 服务 ready 后正式运行
python3 workload.py \
  --port 30000 \
  --sglang-commit <40-char-commit> \
  --num-trials 8 \
  --prefix-tokens 4096 \
  --tail-tokens 128 \
  --output-dir results
```

默认服务配置启用 `write_through`、cache report 和 HiCache 指标，不启用 L3 或投机
解码，并保持 TP/DP/PP/attention CP 均为 1；脚本会读取 `/server_info` 验证这些
边界。`HICACHE_RATIO=4.0` 为 host pool 留出空间，避免驱逐流量在测量前覆盖 W 阶段
的 L2 数据。可以用环境变量调整：

```bash
MEM_FRACTION=0.75 HICACHE_RATIO=5.0 HICACHE_IO=direct \
  MODEL_PATH=/path/to/model bash run_server.sh
```

### L3 机制对照臂

file 后端下可用以下参数构造机制分离实验（见
`evidence/2026-11-11-wsl2-rtx5070ti-l3-*/README.md` 的对照结果）：

- `--salt-mode none`：全请求不携带 `cache_salt`。salted 臂的
  `index_evicted` 并非存储缺失，而是 prefetch 以 anchor=root 的
  `salt=None` 插入路径、带 salt 的 re-match 不可见；saltless 臂可
  归属命中（本机实测 R `storage=640–999`）。
- `--evict-content-mode per-request`：saltless 下必需——共享 E 内容
  在内容寻址缓存中 dedup，不产生驱逐压力（有护栏硬检查）。
- `--post-evict-wait <秒>`：E 后等待 backup 队列 drain（静默收敛
  轮询；丢弃写永不 ack）。cache 模式下不等待会导致 prefetch staging
  分配与在途写回竞争 host 池，后续 R 被静默撤销。
- `HICACHE_HOST_MEMORY_MODE=buffer_only bash run_server.sh`：host 池
  仅作 staging、不驻留 L2，storage 命中经 staged prefetch 直接归属，
  不依赖 radix 路径可见性（本机 6/6 clean complete）。

如果自动驱逐后 R 没有达到默认 90% host 命中率，脚本会失败。应先检查结果包里的
`eviction`、`cached_tokens_details` 和 counter 增量，再决定是否提高
`--evict-factor`；不要直接把失败运行解释为“未复现”。

## 结果解释

成功结果位于 `results/results-<UTC>.json`，其中：

- `status=complete` 表示路径证据和测量均通过；`failed` 仍会保存错误及已有原始数据；
- `summary.paired_ttft_ratio_median` 是每轮 `TTFT_R / TTFT_P` 的中位数；
- `summary.l2_slower_than_cold_in_this_run` 只描述本机、本模型、本次参数；
- `metrics.counter_deltas` 用于交叉核对 D2H backup、device eviction 和 H2D load-back。

在真实 GPU 上跑完并审核 JSON 后才能给出性能结论。目前有一组已审计结果：
[`evidence/2026-10-26-wsl2-l2-baseline/`](evidence/2026-10-26-wsl2-l2-baseline/)
（WSL2 / RTX 3060 Laptop / Qwen2.5-0.5B，6 轮，R/P 中位比 0.215）。该结果只描述
该机、该模型、该配置，不代表 #31600；上游合入 PR
[#43375](https://github.com/sgl-project/sglang/pull/43375) 后应以新 commit 重跑。
