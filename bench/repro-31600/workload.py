#!/usr/bin/env python3
"""HiCache L2 单卡微基准：比较 host 回载与等长冷 prefill 的 TTFT。"""

import argparse
import json
import math
import platform
import random
import shutil
import socket
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests


_TOKEN_SEED = (
    "Hierarchical KV cache moves inactive prefixes between GPU and host memory. "
    "This benchmark uses exact token ids and isolated cache namespaces. "
)
_METRIC_NAMES = (
    "sglang:hicache_host_used_tokens",
    "sglang:hicache_host_total_tokens",
    "sglang:hicache_backup_tokens_total",
    "sglang:hicache_backup_bytes_total",
    "sglang:hicache_backup_duration_seconds",
    "sglang:hicache_dropped_tokens_total",
    "sglang:evicted_tokens_total",
    "sglang:load_back_tokens_total",
    "sglang:load_back_bytes_total",
    "sglang:load_back_duration_seconds",
)
_COUNTER_NAMES = (
    "sglang:hicache_backup_tokens_total",
    "sglang:evicted_tokens_total",
    "sglang:load_back_tokens_total",
    "sglang:load_back_bytes_total",
)


def build_exact_token_ids(seed_ids: list[int], target_len: int) -> list[int]:
    if target_len <= 0:
        raise ValueError("token 数必须大于 0")
    if not seed_ids:
        raise ValueError("tokenizer 返回了空 token 序列")
    repeats = math.ceil(target_len / len(seed_ids))
    return (seed_ids * repeats)[:target_len]


def build_measurement_plan(num_trials: int, seed: int) -> list[list[str]]:
    if num_trials <= 0:
        raise ValueError("num_trials 必须大于 0")
    orders = [["R", "P"] if i % 2 == 0 else ["P", "R"] for i in range(num_trials)]
    random.Random(seed).shuffle(orders)
    return orders


def cache_details(meta: dict) -> dict[str, int]:
    details = meta.get("cached_tokens_details") or {}
    return {
        "device": int(details.get("device", 0)),
        "host": int(details.get("host", 0)),
        "storage": int(details.get("storage", 0)),
    }


def metric_total(metrics_text: str, metric_name: str) -> float | None:
    values = []
    for line in metrics_text.splitlines():
        if not line or line.startswith("#"):
            continue
        sample_name = line.split("{", 1)[0].split(None, 1)[0]
        if sample_name != metric_name:
            continue
        fields = line.rsplit(None, 2)
        values.append(float(fields[-2] if len(fields) == 3 else fields[-1]))
    return sum(values) if values else None


def filter_metrics(metrics_text: str) -> str:
    lines = [
        line
        for line in metrics_text.splitlines()
        if any(name in line for name in _METRIC_NAMES)
    ]
    return "\n".join(lines) + ("\n" if lines else "")


def summarize(results: list[dict]) -> dict:
    ttfts = [item["ttft_s"] for item in results]
    totals = [item["total_s"] for item in results]
    return {
        "n": len(results),
        "ttft_median_s": statistics.median(ttfts),
        "ttft_mean_s": statistics.fmean(ttfts),
        "total_median_s": statistics.median(totals),
    }


def local_gpu_inventory(host: str) -> list[str] | None:
    if host not in ("127.0.0.1", "localhost", "::1"):
        return None
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi is None:
        return None
    result = subprocess.run(
        [
            nvidia_smi,
            "--query-gpu=name,driver_version,memory.total",
            "--format=csv,noheader,nounits",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


class SGLangClient:
    def __init__(self, base_url: str, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def health(self) -> None:
        response = self.session.get(f"{self.base_url}/health", timeout=10)
        response.raise_for_status()

    def get_json(self, path: str) -> dict:
        response = self.session.get(f"{self.base_url}{path}", timeout=30)
        response.raise_for_status()
        return response.json()

    def tokenize(self, text: str) -> tuple[list[int], int]:
        response = self.session.post(
            f"{self.base_url}/tokenize",
            json={"prompt": text, "add_special_tokens": False},
            timeout=30,
        )
        response.raise_for_status()
        body = response.json()
        return body["tokens"], int(body["max_model_len"])

    def flush_cache(self) -> None:
        response = self.session.post(
            f"{self.base_url}/flush_cache", params={"timeout": 30}, timeout=40
        )
        response.raise_for_status()

    def metrics(self) -> str:
        response = self.session.get(f"{self.base_url}/metrics", timeout=30)
        response.raise_for_status()
        return response.text

    def generate(
        self,
        input_ids: list[int],
        cache_salt: str,
        rid: str,
        max_new_tokens: int,
    ) -> dict:
        payload = {
            "input_ids": input_ids,
            "cache_salt": cache_salt,
            "rid": rid,
            "sampling_params": {
                "max_new_tokens": max_new_tokens,
                "temperature": 0,
                "ignore_eos": True,
            },
            "stream": True,
        }
        started = time.perf_counter()
        ttft = None
        output_events = 0
        last_completion_tokens = 0
        final_meta = {}
        saw_done = False

        with self.session.post(
            f"{self.base_url}/generate",
            json=payload,
            stream=True,
            timeout=self.timeout,
        ) as response:
            response.raise_for_status()
            for raw_line in response.iter_lines(decode_unicode=False):
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8")
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    saw_done = True
                    break
                if not body:
                    continue
                try:
                    event = json.loads(body)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"收到无法解析的 SSE JSON: {body[:160]}") from exc
                if "error" in event:
                    raise RuntimeError(f"SGLang 返回错误: {event['error']}")
                meta = event.get("meta_info") or {}
                if meta:
                    final_meta = meta
                completion_tokens = int(meta.get("completion_tokens", 0))
                if completion_tokens > last_completion_tokens:
                    output_events += completion_tokens - last_completion_tokens
                    last_completion_tokens = completion_tokens
                    if ttft is None:
                        ttft = time.perf_counter() - started

        total = time.perf_counter() - started
        if not saw_done:
            raise RuntimeError(f"请求 {rid} 的 SSE 流未正常结束")
        if ttft is None:
            raise RuntimeError(f"请求 {rid} 没有产生可计时的输出 token 事件")

        return {
            "rid": rid,
            "cache_salt": cache_salt,
            "input_token_count": len(input_ids),
            "ttft_s": ttft,
            "total_s": total,
            "output_token_events": output_events,
            "prompt_tokens": final_meta.get("prompt_tokens"),
            "completion_tokens": final_meta.get("completion_tokens"),
            "cached_tokens": final_meta.get("cached_tokens", 0),
            "cached_tokens_details": cache_details(final_meta),
        }


def safe_server_snapshot(server_info: dict, model_info: dict) -> dict:
    fields = (
        "version",
        "model_path",
        "served_model_name",
        "tokenizer_path",
        "load_format",
        "dtype",
        "kv_cache_dtype",
        "mem_fraction_static",
        "tp_size",
        "dp_size",
        "pp_size",
        "attn_cp_size",
        "speculative_algorithm",
        "enable_hierarchical_cache",
        "hicache_ratio",
        "hicache_host_memory_mode",
        "hicache_write_policy",
        "hicache_io_backend",
        "hicache_storage_backend",
        "enable_metrics",
        "enable_cache_report",
    )
    snapshot = {key: server_info.get(key) for key in fields if key in server_info}
    internal_states = server_info.get("internal_states") or []
    snapshot["memory_usage"] = (
        internal_states[0].get("memory_usage") if internal_states else None
    )
    snapshot["model_info"] = {
        key: model_info[key]
        for key in ("model_path", "tokenizer_path", "is_generation", "weight_version")
        if key in model_info
    }
    return snapshot


def validate_configuration(
    snapshot: dict, expected_storage_backend: str | None = None
) -> int:
    required = {
        "tp_size": 1,
        "dp_size": 1,
        "pp_size": 1,
        "attn_cp_size": 1,
        "speculative_algorithm": None,
        "enable_hierarchical_cache": True,
        "hicache_host_memory_mode": "cache",
        "enable_metrics": True,
        "enable_cache_report": True,
        "hicache_storage_backend": expected_storage_backend,
    }
    for key, expected in required.items():
        if snapshot.get(key) != expected:
            raise RuntimeError(f"服务配置 {key}={snapshot.get(key)!r}，要求为 {expected!r}")
    if snapshot.get("hicache_write_policy") != "write_through":
        raise RuntimeError("本基准要求 --hicache-write-policy write_through")
    memory_usage = snapshot.get("memory_usage") or {}
    capacity = int(memory_usage.get("token_capacity", 0))
    if capacity <= 0:
        raise RuntimeError("无法从 /server_info 取得有效的 device token_capacity")
    return capacity


def validate_cache_result(
    phase: str, result: dict, min_host_hit_ratio: float
) -> None:
    details = result["cached_tokens_details"]
    if phase == "R":
        minimum = math.floor(result["input_token_count"] * min_host_hit_ratio)
        # host 与 storage 都是合法的分层命中；L2-only 配置下 storage 恒为 0
        if details["host"] + details["storage"] < minimum:
            raise RuntimeError(
                f"{result['rid']} 未形成足够的分层命中: "
                f"host={details['host']}, storage={details['storage']}, "
                f"要求 >= {minimum}, details={details}"
            )
    elif sum(details.values()) != 0:
        raise RuntimeError(
            f"{result['rid']} 不是冷 prefill: cached_tokens_details={details}"
        )


def counter_snapshot(metrics_text: str) -> dict[str, float | None]:
    return {name: metric_total(metrics_text, name) for name in _COUNTER_NAMES}


def counter_deltas(before: dict, after: dict) -> dict[str, float | None]:
    return {
        name: (after[name] - (before[name] or 0.0))
        if after[name] is not None
        else None
        for name in _COUNTER_NAMES
    }


def validate_metric_deltas(deltas: dict) -> None:
    for name in (
        "sglang:hicache_backup_tokens_total",
        "sglang:evicted_tokens_total",
        "sglang:load_back_tokens_total",
    ):
        if deltas.get(name) is None or deltas[name] <= 0:
            raise RuntimeError(f"指标 {name} 没有正增量，无法证明目标缓存路径已执行")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=30000)
    parser.add_argument("--num-trials", type=int, default=8)
    parser.add_argument("--prefix-tokens", type=int, default=4096)
    parser.add_argument("--tail-tokens", type=int, default=128)
    parser.add_argument("--max-new-tokens", type=int, default=1)
    parser.add_argument("--evict-request-tokens", type=int, default=1024)
    parser.add_argument(
        "--evict-factor",
        type=float,
        default=1.10,
        help="E 阶段总 token 数相对 device token_capacity 的倍数",
    )
    parser.add_argument(
        "--evict-total-tokens",
        type=int,
        default=None,
        help="显式覆盖 E 阶段总 token 数",
    )
    parser.add_argument("--backup-wait", type=float, default=10.0)
    parser.add_argument("--min-host-hit-ratio", type=float, default=0.90)
    parser.add_argument(
        "--expected-storage-backend",
        default=None,
        help="要求服务端 hicache_storage_backend 等于该值（如 file）；"
        "开启后 R 阶段把 host+storage 都计为分层命中。",
    )
    parser.add_argument("--seed", type=int, default=31600)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument(
        "--sglang-commit",
        default=None,
        help="构建服务所用的 SGLang commit；用于结果追溯",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("results"))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    for flag, value in (
        ("--num-trials", args.num_trials),
        ("--prefix-tokens", args.prefix_tokens),
        ("--tail-tokens", args.tail_tokens),
        ("--max-new-tokens", args.max_new_tokens),
        ("--evict-request-tokens", args.evict_request_tokens),
    ):
        if value <= 0:
            raise ValueError(f"{flag} 必须大于 0")
    if args.evict_factor <= 1 and args.evict_total_tokens is None:
        raise ValueError("--evict-factor 必须大于 1，或显式给出 --evict-total-tokens")
    if args.evict_total_tokens is not None and args.evict_total_tokens <= 0:
        raise ValueError("--evict-total-tokens 必须大于 0")
    if args.backup_wait < 0:
        raise ValueError("--backup-wait 不能小于 0")
    if not 0 < args.min_host_hit_ratio <= 1:
        raise ValueError("--min-host-hit-ratio 必须位于 (0, 1]")
    if args.sglang_commit is None:
        if not args.dry_run:
            raise ValueError("正式运行必须提供 --sglang-commit（40 位 commit SHA）")
    elif len(args.sglang_commit) != 40 or any(
        char not in "0123456789abcdefABCDEF" for char in args.sglang_commit
    ):
        raise ValueError("--sglang-commit 必须是 40 位十六进制 commit SHA")


def print_dry_run(args: argparse.Namespace) -> None:
    prompt_len = args.prefix_tokens + args.tail_tokens
    token_ids = build_exact_token_ids([101, 102, 103], prompt_len)
    plan = build_measurement_plan(args.num_trials, args.seed)
    print(f"离线 dry-run：每个 W/R/P 请求 {len(token_ids)} 个精确 token id")
    print(f"试验数：{args.num_trials}；配对顺序：{plan}")
    print("E 阶段请求数将在运行时根据 /server_info 的 token_capacity 推导")
    print("W/R 每轮共用一个 salt；P 与 E 使用独立 salt，避免跨轮前缀污染")


def run_benchmark(args: argparse.Namespace, record: dict) -> None:
    base_url = f"http://{args.host}:{args.port}"
    client = SGLangClient(base_url, args.timeout)
    client.health()

    server_info = client.get_json("/server_info")
    model_info = client.get_json("/model_info")
    snapshot = safe_server_snapshot(server_info, model_info)
    record["server"] = snapshot
    token_capacity = validate_configuration(snapshot, args.expected_storage_backend)

    seed_ids, max_model_len = client.tokenize(_TOKEN_SEED)
    prompt_len = args.prefix_tokens + args.tail_tokens
    if prompt_len + args.max_new_tokens >= max_model_len:
        raise RuntimeError(
            f"请求长度 {prompt_len} 超出模型上下文限制 {max_model_len}"
        )
    if args.evict_request_tokens + args.max_new_tokens >= max_model_len:
        raise RuntimeError("--evict-request-tokens 超出模型上下文限制")
    prompt_ids = build_exact_token_ids(seed_ids, prompt_len)
    evict_ids = build_exact_token_ids(seed_ids, args.evict_request_tokens)

    run_id = record["run_id"]
    print("== warmup：预热后清空缓存与指标 ==")
    client.generate(
        build_exact_token_ids(seed_ids, min(128, prompt_len)),
        f"warmup-{run_id}",
        f"warmup-{run_id}",
        args.max_new_tokens,
    )
    client.flush_cache()

    metrics_before = client.metrics()
    counters_before = counter_snapshot(metrics_before)
    record["metrics"] = {
        "before": filter_metrics(metrics_before),
        "counters_before": counters_before,
    }

    trial_salts = [f"l2-{run_id}-{i:03d}" for i in range(args.num_trials)]
    cold_salts = [f"cold-{run_id}-{i:03d}" for i in range(args.num_trials)]

    print(f"== W：写入 {args.num_trials} 个相互隔离的 L2 候选 ==")
    writes = record["phases"]["W"] = []
    for i, salt in enumerate(trial_salts):
        writes.append(
            client.generate(
                prompt_ids, salt, f"W-{run_id}-{i:03d}", args.max_new_tokens
            )
        )
    print(f"W TTFT median: {summarize(writes)['ttft_median_s'] * 1000:.1f} ms")

    print(f"== 等待 {args.backup_wait:.1f}s，让 write_through 备份完成 ==")
    time.sleep(args.backup_wait)

    evict_total = args.evict_total_tokens or math.ceil(
        token_capacity * args.evict_factor
    )
    evict_requests = math.ceil(evict_total / args.evict_request_tokens)
    record["eviction"] = {
        "device_token_capacity": token_capacity,
        "target_total_tokens": evict_total,
        "request_tokens": args.evict_request_tokens,
        "num_requests": evict_requests,
    }
    print(
        f"== E：{evict_requests} 个独立请求，共 {evict_requests * args.evict_request_tokens} "
        f"tokens（device capacity={token_capacity}） =="
    )
    evictions = record["phases"]["E"] = []
    for i in range(evict_requests):
        evictions.append(
            client.generate(
                evict_ids,
                f"evict-{run_id}-{i:04d}",
                f"E-{run_id}-{i:04d}",
                args.max_new_tokens,
            )
        )

    plan = build_measurement_plan(args.num_trials, args.seed)
    record["measurement_plan"] = plan
    trials = []
    record["trials"] = trials
    print("== M：逐轮配对测量 R(L2 reload) 与 P(cold prefill) ==")
    for index, order in enumerate(plan):
        trial = {"trial": index, "order": order, "results": {}}
        trials.append(trial)
        for phase in order:
            salt = trial_salts[index] if phase == "R" else cold_salts[index]
            result = client.generate(
                prompt_ids,
                salt,
                f"{phase}-{run_id}-{index:03d}",
                args.max_new_tokens,
            )
            trial["results"][phase] = result
            validate_cache_result(phase, result, args.min_host_hit_ratio)
        trial["ttft_ratio_r_over_p"] = (
            trial["results"]["R"]["ttft_s"] / trial["results"]["P"]["ttft_s"]
        )
        details = trial["results"]["R"]["cached_tokens_details"]
        print(
            f"trial {index:02d} {''.join(order)}: ratio={trial['ttft_ratio_r_over_p']:.3f}, "
            f"R cache={details}"
        )
    metrics_after = client.metrics()
    counters_after = counter_snapshot(metrics_after)
    deltas = counter_deltas(counters_before, counters_after)
    record["metrics"].update(
        {
            "after": filter_metrics(metrics_after),
            "counters_after": counters_after,
            "counter_deltas": deltas,
        }
    )
    validate_metric_deltas(deltas)

    reload_results = [trial["results"]["R"] for trial in trials]
    cold_results = [trial["results"]["P"] for trial in trials]
    ratios = [trial["ttft_ratio_r_over_p"] for trial in trials]
    record["summary"] = {
        "reload": summarize(reload_results),
        "cold_prefill": summarize(cold_results),
        "paired_ttft_ratio_median": statistics.median(ratios),
        "l2_slower_than_cold_in_this_run": statistics.median(ratios) > 1.0,
        "scope": "single-GPU HiCache L2 microbenchmark; not an issue #31600 reproduction",
    }
    print(
        f"配对 TTFT R/P median={record['summary']['paired_ttft_ratio_median']:.3f}；"
        "该结果只描述本次单卡 L2 微基准，不代表复现 #31600。"
    )


def main() -> int:
    args = parse_args()
    try:
        validate_args(args)
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        print_dry_run(args)
        return 0

    started_at = datetime.now(timezone.utc)
    run_id = started_at.strftime("%Y%m%dT%H%M%SZ")
    record = {
        "schema_version": 1,
        "benchmark": "sglang-hicache-l2-single-gpu",
        "run_id": run_id,
        "started_at_utc": started_at.isoformat(),
        "environment": {
            "client_hostname": socket.gethostname(),
            "client_platform": platform.platform(),
            "client_python": platform.python_version(),
            "server_host": args.host,
            "local_gpu_inventory": local_gpu_inventory(args.host),
        },
        "args": {**vars(args), "output_dir": str(args.output_dir)},
        "phases": {},
        "status": "running",
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = args.output_dir / f"results-{run_id}.json"

    exit_code = 0
    try:
        run_benchmark(args, record)
        record["status"] = "complete"
    except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
        record["status"] = "failed"
        record["error"] = str(exc)
        print(f"基准失败：{exc}", file=sys.stderr)
        exit_code = 1
    finally:
        record["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        output_path.write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"结果包：{output_path}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
