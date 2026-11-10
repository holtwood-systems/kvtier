# 环境记录（P0：版本与环境可追溯性）

> 更新时间：2026-11-11。本文件记录构建与运行本基准所用的所有可追溯信息。
> 正式 GPU 运行时，`workload.py --sglang-commit` 必须与下文"服务端"一节的 commit 一致。

## 2026-11-11 实际运行（WSL2 / RTX 5070 Ti，L3 file 后端）

- OS：WSL2，Linux 6.18.33.2-microsoft-standard-WSL2；
- GPU：RTX 5070 Ti 16384 MiB（sm_120），驱动 616.56（CUDA UMD 13.3）；RAM 16GB；
- 服务端：SGLang `6afee75f918f3d59bdae7d619ba26b0ca22b1354`
  （`0.0.0.dev1+g6afee75f9`，editable，`third_party/sglang` 浅克隆）+
  **复打** `evidence/2026-10-26-wsl2-l2-baseline/sglang-43375-local.patch`
  （pinned commit 不含 WSL2 host-alloc 修复；WSL2 必须，否则 kernel IO
  cudaHostRegister 路径 illegal access）；
- 构建坑位（本机新增）：
  - rustc 1.90 < rustpython-ruff 要求 → `rustup update stable` → 1.99.1；
  - HiCache native hash 扩展 JIT 编译需 `<openssl/sha.h>`；系统无
    libssl-dev，用 miniconda openssl 3.6 经 `CPLUS_INCLUDE_PATH` /
    `LIBRARY_PATH` 编译进 torch 扩展缓存（server 进程须带同样 env）；
  - host 预算：`psutil.available − 10GiB 硬保留` − 进程占用 ≈ 0.83GB
    → `HICACHE_RATIO` 压到 0.12（host pool 43,024 tokens / 0.53GB）；
- 模型：`/home/shane/models/Qwen2.5-0.5B-Instruct`（HF BF16，snapshot 下载）；
- 配置：`MEM_FRACTION=0.35`（device pool 358,532 tokens）、
  `HICACHE_RATIO=0.12`、`HICACHE_IO=kernel`、`HICACHE_LAYOUT=page_first`、
  `HICACHE_STORAGE_BACKEND=file` +
  `SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR=/tmp/sglang-l3-storage`；
- 结果：`evidence/2026-11-11-wsl2-rtx5070ti-l3-file/`，
  status=failed / `index_evicted`（负结果，审计判定结构合法）；
- workload.py 修复：`record["args"]` 对 Path 值未序列化导致失败包
  写入崩溃 → `jsonable_args()`（DEV-20 交付的后续修复）。

## 2026-10-26 实际运行（WSL2 / RTX 3060 Laptop）

- OS：WSL2，Linux 6.18.54.1-microsoft-standard-WSL2；
- GPU：RTX 3060 Laptop 6144 MiB，驱动 610.88（CUDA UMD 13.3），sm_86；
- 服务端：SGLang `6afee75f918f3d59bdae7d619ba26b0ca22b1354`
  （`0.0.0.dev2+g6afee75f9`）+ 本地补丁
  `evidence/2026-10-26-wsl2-l2-baseline/sglang-43375-local.patch`
  （PR #43375 的 WSL2 host 分配部分，sha256
  `d9e99574df7ae920619a292059f2ce6cb486b12f8bb7342b92c1e93e7530df68`）；
- 模型：`$HOME/models/Qwen2.5-0.5B-Instruct`，BF16，
  `model.safetensors` 988,097,824 B，
  sha256 `fdf756fa7fcbe7404d5c60e26bff1a0c8b8aa1f72ced49e7dd0210fe288fb7fe`；
- 配置：`MEM_FRACTION=0.28`（设备池 33,744 tokens）、`HICACHE_RATIO=2.8`
  （host pool 94,484 tokens / 1.16 GB）、`HICACHE_IO=kernel`、
  `HICACHE_LAYOUT=page_first`、`DISABLE_CUDA_GRAPH=1`、N=6；
- host 预算：SGLang `host_memory_budget_bytes()` = `psutil.available - 10 GiB`
  ≈ 1.18 GB，因此 host pool 不能按默认 ratio 4 放大；
- 结果：`evidence/2026-10-26-wsl2-l2-baseline/results.json`，
  R/P TTFT 中位比 0.215，审计通过；
- WSL2 根因：`cudaHostRegister` 后 host 地址与 `cudaHostGetDevicePointer`
  返回值不一致，`kernel` IO 后端直接使用 host 地址会触发 illegal access；
  修复后改用 torch pinned memory（地址恒等）。

## 2026-08-28 构建基线（历史记录）

### 硬件

- GPU：NVIDIA GeForce RTX 3060 Laptop，6144 MiB
- 驱动：610.57.01（KMD 610.88），CUDA UMD 13.3

## 操作系统与 Python

- OS：Linux（Debian/Ubuntu 系）
- Python：3.12.3（`/usr/bin/python3`）
- 环境管理工具：uv 0.9.13（系统缺 `python3.12-venv`，改用 uv 创建 venv，无需 sudo）

## 上游源码（third_party，只读浅克隆）

| 仓库 | commit | 说明 |
|---|---|---|
| sglang | `6afee75f918f3d59bdae7d619ba26b0ca22b1354` | 基准服务端构建基线；docs 01/02 的核验基线为 `67853c580482dbc9b69aad5022a0cdd3a1606570`，两者不同，正式运行以实际服务构建来源为准 |
| nano-vllm | `bb823b3e06983d71485a8e1f23715ebd87d98ef8` | 仅用于架构对照阅读 |

## 基准客户端（已就绪）

- 位置：`bench/repro-31600/.venv`（uv venv，CPython 3.12.3）
- 依赖：`requirements.txt` → `requests==2.31.0`
- 验证：`make test`（根目录）自动优先使用该 venv；PYTHONDONTWRITEBYTECODE=1 下
  14 项离线测试通过，且不写入任何 `__pycache__`/`.pyc`。

## 服务端（P1 已构建完成）

- 方式：`uv venv ~/.venvs/sglang-server`，editable 安装
  `uv pip install -e third_party/sglang/python`，与上表 commit 严格对应；
- ⚠️ 首次尝试失败于 Rust 构建（rustc 1.90.0 < rustpython-ruff 0.15.8 要求的 1.92）；
  **2026-08-28 已修复**：`rustup update stable` → rustc 1.98.0，重装成功；
- 验证：`import sglang` 报告版本 `0.0.0.dev2+g6afee75f9`（setuptools-scm 从浅克隆
  git 元数据生成，`+g6afee75f9` 即上表 commit 短哈希，可追溯性成立）；
  torch 2.13.0+cu130，CUDA 可用，识别 RTX 3060 Laptop；
- venv 体积 8.3 GB；uv 缓存 17 GB（含 torch/cu13 全家桶，重装无需再下载）；
- **P0-2 验收达成**：`--sglang-commit 6afee75f918f3d59bdae7d619ba26b0ca22b1354`
  与实际服务构建来源一致。

## 模型（P1-1 已选定）

- **2026-08-28 原定**：`$HOME/models/Qwen2.5-1.5B-Instruct`（HF 原版 BF16，
  2.9 GB，`model.safetensors`；2026-08-28 由 `hf download` 获取）；
- **2026-10-26 实际**：改用 `$HOME/models/Qwen2.5-0.5B-Instruct` 完成运行。
  原因：Windows 桌面侧占用约 1.97 GB 显存，1.5B 权重加载后 SGLang 报告
  KV cache 最低预算需 `mem-fraction-static > 0.827`，物理空闲显存不足；
  0.5B 为同家族、同 tokenizer 接口，可在 6 GB 卡上稳定运行。其绝对 TTFT
  不能外推到 1.5B/7B；
- 权重 dtype：**bfloat16**（config `torch_dtype`）；上下文长度：**32768**
  （`max_position_embeddings`）；架构 Qwen2ForCausalLM，28 层 × 2 KV heads；
- 每 token KV 体积 ≈ 28 层 × 2 KV heads × 128 head_dim × 2(K+V) × 2B = **28 KB/token**，
  4096-token 前缀 ≈ 117 MB；
- 显存约束（2026-08-28 实测）：dGPU 有 ~3.4 GB 无法归因的图形占用（Wayland/浏览器，
  进程表不可见），启动前由用户释放；`MEM_FRACTION` 与 `HICACHE_RATIO` 按启动时实测
  空闲显存与系统内存（仅 9 GB，host pool 需限额）现场确定并记录于结果包。

## 验证入口

```bash
make test          # 离线 14 项测试（隔离环境、零字节码）
cd bench/repro-31600 && python3 workload.py --dry-run
```
