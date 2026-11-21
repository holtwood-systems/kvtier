#!/usr/bin/env bash
# #49221 探索臂（0.5B）序列：e1 梯度基线 → e5 判定形状 → e3 timeout → e2 ratio12 → e4 buffer
set -uo pipefail
DIR=/root/src/kvtier/bench/repro-49221
LOGROOT=/root/src/kvtier/bench/repro-31600/evidence/2026-11-20-repro-49221
mkdir -p "$LOGROOT"
ARMS=(e1-gradient e5-judgment e3-timeout e2-ratio12 e4-buffer)
N=${#ARMS[@]}
for i in "${!ARMS[@]}"; do
  ARM=${ARMS[$i]}
  if [ "$i" -lt $((N-1)) ]; then
    HOLD_LOCK=1 bash "$DIR/run_arm.sh" "$ARM" > "$LOGROOT/driver-$ARM.log" 2>&1
  else
    bash "$DIR/run_arm.sh" "$ARM" > "$LOGROOT/driver-$ARM.log" 2>&1
  fi
  echo "[$(date -u +%FT%TZ)] arm $ARM rc=$?" >> "$LOGROOT/sequence-progress.txt"
done
echo "[$(date -u +%FT%TZ)] E-SEQUENCE DONE" >> "$LOGROOT/sequence-progress.txt"
