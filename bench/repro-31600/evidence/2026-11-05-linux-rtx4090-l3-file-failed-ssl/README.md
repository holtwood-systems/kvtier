# L3 file 首轮启动失败（环境缺陷，已修复）

`--hicache-storage-backend file` 首次启动在 scheduler 初始化阶段崩溃：
`HiCache native hash extension` JIT 编译失败——`hash_binding.cpp`
需要 `openssl/sha.h`，容器当时缺 `libssl-dev`。

修复：`apt-get install libssl-dev` 后扩展正常编译加载，服务可起
（见 `../2026-11-05-linux-rtx4090-l3-file/` 复跑结果）。上游部署
L3 的一个隐性系统依赖在此记录。

产物：`server-startup.log`（含完整崩溃栈）、`STATUS.txt`。
