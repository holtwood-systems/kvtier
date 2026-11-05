# Agent Note: 原生 Linux RTX 4090 HiCache L2 基线与 cu12 工具链移植

Status: implemented

## Problem

在原生 Linux + RTX 4090（sm_89，驱动 550/CUDA 12.4）跑 pinned SGLang
`6afee75f9` 的 HiCache L2 微基准时，官方工具链三层不可用：
pyproject 锁 `torch==2.13.0` 但只有 cu130 构建（需驱动 ≥580）；
sgl-kernel 全部官方 wheel（PyPI 与 docs.sglang.ai）均为 cu13 且只带
sm90/sm100 变体，`libnvrtc.so.13` 在本机不存在；
cuda-compat-13-0 前向兼容包被消费卡硬性拒绝（Error 804）。

## Decision

证据包落在 `bench/repro-31600/evidence/2026-11-05-linux-rtx4090-l2-baseline/`，
审计通过（8/8 trials，host 4223/4224，paired R/P 0.374922），
**kernel IO 后端在无 PR #43375 补丁下全程正常**——WSL2 的
cudaHostRegister 地址不一致是平台特有问题。

工具链移植决策（均记录在 evidence README 偏离项）：
1. venv 内 torch 降为 2.11.0+cu128（cu128 索引可得的最高版本；
   2.13 无 cu128），import/http_server/服务全程未观察到 API 缺口；
2. sgl-kernel 0.4.6.post1 从 aot 目录源码构建，本地构建期裁剪
   gencode 至 sm_80/89 并关闭 FA3/FlashMLA（对本模型在 sm_89 均不可达，
   懒加载不触发），补丁留档 deps-sglk/arch-trim-sm89.patch；
3. FetchContent 依赖用 curl --http1.1 预取 + `FETCHCONTENT_SOURCE_DIR_REPO-*`
   注入，绕开 GitHub HTTP/2 断流；
4. host pool 收缩到 mem-fraction 0.40 / hicache-ratio 2.0（cgroup
   96 GiB 上限；sglang 的 host_memory_budget 读宿主 psutil 会高估）。

## Alternatives considered

- 升级驱动 ≥580 走官方 torch 2.13+cu130：非本环境权限，否决。
- cu124/cu126 sgl-kernel wheel：docs.sglang.ai 索引不存在；PyPI
  wheel 也均为 cu13，否决。
- sgl_kernel 全量 gencode 构建：506 目标在 96GiB cgroup 下 OOM 频发
  且 sm90/100/120 内核对 sm_89 无用，裁剪是本机证据目的下的等价选择。

## Consequences

产出 wheel 只含 sm_80/89 fatbin，不可移植到其他 GPU；flash_ops
（FA3）与 flashmla_ops 模块缺失，引用它们的代码路径（fa3/MLA
后端）在本 wheel 下不可用——本基准的 Qwen2.5 稠密模型不经过这些
路径。若后续需要官方语义 wheel，只能等驱动升级。

## Risks

torch 2.11 与 pinned 2.13 的 API 差可能在未覆盖的代码路径上暴露；
本基线不能证明 sglang 该 commit 在 2.11 下普遍可用，只证明本次
服务/负载路径可用。

## Verification

`import sgl_kernel` + rmsnorm kernel CUDA 调用实测通过；
validate_results.py 对结果包审计通过；两次构建失败模式
（cgroup OOM 与 /tmp 写满）均定位并绕过（-j6/-k0 循环、TMPDIR=/data）。
