#!/usr/bin/env python3
"""Validate a HiCache L2 benchmark result package before using its data."""

import argparse
import json
import math
import statistics
import sys
from pathlib import Path


_REQUIRED_COUNTERS = (
    "sglang:hicache_backup_tokens_total",
    "sglang:evicted_tokens_total",
    "sglang:load_back_tokens_total",
)
_EXPECTED_SERVER_CONFIG = {
    "tp_size": 1,
    "dp_size": 1,
    "pp_size": 1,
    "attn_cp_size": 1,
    "speculative_algorithm": None,
    "enable_hierarchical_cache": True,
    "hicache_host_memory_mode": "cache",
    "enable_metrics": True,
    "enable_cache_report": True,
}
_FAILURE_CLASSES = {
    "config",
    "not_backed_up",
    "index_evicted",
    "storage_read_failed",
    "transfer_anomaly",
    "unknown",
}


class ValidationError(ValueError):
    """Raised when a result package is structurally or semantically invalid."""


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def _number(value: object, label: str, *, positive: bool = False) -> float:
    _expect(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value),
        f"{label} 必须是有限数字",
    )
    if positive:
        _expect(value > 0, f"{label} 必须大于 0")
    return float(value)


def _cached_details(result: dict, label: str) -> dict[str, int]:
    details = result.get("cached_tokens_details")
    _expect(isinstance(details, dict), f"{label} 缺少 cached_tokens_details")
    normalized = {}
    for key in ("device", "host", "storage"):
        value = details.get(key)
        _number(value, f"{label}.cached_tokens_details.{key}")
        _expect(int(value) == value and value >= 0, f"{label}.{key} 必须是非负整数")
        normalized[key] = int(value)
    return normalized


def _validate_generation(result: dict, label: str, prompt_len: int) -> dict[str, int]:
    _expect(isinstance(result, dict), f"{label} 必须是对象")
    _expect(result.get("input_token_count") == prompt_len, f"{label} token 数不一致")
    _number(result.get("ttft_s"), f"{label}.ttft_s", positive=True)
    total_s = _number(result.get("total_s"), f"{label}.total_s", positive=True)
    _expect(total_s >= result["ttft_s"], f"{label}.total_s 不能小于 ttft_s")
    output_events = result.get("output_token_events")
    _expect(
        isinstance(output_events, int) and output_events > 0,
        f"{label} 没有有效输出 token 事件",
    )
    details = _cached_details(result, label)
    _expect(
        sum(details.values()) <= prompt_len,
        f"{label} 缓存 token 数超过输入 token 数",
    )
    return details


def _validate_commit(value: object) -> None:
    _expect(isinstance(value, str) and len(value) == 40, "sglang-commit 必须是 40 位 SHA")
    _expect(
        all(char in "0123456789abcdefABCDEF" for char in value),
        "sglang-commit 必须是十六进制 SHA",
    )


def _validate_complete(record: dict) -> dict:
    args = record.get("args")
    _expect(isinstance(args, dict), "complete 结果缺少 args")
    _validate_commit(args.get("sglang_commit"))

    num_trials = args.get("num_trials")
    _expect(isinstance(num_trials, int) and num_trials > 0, "num_trials 无效")
    prefix_tokens = args.get("prefix_tokens")
    tail_tokens = args.get("tail_tokens")
    _expect(
        isinstance(prefix_tokens, int)
        and isinstance(tail_tokens, int)
        and prefix_tokens > 0
        and tail_tokens > 0,
        "prefix_tokens 或 tail_tokens 无效",
    )
    evict_request_tokens = args.get("evict_request_tokens")
    _expect(
        isinstance(evict_request_tokens, int) and evict_request_tokens > 0,
        "evict_request_tokens 无效",
    )
    prompt_len = prefix_tokens + tail_tokens
    min_host_hit_ratio = _number(
        args.get("min_host_hit_ratio"), "min_host_hit_ratio"
    )
    _expect(0 < min_host_hit_ratio <= 1, "min_host_hit_ratio 必须位于 (0, 1]")

    server = record.get("server")
    _expect(isinstance(server, dict), "complete 结果缺少 server")
    for key, expected in _EXPECTED_SERVER_CONFIG.items():
        _expect(server.get(key) == expected, f"server.{key} 不符合基准边界")
    expected_backend = args.get("expected_storage_backend")
    _expect(
        server.get("hicache_storage_backend") == expected_backend,
        f"server.hicache_storage_backend={server.get('hicache_storage_backend')!r} "
        f"与 args.expected_storage_backend={expected_backend!r} 不一致",
    )
    _expect(
        server.get("hicache_write_policy") == "write_through",
        "write policy 不符合基准边界",
    )
    memory_usage = server.get("memory_usage")
    _expect(isinstance(memory_usage, dict), "server 缺少 memory_usage")
    _number(memory_usage.get("token_capacity"), "device token_capacity", positive=True)

    eviction = record.get("eviction")
    _expect(isinstance(eviction, dict), "complete 结果缺少 eviction")
    evict_requests = eviction.get("num_requests")
    _expect(
        isinstance(evict_requests, int) and evict_requests > 0,
        "eviction.num_requests 无效",
    )
    _expect(
        eviction.get("request_tokens") == evict_request_tokens,
        "eviction.request_tokens 与 args 不一致",
    )

    phases = record.get("phases")
    _expect(isinstance(phases, dict), "complete 结果缺少 phases")
    writes = phases.get("W")
    evictions = phases.get("E")
    _expect(
        isinstance(writes, list) and len(writes) == num_trials,
        "W 阶段数量不一致",
    )
    _expect(
        isinstance(evictions, list) and len(evictions) == evict_requests,
        "E 阶段数量不一致",
    )
    for index, result in enumerate(writes):
        _validate_generation(result, f"W[{index}]", prompt_len)
    for index, result in enumerate(evictions):
        _validate_generation(result, f"E[{index}]", evict_request_tokens)

    plan = record.get("measurement_plan")
    trials = record.get("trials")
    _expect(
        isinstance(plan, list) and len(plan) == num_trials,
        "measurement_plan 数量不一致",
    )
    _expect(
        isinstance(trials, list) and len(trials) == num_trials,
        "trials 数量不一致",
    )
    ratios = []
    for index, (order, trial) in enumerate(zip(plan, trials)):
        _expect(order in (["R", "P"], ["P", "R"]), f"trial {index} 顺序无效")
        _expect(isinstance(trial, dict), f"trial {index} 必须是对象")
        _expect(trial.get("order") == order, f"trial {index} 顺序记录不一致")
        results = trial.get("results")
        _expect(isinstance(results, dict), f"trial {index} 缺少 results")
        _expect(set(results) == {"R", "P"}, f"trial {index} 必须同时包含 R/P")
        r_details = _validate_generation(results["R"], f"trial {index}.R", prompt_len)
        p_details = _validate_generation(results["P"], f"trial {index}.P", prompt_len)
        _expect(
            r_details["host"] + r_details["storage"]
            >= math.floor(prompt_len * min_host_hit_ratio),
            f"trial {index}.R 的分层命中率不足",
        )
        _expect(sum(p_details.values()) == 0, f"trial {index}.P 不是冷 prefill")
        ratio = results["R"]["ttft_s"] / results["P"]["ttft_s"]
        _number(trial.get("ttft_ratio_r_over_p"), f"trial {index}.ratio", positive=True)
        _expect(
            math.isclose(trial["ttft_ratio_r_over_p"], ratio, rel_tol=1e-9),
            f"trial {index}.ratio 与原始 TTFT 不一致",
        )
        ratios.append(ratio)

    metrics = record.get("metrics")
    _expect(isinstance(metrics, dict), "complete 结果缺少 metrics")
    deltas = metrics.get("counter_deltas")
    _expect(isinstance(deltas, dict), "complete 结果缺少 counter_deltas")
    for name in _REQUIRED_COUNTERS:
        _number(deltas.get(name), f"metrics.counter_deltas.{name}", positive=True)

    if expected_backend is not None:
        stage_counters = metrics.get("stage_counters")
        _expect(
            isinstance(stage_counters, dict)
            and isinstance(stage_counters.get("post_write"), dict)
            and isinstance(stage_counters.get("post_evict"), dict),
            "L3 complete 包缺少分阶段 counter 快照（metrics.stage_counters）",
        )
        backuped_delta = (
            (stage_counters["post_evict"] or {}).get("sglang:backuped_tokens_total")
            or 0
        ) - ((metrics.get("counters_before") or {}).get(
            "sglang:backuped_tokens_total"
        ) or 0)
        snapshots = record.get("storage_snapshots")
        bin_growth = None
        if isinstance(snapshots, dict):
            def _bin_bytes(entries):
                if not isinstance(entries, list):
                    return None
                return sum(
                    item["size"]
                    for item in entries
                    if isinstance(item, dict) and str(item.get("name", "")).endswith(".bin")
                )
            pre = _bin_bytes(snapshots.get("pre_write"))
            post = _bin_bytes(snapshots.get("post_evict"))
            if pre is not None and post is not None:
                bin_growth = post - pre
        _expect(
            backuped_delta > 0 or (bin_growth is not None and bin_growth > 0),
            "L3 complete 包缺少 L3 落盘证据：backuped_tokens_total 无增量 "
            "且 .bin 无增长——不能仅凭 cached_tokens_details.storage 声称经过 L3",
        )

    summary = record.get("summary")
    _expect(isinstance(summary, dict), "complete 结果缺少 summary")
    reload_summary = summary.get("reload")
    cold_summary = summary.get("cold_prefill")
    _expect(isinstance(reload_summary, dict), "summary.reload 缺失")
    _expect(isinstance(cold_summary, dict), "summary.cold_prefill 缺失")
    _expect(reload_summary.get("n") == num_trials, "reload 摘要数量不一致")
    _expect(cold_summary.get("n") == num_trials, "cold_prefill 摘要数量不一致")
    median = _number(
        summary.get("paired_ttft_ratio_median"),
        "paired_ttft_ratio_median",
        positive=True,
    )
    _expect(math.isclose(median, statistics.median(ratios), rel_tol=1e-9), "summary ratio 不一致")
    _expect(
        summary.get("scope")
        == "single-GPU HiCache L2 microbenchmark; not an issue #31600 reproduction",
        "summary.scope 不符合实验边界",
    )
    return {
        "status": "complete",
        "run_id": record["run_id"],
        "trials": num_trials,
        "paired_ttft_ratio_median": median,
    }


def validate_record(record: object) -> dict:
    _expect(isinstance(record, dict), "结果包根对象必须是 JSON object")
    _expect(record.get("schema_version") == 1, "schema_version 必须为 1")
    _expect(record.get("benchmark") == "sglang-hicache-l2-single-gpu", "benchmark 名称不匹配")
    _expect(isinstance(record.get("run_id"), str) and record["run_id"], "run_id 缺失")
    status = record.get("status")
    _expect(status in ("complete", "failed"), "status 必须是 complete 或 failed")
    if status == "failed":
        _expect(
            isinstance(record.get("error"), str) and record["error"],
            "failed 结果缺少 error",
        )
        classification = record.get("failure_classification")
        if classification is not None:
            _expect(
                classification in _FAILURE_CLASSES,
                f"failure_classification={classification!r} 不在已知枚举",
            )
        return {
            "status": "failed",
            "run_id": record["run_id"],
            "error": record["error"],
            "failure_classification": classification,
        }
    return _validate_complete(record)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path, help="workload.py 生成的 JSON 结果包")
    args = parser.parse_args(argv)
    try:
        record = json.loads(args.result.read_text(encoding="utf-8"))
        result = validate_record(record)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        print(f"结果包校验失败：{exc}", file=sys.stderr)
        return 2
    if result["status"] == "failed":
        print(f"结果包结构有效，但基准状态为 failed：{result['error']}", file=sys.stderr)
        return 1
    print(
        f"结果包校验通过：{result['run_id']}；trials={result['trials']}；"
        f"paired_ttft_ratio_median={result['paired_ttft_ratio_median']:.6f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
