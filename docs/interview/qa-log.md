# 面试 Q&A 日课（每天收工追加）

> 规则：每天挑当天最意外的 2-3 个决策，先用自己的话写答案（口述转文字也行），
> 再对照出处修订。答案写自己的版本，不抄文档。

## 2026-11-20（D1：环境确认 + 五路桌面分析）

**Q1. nvidia-smi 完全正常，但所有 CUDA 程序初始化失败（Error 804），你怎么排查？**
（要点指针：/proc/driver/nvidia/version → find libcuda 两套并存 → ld.so.conf.d 的
`00-` 前缀优先级 → forward compat 不支持 GeForce；详见
`docs/interview/war-story-01-cuda-804.md`。注意准备"为什么 ldconfig 刷新没用"的追问）

**Q2. L2 回载什么时候比冷 prefill 快、什么时候反而慢？用你的实测数字讲。**
（要点指针：R/P 0.375（kernel 后端）vs 0.988（direct 后端）vs 16.8-32.6×（L3 file，
11-11 实测）；加上 12.3KB/token × PCIe 25GB/s 的 roofline 外推——多长 prefix 后
回载变成 PCIe-bound？见 `numbers-bank.md`）

**Q3. cache_salt 在分层 KV 缓存里解决什么问题？它的归属错误会导致什么现象？**
（要点指针：prefetch 插入以 anchor=root 的 cache_salt=None 命名 → salted re-match
不可见 → `index_evicted` 假象；nosalt 臂对照证实因果；main 已修。见 11-11
`l3-cache-nosalt/README.md`——这是你自己的双臂因果实验，讲出来极有说服力）

**Q4. 什么是 staging 饥饿？buffer_only 模式为什么不受影响？**
（要点指针：prefetch 暂存与写回竞争同一 host 池 + `protect_host` 钉住 + drain 慢
→ 每 R 只分到 722-999 token → 不足 `prefetch_threshold` → 静默 revoke；buffer_only
的 host 仅瞬时中转、预算分离 → 6/6 全命中）

**Q5. SGLang 和 vLLM 在 KV 卸载上的架构分野是什么？各自 bug 分布说明什么？**
（要点指针：树内统一 vs 树外契约；SGLang 计账耦合 scheduler（OOM 家族）vs vLLM
语义降级 best-effort；"两家的 bug 恰好证明对方的卖点"。见
`docs/interview/sglang-vs-vllm-kv-offload.md`）

**Q6. 为什么你的基准要绑定 40 位 commit + 模型 sha256 + 结果审计器？没有它会怎样？**
（要点指针：三个月 mem_cache 660 commits、silent semantic drift、负结果也可信的
前提是配置可复现；举 #47481 的 buffer_only 66.3→74.5 需要 main 新指标才可归因的例子）

**Q7. 用 60 秒讲清 SGLang HiCache 的完整机制。**
（要点指针：docs/03-hicache-dataflow-main.md 第十一节有现成 60 秒版。三个机制点必须
内化：①命名空间即密钥（salt 种子进 SHA256 页 hash 链，anchor 不一致直接断言）；
②查询零占用、命中才分配（staging 按 hit 尺寸分配、可二分 partial、未兑现命中带
reason 归因）；③容量是预约制而非抢占制（reclaim_for_load_back、SWA window 先回收、
buffer 模式写/取预算分离））

## 待办

- [ ] 用户用自己的话写 Q1-Q6 答案（每次 2-3 题即可，不必一天全写）
- [ ] 我根据答案反馈：哪里数字记错了、哪里缺机制细节
