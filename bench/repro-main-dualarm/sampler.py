#!/usr/bin/env python3
"""1 Hz /metrics 连续采样器（实验 0 V1 水位时序数据源）。

按固定间隔拉取 /metrics，解析 host 水位 gauge 与 prefetch/backup
counter（复用 repro-31600/workload.py 的解析函数，保证与结果包
counter 口径一致），逐行追加 JSONL：

  {"t": <epoch 秒>, "host_used": f, "host_total": f,
   "backuped": f, "hicache_backup": f, "prefetch_hit": f|null,
   "load_back": f, "evicted": f,
   "unfulfilled": {reason: f}, "deferred": {reason: f}}

SIGTERM/Ctrl-C 优雅退出；纯客户端，零 GPU 时间。设计文档：
docs/bench-design/2026-11-20-repro-main-dualarm.md §8/§9 V1。
"""

import argparse
import importlib.util
import json
import signal
import sys
import time
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "repro-31600" / "workload.py"
_SPEC = importlib.util.spec_from_file_location("hicache_l2_workload", _MODULE_PATH)
workload = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(workload)

import requests  # noqa: E402


def snapshot(base_url: str, timeout: float) -> dict:
    text = requests.get(f"{base_url}/metrics", timeout=timeout).text

    def total(name: str):
        return workload.metric_total(text, name)

    return {
        "host_used": total("sglang:hicache_host_used_tokens"),
        "host_total": total("sglang:hicache_host_total_tokens"),
        "backuped": total("sglang:backuped_tokens_total"),
        "hicache_backup": total("sglang:hicache_backup_tokens_total"),
        "prefetch_hit": total("sglang:storage_prefetch_hit_tokens_total"),
        "load_back": total("sglang:load_back_tokens_total"),
        "evicted": total("sglang:evicted_tokens_total"),
        "unfulfilled": workload.metric_reason_totals(
            text, "sglang:storage_prefetch_unfulfilled_tokens_total"
        ),
        "deferred": workload.metric_reason_totals(
            text, "sglang:storage_prefetch_deferred_tokens_total"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()

    base_url = f"http://127.0.0.1:{args.port}"
    stop = False

    def _stop(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("a", encoding="utf-8") as fh:
        while not stop:
            try:
                row = {"t": time.time(), **snapshot(base_url, 5.0)}
            except requests.RequestException as exc:
                row = {"t": time.time(), "error": str(exc)}
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
