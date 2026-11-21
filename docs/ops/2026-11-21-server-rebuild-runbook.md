# 4090 服务器重建 Runbook（2026-11-21 固化）

> 场景：租用容器可被重置/下线（2026-11-06 后发生过一次；2026-11-21 再次失联）。
> 本文档浓缩两次环境重建的全部实操经验，目标：重置后 **1 小时内**恢复实验能力。
> 来源：Agent-1（6afee75f 基线环境）与 Agent-7（main 环境）的实建报告。

## 0. 服务器档案

| 项 | 值 |
|---|---|
| 访问 | `sshpass -f <PASSFILE> ssh -p <PORT> root@<SERVER_HOST>`（凭据不入库；示例占位符见 scripts/recover_4090_server.sh 用法） |
| 形态 | Ubuntu 22.04.05 容器（无 dmesg；`/` 仅 40G；`/data` 500G NVMe；宿主内存 1TB 共享） |
| GPU | RTX 4090 48GB 改装（49140 MiB，sm_89），驱动 550.107.02（CUDA 12.4 级） |
| 代理 | `export https_proxy=http://127.0.0.1:8894`（mihomo，HK 出口，tmux 会话 `mihomo`，死则 `tmux new -d -s mihomo '/usr/local/bin/mihomo -d /etc/mihomo'`） |
| 镜像 | PyPI 走 `https://mirrors.aliyun.com/pypi/simple/`；HF 走 `HF_ENDPOINT=https://hf-mirror.com`；GitHub 慢则 `https://ghfast.top/` 前缀 |
| 已知常态 | 宿主侧 ~3.5GB 不可见显存占用（"幽灵"）+ 同机并发租户——**每次测量窗口前核验 GPU 独占** |

## 1. 重置后必做（严格按序）

### Step 0：CUDA 804 修复（最高优先，不做则一切 CUDA 死）
```bash
mv /etc/ld.so.conf.d/00-compat-*.conf /etc/ld.so.conf.d/00-compat-*.conf.bak-disabled
ldconfig
# 验证：任意 torch 的 cuda.is_available() == True
```
根因：镜像把 `/usr/local/cuda-12.8/compat`（570 前向兼容库）置于 ldconfig 搜索序
首位，屏蔽与内核驱动匹配的 550 库；GeForce 不支持 forward compat → Error 804。
**任何脚本禁止把 compat 目录加入 LD_LIBRARY_PATH。**

### Step 1：GPU 快速自检（可选，5 分钟）
本地仓库 `bench/repro-31600/evidence/2026-11-20-4090-hw-stability/vram_stress.py`
可复跑（改 DUR_S=300 短版）；上次全量结果 ALL PASS / D2D 937 GB/s。

### Step 2：磁盘布局
所有 venv / 缓存 / 模型 / 构建目录放 `/data`（根分区 40G 会被撑爆）。
uv 缓存统一 `UV_CACHE_DIR=/data/uv-cache`。

### Step 3：基线环境（6afee75f）
- venv：`uv venv ~/.venvs/sglang-server --python 3.12`（CPython 3.12.16）
- sglang：浅取 `6afee75f918f3d59bdae7d619ba26b0ca22b1354` 至
  `/root/src/kvtier/third_party/sglang`，editable 安装
- torch：**2.11.0+cu128 全家桶**（torch/torchaudio/torchvision 都要 `+cu128` 变体，
  `download.pytorch.org/whl/cu128` 经代理；PyPI 默认 torchaudio 缺 libcudart 会炸 import）
- sgl-kernel 0.4.6.post1 源码构建：`CUDA_HOME=/usr/local/cuda-12.8`、
  `TORCH_CUDA_ARCH_LIST="8.9"`、arch-trim-sm89 补丁、FetchContent 依赖 gh-proxy 预取、
  产物 wheel 用 uv 装（uv venv 无 pip 模块）；参考 `/root/finish-sglk-4090.sh`
  （重置后丢失则按 kvtier 仓 `finish-sglk.sh` + `deps-sglk/` 重建，D 目录用 /data/deps-sglk）

### Step 4：main 环境（e183c415b）
- 源码：`git init /root/src/sglang-main && git fetch --depth 1 origin e183c415b` …
  （`e183c415bdfc5995f6288b1651e1c320c6a74015`，与 bench-design 锚点一致）
- venv：物理在 `/data/kvtier-main-venv`，`/root/envs/kvtier-main` 为符号链接
- **torch 用 cu12x 变体**（2.14.x 的 cu128/cu126 构建；驱动 550 跑不了 cu130！
  ——2026-11-20 的教训：main 默认解析拉 cu130 → cuda.is_available()=False）
- **sgl-kernel 已更名 `sglang-kernel==0.4.11`**，预编译 wheel 是 cu13 系——**必须源码
  构建**（CUDA_HOME=/usr/local/cuda-12.8，CUTLASS 重型单元，1-3h）
- Rust 扩展：sglang main 含 PyO3 扩展（radix Rust 后端），需 rustup 钉定的 Rust
  工具链（`source /root/.cargo/env` 或补装），勿用 `SGLANG_BUILD_RUST_EXTS=none` 降级
- flashinfer 等 cu13 系配套换 cu12 兼容变体；`sgl-deep-ep==0.1.3` 在 aliyun 镜像
  页面有解析缺陷 → wheel 下载到 `/data/wheels/kvtier-main` 用 `--find-links` 绕行
- 单卡实验用不到的 cu13-only 分布式依赖（nvshmem4py-cu13 等）可 --no-deps 绕过，逐项记录

### Step 5：kvtier 检出 + harness
```bash
git clone <kvtier-origin> /root/src/kvtier && cd /root/src/kvtier   # 71a0fe6+
# 本地生成补丁（在本地仓库）：git diff -- bench/repro-31600 > /tmp/harness-main.patch
scp -P 40028 /tmp/harness-main.patch root@<server>:/root/src/kvtier/
git apply /root/src/kvtier/harness-main.patch
make test   # 预期 44/44（40 + 4 expect-mode 诊断）
```
bench 客户端 venv：uv + `requests==2.31.0`（ENVIRONMENT.md 惯例）。

### Step 6：模型
- `Qwen/Qwen2.5-0.5B-Instruct` BF16 → `/root/models/`，sha256 校验 `fdf756fa…88fb7fe`
- `Qwen/Qwen3-4B` → `/root/models/Qwen3-4B`（#49221 判定臂用；上次下载未确认完成）

### Step 7：实验恢复点
烟测（`--hicache-ratio 0.05` 确认 `unfulfilled{reason=host_capacity}` 可触发）→
实验 0 双臂（`docs/bench-design/2026-11-20-repro-main-dualarm.md`）→
#49221（`...-49221.md`）。测量窗口前核验 GPU 独占。

## 2. 已离机资产（安全，无需重建）

- 迁移基线证据包（R/P 0.4817，audit 通过）：本地
  `bench/repro-31600/evidence/2026-11-20-4090-migration-l2baseline/`
- 硬件稳定性证据：本地 `.../2026-11-20-4090-hw-stability/`
- harness 全部改动（44 测试绿）：本地工作区 `git diff -- bench/repro-31600`
- 全部设计/决策/审计文档：本地 `docs/{decisions,bench-design,upstream,interview}/`
- 两份环境配方原始报告：各代理最终报告（要点已并入本文档）

## 3. 需重建清单（按 Step 顺序）

基线 venv（~40min）｜sgl-kernel 0.4.6 构建（~40min）｜main 克隆+venv（~15min）｜
torch cu12x 下载（~20min）｜sglang-kernel 0.4.11 源码构建（1-3h，最长项）｜
模型下载（0.5B 1GB + 4B ~8GB）
