# 2026-11-05 单卡 HiCache L2 回载基线（原生 Linux / RTX 4090）

## 结论

在原生 Linux + RTX 4090 上完成并审计了一组单卡 HiCache L2 微基准：

- 运行 ID：`20261105T144330Z`；`num_trials=8`；前缀 4096 + tail 128 tokens；
- L2 回载 TTFT 中位数 **24.4 ms**，冷 prefill **65.1 ms**，
  配对 R/P 中位数 **0.375**；
- 8/8 轮 R 请求 `cached_tokens_details.host` 均为 **4223/4224**（100% L2 命中），
  冷 P 请求命中为 0；`min_host_hit_ratio=0.90` 达成；
- `validate_results.py` 通过（`paired_ttft_ratio_median=0.374922`）。

**关键结论：`hicache-io-backend kernel` 在原生 Linux 上无需任何补丁即可工作。**
WSL2 基线中 M 阶段第一个 R 请求必然在 flashinfer prefill `begin_forward` 触发
`cudaErrorIllegalAddress`（根因是 WSL2 的 `cudaHostRegister` host/device 地址映射
不一致，需要 PR #43375 的 host 分配补丁绕过）；本次原生 Linux 从服务启动、
write_through 备份、驱逐到 8 轮 L2 回载全程无补丁、无地址类错误。

**范围**：只描述本机、本模型、本配置下 host→device 回载 vs 等长冷 prefill 的
TTFT；不是 #31600 复现，不代表多卡/L3/EAGLE/decode 吞吐。

## 环境

| 项 | 值 |
|---|---|
| GPU | NVIDIA GeForce RTX 4090，49140 MiB，sm_89 |
| 驱动 | 550.107.02（CUDA UMD 12.4） |
| OS | 原生 Linux 5.15.0-60-generic（非 WSL2） |
| Python | 3.12.16（`~/.venvs/sglang-server`） |
| torch | 2.11.0+cu128（见「偏离项」） |
| SGLang | `6afee75f918f3d59bdae7d619ba26b0ca22b1354`（`0.0.0.dev2+g6afee75f9`） |
| sgl-kernel | 0.4.6.post1，**源码本地构建**（见「偏离项」） |
| 模型 | `Qwen2.5-0.5B-Instruct` HF BF16，`model.safetensors` 988,097,824 B，sha256 `fdf756fa…88fb7fe` |

## 服务配置与偏离默认项

| 项 | 值 | 原因 |
|---|---|---|
| `mem-fraction-static` | 0.40 | device KV 1,555,940 tokens / 17.8 GB |
| `hicache-ratio` | 2.0 | host pool ~3.1M tokens / ~38 GB；受容器 cgroup 96 GiB 内存上限约束（host 预算 `psutil.available-10GiB` 误读到宿主 1TB，0.78/4.0 默认组合需 153GB → 启动即被 OOM kill，见 server-startup.log 首轮失败记录） |
| `hicache-io-backend` | kernel | 本次验证对象 |
| `hicache-mem-layout` | page_first | 默认 |
| `num-trials` | 8（默认） | host pool 充裕，无需 WSL2 的减轮次 |
| CUDA graphs | 启用 | 默认捕获成功（44.7s prefill + 0.8s decode） |
| 本地补丁 | **无** | 与 WSL2 包的核心差异 |

## 工具链偏离项（与 WSL2 基线的环境差异）

1. **torch 2.11.0+cu128 替代 pinned 2.13.0**：该 commit 的 pyproject 锁
   `torch==2.13.0`，但 2.13 官方构建仅 cu130（要求驱动 ≥580），本机驱动
   550（CUDA 12.4）不可用；cu128 索引最高版本即 2.11.0。
   `cuda-compat-13-0` 前向兼容包实测被消费卡拒绝（Error 804）。
   `import sglang`/`http_server`/服务全程在 2.11 下通过，未观察到 API 缺口。
2. **sgl-kernel 0.4.6.post1 源码构建**：所有官方 wheel（PyPI 与
   docs.sglang.ai cu129 索引）均为 cu13 且只带 sm90/sm100 变体——即使能装
   也没有 sm_89 内核。本地从 `third_party/sglang/python/sglang/kernels/aot`
   编译，`ENABLE_BELOW_SM90=ON`；为适配 96 GiB cgroup 构建时限定了本地
   gencode 列表（sm_80/sm_89，未编 sm_90a/100a/120a 与 FA3/FlashMLA——
   两者对 sm_89 上的本模型均不可达，补丁留档
   `../../deps-sglk/arch-trim-sm89.patch`）。wheel 仅含本机架构，
   不可移植到其他 GPU。
3. **依赖离线预取**：GitHub 连接不稳，7 个 FetchContent tarball 经
   `curl --http1.1` 预取后以 `FETCHCONTENT_SOURCE_DIR_REPO-*` 注入。
4. **构建资源坑**：容器 cgroup 内存上限 96 GiB（`free` 显示为宿主 1TB）；
   且 `/` 仅 40GB，nvcc 中间文件曾写爆 `/tmp`（27GB），须 `TMPDIR=/data`。

## 与 WSL2 基线（2026-10-26，RTX 3060 Laptop）对比

| 指标 | WSL2 3060 Laptop | 本包 4090 | 说明 |
|---|---|---|---|
| L2 reload TTFT 中位 | 72.3 ms | 24.4 ms | 不同硬件/配置，仅并列展示 |
| 冷 prefill TTFT 中位 | 338.0 ms | 65.1 ms | 同上 |
| 配对 R/P 中位 | 0.2147 | 0.3749 | 回载/冷启耗时比；两包 server 参数、
   | | | | device capacity、host pool 均不同 |
| host 命中 | 4223/4224 ×6 | 4223/4224 ×8 | 均 100% L2 命中 |
| kernel IO | 需 PR #43375 补丁 | **无需补丁** | 核心结论 |
| CUDA graph | 禁用（显存所限） | 启用 | 配置差异 |

跨硬件数值不构成优化前后对比；可比较的是**机制层结论**：WSL2 特有的
host 指针地址不一致问题在原生 Linux 不存在，kernel IO 后端开箱可用。

## 产物

- `results-20261105T144330Z.json`：逐 trial TTFT、cached_tokens_details、
  Prometheus 增量、环境/参数快照（validate_results.py 通过）
- `server-startup.log`：服务启动全量日志（含首轮 153GB host pool OOM 记录）
- 服务端命令：`MODEL_PATH=… MEM_FRACTION=0.40 HICACHE_RATIO=2.0
  PATH=<venv>/bin:$PATH bash run_server.sh`
- 负载命令：`python workload.py --sglang-commit 6afee75f…22b1354
  --output-dir evidence/2026-11-05-linux-rtx4090-l2-baseline/`
