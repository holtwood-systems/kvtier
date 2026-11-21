#!/usr/bin/env python3
"""实验 0（repro-main-dualarm）离线判定器：读各臂结果包，按设计文档
（docs/bench-design/2026-11-20-repro-main-dualarm.md）§3/§5 的 G0/G1/G2
规则计算 arm_verdict 判定输入。

输入：证据根目录（默认
bench/repro-31600/evidence/2026-11-20-main-dualarm），每臂子目录含：
  results/results-*.json   workload 结果包
  host-pool-log.txt        启动日志 host pool 行
  samples-<arm>.jsonl      1 Hz 采样器（host 水位门禁）
  workload-<arm>.log       带 UTC 时间戳的相位标记（定位 M 起点）
输出：JSON（stdout 或 --output）——逐臂判定输入 + 门禁逐项核对。

纯离线、零 GPU。
"""

import argparse
import glob
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

PREFIX_TOKENS = 4224  # 4096 + 128；驻留分级基准
L2_FRACTION = 0.5
L3_FRACTION = 0.9
ARM_EXPECT = {
    # arm: (ratio, host_pool_expected_tokens)
    "A1": (0.12, 43024),
    "A2": (0.12, 43024),
    "A3": (0.12, 43024),
    "A4": (0.12, 43024),
    "B1": (0.12, 43024),
    "B2": (0.12, 43024),
    "B3": (0.12, 43024),
    "B4": (0.12, 43024),
    "C2": (0.48, 172095),
    "SMOKE": (0.05, 17927),
}
DEVICE_CAPACITY = 358532
UNFULFILLED_PREFIX = 'sglang:storage_prefetch_unfulfilled_tokens_total{reason="'


def load_latest_results(evidir: Path):
    files = sorted(glob.glob(str(evidir / "results" / "results-*.json")))
    if not files:
        return None
    return json.loads(Path(files[-1]).read_text(encoding="utf-8"))


def parse_host_pool_tokens(evidir: Path, arm: str):
    path = evidir / "host-pool-log.txt"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    match = re.search(r"hierarchical KV host pool: (\d+) tokens", text)
    return int(match.group(1)) if match else None


def parse_m_start_epoch(evidir: Path, arm: str):
    path = evidir / f"workload-{arm}.log"
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if "== M" in line:
            stamp = line.split(" ", 1)[0]
            try:
                return datetime.fromisoformat(stamp).timestamp()
            except ValueError:
                return None
    return None


def host_used_at(samples_path: Path, m_start: float):
    if not samples_path.exists() or m_start is None:
        return None
    best = None
    for line in samples_path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = row.get("t")
        if t is None or t > m_start:
            continue
        if row.get("host_used") is not None and row.get("host_total"):
            best = row
    return best


def stage_reason_delta(record: dict, reason: str, start: str, end: str):
    stages = ((record.get("metrics") or {}).get("stage_counters") or {})
    start_snap = stages.get(start) or {}
    end_snap = stages.get(end) or {}
    key = f'{UNFULFILLED_PREFIX}{reason}"}}'
    if key not in end_snap:
        return None
    return float(end_snap[key]) - float(start_snap.get(key) or 0.0)


def arm_verdict(arm: str, evidir: Path):
    record = load_latest_results(evidir)
    if record is None:
        return {"arm": arm, "error": "缺少 results 包"}
    out = {"arm": arm, "results_file": sorted(glob.glob(str(evidir / "results" / "results-*.json")))[-1]}

    # G0：配置与池换算
    server = record.get("server") or {}
    ratio, pool_expect = ARM_EXPECT[arm]
    pool_tokens = parse_host_pool_tokens(evidir, arm)
    capacity = ((server.get("memory_usage") or {}) or {}).get("token_capacity")
    g0 = {
        "host_pool_tokens": pool_tokens,
        "host_pool_expected": pool_expect,
        "host_pool_within_2pct": (
            pool_tokens is not None
            and abs(pool_tokens - pool_expect) <= 0.02 * pool_expect
        ),
        "device_token_capacity": capacity,
        "device_capacity_within_2pct": (
            isinstance(capacity, (int, float))
            and abs(capacity - DEVICE_CAPACITY) <= 0.02 * DEVICE_CAPACITY
        ),
        "backend": server.get("hicache_storage_backend"),
        "host_mode": server.get("hicache_host_memory_mode"),
        "write_policy": server.get("hicache_write_policy"),
        "tp": server.get("tp_size"),
        "commit": (record.get("args") or {}).get("sglang_commit"),
    }
    out["g0"] = g0

    # R 覆盖率与驻留分级（D1/D2 输入）
    trials = record.get("trials") or []
    storages = [
        ((t.get("results") or {}).get("R") or {}).get("cached_tokens_details", {}).get("storage", 0)
        for t in trials
    ]
    hosts = [
        ((t.get("results") or {}).get("R") or {}).get("cached_tokens_details", {}).get("host", 0)
        for t in trials
    ]
    p_cold = [
        sum((((t.get("results") or {}).get("P") or {}).get("cached_tokens_details") or {}).values()) == 0
        for t in trials
    ]
    med = statistics_median(storages) if storages else None
    out["r_coverage"] = {
        "n": len(storages),
        "storage_values": storages,
        "host_values": hosts,
        "hit_trials": sum(1 for s in storages if s > 0),
        "storage_median": med,
        "residence_grade": residence_grade(med),
        "p_all_cold": all(p_cold) if p_cold else None,
    }

    # M 阶段（post_drain→post_measure）unfulfilled 增量与 hit tokens
    m_delta = {}
    for reason in ("host_capacity", "dropped", "below_threshold", "cache_admission_shortfall"):
        m_delta[reason] = stage_reason_delta(record, reason, "post_drain", "post_measure")
    stages = (record.get("metrics") or {}).get("stage_counters") or {}
    hit_start = (stages.get("post_drain") or {}).get("sglang:storage_prefetch_hit_tokens_total")
    hit_end = (stages.get("post_measure") or {}).get("sglang:storage_prefetch_hit_tokens_total")
    m_delta["prefetch_hit_tokens"] = (
        float(hit_end) - float(hit_start or 0.0) if hit_end is not None else None
    )
    out["m_phase_deltas"] = m_delta

    # M 开始时 host 水位（samples + workload 时间戳）
    m_start = parse_m_start_epoch(evidir, arm)
    sample = host_used_at(evidir / f"samples-{arm}.jsonl", m_start)
    out["host_water_at_m_start"] = (
        {
            "m_start_epoch": m_start,
            "host_used": sample.get("host_used"),
            "host_total": sample.get("host_total"),
            "used_ratio": (
                sample.get("host_used") / sample.get("host_total")
                if sample and sample.get("host_total")
                else None
            ),
        }
        if sample
        else {"m_start_epoch": m_start, "note": "无可用样本"}
    )

    # 全程 unfulfilled（before→post_measure，诊断参考）
    deltas = (record.get("metrics") or {}).get("counter_deltas") or {}
    out["run_total_unfulfilled"] = {
        key.split('reason="')[1].rstrip('"}'): value
        for key, value in deltas.items()
        if key.startswith(UNFULFILLED_PREFIX)
    }

    # G1（仅 cache 等压臂 A1/A2/B1/B2 适用）：≥5/6 storage>0 且中位 ≥L2
    # 且三类 M 增量各 <256 且 M 开始 host_used ≤90%
    if arm in ("A1", "A2", "B1", "B2"):
        out["g1_check"] = {
            "coverage_ge_5of6": out["r_coverage"]["hit_trials"] >= 5 and len(storages) == 6,
            "median_ge_l2": med is not None and med >= L2_FRACTION * PREFIX_TOKENS,
            "m_deltas_lt_256": all(
                (m_delta[r] is None or m_delta[r] < 256)
                for r in ("host_capacity", "dropped", "below_threshold")
            ),
            "host_used_le_90pct": (
                out["host_water_at_m_start"].get("used_ratio") is not None
                and out["host_water_at_m_start"]["used_ratio"] <= 0.90
            ),
        }
    return out


def statistics_median(values):
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n == 0:
        return None
    if n % 2:
        return float(sorted_vals[n // 2])
    return (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2.0


def residence_grade(med):
    if med is None:
        return None
    if med >= L3_FRACTION * PREFIX_TOKENS:
        return "L3_full"
    if med >= L2_FRACTION * PREFIX_TOKENS:
        return "L2_majority"
    if med > 0:
        return "L1_path_exists"
    return "L0_none"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent
        / "repro-31600"
        / "evidence"
        / "2026-11-20-main-dualarm",
    )
    parser.add_argument("--arms", nargs="*", default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    arms = args.arms or [a for a in ARM_EXPECT if (args.evidence_root / a).is_dir()]
    verdicts = [arm_verdict(arm, args.evidence_root / arm) for arm in arms]
    payload = {
        "benchmark": "repro-main-dualarm-verdict",
        "prefix_tokens": PREFIX_TOKENS,
        "l2_threshold": L2_FRACTION * PREFIX_TOKENS,
        "l3_threshold": L3_FRACTION * PREFIX_TOKENS,
        "verdicts": verdicts,
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
