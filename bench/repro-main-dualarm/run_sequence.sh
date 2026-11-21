#!/usr/bin/env bash
# 实验 0 正式臂序列：优先臂 A1 A2 A3 C2 B1 B2，预算允许补 A4 B3 B4。
# HOLD_LOCK=1 跨臂持锁（末臂释放）；每臂输出独立 driver 日志，失败不阻断后续臂。
set -uo pipefail
DIR=/root/src/kvtier/bench/repro-main-dualarm
LOGROOT=/root/src/kvtier/bench/repro-31600/evidence/2026-11-20-main-dualarm
mkdir -p "$LOGROOT"
ARMS=(A1 A2 A3 C2 B1 B2 A4 B3 B4)
N=${#ARMS[@]}
for i in "${!ARMS[@]}"; do
  ARM=${ARMS[$i]}
  if [ "$i" -lt $((N-1)) ]; then
    HOLD_LOCK=1 bash "$DIR/run_arm_full.sh" "$ARM" 6 \
      > "$LOGROOT/driver-$ARM.log" 2>&1
  else
    bash "$DIR/run_arm_full.sh" "$ARM" 6 \
      > "$LOGROOT/driver-$ARM.log" 2>&1
  fi
  RC=$?
  echo "[$(date -u +%FT%TZ)] arm $ARM rc=$RC" >> "$LOGROOT/sequence-progress.txt"
done
echo "[$(date -u +%FT%TZ)] SEQUENCE DONE" >> "$LOGROOT/sequence-progress.txt"
