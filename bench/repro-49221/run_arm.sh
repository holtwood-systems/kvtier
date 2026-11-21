#!/usr/bin/env bash
# repro-49221 单臂全流程驱动：GPU 独占核验 → 清 storage → 起服务 →
# health → 采样器+dmon → 七相位 workload → 采集 → 停服。
# 设计文档：docs/bench-design/2026-11-20-repro-49221.md §4/§7/§9。
# 用法：run_arm.sh <arm> [--profile] [--smoke]
#   arm ∈ e1-gradient e2-ratio12 e3-timeout e4-buffer e5-judgment
#          j1-baseline j2-baseline-rerun j3-ratio12 j4-timeout j5-profile
#   e* = 探索臂（Qwen2.5-0.5B）；j* = 判定臂（Qwen3-4B）
#   --profile = V2 剖析窗口（serial→burst16-a，仅 4B 基线一次）
#   --smoke   = 烟测：gradient 相位集 + requests 8 + 上下文核对
set -euo pipefail

ARM="${1:?用法: run_arm.sh <arm> [--profile] [--smoke]}"
shift || true
PROFILE=0; SMOKE=0
for flag in "$@"; do
  case "$flag" in
    --profile) PROFILE=1 ;;
    --smoke) SMOKE=1 ;;
    *) echo "未知参数: $flag" >&2; exit 2 ;;
  esac
done

REPO="${REPO:-/root/src/kvtier}"
PORT="${PORT:-31000}"
STORAGE_ROOT="${STORAGE_ROOT:-/data/hicache-l3/repro-49221}"
EVIDENCE_ROOT="${EVIDENCE_ROOT:-$REPO/bench/repro-31600/evidence/2026-11-20-repro-49221}"
BENCH_PY="$REPO/bench/repro-31600/.venv/bin/python"
WORKLOAD="$REPO/bench/repro-49221/workload.py"
SAMPLER="$REPO/bench/repro-main-dualarm/sampler.py"
RUN_SERVER="$REPO/bench/repro-49221/run_server.sh"
COMMIT="${COMMIT:-e183c415bdfc5995f6288b1651e1c320c6a74015}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-600}"

# —— GPU 协调协议（/data/gpu-coordination/README.md，共享库）——
# 2026-11-21 用户指示共享模式：与 career-prep-7b 共居，显存硬门禁
#（free ≥ NEED + 4GB），e 臂（0.5B，~4GB 实际占用）与 j 臂（4B，
# ~15GB 实际占用：权重 8 + KV 4.8 + graph/runtime ~2-3）分别设 NEED。
MY_LOCK="/data/gpu-coordination/locks/kvtier-main-exp.lock"
LOCK_PURPOSE="#49221 复现 GPU 测量窗口（repro-49221，共享模式）"
LOCK_PORTS="$PORT"
case "$ARM" in
  j*) WINDOW_MODE=shared NEED_MIB=20000 LOCK_TTL_HOURS=8 ;;
  *)  WINDOW_MODE=shared NEED_MIB=8000 LOCK_TTL_HOURS=8 ;;
esac
source /root/src/kvtier/bench/repro-main-dualarm/gpu_window.sh

STORAGE_DIR="$STORAGE_ROOT/$ARM"
EVIDIR="$EVIDENCE_ROOT/$ARM"
mkdir -p "$EVIDIR/results"

# 臂表 → 环境变量（设计 §4/§7/§10）
SERVER_ENV=()
PHASE_SET="judgment"
MODEL_05B="/root/models/Qwen2.5-0.5B-Instruct"
MODEL_4B="/root/models/Qwen3-4B"
case "$ARM" in
  e1-gradient)   SERVER_ENV=(MODEL_PATH="$MODEL_05B"); PHASE_SET="gradient" ;;
  e2-ratio12)    SERVER_ENV=(MODEL_PATH="$MODEL_05B" HICACHE_RATIO=12); PHASE_SET="gradient" ;;
  e3-timeout)    SERVER_ENV=(MODEL_PATH="$MODEL_05B" PREFETCH_POLICY=timeout); PHASE_SET="gradient" ;;
  e4-buffer)     SERVER_ENV=(MODEL_PATH="$MODEL_05B" HICACHE_HOST_MEMORY_MODE=buffer_only PREFETCH_POLICY=wait_complete); PHASE_SET="gradient" ;;
  e5-judgment)   SERVER_ENV=(MODEL_PATH="$MODEL_05B") ;;
  j1-baseline)   SERVER_ENV=(MODEL_PATH="$MODEL_4B") ;;
  j1p-profile)   SERVER_ENV=(MODEL_PATH="$MODEL_4B" EXTRA_SERVER_ARGS="--admin-api-key kvtier49221") ;;
  j2-baseline-rerun) SERVER_ENV=(MODEL_PATH="$MODEL_4B") ;;
  j3-ratio12)    SERVER_ENV=(MODEL_PATH="$MODEL_4B" HICACHE_RATIO=12) ;;
  j4-timeout)    SERVER_ENV=(MODEL_PATH="$MODEL_4B" PREFETCH_POLICY=timeout) ;;
  j5-profile)    SERVER_ENV=(MODEL_PATH="$MODEL_4B") ;;
  *) echo "未知臂: $ARM" >&2; exit 2 ;;
esac

echo "== [$ARM] GPU 窗口申请（协调协议，WINDOW_MODE=${WINDOW_MODE:-exclusive}） =="
acquire_gpu_window

echo "== [$ARM] GPU 基线核验 =="
USED=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1)
PROCS=$(nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader || true)
{
  echo "utc=$(date -u +%FT%TZ) memory_used_mib=$USED window_mode=${WINDOW_MODE:-exclusive} need_mib=${NEED_MIB:-20000}"
  echo "compute_apps:"
  echo "${PROCS:-<none>}"
  if [ "${WINDOW_MODE:-exclusive}" = "shared" ]; then
    echo "coexistence:"
    for f in /data/gpu-coordination/locks/*.lock; do
      [ -e "$f" ] || continue
      [ "$f" = "/data/gpu-coordination/locks/kvtier-main-exp.lock" ] && continue
      echo "--- $f"; cat "$f"
    done
  fi
} | tee "$EVIDIR/gpu-baseline.txt"
if [ "${WINDOW_MODE:-exclusive}" != "shared" ] && [ "${USED:-0}" -gt 4096 ]; then
  echo "GPU 基线 ${USED} MiB 超过 4096 MiB 阈值——不硬跑，释放窗口后退出" >&2
  release_gpu_window
  exit 3
fi

cleanup() {
  kill -TERM -- -"${SERVER_PID:-0}" 2>/dev/null || true
  if [ -n "${HEARTBEAT_PID:-}" ]; then kill "$HEARTBEAT_PID" 2>/dev/null || true; fi
  [ "${HOLD_LOCK:-0}" = "1" ] || release_gpu_window
}

echo "== [$ARM] 清空 storage 目录 $STORAGE_DIR =="
rm -rf "$STORAGE_DIR"; mkdir -p "$STORAGE_DIR"

echo "== [$ARM] 启动服务（日志 $EVIDIR/server.log） =="
setsid env "${SERVER_ENV[@]}" STORAGE_DIR="$STORAGE_DIR" PORT="$PORT" \
  bash "$RUN_SERVER" >"$EVIDIR/server.log" 2>&1 &
SERVER_PID=$!
trap cleanup EXIT

echo "== [$ARM] 等待 /health（至多 ${HEALTH_TIMEOUT}s） =="
ELAPSED=0
until curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "服务进程退出：" >&2; tail -30 "$EVIDIR/server.log" >&2; exit 4
  fi
  if [ "$ELAPSED" -ge "$HEALTH_TIMEOUT" ]; then echo "health 超时" >&2; exit 5; fi
  sleep 5; ELAPSED=$((ELAPSED + 5))
done
echo "health ok after ${ELAPSED}s"
grep -m4 "hierarchical KV host pool" "$EVIDIR/server.log" | tee "$EVIDIR/host-pool-log.txt" || true

"$BENCH_PY" "$SAMPLER" --port "$PORT" --output "$EVIDIR/samples-$ARM.jsonl" &
SAMPLER_PID=$!
nvidia-smi dmon -s pucmt -d 1 -o T -f "$EVIDIR/dmon-$ARM.log" >/dev/null 2>&1 &
DMON_PID=$!

CLIENT_ARGS=(--port "$PORT" --phase-set "$PHASE_SET"
  --expected-storage-backend file --storage-dir "$STORAGE_DIR"
  --sglang-commit "$COMMIT" --output-dir "$EVIDIR/results")
if [ "$SMOKE" = "1" ]; then
  CLIENT_ARGS+=(--requests 8 --drain-timeout 120)
fi
if [ "$PROFILE" = "1" ]; then
  CLIENT_ARGS+=(--profile-start-phase serial --profile-end-phase burst16-a
    --profile-output-dir "/data/prof/repro-49221/$ARM")
  if [ "$ARM" = "j1p-profile" ]; then
    CLIENT_ARGS+=(--profile-admin-key kvtier49221)
  fi
fi

echo "== [$ARM] 运行 workload（phase-set=$PHASE_SET） =="
set +e
PYTHONUNBUFFERED=1 "$BENCH_PY" "$WORKLOAD" "${CLIENT_ARGS[@]}" \
  2>&1 | while IFS= read -r line; do
      printf "%s %s\n" "$(date -u +%FT%TZ)" "$line"
    done | tee "$EVIDIR/workload-$ARM.log"
WL_RC=${PIPESTATUS[0]}
set -e

curl -s "http://127.0.0.1:$PORT/metrics" >"$EVIDIR/metrics-final.txt" || true
curl -s "http://127.0.0.1:$PORT/server_info" >"$EVIDIR/server-info-final.json" || true

kill -TERM "$SAMPLER_PID" 2>/dev/null || true
kill -TERM "$DMON_PID" 2>/dev/null || true
echo "== [$ARM] 停止服务 =="
kill -TERM -- -"$SERVER_PID" 2>/dev/null || true
for _ in $(seq 1 24); do kill -0 -- -"$SERVER_PID" 2>/dev/null || break; sleep 5; done
kill -KILL -- -"$SERVER_PID" 2>/dev/null || true
echo "== [$ARM] 完成（workload exit=$WL_RC） =="
exit "$WL_RC"
