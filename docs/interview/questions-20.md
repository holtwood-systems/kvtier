# 高级 AI Infra 面试追问题库（20 问）

> 用途：D10/D13 mock 面试题库 + 自测。每题附答案指针（指向本仓证据）。
> 数字类问题先背 numbers-bank.md，机制类先读 03-hicache-dataflow-main.md 第十一节。

## A. 排障与war story（3 题）

**1. 遇到过最棘手的 GPU 环境问题是什么？nvidia-smi 正常但 CUDA 全挂你怎么排查？**
指针：`war-story-01-cuda-804.md`——五步链 + "为什么刷 ldconfig 没用"（lcache 优先级）
+ 0 字节占位库的镜像考古。

**2. 共享宿主机上做性能测量，怎么保证数据可信？**
指针：迁移基线的 3.5GB 幽灵显存 + 并发租户归因（`2026-11-20-4090-migration-l2baseline/`）；
对策=测量窗口前 GPU 独占核验、ratio 型指标优先于绝对值、偏离项如实留档。

**3. 你的基准怎么防止"版本漂移导致结论失真"？**
指针：三个月 mem_cache ~660 commits；40 位 commit 锚定 + 模型 sha256 + validate_results
审计门禁；R/P 0.375→0.4817 的环境漂移实例。

## B. HiCache 机制深挖（5 题）

**4. cache_salt 解决什么问题？归属错误时用户看到什么现象？你怎么证明因果的？**
指针：11-11 nosalt 臂双臂对照——index_evicted 假象、anchor=root 插入 cache_salt=None、
re-match 不可见；saltless 臂 storage>0 反证。这是你自己的因果实验。

**5. 什么是 staging 饥饿？为什么 buffer_only 不受影响？main 重构后还可能发生吗？**
指针：43k host 池竞争 + protect_host 钉住 + drain 慢 → 722-999 token 截断 → 静默 revoke；
buffer 模式写/取预算分离；main 改"命中时分配"+ 仲裁层，残留通路见
`bench-design/…-dualarm.md` §3 的 (a)(b)(c)。

**6. 回载什么时候比冷 prefill 慢？给个量化边界。**
指针：R/P 0.375（kernel）vs 0.988（direct）vs 16.8-32.6×（L3 file）；
12.3KB/token × PCIe ~25GB/s → 4223 token≈2ms（不 bound），~100K token 才到拐点。

**7. selective 写策略的陷阱是什么？为什么实验容易误判成 bug？**
指针：hit_count 门控（阈值 2）+ 父未备份子跳过 → 单次访问永不落盘；
与真 bug（index_evicted）的区分方法=显式固定 write_policy + 计数器归因。

**8. admission 期间 KV 页面的生命周期是什么？哪里可能漏防逐引用？**
指针：03 号文档"admission 四道门"+ L3 命中 span 到 admission 才结账的记账法；
#49221/#49226 的窗口 = 分配成功但 admission 排队/重试梯期间无引用。

## C. 架构判断（4 题）

**9. SGLang 树内统一 vs vLLM 树外契约，各自的 bug 分布说明了什么？**
指针：`sglang-vs-vllm-kv-offload.md` TP1——SGLang 计账耦合 scheduler（OOM 家族），
vLLM 语义降级 best-effort；"互证对方卖点"。

**10. 为什么两家都往 connector/linker 方向演化？**
指针：同上 TP3——不 fork、N×M 契约收敛（CSI 类比）、KV events 升格集群级调度资源；
差异：vLLM 时序契约 vs SGLang 拓扑契约。

**11. 如果让你设计分层 KV 缓存，背压怎么做？**
指针：现状=write/prefetch 队列无界、buffer 积压上限只是泄漏兜底（03 号文档发现 9）；
vLLM 谱系（RETRY + sync_load_budget + 退避）可作参照；给出你自己的方案雏形。

**12. 观测性缺口在哪？token 维度和事件维度的区别？**
指针：#37503 已有 unfulfilled{reason}（12 预注册 + 2 处 label 失配）；
revoked/declined 事件计数无 Prometheus 出口 → #36280 复活补丁（14 键动态导出）。

## D. 数据与消融（4 题）

**13. kernel vs direct IO 后端差 2.6×，机制是什么？**
指针：io-direct 证据包——direct 以 copy-engine DMA 阻塞 forward（main 已加告警），
kernel 路径专用 stream + page-first 传输重叠。

**14. FP8 KV 的容量/质量权衡数据？**
指针：1.56M→3.11M tokens 翻倍、R/P 0.375→0.392（+4%）——容量换精度的好交易边界。

**15. CUDA graph 对分层缓存路径的影响？**
指针：no-graph 消融 R/P +11%、写入慢 22%——机制结论不依赖 graph 但 graph 放大收益。

**16. L3 file 后端为什么慢？fast_file 改进了什么？**
指针：file 逐页元数据 + 所有权重启扫描；fast_file readv/writev 直搬 + 原子发布 +
双水位驱逐；注意与 unified key scheme 互斥（03 号文档发现 8）。

## E. 多卡与一致性（2 题）

**17. 多秩一致性怎么维护？丢 ack 会怎样？**
指针：调度线程 lockstep 点 + MIN/MAX collective 折叠；prefetch 失败也必须发同数
PrefetchAck（cache_controller.py:1387 "so tricky"）——表现为 collective 挂起而非报错。

**18. 跨实例/跨秩的前缀共享靠什么？**
指针：内容寻址 hash 链（无 salt）vs radix 命名空间（有 salt）的双层设计；
vLLM 侧对照 NONE_HASH 种子 + canonical 命名空间。

## F. 方法论与元能力（2 题）

**19. 上游贡献怎么选题材才不被拒？**
指针：#36115 关闭评论——"挂在具体 bug 修复或行为变更上"；#36280 复活的维护者点名
信号；竞争 PR 收敛礼仪（#49086→#50582 案例）。

**20. 负结果怎么变成资产？**
指针：L3 file 负结果→双机器复现→双臂机制确认→main 验证设计（实验 0）——
"负结果修正"与"真 bug 确诊"二选一都成立；诚实留档本身就是差异化。
