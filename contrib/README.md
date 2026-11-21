# contrib — 上游贡献草稿

对上游开源项目的 PR 评审、issue 提案等贡献材料。草稿中的「验证笔记」仅供自己核对，
不发布。

## minisgl

| 文件 | 类型 | 状态 |
|---|---|---|
| `minisgl/review-pr143.md` | PR #143 评审评论草稿（append_host 缩进问题） | 待更新 |
| `minisgl/review-pr150.md` | PR #150 评审评论草稿（计数器校验） | 待更新 |
| `minisgl/issue-fast-compare-key-simd.md` | [Perf] `fast_compare_key` SIMD 提案 | 待更新 |

## pr-36280-revival（sglang）

| 文件 | 类型 | 状态 |
|---|---|---|
| `pr-36280-revival/0001-export-storage-prefetch-outcome-stats.patch` | PR #36280 复活补丁（快照键动态导出，锚 main e183c415b，apply/编译/单测已验，全量套件待环境验证） | 待投递（需所有者批准） |
| `pr-36280-revival/README.md` | 原版 vs 复活版差异、14 键清单、#37503 互补、验证记录、投递策略与评论草稿 | 待投递 |

## structural-morph-fixes（sglang）

4× 结构型变形三症状的最小修复草稿（规格：`docs/bench-design/2026-11-21-structural-morph-fix-design.md` §五）。
锚 main e183c415b，三补丁互相独立、可单独 apply，apply/编译/离线单测已验，集成臂（EXP-A/B/D）未跑。

| 文件 | 类型 | 状态 |
|---|---|---|
| `structural-morph-fixes/0001-f1-batch-exists-scandir-snapshot.patch` | F1：file 后端 batch_exists 单次 scandir 快照（治症状①③） | 待实验验证（需所有者批准） |
| `structural-morph-fixes/0002-f4-deadline-cut-accounting.patch` | F4：deadline 零记账出口 counter + WARNING（纯可观测性） | 待实验验证（需所有者批准） |
| `structural-morph-fixes/0003-f2a-deadline-phase-split.patch` | F2-a：装载预算起点移到命中分配点 + 查询相位护栏（治症状③） | 待实验验证（需所有者批准） |
| `structural-morph-fixes/README.md` | 机制/预期效果/EXP 映射/风险回滚/坑/验证记录 | — |
| `structural-morph-fixes/verify/` | 离线验证 harness（F1/F4/F2-a，AST 逐字抽取 + 真实导入两档） | 本机复核用，不发布 |

F3（写队列背压）保持设计态未实现。

## 规则

- 发布前逐条验证草稿内容；「验证笔记」不发布；
- 公开评论或提交前，必须重新核对上游当前 `main`、重复 issue/PR 与最新状态；
- 提交和推送必须得到明确指示。
