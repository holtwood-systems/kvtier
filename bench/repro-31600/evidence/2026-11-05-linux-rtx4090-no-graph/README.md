# 2026-11-05 HiCache L2 矩阵 · disable-cuda-graph（RTX 4090）

环境与 `../2026-11-05-linux-rtx4090-l2-baseline/README.md` 一致；
唯一变量：`DISABLE_CUDA_GRAPH=1`。该配置与 WSL2 基线包
（`2026-10-26-wsl2-l2-baseline`，当时因显存被迫关 graph）对齐。

## 结果（validate 通过，paired_ttft_ratio_median=0.416462）

| 指标 | no-graph | graph（基线） |
|---|---|---|
| R/P TTFT 中位比 | 0.416 | 0.375 |
| W 写入 TTFT 中位 | 80.3 ms | 65.6 ms |
| host 命中 | 4223/4224 ×8 | 4223/4224 ×8 |

## 结论

- 关闭 CUDA graph 使回载比劣化约 11%（0.416 vs 0.375），写入慢 22%，
  但 L2 回载仍明确优于冷 prefill；机制结论不受 graph 配置影响。
- 与 WSL2 包（0.215，3060 Laptop、direct IO + 补丁）不构成同构对照：
  平台、GPU、IO 后端均不同，仅供形状参考。

## 产物

`results-20261106T003107Z.json`、`server-startup.log`、`workload.log`。
