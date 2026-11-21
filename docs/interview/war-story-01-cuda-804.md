# War Story #1：容器里的隐形 CUDA 库劫持（Error 804）

> 2026-11-20，4090 服务器。15 分钟定位，适合"受限环境排障"类面试题。

## 症状（误导性极强）
- `nvidia-smi` 完全正常：GPU 可见、49140 MiB、驱动 550.107.02
- 任何 CUDA 程序初始化即死：`cudaGetDeviceCount() Error 804:
  forward compatibility was attempted on non supported HW`

## 排查链（每步排除一个假设）
1. `/proc/driver/nvidia/version` → 内核驱动 550.107.02，正常
2. `find / -name "libcuda.so*"` → **两套用户态库并存**：
   `/usr/lib/.../libcuda.so.550.107.02`（匹配）与 `/usr/local/cuda-12.8/compat/libcuda.so.570.86.10`（cuda-compat 前向兼容包）
3. `ls -la` 看系统路径软链 → 已指向 550，**但程序仍拿到 570** → 怀疑 ld.so.cache
4. `grep cuda /etc/ld.so.conf.d/` → 命中真凶：
   `00-compat-1010519684.conf: /usr/local/cuda-12.8/compat`
   ——`00-` 前缀 = 搜索序第一，compat 目录**屏蔽**了匹配驱动版本
5. 机制：cuda-compat 是给新用户态 + 旧内核驱动的数据中心卡设计的 forward compat；
   **GeForce 不在支持列表** → 570 libcuda 对 550 内核驱动尝试 forward compat → 804

## 修复与防回归
- `mv 00-compat-*.conf → .bak-disabled && ldconfig` → torch 立即可用
- 防回归：任何新环境脚本**禁止**把 `compat/` 加进 `LD_LIBRARY_PATH`
- 附加发现：镜像里一堆 0 字节的 570.86.10 占位库（apt 安装时 ldconfig 刷屏警告的来源），
  说明是装机脚本注入不完整——容器镜像的"库考古"意识

## 面试一句话版
"nvidia-smi 正常但 CUDA 全挂，Error 804 指向 forward compat；根因是容器镜像把
cuda-compat 570 库通过 `00-` 前缀的 ld.so.conf.d 条目排在了库搜索序首位，屏蔽了
与内核驱动匹配的 550 库——GeForce 不支持 forward compat 所以直接拒初始化。
禁用该条目刷新 ldcache 即修复，并给后续所有环境加了防回归约束。"
