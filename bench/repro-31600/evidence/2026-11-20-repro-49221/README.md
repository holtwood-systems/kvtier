# 2026-11-21 repro-49221：L3 prefetch 命中页 admission 前被逐——独立复现成立（RTX 4090 48GB）

按 `docs/bench-design/2026-11-20-repro-49221.md` 执行。上游 issue
#49221（open）+ 修复 PR #49226（open，未合——本实验验证其合入前基线）。

## TL;DR（判定结论）

**H1 机制在 main 上复现成立**：cache 模式下 burst 相位的
`storage_prefetch_unfulfilled_tokens_total{reason="cache_admission_shortfall"}`
正增量并**主导**该相位未履约总量（j2 burst16：shortfall 82,688 vs
host_capacity 3,392 = **96%**），serial 相位全原因零增量；cached_tokens
跌至 serial 的 28%（issue 实况 34–41%）；剂效拐点、池放大救活（P4）、
buffer_only 免疫、TTFT 4.7–6.3×（P5）全部按预测成立。

**G1 字面判定 3/5 过、2 项字面偏离（各有机理解释与对照臂佐证）**：
- 过：G1-2（≥2 burst ≤0.75×serial）、G1-3（shortfall ≥50% 主导）、
  G1-5（j2 与 j1p 两次独立重复同向）；
- 偏离 1（G1-1 per-request）：serial 有 4-5/32 请求 storage=0——
  **file 后端的 L3 索引驻留 host radix**，65,600 tokens 池容纳不下
  32×6,080 的索引足迹，seed 自身写churn 即逐出部分索引（issue 用
  mooncake，索引独立于 host radix，故其 serial 100%）。对照臂
  j3/e2（ratio12=393,216）serial 恢复 **32/32** 反证此根因；
- 偏离 2（G1-4 host_capacity<64）：burst 中 host_capacity 2,368-4,480
  tokens（占未履约 4-13%）——非主导（G3 条款"host_capacity 主导→调大
  ratio 重跑"已在 j3 执行：ratio12 下全原因归零，非主导确认）。
设计 §5 自身的口径："G1/G2 只锚定相对关系与原因归属，不锚定绝对数"
（§2.3），据此判**机制复现成立**；两处字面偏离如实归档，不复测
（根因明确且有对照臂封堵）。

## 结果总表（cached_sum / storage 命中数；32 请求 × 6,100 prefix）

### 0.5B 探索臂（Qwen2.5-0.5B，host 池 65,600 tokens = ratio 2.0×32,768 页对齐）

| 臂 | serial | burst4 | burst8 | burst16 | burst32 | repeat32 |
|---|---|---|---|---|---|---|
| e1-gradient（wait_complete） | 143,936 (24/32) | 194,560 (32/32) | 53,504 (9/32) | 53,760 (9/32) | 83,904 (14/32) | — |
| e5-judgment（七相位） | 156,096 (26/32) | — | — | 41,472/42,560 (7/32) | 76,416 (13/32) | 83,904/83,904 (14/32) |
| e3-timeout（policy 对照） | 156,096 (26/32) | 194,560 (32/32) | 92,672 (16/32) | 60,096 (10/32) | 83,904 (14/32) | — |
| **e2-ratio12（P4）** | **194,560 (32/32)** | 194,560 (32/32) | 194,560 (32/32) | **194,560 (32/32)** | **194,560 (32/32)** | — |
| **e4-buffer（机制归属）** | 188,544 (32/32) | 194,560 (32/32) | 194,560 (32/32) | **194,560 (32/32)** | **194,560 (32/32)** | — |

- 剂效（P3）：conc 4 满 → conc 8 崩（拐点早于 issue 的 c*≈11，因
  burst 相位 write-through 写回 staging 与 prefetch staging 共用 host 池）；
- e5 与 issue 表逐相对齐：serial→burst16 = 27%（issue 34%）、
  burst32 部分恢复、repeat32 稳定；
- e3：deadline 混入（serial 即 24,320）——policy=timeout 把部分 shortfall
  改写为停读形态（设计 §6-D 预判，单独归类不并入 G2）；
- e4：buffer_only + wait_complete **全免疫**——admission shortfall 是
  cache 模式特有（与 PR #49226 自述修复面一致）；
- TTFT（P5）：e5 serial 0.181s → burst16 1.14-1.22s（6.3×）。

### 4B 判定臂（Qwen3-4B，同 host 池 token 数）

| 臂 | serial | burst16-a/b | burst32 | repeat32-a/b |
|---|---|---|---|---|
| j2-baseline（wait_complete） | 168,256 (28/32)，unfulfilled **全零** | 47,680/47,680 (8/32)，shortfall **82,688×2** | 88,512 (15/32)，shortfall 36,480 | 89,920/83,904 (15/14)，shortfall 36,480/30,400 |
| j1p（第二次重复 + V2 剖析） | 162,176 (27/32)，全零 | 41,472 (7/32)，shortfall **59,520**（96%） | （剖析后停止，见偏离 1） | — |
| **j3-ratio12（P4）** | **194,560 (32/32)** | **194,560 (32/32)×2** | **194,560 (32/32)** | **194,560 (32/32)×2**，全原因 unfulfilled **零** |
| j4-timeout（policy 对照） | 125,696 (21/32)，deadline 24,320 | 混合：shortfall 57-77k + deadline 7-70k + hc 2-4k | 混合 | 混合 |

- TTFT（P5）：j2 serial 1.13s → burst16 5.36/5.38s（**4.7×**）；
- j3 的 burst TTFT 升至 22-53s（wait_complete 串行回载 32×6,080 tokens
  的排队代价）——命中无损失，纯延迟代价，如实记录；
- 冷探针（G0-3）：全部臂 flush 后 `sum(cached_tokens_details)==0` ✓；
- drain：全部相位 enqueued==acked 收敛（无丢弃写）；
- storage 快照（隔离 B）：seed 后 .bin 增长 ≈ 32 前缀，后续相位不变
  （结果包 `storage_snapshots`）。

## 门禁逐项

- G0-1（seed backuped ≥188,416）：4B 臂 ✓（j2 seed acked 1,008,704）；
  0.5B e1/e5 为 143,936-156,096（部分回写 4,498/请求，字面未达，
  同偏离 1 的索引/回写容量根因）；e2/e4 194,560 ✓
- G0-2（serial ≥31/32）：j2 28/32、j1p 27/32——**字面未达**（偏离 1）；
  j3 32/32 ✓。路径存在性与 flush 语义由 27-28 个非零命中 + 冷探针零
  命中双向夹逼证明（设计 §6-C 的判据本意）
- G0-3 冷探针 ✓；G0-4 配置快照 ✓（TP=1、write_through、cache、file、
  wait_complete、commit 40 位）
- G1：3/5 过 + 2 字面偏离（见 TL;DR）
- G2（不复现）不成立——shortfall 主导复现于两模型
- G3：j3 已按条款执行"调大 ratio 重跑"——shortfall 消失而非仍主导，
  排除 staging 分配主导的替代解释

## 环境

- 同实验 0（同日同栈）：SGLang main `e183c415bdfc5995f6288b1651e1c320c6a74015`、
  torch 2.14.1+cu126、sgl-kernel 0.4.11、RTX 4090 48GB、/data NVMe
- 判定模型 `/root/models/Qwen3-4B`：revision
  `1cfa9a7208912126459214e8b04321603b3df60c`；sha256（3 shards）见
  `/root/models/Qwen3-4B-SHA256SUMS.txt`（首 shard
  `328a91d3122359d5547f9d79521205bc0a46e1f79a792dfe650e99fc2d651223`）；
  safetensors 头解析 398 权重 + 3 metadata 键与 index 一致
- 探索模型 `/root/models/Qwen2.5-0.5B-Instruct`（sha 同实验 0）
- 服务：`--trust-remote-code --mem-fraction-static 0.85 --context-length
  8192 --max-total-tokens 32768 --max-running-requests 32
  --chunked-prefill-size 2048 --page-size 64` + HiCache file/wait_complete/
  cache/page_first/kernel/write_through；host 池启动日志 65,600 tokens
  （ratio12 臂 393,216）= 设计值 ±0.1% 页对齐；端口 31000
- 客户端：`bench/repro-49221/workload.py`（七相位/梯度；32 互异 salt
  `shortfall-<uuid8>-<i>` 同 prompt；相位前 sleep 5s+/flush_cache；
  相位边界 reason 维度 counter 差分 + drain 收敛轮询 + 冷探针）
- GPU 协调：career-prep-7b 会话（曾占 22.5GB）于本序列启动前释放
  （03:47–03:51Z 间，锁删除、显存归零），全部测量窗口实际独占；
  锁以共享模式声明（2026-11-21 用户指示），各臂 `gpu-baseline.txt`
  记录取样时点

## V2 kernel 剖面（j1p）

- 剖析窗口 serial→burst16-a；trace 落
  `/data/prof/repro-49221/j1p-profile/1795235966.7180264-TP-0.trace.json.gz`
  （534MB，sha256 `37f634bbdd21c76dcd17995822f67fd257362bc197fa3bb7aee67846e2f520e1`，
  留服务器未入库）；Chrome trace 格式，可按请求分组核对
  "命中请求=copy 段+短 prefill、shortfall 请求=无 copy 段+长纯 prefill"

## 偏离项清单

1. **j1 首跑在 serial 前中止**：main 把 `/start_profile` 划入 admin 门控
   （未设 `--admin-api-key` 时 403）——改 j1p 带
   `--admin-api-key kvtier49221` 重跑；j1p 又在 stop_profile 处客户端
   120s 超时（trace 序列化 534MB 超窗）——**trace 完整落盘、
   serial/burst16-a 数据完整**，其后的 burst16-b/burst32/repeat 由 j2
   完整覆盖（j1p 判定价值不受损：G1-5 只需同向，serial+burst16-a 已同向）。
2. **G0-2/G1-1 字面未达的根因**：file 后端 L3 索引驻留 host radix
   （issue 用 mooncake 索引独立）——设计 §7 的 file 主臂保真度边界，
   j3/e2 ratio12 对照封堵；未改判据、未改上游。
3. **客户端补丁**：admin key 场景 `/flush_cache` 亦需 Bearer——
   `AdminAwareClient`（+ profile POST 头）；纯客户端。
4. **e4-buffer 显式 wait_complete**（与主臂策略一致才是机制对照）。
5. 执行日 11-21；端口 31000；模型路径 /root/models（设计 /data/models）；
   host-fraction 0.8 干扰由显式 ratio 钉住（补遗约束 1）；
   fast_file×unified 互斥不涉及（本实验全 file 后端）；12 值 label
   动态解析（补遗约束 3，实测与实验 0 相同 12 值）。
6. 共享模式执行的用户裁定（对方锁声明 exclusive）已写进锁文件 note
   字段与各臂 gpu-baseline.txt；实际共居只发生在等待期。

## 产物清单

- `e1…e5/j1/j1p/j2/j3/j4/`：`results/results-*.json`、`server.log`、
  `samples-*.jsonl`、`dmon-*.log`、`gpu-baseline.txt`、
  `host-pool-log.txt`、`workload-*.log`（UTC 时间戳相位标记）、
  `metrics-final.txt`、`server-info-final.json`；关键臂另有 `v1-*.png`
- `driver-*.log`、`sequence-progress.txt`：执行记录
- V2 trace：服务器 `/data/prof/repro-49221/j1p-profile/`（未入库，见上）
- 本 README

## 对上游的关系

- **#49226**：本实验 = 其动机的 GPU 级独立复现（main 基线、双模型、
  剂效+容量对照+机制归属齐全）；公开评论前按 PLAN.md P4 重查 PR 状态。
- **#49221**：issue 的机制假设（"L3 completion inserts host pages
  without retaining an eviction reference until admission"）与观测签名
  （burst-only shortfall 主导、串行零、容量阈值、policy 敏感）在
  main@e183c415b 上全部成立；差异面（file vs mooncake 索引容量、
  绝对数值）已如实标注。

## 2026-11-21 补记：V2 kernel trace 离机归档

- `prof-archive/j1p-profile.tar.gz`（529MB）= 服务器 `/data/prof/repro-49221/j1p-profile/`
  的完整打包（j1p 的 serial→burst16-a 窗口 trace；j1p 止于 stop_profile 120s 序列化
  超时，trace 与数据完整）。
- sha256 `949b6c6b4518419678d33c2e0ad5c9c684573f455a28c21e2609af1870cab8e9`，
  本地 sha256sum -c 通过（2026-11-21）。原目录级 sha `37f634bb…` 见正文偏离项记录。
