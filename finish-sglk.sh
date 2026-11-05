#!/usr/bin/env bash
# sgl-kernel 编译自愈循环：ninja -k0 低并发扫完全部目标；
# 每轮因 cgroup OOM(96GiB) 残留的失败目标在下一轮补编。
# 成功后 wheel 落 dist/ 并自动装回 sglang-server venv。
# 用法: nohup bash finish-sglk.sh > /tmp/sglk-loop.log 2>&1 &
set -u
cd /data/open-infra-ai/kvtier/third_party/sglang/python/sglang/kernels/aot
BUILD=build
VENV=/root/.venvs/sglang-server
D=/data/open-infra-ai/kvtier/deps-sglk
export CMAKE_ARGS="-DENABLE_BELOW_SM90=ON -DSGL_KERNEL_COMPILE_THREADS=2 -DSGL_KERNEL_ENABLE_FA3=OFF \
-DFETCHCONTENT_SOURCE_DIR_REPO-CUTLASS=$D/cutlass-57e3cfb47a2d9e0d46eb6335c3dc411498efa198 \
-DFETCHCONTENT_SOURCE_DIR_REPO-FMT=$D/fmt-553ec11ec06fbe0beebfbb45f9dc3c9eabd83d28 \
-DFETCHCONTENT_SOURCE_DIR_REPO-TRITON=$D/triton-3.7.1 \
-DFETCHCONTENT_SOURCE_DIR_REPO-FLASHINFER=$D/flashinfer-bc29697ba20b7e6bdb728ded98f04788e16ee021 \
-DFETCHCONTENT_SOURCE_DIR_REPO-FLASH-ATTENTION=$D/sgl-flash-attn-f89bc2306632d1ec5f97b014dded4254f5b4a907 \
-DFETCHCONTENT_SOURCE_DIR_REPO-FLASHMLA=$D/FlashMLA-05e26647fe840b8baedae486c2d86d5ce4efeb7c \
-DFETCHCONTENT_SOURCE_DIR_REPO-FLASHMLA-CUTLASS=$D/FlashMLA-05e26647fe840b8baedae486c2d86d5ce4efeb7c/csrc/cutlass"
export CMAKE_POLICY_VERSION_MINIMUM=3.5
# /tmp 在 40GB overlay 上会写爆（nvcc 中间文件 27GB+），挪到 /data
export TMPDIR=/data/tmp-sglk
mkdir -p "$TMPDIR"

for round in $(seq 1 40); do
  echo "===== round $round $(date -u +%H:%M:%S) ====="
  if [ ! -f "$BUILD/build.ninja" ]; then
    # 首配（或 build 目录丢失）走完整 wheel 构建
    MAX_JOBS=6 CMAKE_BUILD_PARALLEL_LEVEL=6 uv build --wheel \
      -Cbuild-dir=$BUILD . --verbose --no-build-isolation \
      --python "$VENV/bin/python" && break
  else
    "$VENV/bin/ninja" -C "$BUILD" -k 0 -j6
    # ninja -k0 收尾后若所有 target 完成则返回 0
    if [ $? -eq 0 ]; then
      # 重新走 scikit-build 完成 install+wheel 打包（ninja 已全绿会秒过）
      MAX_JOBS=6 CMAKE_BUILD_PARALLEL_LEVEL=6 uv build --wheel \
        -Cbuild-dir=$BUILD . --verbose --no-build-isolation \
        --python "$VENV/bin/python" && break
    fi
  fi
  sleep 5
done

ls -la dist/ 2>/dev/null
if ls dist/*.whl >/dev/null 2>&1; then
  "$VENV/bin/python" -m pip install --force-reinstall dist/*.whl && \
  "$VENV/bin/python" -c "import sgl_kernel; print('SGK_OK')" && \
  echo "FINISH_SGLK_DONE" && exit 0
fi
echo "FINISH_SGLK_FAILED"
exit 1
