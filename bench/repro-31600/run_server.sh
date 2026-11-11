#!/usr/bin/env bash
# 启动用于 HiCache L2 单卡微基准的 SGLang 服务。
# 用法：MODEL_PATH=/path/to/model bash run_server.sh
set -euo pipefail

MODEL_PATH="${MODEL_PATH:?需要设置 MODEL_PATH，例如 /root/models/Qwen2.5-7B-Instruct}"
PORT="${PORT:-30000}"
MEM_FRACTION="${MEM_FRACTION:-0.78}"
HICACHE_RATIO="${HICACHE_RATIO:-4.0}"
HICACHE_IO="${HICACHE_IO:-kernel}"
HICACHE_LAYOUT="${HICACHE_LAYOUT:-page_first}"
KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-auto}"
DISABLE_CUDA_GRAPH="${DISABLE_CUDA_GRAPH:-0}"
# 可选 L3 后端（file/shm 等）；file 后端路径用
# SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR 环境变量指定。
HICACHE_STORAGE_BACKEND="${HICACHE_STORAGE_BACKEND:-}"
# host 内存模式：cache（L2 host 缓存，默认）或 buffer_only（仅 staging，
# L3 命中经 staged prefetch 直接拼入，不依赖 radix 路径存活）。
HICACHE_HOST_MEMORY_MODE="${HICACHE_HOST_MEMORY_MODE:-cache}"

EXTRA_ARGS=()
if [ "$DISABLE_CUDA_GRAPH" = "1" ]; then
  # 小显存 / 小内存单卡上跳过 graph 捕获：省下 host 预留并缩短启动。
  EXTRA_ARGS+=(--disable-cuda-graph)
fi
if [ -n "$HICACHE_STORAGE_BACKEND" ]; then
  EXTRA_ARGS+=(--hicache-storage-backend "$HICACHE_STORAGE_BACKEND")
fi

exec python3 -m sglang.launch_server \
  --model-path "$MODEL_PATH" \
  --port "$PORT" \
  --mem-fraction-static "$MEM_FRACTION" \
  --kv-cache-dtype "$KV_CACHE_DTYPE" \
  --enable-hierarchical-cache \
  --hicache-host-memory-mode "$HICACHE_HOST_MEMORY_MODE" \
  --hicache-ratio "$HICACHE_RATIO" \
  --hicache-mem-layout "$HICACHE_LAYOUT" \
  --hicache-write-policy write_through \
  --hicache-io-backend "$HICACHE_IO" \
  --enable-metrics \
  --enable-cache-report \
  "${EXTRA_ARGS[@]}" \
  --log-level info
