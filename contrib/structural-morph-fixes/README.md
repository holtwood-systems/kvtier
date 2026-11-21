# sglang 结构型修复草稿：F1 / F4 / F2-a（4× 变形三症状）

- 日期：2026-11-21
- 基线：sglang main `e183c415bdfc5995f6288b1651e1c320c6a74015`（克隆 `/tmp/sglang-main-audit`，
  验证后已 reset，`git status` 干净、HEAD 不变）
- 规格来源：`docs/bench-design/2026-11-21-structural-morph-fix-design.md` §五（F1/F2/F3/F4）。
  本次实现 **F1 + F4 + F2-a**；**F3（写队列背压）保持设计态未实现**（涉及语义安全，等 Go）。
- 产出：三个互相独立、可单独 apply 的 git format-patch 补丁（`git apply` 与 `git am` 均可吃，
  作者/日期为占位，投递前由所有者改定）。
- **未经所有者批准，本目录任何内容不得用于任何实验环境、不得发布、不得 git push。**
  补丁内含的测试仅供上游 CI；`verify/` 下的离线 harness 仅供本机复核。

## 补丁清单

| 文件 | 方案 | 症状 | 改动面（代码，不含测试） | 对应实验 |
|---|---|---|---|---|
| `0001-f1-batch-exists-scandir-snapshot.patch` | F1 batch_exists scandir 快照 | ①③ | hicache_storage.py +53 | EXP-A（判别 + 提速） |
| `0002-f4-deadline-cut-accounting.patch` | F4 零记账出口计数 | ①归因面 | unified_radix_cache.py +25、metrics_collector.py +14 | 所有 main 对照臂采集；EXP-A 校验 |
| `0003-f2a-deadline-phase-split.patch` | F2-a 预算相位拆分 | ③ | unified_radix_cache.py +60/−13 | EXP-B |

叠加顺序任意（三者改动区域两两不相交）；设计文档推荐的合入顺序是 F4 → F1 → F3 → F2-a。

---

## 0001 — F1：file 后端 `batch_exists` 的 scandir 快照化

**机制**。`HiCacheFile` 重载 `batch_exists`（原继承基类逐 key 串行 `exists`，
hicache_storage.py:549-560）：legacy 布局（`_unified_suffixes is None`）+ metadata cache 关闭 +
key 数 ≥ `_BATCH_EXISTS_SCANDIR_MIN_KEYS`（32）时，一次 `os.scandir` 建目录名集合
（只收集 `*.bin`，天然排除写路径的 `.<uuid>.tmp` 与 fast_file 的 `*.bin.tmp.*` 中间态），
按序数连续命中返回最长前缀。其余路径（unified 分片、metadata cache、小批量）原样回退基类。
evictor 启用时对快照命中的前缀逐 key 复核一次（防快照后即被驱逐的正向误报）。
目录不存在/不可读 → `OSError` → 返回 0（与逐 key `os.path.exists` 的失败语义一致）。
mooncake/hf3fs/nixl 等其它后端不继承 `HiCacheFile`，接口签名未变，不受影响。

**预期效果**。C2 臂（page_size=1）单次查询 = 4,223 次目录查找，在单线程 prefetch 管线 +
写风暴（~450 文件/s tmp+rename 同目录）下超过 2.03s deadline 预算。快照化后查询阶段
= 1 次 O(目录) 扫描（设计推算 ~10–30ms @ 12 万项目），预算几乎全额留给 IO 装载；
同时消除与写线程的逐 stat GIL 争用。本机空闲目录实测（4,096 页、3,000 命中）：
快照 4.6ms vs 逐 key 13.8ms，判定逐位一致——空闲态优势有限，目标场景是争用态。

**验证计划**：EXP-A（`C2+F1` vs `C2-main`）。判别门禁 G1-A：R storage 命中 trial ≥5/6 且
每 trial hit ≥4,223（main 为 2/6）——同时证伪/证实设计 §2.3 "W 文件在盘"推断；
G2-A：`unfulfilled{deadline}` 增量较 main 降 ≥80%，`host_capacity`/`dropped` 不增；
恒等式 hit = fulfilled + Σunfulfilled 逐臂闭合。F4 的 counter 应降为 0。

**风险与回滚**。风险：目录极大而批量小时 scandir 反而贵——已用 key 数阈值门控；
快照后新写入视为 miss（与单次 stat 的窗口语义等价或更保守）；`HiCacheFastFile`
继承 `HiCacheFile` 且共享 `.bin` 命名，会同样吃到快照路径（语义等价、有益；
见下"坑"）。回滚 = revert 单个 commit，无状态、无格式、无协议残留。

## 0002 — F4：deadline 截断零记账出口补 counter + WARNING

**机制**。`check_prefetch_progress` 的零记账出口（unified_radix_cache.py:3077-3082 锚点）：
当 `deadline_expired` 且该 op 的命中从未入账
（`request not in _storage_prefetch_hit_remaining_by_reqid`——入账唯一入口是
`_drain_and_alloc_storage_hit` 里的 `_record_storage_prefetch_hit`）时，调新增 helper
`_log_storage_prefetch_deadline_cut_lookup`：一条 WARNING（含 rid + 查询 span tokens）+
新事件计数器 `sglang:storage_prefetch_deadline_cut_lookups_total`（注册于
StorageMetricsCollector，紧挨 #37503 族；方法 `log_storage_prefetch_deadline_cut_lookup`）。
**不改任何控制流**；已入账的切断（trial03/04 形态，记 `unfulfilled{deadline}`）与
普通 miss（走 `poll_miss`）都不触发。事件维度、无 reason label——它必须不污染
hit = fulfilled + Σunfulfilled 的 token 恒等式，也不引入新 label 面
（设计 §F4 提醒过 label 失配前科；本 counter 无 label 维度，无预注册需求）。

**预期效果**。C2 臂 4 个零命中 trial 此前零痕迹（无 counter、无日志、无重试）；
打本补丁后，main 对照臂上该 counter 的读数应 == 零命中 trial 数（4），且
TTFT 分布（2.17–2.59s ≈ 预算 2.03s + prefill）与 WARNING 时间戳可直接对齐归因。

**验证计划**：不单独开臂——在 EXP-A/B/D 的所有 main 对照臂采集；单测断言
计数器 +1 且不产生 token 维度增量。`declined_*` 族的 Prometheus 导出缺口
由 `contrib/pr-36280-revival/` 补丁覆盖，本补丁不重复做（保持正交）。

**风险与回滚**。风险：TP>1 下每个 rank 各计一次（per-rank collector，与 #37503 族
同语义）、各发一条 WARNING——与既有 `rank_consensus` 函数内 WARNING 的多份日志行为一致；
WARNING 频率 = 零记账切断频率，本身就是异常信号，不预期刷屏。回滚 = revert 单 commit。

## 0003 — F2-a：deadline 预算相位拆分（装载相位起表 + 查询护栏）

**机制**。原实现把查询+分配+IO 全记在一个从"首次 blocked admission poll"起表的预算里
（base 1.0s + 0.25s/Ki-token；4,223 页 ≈ 2.03s）。本补丁：

- `_StoragePrefetchWait` 增 `query_timer`/`query_timeout_s` 两字段（带默认值——
  `_mark_prefetch_deadline_expired` 会以默认构造 setdefault，见"坑"）；
- `_start_storage_prefetch_wait`（poll 侧）只起 **query 护栏**：4× 完整预算
  （`_storage_prefetch_query_cap`），防查询挂死后 admission 永久阻塞；
- 新 `_start_storage_prefetch_load_wait` 在**命中分配发射点**起装载预算：
  cache 模式 `prefetch_buffer.put` 前；buffer 模式
  `allocate_and_queue_prefetch` 返回 True 后（False = parked 未发射，不起表）。
  预算按**实际装载页数**（alloc 截断后的 `len(operation.hash_value)`）计；
- `_prefetch_deadline_expired` 同时检查两相位计时器；票决/KV-landed 短路/refetch
  保留原预算等语义不变；`wait_complete`/`best_effort` 策略完全不受影响。

**与设计文本的偏离（2 处，均有意为之）**：
1. 设计说查询相位"交给重试梯兜底（poll_miss）"——未实现为 poll_miss：护栏到期仍走
   deadline 切断语义（terminate + cancel retries）。把切断改成重试要动票决与控制流，
   超出最小改动面；护栏只是防挂死护栏，F1 落地后查询 <50ms，不应触发。
2. buffer 模式启动点设计只写了 `prefetch_buffer.put`——补上了
   `allocate_and_queue_prefetch` 成功分支，且 parked（返回 False）不起表
   （否则 parked 等待会消耗装载预算，违背相位拆分本意）。

**预期效果**。C2 臂：装载只完成 0–1 批（128/4,223）就被切断的原因是查询耗尽了共享预算；
拆分后 2.03s 全额覆盖 IO 装载。注意（设计 §6 已述）：单独跑 F2-a 时查询仍慢
（~2s 查询 + 装载仍可能撞 query 护栏前的等待），TTFT 改善有限——这正是 EXP-B
与 EXP-A 要分开归因的原因。

**验证计划**：EXP-B（`C2+F2a` vs `C2-main`）。G1-B：`deadline` 增量 ≈0（≤128 tokens
容差）∧ 无请求挂起（600s 客户端超时探针，验查询护栏不退化成 A3W 式挂起）；
G2-B：R TTFT 中位 ≤1.5× P TTFT。

**风险与回滚**。风险：admission 尾延迟上界变松（等最慢查询，上限 4× 预算）；
查询护栏到期仍取消重试（见偏离 1）。回滚 = revert 单 commit（`_StoragePrefetchWait`
多出的字段带默认值，回滚后旧 dict/struct 状态无残留）。

---

## 实现中发现的坑（设计文档未预见）

1. **HiCacheFastFile 继承 HiCacheFile**（fast_file_store.py:87，且刻意不调父类
   `__init__` 但自设同构的 `_evictor`/`metadata_cache`/`config_suffix`）：本补丁的
   `batch_exists` 重载会被 fast_file 继承。经核验无害且有益：两者 `.bin` 命名一致、
   两家 LRUFileEvictor 都有 `.enabled`、fast_file 的 tmp（`*.bin.tmp.*`）被同名过滤器
   排除。若上游评审不想波及 fast_file，在其子类覆盖阈值常量即可。
2. **`_try_alloc_storage_hit` 有两个发射点**（cache/buffer），buffer 侧还可能 park——
   起表点必须区分成功发射与 parked，否则 F2-a 语义错位（见上偏离 2）。
3. **`_mark_prefetch_deadline_expired` 以默认构造 setdefault `_StoragePrefetchWait`**
   （:2875-2877）——新增结构体字段必须带默认值，否则票决早于本地 poll 时直接崩。
4. **F4 的判别条件**不能只看 `host_indices is None`：parked-hit 被 deadline 切断时
   remaining 存在、会正常入账 `unfulfilled{deadline}`。必须用
   `_storage_prefetch_hit_remaining_by_reqid` 成员检查精确圈定"查询结果从未被 drain"。
5. **测试环境**：`unified_radix_cache` 无法在缺 `tvm_ffi`/`compressed_tensors`
   （编译产物包，本机 PyPI 不可得）的环境导入——F4 分支逻辑与 F2-a 相位逻辑用
   AST 逐字抽取 harness 验证（`verify/`），F1/F4-collector 用真实导入的测试验证。

## 验证记录（2026-11-21，均在 e183c415b 克隆上）

1. 三个补丁各自在干净 main 上 `git apply --check` **通过**，且实际 apply 干净；
   三补丁叠加 apply 亦干净（改动区域两两不相交）；
2. 每补丁单独及叠加后 `python3 -m py_compile` 全部改动文件 **通过**；
3. 离线单测（`verify/`，临时 venv `/tmp/sgl-verify-venv`：torch 2.5.1+cu121、
   msgspec、prometheus_client；`datasets` 模块 stub）实跑：
   - F1：补丁内 7 测试（前缀判定/缺目录/两种 tmp 排除/小批量回退/metadata cache 回退/
     evictor 复核/惰性 evictor 不复核）**7 passed**；另 4,096 页等价性抽查通过；
   - F4：collector 侧 1 测试（补丁内测试体逐字复刻）**1 passed**；分支重放 3 测试
     （AST 逐字抽取 `check_prefetch_progress` 改动分支 + helper）**3 passed**；
   - F2-a：相位逻辑 8 测试（AST 逐字抽取 `_StoragePrefetchWait` + 4 个方法；
     含回归用例"2.5s 查询不再吃掉 2.03s 装载预算"）**8 passed**；
4. 克隆 working tree 已 reset 回 main，`git status` 干净，HEAD 仍为 `e183c415b`；
   未做任何 commit 到 main / push / 发布。
5. **待环境验证**：`test_stat_loggers_di.py` 与 `test_hicache_file_batch_exists.py`
   的完整 pytest 收集需完整 sglang 依赖链（同 pr-36280-revival 的限制）；
   EXP-A/B/D 集成臂未跑（需所有者批准 + 4090 环境）。

## 回滚

每个补丁 = 单 commit，`git revert` / `git apply -R` 均可独立回滚。F4 纯增量可观测性
（回滚零风险）；F1 回滚后回到逐 key 串行；F2-a 回滚后回到首 poll 起表。无存储格式、
跨秩协议、树状态变更。

## 投递前置条件（发布前逐条重核）

- 重跑 EXP-A 判别臂钉死 §2.3 "W 文件在盘"推断（若仍 0 命中，先回头查盘，勿投 F1）；
- 核对上游 main 是否已合入相关改动（#49226 及其它 hicache 存储 PR）；
- F4 counter 名与 #37503 族核对无撞名；补丁作者/日期占位改为所有者身份；
- 三个补丁若分 PR 投递：F4 先行（纯观测）、F1 次之、F2-a 附 EXP-B 数据；
- 所有公开操作需所有者明确批准（contrib 规则）。
