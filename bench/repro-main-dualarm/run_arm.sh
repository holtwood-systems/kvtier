#!/usr/bin/env bash
# 实验 0（repro-main-dualarm）单臂 SGLang(main) 服务启动器。
# 严格按 docs/bench-design/2026-11-20-repro-main-dualarm.md §7 模板 +
# 附录 A 臂表；键名审计依据 bench-surface-audit §1.1 与
# storage/fast_file/fast_file_store.py（storage_root 环境变量与 file 共用；
# max_size 必须带 SI/IEC 单位，裸数字按字节解析）。
# 用法：run_arm.sh <A1|A2|A3|A4|B1|B2|B3|B4|C2|SMOKE>
set -euo pipefail

ARM="${1:?用法: run_arm.sh <A1|A2|A3|A4|B1|B2|B3|B4|C2|SMOKE>}"
VENV_DIR="${VENV_DIR:-/root/envs/kvtier-main}"
MODEL_PATH="${MODEL_PATH:-/root/models/Qwen2.5-0.5B-Instruct}"
PORT="${PORT:-31000}"
STORAGE_ROOT="${STORAGE_ROOT:-/data/hicache-l3/dualarm}"

PYTHON="$VENV_DIR/bin/python"

# 臂表（设计文档 §4）：salt 由 workload 侧控制，服务端只定这些。
# log-level 规则：cache 臂 debug（revoke 日志侧写），buffer 臂 info。
case "$ARM" in
  A1)    HOST_MODE=cache;       RATIO=0.12; BACKEND=file;      LOG=debug ;;
  A2)    HOST_MODE=cache;       RATIO=0.12; BACKEND=file;      LOG=debug ;;
  A3)    HOST_MODE=buffer_only; RATIO=0.12; BACKEND=file;      LOG=info  ;;
  A4)    HOST_MODE=buffer_only; RATIO=0.12; BACKEND=file;      LOG=info  ;;
  # 补充臂（偏离项）：main 默认 prefetch policy=timeout 使 buffer 臂被
  # deadline 截断（A3 实测 6/6 全 miss）；wait_complete 才能测 H③ 的
  # 驻留率问题本身（与 DEV-19 的 park-and-retry 语义对齐）。
  A3W)   HOST_MODE=buffer_only; RATIO=0.12; BACKEND=file;      LOG=info  ;;
  B1)    HOST_MODE=cache;       RATIO=0.12; BACKEND=fast_file; LOG=debug ;;
  B2)    HOST_MODE=cache;       RATIO=0.12; BACKEND=fast_file; LOG=debug ;;
  B3)    HOST_MODE=buffer_only; RATIO=0.12; BACKEND=fast_file; LOG=info  ;;
  B4)    HOST_MODE=buffer_only; RATIO=0.12; BACKEND=fast_file; LOG=info  ;;
  C2)    HOST_MODE=cache;       RATIO=0.48; BACKEND=file;      LOG=debug ;;
  # 烟测（设计 §12-7）：缩池 0.05 验证 unfulfilled{reason=host_capacity}
  # 可触发且与 debug 日志吻合，判据/环境有问题则停下报告。
  SMOKE) HOST_MODE=cache;       RATIO=0.05; BACKEND=file;      LOG=debug ;;
  *) echo "未知臂: $ARM" >&2; exit 2 ;;
esac

BACKEND_ARGS=(--hicache-storage-backend "$BACKEND")
if [ "$BACKEND" = "fast_file" ]; then
  BACKEND_ARGS+=(--hicache-storage-backend-extra-config '{"max_size":"20G"}')
fi
POLICY_ARGS=()
case "$ARM" in
  A3W) POLICY_ARGS=(--hicache-storage-prefetch-policy wait_complete) ;;
esac

STORAGE_DIR="$STORAGE_ROOT/$ARM"
mkdir -p "$STORAGE_DIR"

exec env SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR="$STORAGE_DIR" \
  "$PYTHON" -m sglang.launch_server \
  --model-path "$MODEL_PATH" \
  --host 127.0.0.1 --port "$PORT" \
  --mem-fraction-static 0.35 \
  --max-total-tokens 358532 \
  --enable-hierarchical-cache \
  --hicache-host-memory-mode "$HOST_MODE" \
  --hicache-ratio "$RATIO" \
  --hicache-io-backend kernel \
  --hicache-mem-layout page_first \
  --hicache-write-policy write_through \
  "${BACKEND_ARGS[@]}" \
  "${POLICY_ARGS[@]}" \
  --enable-metrics --enable-cache-report \
  --log-level "$LOG"
