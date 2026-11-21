# 2026-11-20 · RTX 4090 48GB 改装卡显存稳定性验证

服务器重置后的硬件可行性核查（D1），为后续所有 GPU 实验背书。

## 结论

**ALL PASS**（`rounds=22147`，2026-11-20 07:31:47 UTC）。

- 43 × 1 GiB int64 确定性图案块填充（~44 GiB）+ 8192³ f32 GEMM 混压；
- 25 分钟压力期（2 次中途校验 + 终验），**零位损坏**；
- D2D 拷贝带宽 **937 GB/s**（4 GiB，健康水平，spec ~1 TB/s）；
- 峰值 74 °C / 352.7 W，SM 时钟 2220–2745 MHz，无异常降频
  （throttle 位 0x4 = 正常软件功耗墙，间歇出现）。

## 环境与监控边界

- 机器：租用容器（Ubuntu 22.04.05，内核 5.15.0-60-generic），
  RTX 4090 49140 MiB（48 GB 改装，sm_89），驱动 550.107.02；
- 解释器：`/opt/conda`（ComfyUI 生产环境，Python 3.11 + torch 2.7.1+cu128）——
  未做任何改动；
- **容器内无 dmesg 权限**：内核级 Xid 不可观测，本验证依赖应用层图案校验 +
  nvidia-smi 时序（monitor.csv），该边界如实记入所有后续实验报告；
- 当日前置修复：容器 CUDA Error 804（`/usr/local/cuda-12.8/compat` 570 前向
  兼容库屏蔽 550 匹配库，ld.so.conf.d `00-` 前缀优先），禁用该条目后恢复。

## 文件

- `vram_stress.py` —— 测试脚本（自包含，可复跑）；
- `vram_stress.log` —— 完整日志（含带宽测量与校验事件）；
- `monitor.csv` —— nvidia-smi 5 s 采样（温度/功率/时钟/利用率/显存/throttle 位，
  至 08:02 监控停止）。

## 复跑

```bash
python vram_stress.py          # 默认 25 min；DUR_S/VERIFY_EVERY 可调
nvidia-smi --query-gpu=timestamp,temperature.gpu,power.draw,clocks.sm,clocks.mem,\
utilization.gpu,memory.used,clocks_throttle_reasons.active \
  --format=csv,noheader -l 5 > monitor.csv
```
