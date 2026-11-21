# 数字弹药库（面试速查）

> 每条数字附来源。标注 [估算] 的是从实测推导的外推，面试使用时说明推导链。
> 维护规则：实验每出一组新数，当天追加；不确定的数字宁可不背。

## 硬件与基础设施（本机实测 2026-11-20）

| 数字 | 语境 | 来源 |
|---|---|---|
| 49140 MiB / 48GB 改装 4090 | sm_89，驱动 550.107.02 | nvidia-smi |
| D2D 带宽 937 GB/s | 4GiB 拷贝实测，4090 健康水平（spec ~1TB/s） | vram_stress.log |
| 25 min 满载 22147 轮零位损坏 | 43×1GiB 图案 + GEMM 混压，两次中途校验 | vram_stress.log |
| 峰值 74°C / 352.7W | 无异常降频（0x4=正常软件功耗墙） | monitor.csv |
| 宿主内存 1TB / NVMe 500G | L2=host DRAM、L3=/data NVMe 的物理层 | 容器盘点 |

## HiCache 核心消融（kvtier 已审计结果包，SGLang 6afee75f + Qwen2.5-0.5B）

| 数字 | 语境 | 来源 |
|---|---|---|
| R/P 中位比 0.375 | L2 回载 TTFT 24.4ms vs 冷 prefill 65.1ms，8/8 轮 host 命中 4223/4224 | 2026-11-05 l2-baseline |
| io-backend direct: R/P 0.988 | 回载收益几乎消失；且写入 TTFT 1631.9ms vs kernel 65.6ms → **kernel IO ≈ 2.6× 回载优势** | 2026-11-05 io-direct |
| fp8_e4m3 KV: 容量 1,555,940 → 3,111,880 tokens | 翻倍，R/P 仅劣化到 0.392（+4%） | 2026-11-05 kv-fp8 |
| 关 CUDA graph: R/P 0.375→0.416（+11%），写入慢 22% | 机制结论不依赖 graph | 2026-11-05 no-graph |
| device KV 17.8 GB @ mem-fraction 0.40 | 用预算控制而非大模型制造 L1 压力 | 2026-11-05 配置 |
| WSL2 上 R/P 0.215（72.3 vs 338.0ms）+ cudaHostRegister 非恒一 crash | 跨平台根因定位故事 | 2026-10-26 wsl2-l2 |
| load-back 25,338 tokens = 311 MB | → **12.3 KB/token**（BF16） | 2026-10-26 计数器 |

## Roofline 式外推 [估算]——HPC 叙事武器

- PCIe Gen4 x16 实用 ~25 GB/s：回载 4223 tokens ≈ 52 MB ≈ **2ms**，而 R 的 TTFT 中位 24.4ms
  → 小 prefix 下 L2 回载**不是** PCIe-bound，瓶颈在调度/attention 本身
- 外推拐点：~100K tokens（≈1.2 GB）回载需 ~50ms，**此时 PCIe 才成为主导项**
- D2D 937 GB/s vs PCIe 25 GB/s = **37×**：一旦页在 device 内整理，代价数量级不同
- 推导链要背熟：12.3KB/token 来自 311MB/25338 tokens；25GB/s 是 Gen4 x16 常用工程值

## 上游候选问题的数字（issue 报告值，非自测）

| 数字 | 语境 | 来源 |
|---|---|---|
| #49221：串行 188,416 vs 并发 16 命中 64,768 tokens，shortfall +88,320 | admission 前驱逐假设 | issue 页 |
| #47481：命中率 87.4%→64.0%（第8→10轮），TTFT 0.63→1.78s；buffer_only 66.3→74.5 | 容量压力骤降 | issue 页 |
| #46952：驱逐耗时 0.6ms→260-340ms（~500×） | write_back 阻塞 scheduler | issue 页 |
| mem_cache 目录 ~660 commits / 3 个月 | 重基线必要性的量化 | GitHub API 统计 |
| #36115 关闭教训 | "只欢迎挂在 bug 修复上的贡献"——贡献策略的准绳 | PR 关闭评论 |

## 迁移基线（2026-11-20 复跑，audit 通过，环境重置后）

| 数字 | 语境 |
|---|---|
| R/P 0.4817（11-05 对照 0.3749） | 0.3~0.5 通过区间内 |
| host 命中 8/8 × 4223/4224 | 与 11-05 完全一致 |
| R 52.8 ms / P 109.4 ms | 绝对值偏慢（11-05：24.4/65.1），归因同机并发租户 |
| 3,568 MiB "幽灵"显存占用 | 宿主侧不可见 PID——共享宿主机的直接证据 |
| device pool 1,434,030 tokens | 11-05 为 1,555,940（被幽灵占用挤压） |

## 实验 0 + #49221（2026-11-21，main e183c415b，本会话核心产出）

| 数字 | 语境 |
|---|---|
| salted 4/6 = nosalt 4/6 | salt 归属已修（旧基线 salted 0/6）——因果闭环 |
| 驻留中位 328/320 ≪ 2112 | staging 饥饿未修（分配路径形态） |
| `unfulfilled{host_capacity}` +14,668 / +6,733，dropped=0 | 旧"静默撤销"→ 计数器可见 |
| 4× 池臂 1/6；drain 240s：enq 204,686 / ack 118,670；deadline 8,318；4/6 零命中 = 查询超预算被静默切断（TTFT 聚在预算+prefill；盘上文件数 == acked，排除真丢数据） | 结构型：单线程 L3 写积压 → 读查询超时 → 零记账伪装 miss（修复设计见 11-21 文档；初版"索引逐出"解读已被证据推翻——保留作方法教训） |
| hit = fulfilled + Σunfulfilled（8,446 = 519 + 7,927） | 账目恒等式，每臂精确闭合 |
| fast_file：host_capacity 25,338 = 6×4,223（100%） | 后端不改变结论 |
| #49221：serial 168,256 → burst16 47,680（**28%**，issue 34-41%） | 核心复现签名 |
| shortfall 占未履约 **96-97%**（82,688 vs host_capacity 3,392） | 归因主导性 |
| TTFT 4.7×(4B) / 6.3×(0.5B) | 干扰幅度 |
| 池放大 393,216 → 32/32 全恢复、全原因归零 | 容量介导确证 |
| buffer_only + wait_complete：全免疫 | 机制归属 = cache 模式特有 |
| repeat32 稳定 89,920/83,904 | 可重复性 |
| `hicache_prefetch_outcomes` 0 命中 / 11 份 metrics | #36280 的运行时证据 |

## 待补

- [ ] 上游再介入结果（#49226 证据评论 / #36280 投递 / 新 issue）
