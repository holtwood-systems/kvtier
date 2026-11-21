#!/usr/bin/env bash
# #49221 全序列：e1 e5 e3 e2 e4（0.5B）→ j1(profile) j2 j3 j4（4B）
# 共享模式（2026-11-21 用户指示）；HOLD_LOCK=1 跨臂持锁，末臂释放。
set -uo pipefail
DIR=/root/src/kvtier/bench/repro-49221
LOGROOT=/root/src/kvtier/bench/repro-31600/evidence/2026-11-20-repro-49221
mkdir -p "$LOGROOT"
run() {
  local ARM=$1; shift || true
  if [ "$ARM" != "j4-timeout" ]; then
    HOLD_LOCK=1 bash "$DIR/run_arm.sh" "$ARM" "$@" > "$LOGROOT/driver-$ARM.log" 2>&1
  else
    bash "$DIR/run_arm.sh" "$ARM" "$@" > "$LOGROOT/driver-$ARM.log" 2>&1
  fi
  echo "[$(date -u +%FT%TZ)] arm $ARM rc=$?" >> "$LOGROOT/sequence-progress.txt"
}
run e1-gradient
run e5-judgment
run e3-timeout
run e2-ratio12
run e4-buffer
run j1-baseline --profile
run j2-baseline-rerun
run j3-ratio12
run j4-timeout
echo "[$(date -u +%FT%TZ)] ALL-SEQUENCE DONE" >> "$LOGROOT/sequence-progress.txt"
