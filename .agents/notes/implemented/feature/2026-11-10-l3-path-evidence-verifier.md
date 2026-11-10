# Agent Note: L3 回载路径证据验证器（DEV-20 设计 + 离线实现）

Status: implemented

## Problem

2026-11-05 的 L3 file 负结果（`evidence/2026-11-05-linux-rtx4090-l3-file/`）
只证明「该负载下前缀未在索引幸存」，无法区分 未备份到 L3 / 索引淘汰 /
存储读失败 / 传输异常 四类原因。审计现有工具链发现三个结构性缺口：

1. `workload.py` 的 `_METRIC_NAMES`/`_COUNTER_NAMES` 不含 L3 写出 counter
   `sglang:backuped_tokens_total`（`metrics_collector.py:1877`，storage 启用
   且 `enable_storage_metrics` 时导出）——11-05 包的 metrics 被 filter 后
   根本看不到 L3 是否写过盘。
2. counter 快照只有 全程前/后 两帧；「W 后是否备份」「E 期间是否被覆盖」
   无阶段级证据；`.bin` 落盘文件（`.tmp`→`os.replace` 原子写）这一最强
   可观测面完全未采集。
3. `validate_results.py` 的 `_EXPECTED_SERVER_CONFIG` 硬编码
   `hicache_storage_backend=None`、R 命中只查 `host`——**一个真正的
   L3 complete 包必然被审计器拒绝**（现行负包只因 status=failed 走
   短路校验才"合法"），与 DEV-20「接受结构合法负结果、拒绝缺路径
   证据的 complete」要求不符。

## Pinned 机制核验（commit `6afee75f9`，只读）

- backup 触发链：write_through 下 `_finish_write_through_ack` →
  `write_backup_storage(node_id)` → `cache_controller.write_storage`（异步）
  → `HiCacheFile.set` → `{key}{config_suffix}.bin`（tmp+rename）。
  **W 阶段每个前缀在 D→H ack 后即应发起 H→L3 备份**；备份完成
  的可证信号 = `.bin` 文件出现 + `backuped_tokens_total` 正增量。
- 可观测面：①storage dir `.bin` 清单（文件名=哈希 key+config_suffix，
  `.tmp.{pid}.{tid}.{uuid}` 为半成品）；②`sglang:backuped_tokens_total`；
  ③`sglang:hicache_backup_dropped_tokens_total`（buffer 模式丢包）；
  ④`cached_tokens_details.storage` 逐请求；⑤服务日志
  `HiCache prefetch success` / `Failed to fetch ...`。
- 关键约束：`prefetch_threshold` 默认 **256 token**——命中残量低于阈值时
  根本不查 L3；`LRUFileEvictor` 按盘容量驱逐 `.bin`；`StorageExistenceCache`
  做写侧去重（重复 key 跳写）。`metadata_cache`（TTL 5s）默认关。

## Decision

### workload.py

1. `_METRIC_NAMES`/`_COUNTER_NAMES` 增补 `sglang:backuped_tokens_total` 与
   `sglang:hicache_backup_dropped_tokens_total`。
2. 阶段级 counter 快照：`post_write`（backup_wait 后）、`post_evict`、
   `post_measure`（等价现行 after），存入 `metrics.stage_counters`；
   现行 before/after 帧保留，旧包仍可审计。
3. 新参数 `--storage-dir`（可选）：仅当服务端 localhost 时，在各阶段边界
   快照 `{name,size,mtime}` 的 `.bin`/`.tmp.*` 清单 → `record["storage_snapshots"]`；
   远端服务未传参时跳过并在 record 标注 `storage_snapshots=null`。
4. 失败分类器 `classify_failure(record, exc)`，在 except 分支写入
   `record["failure_classification"]`：
   - `config`：配置校验即失败；
   - `not_backed_up`：无 backuped 增量且无 `.bin` 增长；
   - `index_evicted`：有 L3 写入证据但全部 R 的 host+storage=0；
   - `storage_read_failed`：R 请求错误含 fetch/读取类信号；
   - `transfer_anomaly`：load_back counter 有增量但命中明细为 0；
   - `unknown`：以上都不匹配（如实保留，不猜）。
   分类只基于客户端可采集证据（counter delta + dir 快照 + 请求明细），
   不依赖读服务端日志文件。

### validate_results.py

1. `hicache_storage_backend` 移出静态表：与 `args.expected_storage_backend`
   动态比对（None→要求 None；"file"→要求相等）。
2. R 命中校验改 `host + storage >= floor(prompt_len×min_host_hit_ratio)`，
   与 workload 语义一致。
3. `expected_storage_backend` 非空且 status=complete 时，必须存在
   `metrics.stage_counters`，且 (a) `backuped_tokens_total` 阶段增量 >0 或
   (b) `storage_snapshots` 含 `.bin` 增长——缺一即拒（"complete 缺路径证据"）。
4. `failed` 包继续合法；若带 `failure_classification`，须属于已知枚举。

### 测试与文档

- test_validate_results.py 补：L3 complete（有证据→过）、
  L3 complete 缺证据（拒）、L3 failed+classification（过）、
  host+storage 合并命中语义；test_workload.py 补分类器单测。
- README「证据约束」增补 L3 协议段；ENVIRONMENT.md 不动
  （commit 不变仍 `6afee75f9`）。

## 历史验收口径

- `make test` 全过；旧 L2 证据包仍被审计器原样接受（兼容性回归）。
- 合成 L3 complete 包：有路径证据→接受；缺 stage_counters/备份证据→拒。
- 合成 L3 failed 包（带 failure_classification）→结构合法。
- 不改动 W/E/R/P 协议形状、盐隔离与配对随机化；默认行为 L2-only 不变。

## Alternatives considered

- **读服务端日志文件做分类**：最贴近 prefetch 事件，但要求 client 与
  server 同机且日志路径透传，把 benchmark 与部署形态耦合；保留为运行侧
  补充证据（README 记录），不进 verifier 必需项。
- **按 salt 推算 storage key 精确对账**：key 是上游哈希（加盐前缀链），
  客户端无法稳定复算；退而求其次用"文件存在+增长量"做证据，放弃精确映射。
- **新增独立 L3 脚本而非扩展 workload.py**：协议 90% 重合会复制出
  第二套盐隔离/配对逻辑，违背单源真相；扩展且默认行为不变更优。

## Risks

- `backuped_tokens_total` 依赖 `enable_storage_metrics`（随 enable_metrics
  解析）；若某版本只在首增后导出，post_write 快照可能为 None——分类器
  对 None 按"无 counter 证据"走 dir 快照路径，不误判。
- `.bin` 文件名映射不回 salt：只能做聚合层面证据（增长/未增长），
  无法指认某个 W 前缀是否落盘——已是诚实边界，写进 README。
- 远端服务无 `--storage-dir` 时缺最强证据；此时 L3 complete 仅凭
  counter 增量也可过门禁，降级为次强证据档。

## 后续修复与实跑结果（2026-11-11）

- **修复**：`record["args"]` 直接灌 `vars(args)`，`--storage-dir` 的
  Path 值导致失败包 JSON 序列化崩溃 → `jsonable_args()` 统一转 str，
  补 `test_jsonable_args_serializes_paths`。
- **实跑（WSL2/RTX 5070 Ti）**：`evidence/2026-11-11-wsl2-rtx5070ti-l3-file/`
  status=failed / `index_evicted`，复现 11-05 4090 负结果——L3 索引随
  host 条目同死在新硬件确认。新证据：`.bin` 为内容寻址，跨 run/跨
  namespace dedup；`.bin` 增长不能区分 dedup-hit，`backuped_tokens_total`
  含 dedup 尝试——解释写进了证据包 README 与验证器边界。

## Verification

- `make test`：28/28 通过（新增 6 项 validator L3 用例 + 7 项 workload
  快照/分类器用例，并为既有 Namespace 测试补 `storage_dir` 属性）。
- 回归：`validate_results.py` 复跑 `2026-10-26-wsl2-l2-baseline`（exit 0）
  与 `2026-11-05-linux-rtx4090-l2-baseline`（exit 0），L2 语义不变。
- `2026-11-05-linux-rtx4090-l3-file` 负结果包：status=failed 仍被审计器
  接受（exit 1 为语义结果非结构错误），如实负证据保留。
- `workload.py --dry-run --storage-dir … --expected-storage-backend file`
  打印新协议说明，不触服务。

## Consequences

- GPU 运行时的 L3 闭环路径已就绪：W 后 `.bin`/`backuped_tokens_total`
  可证"真正经过 L3"，失败包自动带分类归因。
- 下一轮 L3 运行建议：`--expected-storage-backend file` +
  `--storage-dir <SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR>`（本机服务），
  驱逐量按 DEV-20 建议缩小以分离"索引淘汰"变量。
- 上游已知约束记录：prefetch_threshold 默认 256 token（命中残量低于阈值
  不查 L3）；`.bin` 文件名无法映射回 salt，证据为聚合层面。
