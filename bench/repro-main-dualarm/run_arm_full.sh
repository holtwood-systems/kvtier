#!/usr/bin/env bash
# 实验 0（repro-main-dualarm）单臂全流程驱动：GPU 独占核验 → 清 storage →
# 起服务 → health/G0 → 采样器+dmon → workload(diagnostic) → 停采 → 停服。
# 设计文档：docs/bench-design/2026-11-20-repro-main-dualarm.md §5/§7/§9。
# 用法：run_arm_full.sh <ARM> [num_trials]
#   SMOKE 臂默认 num_trials=2，其余默认 6。
set -euo pipefail

ARM="${1:?用法: run_arm_full.sh <A1|A2|A3|A4|B1|B2|B3|B4|C2|SMOKE> [num_trials]}"
TRIALS="${2:-6}"
if [ "$ARM" = "SMOKE" ] && [ "$#" -lt 2 ]; then TRIALS=2; fi

REPO="${REPO:-/root/src/kvtier}"
PORT="${PORT:-31000}"
STORAGE_ROOT="${STORAGE_ROOT:-/data/hicache-l3/dualarm}"
EVIDENCE_ROOT="${EVIDENCE_ROOT:-$REPO/bench/repro-31600/evidence/2026-11-20-main-dualarm}"
BENCH_PY="$REPO/bench/repro-31600/.venv/bin/python"
WORKLOAD="$REPO/bench/repro-31600/workload.py"
SAMPLER="$REPO/bench/repro-main-dualarm/sampler.py"
RUN_ARM="$REPO/bench/repro-main-dualarm/run_arm.sh"
COMMIT="${COMMIT:-e183c415bdfc5995f6288b1651e1c320c6a74015}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-420}"

# —— GPU 协调协议（/data/gpu-coordination/README.md，共享库）——
MY_LOCK="/data/gpu-coordination/locks/kvtier-main-exp.lock"
LOCK_PURPOSE="实验0 main 双臂 GPU 测量窗口（repro-main-dualarm）"
LOCK_PORTS="$PORT"
source "$(dirname "$(readlink -f "$0")")/gpu_window.sh"

EVIDIR="$EVIDENCE_ROOT/$ARM"
STORAGE_DIR="$STORAGE_ROOT/$ARM"
mkdir -p "$EVIDIR/results"

echo "== [$ARM] GPU 窗口申请（协调协议） =="
acquire_gpu_window

echo "== [$ARM] GPU 独占核验 =="
USED=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1)
PROCS=$(nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader || true)
{
  echo "utc=$(date -u +%FT%TZ) memory_used_mib=$USED"
  echo "compute_apps:"
  echo "${PROCS:-<none>}"
} | tee "$EVIDIR/gpu-baseline.txt"
if [ "${USED:-0}" -gt 4096 ]; then
  echo "GPU 基线 ${USED} MiB 超过 4096 MiB 阈值——不硬跑，释放窗口后退出" >&2
  release_gpu_window
  exit 3
fi
# 窗口随脚本退出清理（顺序臂用 HOLD_LOCK=1 跨调用持有；TTL 3h 兜底）
cleanup() {
  kill -TERM -- -"${SERVER_PID:-0}" 2>/dev/null || true
  if [ -n "${HEARTBEAT_PID:-}" ]; then kill "$HEARTBEAT_PID" 2>/dev/null || true; fi
  [ "${HOLD_LOCK:-0}" = "1" ] || release_gpu_window
}

echo "== [$ARM] G0-5：清空 storage 目录 $STORAGE_DIR =="
rm -rf "$STORAGE_DIR"
mkdir -p "$STORAGE_DIR"

echo "== [$ARM] 启动服务（日志 $EVIDIR/server.log） =="
setsid bash "$RUN_ARM" "$ARM" >"$EVIDIR/server.log" 2>&1 &
SERVER_PID=$!
trap cleanup EXIT

echo "== [$ARM] 等待 /health（至多 ${HEALTH_TIMEOUT}s） =="
ELAPSED=0
until curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "服务进程退出，见 $EVIDIR/server.log" >&2
    tail -30 "$EVIDIR/server.log" >&2
    exit 4
  fi
  if [ "$ELAPSED" -ge "$HEALTH_TIMEOUT" ]; then
    echo "health 等待超时" >&2
    exit 5
  fi
  sleep 5; ELAPSED=$((ELAPSED + 5))
done
echo "health ok after ${ELAPSED}s"

echo "== [$ARM] G0-1：启动日志 host pool 换算核对 =="
grep -m4 "hierarchical KV host pool" "$EVIDIR/server.log" | tee "$EVIDIR/host-pool-log.txt" || true

echo "== [$ARM] 旁证采集：1 Hz 采样器 + nvidia-smi dmon =="
"$BENCH_PY" "$SAMPLER" --port "$PORT" --output "$EVIDIR/samples-$ARM.jsonl" &
SAMPLER_PID=$!
nvidia-smi dmon -s pucmt -d 1 -o T -f "$EVIDIR/dmon-$ARM.log" >/dev/null 2>&1 &
DMON_PID=$!

# 客户端参数（设计 §7）：salt 臂表 + 后端 + 逐臂 storage 目录
case "$ARM" in
  A1|B1|A3|B3|A3W) SALT_ARGS=(--salt-mode per-request) ;;
  A2|B2|C2|A4|B4|SMOKE) SALT_ARGS=(--salt-mode none --evict-content-mode per-request) ;;
  *) echo "未知臂 $ARM" >&2; exit 2 ;;
esac
case "$ARM" in
  A*|C2|SMOKE) BACKEND=file ;;
  B*) BACKEND=fast_file ;;
esac

echo "== [$ARM] 运行 workload（diagnostic，trials=$TRIALS） =="
set +e
PYTHONUNBUFFERED=1 "$BENCH_PY" "$WORKLOAD" \
  --port "$PORT" \
  --num-trials "$TRIALS" \
  --prefix-tokens 4096 --tail-tokens 128 \
  --evict-factor 1.3 \
  --expected-storage-backend "$BACKEND" \
  --storage-dir "$STORAGE_DIR" \
  "${SALT_ARGS[@]}" \
  --post-evict-wait 240 \
  --expect-mode diagnostic \
  --sglang-commit "$COMMIT" \
  --output-dir "$EVIDIR/results" \
  2>&1 | while IFS= read -r line; do
      printf "%s %s\n" "$(date -u +%FT%TZ)" "$line"
    done | tee "$EVIDIR/workload-$ARM.log"
WL_RC=${PIPESTATUS[0]}
set -e
echo "workload exit=$WL_RC"

echo "== [$ARM] 采集最终 /metrics 与 /server_info 全文 =="
curl -s "http://127.0.0.1:$PORT/metrics" >"$EVIDIR/metrics-final.txt" || true
curl -s "http://127.0.0.1:$PORT/server_info" >"$EVIDIR/server-info-final.json" || true

kill -TERM "$SAMPLER_PID" 2>/dev/null || true
kill -TERM "$DMON_PID" 2>/dev/null || true
echo "== [$ARM] 停止服务 =="
kill -TERM -- -"$SERVER_PID" 2>/dev/null || true
for _ in $(seq 1 24); do
  kill -0 -- -"$SERVER_PID" 2>/dev/null || break
  sleep 5
done
kill -KILL -- -"$SERVER_PID" 2>/dev/null || true

echo "== [$ARM] 完成：$EVIDIR（workload exit=$WL_RC） =="
ls -la "$EVIDIR" || true
exit "$WL_RC"
