#!/usr/bin/env python3
"""V1 分阶段水位时序出图（实验 0 双臂，离线、零 GPU）。

用法：plot_v1.py <arm-evidence-dir> [--output <png>]

输入（run_arm_full.sh 产物）：
  samples-<arm>.jsonl   1 Hz 采样器（水位/双累计/unfulfilled）
  workload-<arm>.log    带 UTC 时间戳的相位标记与 trial 行
  results/results-*.json（trial R storage 命中着色）
图面（设计 §9 V1）：x=墙钟；W/E/M 相位边界竖虚线；通道 1 host 水位
（total 虚线）；通道 2 backuped vs hicache_backup 双累计（间隙=丢弃/在途）；
通道 3 unfulfilled 的 host_capacity/dropped/below_threshold 阶梯；
通道 4 trial 完成时刻散点（R 绿=storage>0 红=0，P 蓝）。

注：trial 散点取 workload 日志行时间戳（该行在 R、P 都完成后打印），
标记的是 trial 完成时刻而非单请求时刻——已在实验 README 的偏离项中
如实标注。
"""

import argparse
import glob
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

_MARKERS = {
    "W_start": "== W",
    "W_end": "== 等待",
    "E_start": "== E",
    "E_end": "post_evict",
    "M_start": "== M",
}


def parse_log_events(path: Path):
    events = {}
    trials = []
    if not path.exists():
        return events, trials
    for line in path.read_text(encoding="utf-8").splitlines():
        stamp, _, rest = line.partition(" ")
        try:
            t = datetime.fromisoformat(stamp).timestamp()
        except ValueError:
            continue
        if "== W" in rest and "W_start" not in events:
            events["W_start"] = t
        elif rest.startswith("== 等待") and "W_end" not in events:
            events["W_start"] = events.get("W_start", t)
            events["W_end"] = t
        elif "== E" in rest and "E_start" not in events and "个独立请求" in rest:
            events["E_start"] = t
        elif "== M" in rest and "M_start" not in events:
            events["M_start"] = t
        elif rest.startswith("trial ") and "cache=" in rest:
            trials.append({"t": t, "line": rest})
    return events, trials


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("arm_dir", type=Path)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    arm_dir = args.arm_dir
    arm = arm_dir.name
    samples_path = arm_dir / f"samples-{arm}.jsonl"
    log_path = arm_dir / f"workload-{arm}.log"
    results_files = sorted(glob.glob(str(arm_dir / "results" / "results-*.json")))

    rows = []
    for line in samples_path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not rows:
        print("无采样数据", file=sys.stderr)
        return 1
    t0 = rows[0]["t"]
    ts = [r["t"] - t0 for r in rows]

    events, log_trials = parse_log_events(log_path)
    events = {k: v - t0 for k, v in events.items()}

    r_hits = []
    if results_files:
        record = json.loads(Path(results_files[-1]).read_text(encoding="utf-8"))
        for trial in record.get("trials") or []:
            details = ((trial.get("results") or {}).get("R") or {}).get(
                "cached_tokens_details"
            ) or {}
            r_hits.append((details.get("storage", 0) or 0) > 0)

    fig, axes = plt.subplots(4, 1, figsize=(14, 12), sharex=True)
    fig.suptitle(f"V1 timeline — {arm}")

    # 通道 1：host 水位
    ax = axes[0]
    used = [r.get("host_used") for r in rows]
    total = [r.get("host_total") for r in rows]
    ax.plot(ts, used, label="host_used", color="tab:blue")
    ax.plot(ts, total, label="host_total", ls="--", color="tab:gray")
    ax.set_ylabel("host tokens")
    ax.legend(loc="upper left")

    # 通道 2：backup 双累计
    ax = axes[1]
    ax.plot(ts, [r.get("backuped") for r in rows], label="backuped (acked)", color="tab:green")
    ax.plot(ts, [r.get("hicache_backup") for r in rows], label="hicache_backup (enqueued)", color="tab:orange", ls="--")
    ax.set_ylabel("tokens (cumulative)")
    ax.legend(loc="upper left")

    # 通道 3：unfulfilled 阶梯
    ax = axes[2]
    colors = {
        "host_capacity": "tab:red",
        "dropped": "tab:purple",
        "below_threshold": "tab:brown",
    }
    for reason, color in colors.items():
        series = [((r.get("unfulfilled") or {}).get(reason)) for r in rows]
        if any(v is not None for v in series):
            ax.plot(ts, series, label=reason, color=color, drawstyle="steps-post")
    ax.set_ylabel("unfulfilled tokens")
    ax.legend(loc="upper left")

    # 通道 4：trial 完成散点
    ax = axes[3]
    for idx, trial in enumerate(log_trials):
        hit = r_hits[idx] if idx < len(r_hits) else None
        color = "tab:green" if hit else ("tab:red" if hit is False else "tab:olive")
        ax.axvline(trial["t"] - t0, color=color, alpha=0.5, lw=1)
    ax.set_ylabel("trials (R hit=green / miss=red)")
    ax.set_yticks([])

    for ax in axes:
        for name, t in events.items():
            ax.axvline(t, color="black", ls=":", lw=1, alpha=0.7)
            ax.text(t, ax.get_ylim()[1], f" {name}", fontsize=8, va="top")
    axes[-1].set_xlabel(f"seconds since {datetime.fromtimestamp(t0, tz=timezone.utc).isoformat()}")

    output = args.output or (arm_dir / f"v1-{arm}.png")
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    fig.savefig(output, dpi=130)
    print(f"saved {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
