#!/usr/bin/env bash
# 4090 服务器一键状态评估 + 安全修复（Runbook Step 0-2 的可执行化）
# 用法:
#   HOST=<SERVER_HOST> PORT=<PORT> PASSFILE=<PASSFILE> bash scripts/recover_4090_server.sh
# 幂等安全：唯一的变更操作 = 804 修复（compat conf 禁用 + ldconfig），其余全部只读。
# 详见 docs/ops/2026-11-21-server-rebuild-runbook.md
set -euo pipefail

HOST="${HOST:?需要 HOST}"
PORT="${PORT:?需要 PORT}"
PASSFILE="${PASSFILE:?需要 PASSFILE}"

SSH() { sshpass -f "$PASSFILE" ssh -p "$PORT" -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new root@"$HOST" "$@"; }

echo "=== [1/5] 连通性 ==="
SSH 'echo OK; hostname; uptime -s' || { echo "!! SSH 不通或密码错误——先解决凭据"; exit 1; }

echo; echo "=== [2/5] Step 0: CUDA 804 修复（幂等） ==="
SSH '
# 禁用所有处于激活状态的 00-compat 条目（重部署会以新随机后缀再生该文件，
# 不能因旧 .bak-disabled 存在而跳过——2026-11-21 的实测教训）
disabled_any=0
for f in /etc/ld.so.conf.d/00-compat-*.conf; do
  [ -e "$f" ] || continue
  mv "$f" "$f.bak-disabled" && disabled_any=1
done
if [ "$disabled_any" = 1 ]; then
  ldconfig
  echo "已禁用新出现的 compat 条目并刷新 ldcache"
else
  echo "无激活的 compat 条目，跳过"
fi
PY=$(ls /opt/conda/bin/python 2>/dev/null || echo "")
if [ -n "$PY" ]; then
  $PY -c "import torch; print(\"base torch cuda:\", torch.cuda.is_available())" 2>/dev/null || echo "(base torch 不可用，见下步)"
fi'

echo; echo "=== [3/5] GPU 自检 ==="
SSH 'nvidia-smi --query-gpu=name,memory.total,memory.used,driver_version --format=csv,noheader'

echo; echo "=== [4/5] 资产盘点（哪些还在、哪些要重建） ==="
SSH '
for p in /root/.venvs/sglang-server /root/envs/kvtier-main /data/kvtier-main-venv \
         /root/src/kvtier /root/src/sglang-main /data/deps-sglk \
         /root/models/Qwen2.5-0.5B-Instruct /root/models/Qwen3-4B; do
  if [ -e "$p" ]; then echo "存在: $p"; else echo "缺失: $p  -> 需按 Runbook 对应 Step 重建"; fi
done
df -h /data / | tail -2'

echo; echo "=== [5/5] main 环境严格判据（若存在） ==="
SSH 'if [ -x /root/envs/kvtier-main/bin/python ]; then
  timeout 120 /root/envs/kvtier-main/bin/python -c "
import sglang, sgl_kernel, torch
assert torch.cuda.is_available()
x = torch.randn(256, 256, device=\"cuda\"); y = x @ x; torch.cuda.synchronize()
print(\"MAIN_ENV_READY\", sglang.__version__, sgl_kernel.__version__, torch.__version__)
" 2>&1 | tail -1
else echo "main venv 不存在 -> Runbook Step 4 重建"; fi'

echo; echo "完成。按盘点结果执行 Runbook 对应 Step；全部就绪后: 烟测 -> 实验 0 -> #49221。"
