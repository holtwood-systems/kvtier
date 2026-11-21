#!/usr/bin/env bash
# repro-49221 服务启动器（main 参数名，设计文档 §7 模板）。
# 判定臂默认（Qwen3-4B + file L3 + wait_complete + ratio 2.0）；
# 探索/剂效/对照臂经环境变量替换：
#   MODEL_PATH / PORT / HICACHE_RATIO / HICACHE_HOST_MEMORY_MODE /
#   PREFETCH_POLICY / VENV_DIR / STORAGE_DIR / EXTRA_SERVER_ARGS
# 注意（bench-surface-audit A1）：main 已移除 --disable-cuda-graph，
# 本实验默认开 graph（issue 的该 flag 无法也无需在 main 传递）。
set -euo pipefail

VENV_DIR="${VENV_DIR:-/root/envs/kvtier-main}"
MODEL_PATH="${MODEL_PATH:-/root/models/Qwen3-4B}"
PORT="${PORT:-31000}"
HICACHE_RATIO="${HICACHE_RATIO:-2.0}"
HICACHE_HOST_MEMORY_MODE="${HICACHE_HOST_MEMORY_MODE:-cache}"
PREFETCH_POLICY="${PREFETCH_POLICY:-wait_complete}"
STORAGE_DIR="${STORAGE_DIR:-/data/hicache-l3/repro-49221/default}"
MEM_FRACTION="${MEM_FRACTION:-0.85}"
LOG_LEVEL="${LOG_LEVEL:-info}"

PYTHON="$VENV_DIR/bin/python"
mkdir -p "$STORAGE_DIR"

exec env SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR="$STORAGE_DIR" \
  "$PYTHON" -m sglang.launch_server \
  --model-path "$MODEL_PATH" \
  --host 127.0.0.1 --port "$PORT" \
  --trust-remote-code \
  --mem-fraction-static "$MEM_FRACTION" \
  --context-length 8192 \
  --max-total-tokens 32768 \
  --max-running-requests 32 \
  --chunked-prefill-size 2048 \
  --page-size 64 \
  --enable-hierarchical-cache \
  --hicache-host-memory-mode "$HICACHE_HOST_MEMORY_MODE" \
  --hicache-ratio "$HICACHE_RATIO" \
  --hicache-io-backend kernel \
  --hicache-mem-layout page_first \
  --hicache-write-policy write_through \
  --hicache-storage-backend file \
  --hicache-storage-prefetch-policy "$PREFETCH_POLICY" \
  --enable-metrics --enable-cache-report \
  --log-level "$LOG_LEVEL" \
  ${EXTRA_SERVER_ARGS:-}
