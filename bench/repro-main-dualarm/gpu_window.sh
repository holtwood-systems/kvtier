#!/usr/bin/env bash
# GPU 协调协议共享库（/data/gpu-coordination/README.md）。
# 提供：gpu_busy / others_lock_active / acquire_gpu_window / release_gpu_window
# 用法（source 后）：
#   MY_LOCK=/data/gpu-coordination/locks/kvtier-main-exp.lock
#   source gpu_window.sh   # 定义函数（MY_LOCK 需先设好）
#   acquire_gpu_window     # 等他人锁/GPU 空闲 → 挂自己的锁 + 30min 心跳
#   release_gpu_window     # 删锁 + 停心跳
# 说明：本容器重部署后底噪 ~4 MiB；>1GB 视为对方会话在跑（其 PID 跨
# namespace 不可见）。绝不 kill 他人进程；等待超时在留言板记录后继续等。

LOCK_DIR="/data/gpu-coordination/locks"
GPU_BUSY_MIB="${GPU_BUSY_MIB:-1024}"
LOCK_WAIT_TIMEOUT_MIN="${LOCK_WAIT_TIMEOUT_MIN:-180}"
LOCK_TTL_HOURS="${LOCK_TTL_HOURS:-3}"
# 共享模式（2026-11-21 用户指示）：不再等他人锁/独占空闲；改为显存硬门禁
# ——free ≥ NEED_MIB + 4096 余量才开跑（连续 3 次不足则退出报告）。
# 锁声明 gpu_need=shared_<NEED>GB；共居事实写入 gpu-baseline 记录。
WINDOW_MODE="${WINDOW_MODE:-exclusive}"
NEED_MIB="${NEED_MIB:-20000}"

gpu_busy() {
  local used procs
  used=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')
  procs=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | grep -c . || true)
  [ "${used:-0}" -gt "$GPU_BUSY_MIB" ] || [ "${procs:-0}" -gt 0 ]
}

gpu_free_mib() {
  local total used
  total=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')
  used=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')
  echo $(( ${total:-0} - ${used:-0} ))
}

others_lock_active() {
  local f now ttl ttl_s age
  now=$(date +%s)
  for f in "$LOCK_DIR"/*.lock; do
    [ -e "$f" ] || continue
    [ "$f" = "$MY_LOCK" ] && continue
    ttl=$(grep -E '^ttl_hours:' "$f" | awk '{print $2}')
    age=$(( now - $(stat -c %Y "$f") ))
    ttl_s=$(( ${ttl:-2} * 3600 ))
    if [ "$age" -lt "$ttl_s" ]; then
      echo "活跃锁: $f (age=${age}s ttl=${ttl_s}s)"
      return 0
    fi
  done
  return 1
}

acquire_gpu_window() {
  local waited=0 line
  if [ "${WINDOW_MODE:-exclusive}" = "shared" ]; then
    # 共享模式：显存硬门禁（free ≥ NEED + 4GB 余量），3 次 × 5 分钟不足则退出
    local tries=0 free
    while :; do
      if [ -e "$MY_LOCK" ]; then
        ( while [ -e "$MY_LOCK" ]; do touch "$MY_LOCK"; sleep 1800; done ) &
        HEARTBEAT_PID=$!
        return 0
      fi
      free=$(gpu_free_mib)
      if [ "$free" -ge $(( NEED_MIB + 4096 )) ]; then
        mkdir -p "$LOCK_DIR"
        cat > "$MY_LOCK" <<EOF
owner: kvtier-main-experiment-session
purpose: ${LOCK_PURPOSE:-kvtier main 实验 GPU 测量窗口}
since: $(date -u +%FT%TZ)
ttl_hours: ${LOCK_TTL_HOURS}
ports: ${LOCK_PORTS:-31000}
gpu_need: shared_$(( NEED_MIB / 1024 ))GB
note: 用户指示与 career-prep-7b 共居（其锁声明 exclusive，2026-11-21 用户裁定共享执行）
EOF
        ( while [ -e "$MY_LOCK" ]; do touch "$MY_LOCK"; sleep 1800; done ) &
        HEARTBEAT_PID=$!
        echo "已挂共享锁 $MY_LOCK（need=${NEED_MIB}MiB, free=${free}MiB）"
        return 0
      fi
      tries=$((tries + 1))
      if [ "$tries" -ge 3 ]; then
        echo "共享模式显存门禁连续 3 次不足：free=${free}MiB < ${NEED_MIB}+4096" >&2
        return 1
      fi
      echo "显存不足（free=${free}MiB），5 分钟后重查（$tries/3）"
      sleep 300
    done
  fi
  while :; do
    if [ -e "$MY_LOCK" ]; then
      # 顺序臂复用已持有的锁：只重启心跳
      ( while [ -e "$MY_LOCK" ]; do touch "$MY_LOCK"; sleep 1800; done ) &
      HEARTBEAT_PID=$!
      return 0
    fi
    line=$(others_lock_active || true)
    if [ -n "$line" ]; then
      echo "等待他人锁：$line"
      sleep 300; waited=$((waited + 5))
    elif gpu_busy; then
      echo "GPU 忙（对方实验在跑）：$(nvidia-smi --query-gpu=memory.used --format=csv,noheader | head -1)"
      sleep 300; waited=$((waited + 5))
    else
      mkdir -p "$LOCK_DIR"
      cat > "$MY_LOCK" <<EOF
owner: kvtier-main-experiment-session
purpose: ${LOCK_PURPOSE:-kvtier main 实验 GPU 测量窗口}
since: $(date -u +%FT%TZ)
ttl_hours: ${LOCK_TTL_HOURS}
ports: ${LOCK_PORTS:-31000}
gpu_need: exclusive
EOF
      ( while [ -e "$MY_LOCK" ]; do touch "$MY_LOCK"; sleep 1800; done ) &
      HEARTBEAT_PID=$!
      echo "已挂锁 $MY_LOCK"
      return 0
    fi
    if [ "$waited" -ge "$LOCK_WAIT_TIMEOUT_MIN" ]; then
      echo "等待锁/窗口超 ${LOCK_WAIT_TIMEOUT_MIN} 分钟——留言板记录后继续轮询" >&2
      printf '\n%s kvtier-main 实验等待窗口超时（%s min），继续轮询。\n' \
        "$(date -u +%FT%TZ)" "$LOCK_WAIT_TIMEOUT_MIN" \
        >> /data/gpu-coordination/messages/2026-11-21-from-kvtier-main-session.md
      waited=0
    fi
  done
}

release_gpu_window() {
  rm -f "$MY_LOCK"
  if [ -n "${HEARTBEAT_PID:-}" ]; then
    kill "$HEARTBEAT_PID" 2>/dev/null || true
  fi
}
