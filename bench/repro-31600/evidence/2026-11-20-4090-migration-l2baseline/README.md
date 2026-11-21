# 2026-11-20 迁移验证：RTX 4090 租用容器复跑 L2 回载基线

## 结论

在新租用的 RTX 4090 48GB 改装卡容器上，按 `2026-11-05-linux-rtx4090-l2-baseline`
的精确配方复建环境并复跑同参数基线，判定**环境迁移有效**：

- 运行 ID：`20261120T083927Z`；`num_trials=8`；前缀 4096 + tail 128 tokens；
- L2 回载 TTFT 中位数 **52.8 ms**，冷 prefill **109.4 ms**，
  配对 R/P 中位数 **0.4817**（对照线 0.3~0.5 区间内）；
- 8/8 轮 R 请求 `cached_tokens_details.host` 均为 **4223/4224**（99.98% L2 命中，
  ≥ 99% 达成），冷 P 命中为 0；`min_host_hit_ratio=0.90` 达成；
- Prometheus 增量：`hicache_backup_tokens_total +1,647,125`、
  `evicted_tokens_total +247,000`、`load_back_tokens_total +33,784`
  （= 8 × 4223，逐 token 对账吻合）；
- `validate_results.py` 通过（`paired_ttft_ratio_median=0.481669`），status=complete。

机制层结论与 11-05 一致：`hicache-io-backend kernel` 在原生 Linux 上**无需任何
sglang 补丁**即可完成 write_through 备份、驱逐与 8 轮 host→device 回载，全程无
地址类错误。

**范围**：只描述本机、本模型、本配置下 host→device 回载 vs 等长冷 prefill 的
TTFT；不是 #31600 复现。绝对 TTFT 与 11-05 不同机器不可直接比（见「环境异常」）。

## 环境

| 项 | 值 |
|---|---|
| GPU | NVIDIA GeForce RTX 4090（48GB 改装卡），49140 MiB，sm_89 |
| 驱动 | 550.107.02（CUDA UMD 12.4） |
| OS | Ubuntu 22.04.5 容器，Linux 5.15.0-60-generic |
| Python | 3.12.16（`~/.venvs/sglang-server`，uv venv + python-build-standalone） |
| torch | 2.11.0+cu128（torchaudio 2.11.0+cu128 / torchvision 0.26.0+cu128） |
| SGLang | `6afee75f918f3d59bdae7d619ba26b0ca22b1354`（`0.0.0.dev1+g6afee75f9`，editable，浅取单 commit） |
| sgl-kernel | 0.4.6.post1，**源码本地构建**（同 11-05 配方，见「工具链偏离项」） |
| 模型 | `/root/models/Qwen2.5-0.5B-Instruct` HF BF16，`model.safetensors` 988,097,824 B，sha256 `fdf756fa…88fb7fe`（与 ENVIRONMENT.md 记录一致，已校验） |
| GPU 门禁 | vram_stress **ALL PASS**（rounds=22147；D2D copy 937.0 GB/s 参考值见 gpu-stability-gate.log）；门禁通过前未启动任何 GPU 工作 |

## 服务配置与偏离默认项

与 11-05 对照配置**逐项一致**，无偏离：

| 项 | 值 | 本次实际 |
|---|---|---|
| `mem-fraction-static` | 0.40 | 0.40（device pool 1,434,030 tokens / 16.4 GB，见「环境异常」） |
| `hicache-ratio` | 2.0 | 2.0 |
| `hicache-io-backend` | kernel | kernel |
| `hicache-mem-layout` | page_first | page_first |
| `hicache-host-memory-mode` | cache | cache |
| `hicache-write-policy` | write_through | write_through |
| `num-trials` | 8（默认） | 8 |
| CUDA graphs | 启用 | 启用（捕获成功） |
| 本地补丁（sglang 侧） | 无 | 无（仅 sgl-kernel 构建 arch-trim 补丁，同 11-05） |

workload 侧全部默认参数（`--prefix-tokens 4096 --tail-tokens 128 --num-trials 8
--seed 31600` 等），与 11-05 结果包 `args` 快照一致。

## 与 11-05 对照基准对比

| 指标 | 2026-11-05 4090（对照） | 本次 2026-11-20 4090 迁移 | 说明 |
|---|---|---|---|
| L2 reload TTFT 中位 | 24.4 ms | 52.8 ms | 绝对值受共享环境干扰（见下），不构成配置回归证据 |
| 冷 prefill TTFT 中位 | 65.1 ms | 109.4 ms | 同上 |
| 配对 R/P 中位 | 0.3749 | **0.4817** | 均在「回载显著快于冷启」区间，迁移判定线 0.3~0.5 内 |
| host 命中 | 4223/4224 ×8 | 4223/4224 ×8 | 完全一致，命中机制无回归 |
| device pool | 1,555,940 tokens | 1,434,030 tokens | 外来显存占用 3.5 GiB 所致（见下） |
| kernel IO 补丁 | 无需 | 无需 | 机制结论保持 |
| validate_results | 通过 | 通过 | — |

判定：R/P 比值与 host 命中率两条通过线均达成，**迁移有效**；绝对 TTFT 约 2.2x
偏慢如实记录，候选归因见「环境异常」，未做任何重跑凑数。

## 工具链偏离项（相对 11-05 记录 / ENVIRONMENT.md）

1. **uv 0.13.9**（pip 自 aliyun 源装入系统）；CPython 3.12.16 经
   `gh-proxy.com` 镜像下载 python-build-standalone（ghfast.top 对该资源 403，
   直连 GitHub 仅 ~12 KB/s）。UV_CACHE_DIR 后移至 `/data/uv-cache`。
2. **sglang 版本串 `0.0.0.dev1+g6afee75f9`（11-05 为 dev2）**：本次为
   `git fetch --depth 1 origin <sha>` 单 commit 浅取，setuptools-scm 的
   tag 距离计算与当时多取一 commit 的克隆不同；`+g6afee75f9` 可追溯性等价。
3. **torch 家族补齐**：pyproject 的 `torchaudio==2.11.0`（PyPI 变体）在
   torch 2.11.0+cu128 下 `import sglang` 时因 `libcudart.so.13` 缺失败
   （transformers → torchaudio 链）。显式改装 `torchaudio==2.11.0+cu128`、
   并对齐 `torchvision==0.26.0+cu128` 后恢复。11-05 README 只记了 torch。
   `deep_gemm` 的 `_C.so` 同为 cu13 变体，但 sglang 对其按模型惰性加载并
   吞掉 import error（Qwen2 不触达），未处理。
4. **sgl-kernel 0.4.6.post1 源码构建**（同 11-05 路线）：
   `python/sglang/kernels/aot` + `ENABLE_BELOW_SM90=ON` + FA3/FlashMLA 关闭；
   - arch-trim 补丁：`deps-sglk/arch-trim-sm89.patch`，sha256 实测
     `d8f800847d10964d55f154be5ecd36ef9ad553fa1031b897d2ec340b31482e48`
     （注意与 ENVIRONMENT.md 2026-10-26 段记录的 `d9e99574…` 区分——那是
     sglang-43375 补丁的哈希，不是本文件）；
   - 补丁后 `aot/CMakeLists.txt` sha256
     `726a6caa677492fef401e966f4dad3f493e0fe04694fa4f9422f644ab61cfb0b`；
   - 产物 wheel sha256
     `dba9d4eae657b78298bc898d6fb5d72c4257e4c6b155c88b8aa419112e4286e3`
     （26.9 MB，仅本机架构，不可移植）；
   - FetchContent 依赖 5 个（cutlass/fmt/triton/flashinfer/sgl-attn）经
     `gh-proxy.com` tarball 预取到 `/data/deps-sglk/`，SHA256 全部与
     CMakeLists 内 URL_HASH 一致；FlashMLA 按补丁已禁用，未取。
5. **finish-sglk.sh 最小适配**（服务器 `/root/finish-sglk-4090.sh`）：
   路径 `/data/open-infra-ai/kvtier` → `/root/src/kvtier`、`D` →
   `/data/deps-sglk`、显式 `CUDA_HOME=/usr/local/cuda-12.8`（nvcc V12.8.61；
   未触碰 compat 目录）、venv 预装 ninja/cmake/scikit-build-core、wheel 用
   `uv pip install` 落装（uv venv 无 pip 模块，原脚本 `python -m pip` 失败）。
   `-k0` 自愈循环 / MAX_JOBS=6 / TMPDIR=/data 原样保留；本次一轮即全绿，
   未触发 cgroup OOM（容器内存上限同为 96 GiB）。
6. **Rust**：rustup stable 1.100.0（rsproxy.cn 镜像），满足 editable 安装
   setuptools-rust 对 rustc ≥ 1.92 的要求（ENVIRONMENT.md 2026-08-28 坑位）。
7. **未动 `/opt/conda`**（ComfyUI 生产环境）；未向 LD_LIBRARY_PATH 添加
   `/usr/local/cuda-12.8/compat`。

## 环境异常（如实记录，未做掩盖性重跑）

1. **外来显存占用**：基准全程有 3,568 MiB 显存被宿主侧不可见 PID（648030）
   占用。mem-fraction 0.40 下 device pool 由 11-05 的 1,555,940 tokens 降为
   1,434,030 tokens（可用显存 24.32 GB），仍在充足区间；workload 依
   `/server_info` 实测容量推导驱逐流量，逻辑不受影响。
2. **并发租户活动**：运行窗口内同机有其他会话向 `/root/models` 下载
   Qwen2.5-7B-Instruct-AWQ（5.8 GB）与 gguf（1 GB）、经 /tmp pip 安装
   torch 2.14.1（py3.11）。这解释了根分区压力（uv 缓存已迁 /data）与
   绝对 TTFT 整体偏慢（R 与 P 同步变慢约 2.2x，比值机制不受影响）的
   主要候选归因；无法在本容器内验证宿主侧负载。
3. deep_gemm / 官方 sgl-kernel(+cu129) wheel 的 cu13 依赖问题见偏离项 3/4，
   均不触达本基准路径。

## 产物

- `results-20261120T083927Z.json`：逐 trial TTFT、cached_tokens_details、
  Prometheus 增量、环境/参数快照（sha256
  `b86d78c9a8590a9e7e5010cf59d25bdb0cc5f902307a6cfdac523f5da9ffbdd2`；
  validate_results.py 通过）
- `server-startup.log`：服务启动全量日志（server_args 快照、KV 池分配、
  CUDA graph 捕获；含 deep_gemm 惰性加载 ignore 记录）
- `env-versions.txt`：服务端版本清单（torch/torchaudio/torchvision/
  sgl_kernel/sglang/transformers/flashinfer）
- `gpu-stability-gate.log`：显存稳定性门禁日志摘录（ALL PASS + D2D 937 GB/s）
- `sglk-build-summary.log`：sgl-kernel 构建关键行摘录

服务端命令（`bench/repro-31600/` 下）：

```bash
MODEL_PATH=/root/models/Qwen2.5-0.5B-Instruct MEM_FRACTION=0.40 HICACHE_RATIO=2.0 \
HICACHE_IO=kernel HICACHE_LAYOUT=page_first \
PATH=/root/.venvs/sglang-server/bin:$PATH bash run_server.sh
# 记录于 evidence/2026-11-20-4090-migration-l2baseline/server-startup.log

.venv/bin/python workload.py \
  --sglang-commit 6afee75f918f3d59bdae7d619ba26b0ca22b1354 \
  --output-dir evidence/2026-11-20-4090-migration-l2baseline/
# 先 --dry-run 核对计划（8 轮、配对顺序固定）后正式运行

make audit RESULT=bench/repro-31600/evidence/2026-11-20-4090-migration-l2baseline/results-20261120T083927Z.json
```

构建配方入口：服务器 `/root/finish-sglk-4090.sh`（仓库 `finish-sglk.sh` 的
路径适配版）+ `/data/deps-sglk/`（预取依赖）。离线测试：`make test`
29 项全绿（71a0fe6 的测试集较 ENVIRONMENT.md 记录的 14 项有扩充）。
