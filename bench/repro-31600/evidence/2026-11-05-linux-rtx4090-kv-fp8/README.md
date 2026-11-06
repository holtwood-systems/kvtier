# 2026-11-05 HiCache L2 矩阵 · kv-cache-dtype=fp8_e4m3（RTX 4090）

环境与 `../2026-11-05-linux-rtx4090-l2-baseline/README.md` 一致；
唯一变量：`KV_CACHE_DTYPE=fp8_e4m3`。

## 结果（validate 通过，paired_ttft_ratio_median=0.391591）

| 指标 | fp8_e4m3 | auto/bf16（基线） |
|---|---|---|
| device 容量 | **3,111,880 tokens** | 1,555,940 |
| E 驱逐需求 | 3,423,232 tokens / 3343 req | 1,712,128 / 1672 |
| R/P TTFT 中位比 | 0.392 | 0.375 |
| W 写入 TTFT 中位 | 67.9 ms | 65.6 ms |
| host 命中 | 4223/4224 ×8 | 4223/4224 ×8 |

## 结论

- fp8 KV 使 device 与 host pool 容量翻倍；HiCache L2 链路（write_through
  备份、kernel IO 回载、命中计费）在 fp8 下功能无损，回载 TTFT 仍显著
  优于冷 prefill（0.392 vs 1.0），代价仅为 ~4% 的回载比劣化。
- kernel IO 对 fp8 数据的搬运正确性由此得到单配置验证；未覆盖其他
  dtype（e5m2/mxfp4）与更大模型。

## 产物

`results-20261106T001840Z.json`、`server-startup.log`、`workload.log`。
