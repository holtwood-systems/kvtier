# 2026-11-05 HiCache L3 file 后端探测（RTX 4090）—— 负结果

环境与 `../2026-11-05-linux-rtx4090-l2-baseline/README.md` 一致；
变量：`HICACHE_STORAGE_BACKEND=file` +
`SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR=/data/hicache-l3`，
`HICACHE_RATIO=0.4`（host pool 622k tokens，故意小于 E 阶段 1.71M
tokens 驱逐量，以观察 L3 行为）。workload 用新加的
`--expected-storage-backend file`（见仓内 feature 笔记）+ `--min-host-hit-ratio 0.5`。

## 结果（validate：结构合法，status=failed —— 如实负证据）

- file 后端创建成功，storage 目录收到 `.bin` 页文件（~51MB）。
- W/E 期间持续出现 `HiCache prefetch success`（matched 256–512 token
  级部分命中、loaded>0），**L3 索引查询与回载链路机械可用**。
- 但 M 阶段首个 R 请求 `host=0, storage=0`：8 个 W 前缀在 host pool
  被 1.71M 驱逐写冲垮后**没有在 L3 索引中幸存**，三层全 miss，
  基准按契约失败（status=failed，负结果原样保留）。
- W TTFT 中位 1400.8ms：file 后端使写入路径显著变慢（对比基线 65.6ms）。

## 解读边界

- 不能据此断言"file 后端不保留数据"：host→L3 是否备份、备份触发
  时机、file 索引容量/驱逐策略均未被本负载分离测量；51MB 落盘量
  相对 622k-token host pool 的 KV 体积明显偏小，提示备份路径并未
  覆盖全部 host 驱逐。
- 已证实的只有：该负载形状下 L3 file 配置无法支撑 R 阶段命中。
- 如需 L3 正向证据，后续应单独设计：小 evict 量、明确 backup 触发
  点、或 buffer_only host 模式（后者是另一套 host-memory-mode 契约）。

## 产物

`results-20261106T004152Z.json`（status=failed）、`server-startup.log`、
`workload.log`。另：同配置首轮失败包（缺 openssl 头致 native hash
扩展 JIT 失败）保留于 `../2026-11-05-linux-rtx4090-l3-file-failed-ssl/`。
