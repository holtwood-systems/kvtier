# 2026-11-05 HiCache L2 矩阵 · io-backend=direct（RTX 4090）

环境、模型、工具链与 `../2026-11-05-linux-rtx4090-l2-baseline/README.md`
完全一致；唯一变量：`HICACHE_IO=direct`（layout 显式 `page_first_direct`，
与 server_args 自动解析结果一致）。

## 结果（validate 通过，paired_ttft_ratio_median=0.988366）

| 指标 | direct | kernel（基线） |
|---|---|---|
| R/P TTFT 中位比 | **0.988** | 0.375 |
| W 写入 TTFT 中位 | 1631.9 ms | 65.6 ms |
| host 命中 | 4223/4224 ×8 | 4223/4224 ×8 |

## 结论

- direct 后端在原生 Linux 上同样无需补丁即可工作（功能层结论）。
- 但其 CPU↔GPU 传输慢到使 L2 回载几乎无收益（R≈P，比值 0.988），
  且 write_through 写入路径本身慢约 25 倍。kernel 后端相对 direct
  的收益在本负载下被反向量化为 ~2.6× 回载优势。
- 该对比只在本模型/配置/单卡下成立，不外推。

## 产物

`results-20261106T000502Z.json`、`server-startup.log`、`workload.log`。
