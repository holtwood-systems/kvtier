# 2026-11-21 实验 0：L3 链路 main 双臂验证（RTX 4090 48GB）——G1 不成立，②未修（结构型）

按 `docs/bench-design/2026-11-20-repro-main-dualarm.md` 执行（设计日 11-20，
执行日 11-21）。DEV-20 同形状负载在 SGLang main 上重跑 2×2 双臂 × 双后端
+ 剂量臂 + 烟测 + 1 个补充臂（A3W，见偏离项 4）。

## TL;DR（预测矩阵对照结论）

**矩阵命中第 2 行"①修 ②未修"，且剂量臂判定为结构型；G1 不成立 →
满足"潜在真 bug 信号"判据，只记录证据，未触碰上游代码。**

| 判定 | 结论 | 关键证据 |
|---|---|---|
| 根因①（salt 归属） | **已修** | A1（salted）4/6 R `storage>0`，与 A2（nosalt）4/6 无差异（覆盖率差 0 ≤ 1 trial）；6afee75f 基线 salted 为 0/6 |
| 根因②（staging 饥饿） | **未修，分配路径形态** | A1/A2 中位驻留 328/320 ≪ L2(2112)；M 阶段 `unfulfilled{host_capacity}` +14,668 / +6,733（≥256 主签名成立）；`dropped`=0（6afee75f 的静默撤销形态已消失，代之以计数器可见的分配失败） |
| 剂量臂 C2（4× 池） | **结构型** | 172,096 tokens 池下 1/6、中位 0，未恢复 ≥L2；且失败形态随容量改变：`host_capacity` 归零，由 **deadline 截断 + L3 索引未命中**接管（4/6 查询即 miss），drain 240s 未收敛（enqueued=204,686 vs acked=118,670） |
| G1（双根因闭合） | **不成立** | A1/A2 覆盖 4/6 < 5/6，中位 <L2，M 增量 host_capacity ≥256 |

## 环境

- 机器：容器 `fa19b8931a47`（重部署后），RTX 4090 48GB（sm_89），宿主内存 1TB，
  `/data` 500G NVMe；GPU 底噪：重部署后空闲 1–4 MiB（旧 ~3.5GB 幽灵消失）；
  同机另一会话跑旧环境 L3 实验，经 `/data/gpu-coordination/` 协调协议串行使用
  GPU（本实验全部窗口独占，见各臂 `gpu-baseline.txt`，实测 1–4 MiB 起跑）
- 服务端：SGLang main `e183c415bdfc5995f6288b1651e1c320c6a74015`
  （`0.0.0.dev1+ge183c415b`，editable `/root/src/sglang-main`）；
  **torch 2.14.1+cu126**（首次构建 cu130 与驱动 550.107.02 不兼容，cu12x 重建）；
  **sgl-kernel 0.4.11** 源码构建（`/data/build-sglk-main`，
  `TORCH_CUDA_ARCH_LIST=8.9`，`CUDA_HOME=/usr/local/cuda-12.8`）
- 严格判据过闸记录：`import sglang/sgl_kernel/torch` + `cuda.is_available()` +
  256² matmul 实测通过（GATE-OK）
- harness：`bench/repro-31600` 71a0fe6 + main 适配补丁（reason 级指标解析、
  `SGLANG_VER=main` 开关、validate 兼容）+ 本次补齐 `--expect-mode diagnostic`
  （偏离项 2）；服务器 `make test` 44/44（40 旧 + 4 新）
- 模型：`/root/models/Qwen2.5-0.5B-Instruct`，
  `model.safetensors` sha256 `fdf756fa7fcbe7404d5c60e26bff1a0c8b8aa1f72ced49e7dd0210fe288fb7fe`（与 DEV-20 逐字节一致）
- 客户端：`bench/repro-31600/.venv`（workload.py diagnostic 模式，
  `--num-trials 6 --prefix-tokens 4096 --tail-tokens 128 --evict-factor 1.3
  --post-evict-wait 240 --expect-mode diagnostic`；
  nosalt 臂 `--salt-mode none --evict-content-mode per-request`）
- 服务参数：`--mem-fraction-static 0.35 --max-total-tokens 358532
  --hicache-io-backend kernel --hicache-mem-layout page_first
  --hicache-write-policy write_through --page-size` 默认(1)；端口 31000
  （协调协议）；**未传** `--hicache-storage-prefetch-policy`（main 默认
  `timeout`——见偏离项 3/发现 5）；page/graph 等其余与设计 §7 一致
- 驱动/采样：每臂 `run_arm_full.sh`：GPU 基线核验 → 清 storage → 起服 →
  health → host-pool 日志截取 → 1 Hz 采样器（samples-*.jsonl）+
  nvidia-smi dmon → workload（时间戳日志）→ metrics/server_info 全文 → 停服；
  V1 图 `plot_v1.py` 本地离线出图（A1/A2/A3/B1/C2/SMOKE 各一张）

## G0 核对（全臂通过）

- 等压臂 host pool 启动日志 **43,024 tokens**（设计 43,024±2%，精确命中）；
  C2 = **172,096**（设计 172,095±2%）；SMOKE = **17,927**（0.05×358,532 精确）
- `/server_info` device `token_capacity` = **358,532**（全臂，±2% 内精确）；
  E=456 请求 / 466,944 tokens（与 DEV-20 同形逐 token 相等）
- 配置快照：write_through / 后端与臂一致 / host mode 与臂一致 / TP=1 /
  metrics+cache_report 开启；commit 40 位已录（results 包 `args.sglang_commit`）
- W 落盘证据：各臂 `backuped_tokens_total` post_write 正增量；
  P 探针全冷（diagnostic 模式下 P 判据仍 strict，全部通过）
- storage 目录每臂 run 前清空（`rm -rf` 后重建）

## 结果总表（每臂 6 trial；R 判据 storage>0；M 增量 = post_drain→post_measure）

| 臂 | 配置 | hits/6 | storage 逐 trial | 中位(级) | M: host_capacity | M: deadline | M: dropped/bt | M: hit_tokens | drain 收敛 |
|---|---|---|---|---|---:|---:|---:|---:|---|
| SMOKE | cache/file/0.05/nosalt, 2 trial | 1/2 | 519, 0 | —(烟测) | 7,927 | 0 | 0/0 | 8,446 | 是(14s) |
| A1 | cache/file/0.12/salted | 4/6 | 512,640,384,272,0,0 | 328 (L1) | **14,668** | 4,639 | 0/0 | 21,115 | 是 |
| A2 | cache/file/0.12/nosalt | 4/6 | 384,0,0,512,384,256 | 320 (L1) | **6,733** | 8,623 | 0/0 | 16,892 | 是 |
| A3 | buffer/file/0.12/salted | 0/6 | 全 0 | 0 (L0) | 0 | **21,115** | 0/0 | 21,115 | **否** |
| A4 | buffer/file/0.12/nosalt | 0/6 | 全 0 | 0 (L0) | 0 | **25,338** | 0/0 | 25,338 | **否** |
| B1 | cache/fast_file/0.12/salted | 0/6 | 全 0 | 0 (L0) | **25,338** | 0 | 0/0 | 25,338 | 是 |
| B2 | cache/fast_file/0.12/nosalt | 0/6 | 全 0 | 0 (L0) | **25,338** | 0 | 0/0 | 25,338 | 是 |
| B3 | buffer/fast_file/0.12/salted | 0/6 | 全 0 | 0 (L0) | 0 | **8,446** | 0/0 | 8,446 | **否** |
| B4 | buffer/fast_file/0.12/nosalt | 0/6 | 全 0 | 0 (L0) | 0 | **8,446** | 0/0 | 8,446 | **否** |
| C2 | cache/file/**0.48**/nosalt | 1/6 | 0,0,0,128,0,0 | 0 (L0) | 0 | **8,318** | 0/0 | 8,446 | **否**(240s 超时) |
| A3W | buffer/file/0.12/salted/**wait_complete** | — | R-000 挂起 | — | — | — | — | — | 否 |

**账目恒等式（每臂精确闭合）**：`hit_tokens = Σ fulfilled(storage) + Σ unfulfilled`。
例：A1 = 1,808(=512+640+384+272) + 14,668(hc) + 4,639(deadline) = 21,115 ✓；
SMOKE = 519 + 7,927 = 8,446 ✓。计数器语义自洽，可直接作为上游报告数据。

## 判定细节

### D1（①，A1 vs A2）
两臂覆盖率差 0 trial、驻留中位差 8 token——salted 与 saltless 行为无差异。
6afee75f 上 salted 全 0 的归属缺陷在 main 已消除（静态核对 §2-1 的行为级确认）。

### D2（②，A1/A2 主签名）
`host_capacity` 主导且 `dropped`/`below_threshold` 均为 0：main 的饥饿
残留从 6afee75f 的"分配不足静默撤销（revoke，仅 debug 日志）"变形为
"命中时分配失败 → `unfulfilled{host_capacity}` 计数器出口"——正是设计
§3 预期的"②未修（分配路径形态）"。debug 日志侧：被截断 trial 可见
`HiCache prefetch success ... loaded=519`（部分命中如实记录），全额失败
trial 无 prefetch 完成行；**host_capacity 路径无专属文本日志行**（观测性
发现 4）。

### 剂量臂 C2（容量边界 vs 结构）
4× 容量未恢复驻留 → 按设计 G2 判"结构型"。机制形态随容量改变：
- 大池放行了更多 E 写回（enqueued 204,686 vs A1 75,662），240s 内 drain
  未收敛（86,016 tokens 在途钉住）；
- M 期 `host_capacity`=0（池足够），但 4/6 查询 L3 索引未命中（hit 仅
  8,446≈2×4,224——host radix 容量大 → E 条目挤掉 W 前缀的 storage 引用，
  DEV-20 的 `index_evicted` 机制在大池下复活），命中的 2 个查询又被
  deadline 截断（8,318）。
- 结论措辞：**"结构型"——饥饿不是单纯容量边界问题；容量放大反而切换到
  索引逐出+IO 截断的复合形态**。

### buffer 臂（H③）与 prefetch 策略（发现 5）
main 默认 `--hicache-storage-prefetch-policy=timeout`。buffer_only 四臂
（A3/A4/B3/B4）全部 `deadline` 全额未履约（hit 100% 但 0 加载）。
补充臂 A3W（wait_complete）：R-000 即挂起（server.log 有 6 处 park 记录），
客户端 600s 超时。**H③ 的"部分驻留"形态在 main 不可达**：timeout→截断
为零，wait_complete→挂起；6afee75f 的 1373–1536 部分加载语义已不存在。
6afee75f 的两个 buffer workaround（metrics clamp、STRICT_MEM_CHECK）在
main **均不再触发**（无崩溃，G3 未命中）。→ #47481 的 H_chain 预测需要
按"策略级截断"重新表述。

### fast_file 增量对比
B1/B2（cache）100% `host_capacity`（25,338 = 6×4,223 全额），比 file 臂
（部分命中）更彻底；B3/B4（buffer）与 file-buffer 同为 deadline 主导。
fast_file 不改变 ①② 结论，但在 cache 模式下饥饿更完全。fast_file 目录
布局：`<root>/<namespace>/` 子目录 + 非 `.bin` 命名，`storage_snapshots`
对 fast_file 臂不可解析（G3 降级条款如约触发：该四臂仅用 counter 判定）。

## 可观测性核对清单（设计 §6）

1. **revoke 出口**：全臂 `dropped`=0，cache 臂 debug 日志无 revoke 行——
   counter 与日志一致（无双轨缺口）；main 上 revoke 未触发（host_capacity
   先于 revoke 出口）。
2. **分类缺口（#36280 绑定）**：全部 11 份 `metrics-final.txt` grep
   `hicache_prefetch_outcomes` = **0 命中**；14 键 `_prefetch_outcome_stats`
   事件分类只存在于进程内 dict 的论断获得运行时证据。实测 collector 预建
   **12** 个 reason label（审计文档记 11，差 1：现含 `dropped`），
   `hit/fulfilled/unfulfilled` 三量恒等闭合说明 token 维度计数器自洽。
3. **#49226 计数器面**：本实验全部判定信号（`unfulfilled{reason}` 族）
   在 #49226 diff 中未改名——数据可直接用于其前/后对比。
4. **阈值语义**：`below_threshold` 全臂 0，本次无绑定验证样本（如实记录）。
5. **buffer 丢弃映射**：6afee75f 的 `dropped reason=overlap`（内部 staged
   丢弃）在 main 映射为 `unfulfilled{reason=deadline}`（timeout 策略停读）；
   `device_overlap`/`already_cached` 均 0。
6. **flush_cache 重置**：counters_before 逻辑工作正常，M 期增量干净
   （阶段边界无跨相位污染）。

## 主要发现汇总（潜在上游 bug 信号，只记录不修复）

1. **G1 不成立**：main 未闭合 DEV-20 根因②——等压 43k 池下 prefetch
   staging 分配失败（`host_capacity` 主签名），salted/nosalt 一视同仁。
2. **结构型证据**：4× 容量不恢复，且形态切换为索引逐出 + deadline。
3. **buffer_only R 路径不可用**：默认 timeout 下 deadline 全额截断；
   wait_complete 下请求挂起（无进展 park）。
4. **host_capacity 无文本日志**：分配失败只走计数器，debug 级亦无行。
5. **drain 收敛性**：buffer 臂与 C2 的 backup drain 240s 不收敛
   （A3/A4/B3/B4/C2），write-through 在途钉住的收敛上限需要更长窗口
   或有丢弃（`hicache_backup_dropped_tokens_total` 为 None——该 counter
   未出现在 /metrics，佐证 #37503 族并非全量导出）。

## 偏离项清单

1. **执行日**：设计 2026-11-20，实际 2026-11-21（main 环境两度重建：
   cu130→cu126、sgl-kernel 构建中断恢复——wheel 已就绪仅补安装）。
2. **harness 补齐 `--expect-mode diagnostic`**：设计 §4 要求（"R storage=0
   不再触发 failed"）但工作区 diff 未含；本次实现（workload.py +
   validate_results.py + 4 项单测，make test 44/44）。无它则预测矩阵的
   覆盖率判定（如"A1 全 6 trial storage=0"）不可评估。
3. **prefetch policy 默认值**：设计模板未含 `--hicache-storage-prefetch-
   policy`（6afee75f 无此 flag）；main 默认 `timeout` 使 buffer 臂被
   deadline 主导——按"重试梯/停读形态单独归类"处理（#49221 设计 §6-D
   同款纪律），并补 A3W 补充臂隔离该变量。
4. **补充臂 A3W**（超出设计 9 臂）：buffer_only + wait_complete + file，
   用于回答 H③ 本体；结果为挂起（本身是发现 3 的证据）。
5. **端口 31000**（设计 30031）：GPU 协调协议要求避让另一会话的 30000。
6. **模型路径** `/root/models/...`（设计写 `/home/shane/models/...`，
   为该服务器实际路径，sha 一致）。
7. **fast_file `max_size` 取 `"20G"`**：设计写 `20`——按 main
   `parse_size_to_bytes` 语义裸数字会按**字节**解析（20B），必须带单位。
8. **B3/B4 storage_snapshots 降级**（G3 预案条款）：fast_file 命名布局
   非单 token `.bin`，快照为空数组，判定仅用 counter。
9. **failure_classification 误标**：A3/A4/B3/B4/A3W 的 `index_evicted`/
   超时分类是 6afee75f 时代启发式（load_back==0 或 HTTP 超时），实际
   归因以 counter 为准（deadline/hang）——分类器不认识新 reason，如实
   留档不改代码。
10. **V1 trial 散点**为 workload 日志行时间戳（trial 完成时刻），非单请求
    时刻（plot_v1.py 文档注明）；V3 kernel 剖面未触发（触发条件"②未修
    签名臂的重跑"——重跑未发生：各臂判定不在边界，无可重复性条款触发）。
11. **GPU 协调**：同机另一会话（旧环境 L3 实验）并行，全程按
    `/data/gpu-coordination/` 协议串行（锁文件 + 心跳 + 轮询等待），
    本实验各测量窗口实际独占。

## 产物清单

- `SMOKE/`：烟测（判据验证：`hit 8,446 = 519 fulfilled + 7,927 host_capacity`
  恒等成立；pool 17,927 精确）
- `A1…C2/`：各臂 `results/results-*.json`（完整结果包）、`server.log`、
  `samples-<arm>.jsonl`（1 Hz）、`dmon-<arm>.log`、`gpu-baseline.txt`、
  `host-pool-log.txt`、`metrics-final.txt`、`server-info-final.json`、
  `workload-<arm>.log`（UTC 时间戳）；A1/A2/A3/B1/C2/SMOKE 另有 `v1-*.png`
- `A3W/`：补充臂（wait_complete 挂起证据）
- `arm_verdicts.json`：verdict.py 全臂判定输出（G0/G1 逐项 + M 期增量）
- `driver-*.log`、`sequence-progress.txt`、`sequence-runner.log`：序列执行记录
- 本 README

## 对两份主实验的影响（设计 §11 的更新）

- **repro-49221**：背景更尖锐——等压下 staging 饥饿（host_capacity）与
  admission 窗口叠加，其 §6 隔离设计需启用 `host_capacity/deadline` 分流
  分支；判定臂的 `wait_complete` 参数与 buffer 臂解读需参照发现 3/5。
- **repro-47481**：H_chain 在 main 的 file/fast_file 侧被策略级截断取代
  （deadline），预测需下调并改写机制表述。
- **#49226**：等压臂 ② 签名 + C2 结构型 = 其容量排队/FIFO 的 GPU 级动机
  证据成立；**#36280**：14 键事件分类未导出的运行时证据已留档（§6-2）。

## 入库策展说明（2026-11-21）

SMOKE/A1/A2/A3/A3W/A4/C2 七个臂的 `results/*.json`（单文件 5–44MB，内嵌逐页
storage 快照枚举）按仓库体积纪律（2026-11-11 先例）**留档本地不入库**；本目录
README/arm_verdicts.json/metrics-final.txt/server.log/samples/dmon/V1 图均已入库，
审计口径数字均取自原始记录。需要完整包复审计者可向所有者索取。
