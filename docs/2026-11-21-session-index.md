# 2026-11-20/21 会话产出物总索引

> 一页导航：本轮全部产出、状态与待决项。所有文件均为**未提交**的工作区/未跟踪文件
> （遵守"提交需所有者指示"规则）。

## 决策与计划

| 文件 | 内容 | 状态 |
|---|---|---|
| `docs/decisions/2026-11-20-hicache-candidate-go-nogo.md` | Go/No-Go v1.0：五门禁、候选矩阵、倾向方案、No-Go 条款 | ✅ **用户已批 Go（两实验）**，待服务器恢复执行 |

## 实验设计（bench-design/）

| 文件 | 内容 |
|---|---|
| `2026-11-20-repro-main-dualarm.md` | **实验 0（首选）**：DEV-20 双臂 × main 双修复验证；含 2026-11-21 补遗三条约束（host-fraction 默认 0.8 / fast_file×unified key 互斥 / 12 值 label 失配判读） |
| `2026-11-20-repro-49221.md` | #49221 独立复现（七相位、Qwen3-4B 判定 + 0.5B 快扫，~4h） |
| `2026-11-20-repro-47481.md` | #47481 备选（L2/L3 解耦扫描设计，~5.5h，未批准执行） |

## 上游分析（upstream/ + docs/）

| 文件 | 内容 |
|---|---|
| `docs/upstream/2026-11-20-memcache-diff-6afee75f-to-main.md` | 七主题静态 diff（salt 已修/staging 重构/背压仍无界/L3 大改） |
| `docs/upstream/2026-11-20-bench-surface-audit.md` | bench 依赖面三级分类（2 报错/5 变义/34 无影响）+ #36280 评估 |
| `docs/03-hicache-dataflow-main.md` | **main 版完整链路**（652 行、行号锚定、双 mermaid 图、60 秒版、9 个新发现） |

## 上游贡献备胎（contrib/）

| 文件 | 内容 |
|---|---|
| `contrib/pr-36280-revival/0001-*.patch` | prefetch outcome 指标导出复活版（+197/−2，动态 14 键、tokens/events 双路由、4 单测实跑通过；apply/py_compile 验证） |
| `contrib/pr-36280-revival/README.md` | 与原 PR 三处差异、14 键清单（行号锚定）、投递策略（协调评论→新 PR 引用）、英文评论草稿 |

## 运维（ops/）

| 文件 | 内容 |
|---|---|
| `docs/ops/2026-11-21-server-rebuild-runbook.md` | 服务器重置重建手册（Step 0 的 804 修复→两代环境配方→坑清单→已离机/需重建清单） |

## 证据（bench/repro-31600/evidence/）

| 目录 | 内容 |
|---|---|
| `2026-11-20-4090-hw-stability/` | 显存稳定性 ALL PASS（22147 轮零损坏、937 GB/s、含监控边界声明） |
| `2026-11-20-4090-migration-l2baseline/` | 迁移基线 R/P 0.4817、host 命中 8/8×4223/4224、audit 通过、环境偏离项记录 |

## 工具链（bench/repro-31600/ 工作区改动）

- `workload.py`/`run_server.sh`/`validate_results.py` + 2 个测试文件：main 指标适配
  （reason 级解析、`SGLANG_VER=main` 开关），**本地 make test 44/44 绿**；
  服务器同步方案：`git diff -- bench/repro-31600` 生成 patch → scp → git apply

## 面试材料（interview/）

| 文件 | 内容 |
|---|---|
| `numbers-bank.md` | 全部实测数字 + roofline 外推（12.3KB/token × PCIe） |
| `war-story-01-cuda-804.md` | Error 804 五步排查链 + 一句话面试版 |
| `sglang-vs-vllm-kv-offload.md` | 对比表、5 talking points、8 追问预判 |
| `qa-log.md` | 日课七问（Q7=60 秒讲清 HiCache），**待用户作答** |

## 待决项（按优先级，2026-11-21 深夜更新——实验已完成后的决策包）

1. **上游三文案审阅**（全部只写未发，批准即发）：
   - A `docs/upstream/draft-49226-comment.md` —— #49226 修复 PR 的独立复现证据评论
   - B `contrib/pr-36280-revival/PR-BODY-DRAFT.md` —— 观测性 PR 正文（补丁在同目录，已验证）
   - C `docs/upstream/draft-issue-structural-morph.md` —— 4× 结构型新 issue（发布前建议重跑 EXP-A 判别臂）
2. **修复实施 Go**：F1/F4/F2-a 已预实现为可审补丁 `contrib/structural-morph-fixes/`
   （0001 scandir 快照 / 0002 deadline-cut 计数 / 0003 计时相位拆分；apply+py_compile+
   19 离线测试全绿，两处有意偏离与 5 个实现坑见其 README）；F3 保持设计态。
   验证实验设计见 `docs/bench-design/2026-11-21-structural-morph-fix-design.md` §六
3. **git 提交**：全部工作区改动（含 301MB+20MB 证据包），何时 commit/push 由用户决定
4. **qa-log 七问作答 / D10 mock**：面试线收尾

## 2026-11-21 实验产出（新增）

- `bench/repro-31600/evidence/2026-11-20-main-dualarm/`（301MB）：实验 0 双臂——salt 已修
  （salted=nosalt=4/6）、饥饿未修（host_capacity +14,668）、4× 结构型变形（零记账查询切断）
- `bench/repro-31600/evidence/2026-11-20-repro-49221/`（20MB）：#49221 复现成立——burst16 命中
  28%、shortfall 占 96%、池放大全恢复、buffer_only 免疫
- 服务器 `/data/prof/repro-49221/j1p-profile/`（534MB kernel trace，未离机）
- 面试材料更新：story-3min 结局 A 定稿（实数）、numbers-bank +13 条、questions-20
