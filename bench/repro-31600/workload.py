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
    "sglang:hicache_backup_dropped_tokens_total",
    "sglang:backuped_tokens_total",
    "sglang:evicted_tokens_total",
    "sglang:load_back_tokens_total",
    "sglang:load_back_bytes_total",
    "sglang:load_back_duration_seconds",
)
_COUNTER_NAMES = (
    "sglang:hicache_backup_tokens_total",
    "sglang:backuped_tokens_total",
    "sglang:hicache_backup_dropped_tokens_total",
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
        cache_salt: str | None,
        rid: str,
        max_new_tokens: int,
    ) -> dict:
        payload = {
            "input_ids": input_ids,
            "rid": rid,
            "sampling_params": {
                "max_new_tokens": max_new_tokens,
                "temperature": 0,
                "ignore_eos": True,
            },
            "stream": True,
        }
        if cache_salt is not None:
            payload["cache_salt"] = cache_salt
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
        "enable_metrics": True,
        "enable_cache_report": True,
        "hicache_storage_backend": expected_storage_backend,
    }
    for key, expected in required.items():
        if snapshot.get(key) != expected:
            raise RuntimeError(f"服务配置 {key}={snapshot.get(key)!r}，要求为 {expected!r}")
    if snapshot.get("hicache_host_memory_mode") not in ("cache", "buffer_only"):
        raise RuntimeError(
            f"服务配置 hicache_host_memory_mode={snapshot.get('hicache_host_memory_mode')!r}，"
            "要求为 cache 或 buffer_only"
        )
    if snapshot.get("hicache_write_policy") != "write_through":
        raise RuntimeError("本基准要求 --hicache-write-policy write_through")
    memory_usage = snapshot.get("memory_usage") or {}
    capacity = int(memory_usage.get("token_capacity", 0))
    if capacity <= 0:
        raise RuntimeError("无法从 /server_info 取得有效的 device token_capacity")
    return capacity


def validate_cache_result(
    phase: str,
    result: dict,
    min_host_hit_ratio: float,
    storage_mode: bool = False,
) -> None:
    details = result["cached_tokens_details"]
    if phase == "R":
        if storage_mode:
            # L3 臂的判定标准是路径存在性：storage>0 即正向命中；
            # 分层驻留率（host+storage/input）如实记录但不作门槛——
            # storage 覆盖受写回与驱逐策略影响，本就可能是部分命中。
            if details["storage"] <= 0:
                raise RuntimeError(
                    f"{result['rid']} 未形成 L3 命中: details={details}"
                )
            return
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


_FAILURE_CLASSES = (
    "config",
    "not_backed_up",
    "index_evicted",
    "storage_read_failed",
    "transfer_anomaly",
    "unknown",
)


def storage_dir_snapshot(storage_dir: Path) -> list[dict] | None:
    """枚举 storage 目录中的 .bin 与 .tmp.* 页文件（仅本机服务可用）。"""
    if not storage_dir.is_dir():
        return None
    entries = []
    for path in sorted(storage_dir.iterdir()):
        if not (path.name.endswith(".bin") or ".tmp." in path.name):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append(
            {"name": path.name, "size": stat.st_size, "mtime": stat.st_mtime}
        )
    return entries


def bin_bytes_growth(before: list[dict] | None, after: list[dict] | None) -> int | None:
    """两个 .bin 快照之间的总字节增量；任一缺失返回 None。"""
    if before is None or after is None:
        return None
    def total(entries: list[dict]) -> int:
        return sum(item["size"] for item in entries if item["name"].endswith(".bin"))
    return total(after) - total(before)


def _stage_delta(record: dict, stage: str, name: str) -> float | None:
    metrics = record.get("metrics") or {}
    before = (metrics.get("counters_before") or {}).get(name)
    stage_counters = (metrics.get("stage_counters") or {}).get(stage) or {}
    after = stage_counters.get(name)
    if after is None:
        return None
    return float(after) - float(before or 0.0)


def classify_failure(exc: BaseException, record: dict) -> str:
    """按已采集证据给失败归因；不匹配任何规则时如实返回 unknown。"""
    if not (record.get("phases") or {}).get("W"):
        return "config"
    error_text = str(exc).lower()
    if "short read" in error_text or "failed to fetch" in error_text:
        return "storage_read_failed"
    storage_writes = _stage_delta(
        record, "post_evict", "sglang:backuped_tokens_total"
    ) or _stage_delta(record, "post_write", "sglang:backuped_tokens_total")
    snapshots = record.get("storage_snapshots") or {}
    growth = bin_bytes_growth(snapshots.get("post_write"), snapshots.get("post_evict"))
    if growth is None:
        growth = bin_bytes_growth(snapshots.get("pre_write"), snapshots.get("post_write"))
    backed_up = (storage_writes or 0) > 0 or (growth is not None and growth > 0)
    if not backed_up:
        return "not_backed_up"
    load_back = _stage_delta(
        record, "post_measure", "sglang:load_back_tokens_total"
    )
    if (load_back or 0) > 0:
        return "transfer_anomaly"
    return "index_evicted"


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
    parser.add_argument(
        "--storage-dir",
        type=Path,
        default=None,
        help="服务端 storage 后端的落盘目录（如 file 后端的 "
        "SGLANG_HICACHE_FILE_BACKEND_STORAGE_DIR）；仅当服务在本机时有效，"
        "用于分阶段快照 .bin 页文件作为 L3 路径证据。",
    )
    parser.add_argument(
        "--salt-mode",
        choices=("per-request", "none"),
        default="per-request",
        help="cache_salt 命名空间策略：per-request（默认，W/R 每轮独立、"
        "E/P 独立）或 none（全请求不带 salt，验证 L3 命中归属）",
    )
    parser.add_argument(
        "--post-evict-wait",
        type=float,
        default=0.0,
        help="E 阶段结束后追加等待秒数：轮询直到 backuped_tokens_total "
        "追平 hicache_backup_tokens_total（在途写回全部落盘、host staging "
        "页解除保护），或超时为止。cache 模式下 E 的写回 backlog 会钉住 "
        "host 池，使 M 阶段 prefetch 分配失败被静默撤销（实测 R-000 仅 "
        "分配到 768/4224、R-001 分配不足阈值被撤销）。0 表示不等待。",
    )
    parser.add_argument(
        "--evict-content-mode",
        choices=("shared", "per-request"),
        default="shared",
        help="E 阶段内容策略：shared（默认，全部 E 共用一段内容，salt "
        "隔离命名空间）或 per-request（每请求独立内容；salt-mode=none "
        "时必需——共享内容在内容寻址下会 dedup，不产生驱逐压力）",
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
    if args.storage_dir is not None and args.host not in (
        "127.0.0.1",
        "localhost",
        "::1",
    ):
        raise ValueError("--storage-dir 仅在服务端位于本机时可用")
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
    if args.salt_mode == "none":
        print("salt-mode=none：全请求不带 cache_salt（E 与 W 共享前缀将保活浅层路径）")
    else:
        print("W/R 每轮共用一个 salt；P 与 E 使用独立 salt，避免跨轮前缀污染")
    print("分阶段 counter 快照：post_write / post_evict / post_measure")
    if args.storage_dir is not None:
        print(f"storage 目录快照：{args.storage_dir}（pre_write + 各阶段边界）")
    else:
        print("未传 --storage-dir：L3 证据将仅有 backuped_tokens_total counter")
    if args.expected_storage_backend is not None:
        print(
            f"L3 模式：期望后端 {args.expected_storage_backend}，"
            "R 命中按 host+storage 计，失败时写 failure_classification"
        )


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
    if args.salt_mode == "none" and args.evict_content_mode == "shared":
        raise RuntimeError(
            "salt-mode=none 下共享 E 内容会在内容寻址缓存中 dedup，"
            "不产生驱逐压力（实测 R 直接 device 命中）；"
            "请改用 --evict-content-mode per-request"
        )
    evict_ids = build_exact_token_ids(seed_ids, args.evict_request_tokens)
    # W/R 每轮使用相互独立的内容而非依赖 cache_salt 隔离：
    # saltless 命名空间下同内容跨轮 dedup——R-000 从 L3 重新物化
    # 路径后 R-001 直接 device 命中（实测 4223），逐轮独立性
    # 必须由内容本身保证。种子区间与 E(7000–9386)、P(15000–17005)
    # 全不相交；salted 模式下内容不同不损害原有 salt 隔离语义。
    trial_prompt_ids = [
        build_exact_token_ids(
            [20000 + i, 21000 + i, 22000 + i], prompt_len
        )
        for i in range(args.num_trials)
    ]
    # P 冷基线必须用与 W 不同的内容：storage 后端按无 salt 内容
    # hash 寻址，同内容探针会命中 L3 而不再冷；且必须逐轮不同——
    # P-000 写入后，后续同内容 P 会命中其 storage 页（实测
    # P-001 命中 1024）；且 saltless 命名空间下跨请求共享
    # 任意前缀即产生命中（P-001 曾因共享 [211,212] 头命中
    # device=2），故探针种子逐轮全不相交，且避开 W(101–103)
    # 与 E(7000–9386) 区间。L2-only 模式下不同内容同样是
    # 冷路径，不改变 P 的基线语义。
    probe_ids = [
        build_exact_token_ids(
            [15000 + i, 16000 + i, 17000 + i], prompt_len
        )
        for i in range(args.num_trials)
    ]

    run_id = record["run_id"]
    print("== warmup：预热后清空缓存与指标 ==")
    client.generate(
        build_exact_token_ids(seed_ids, min(128, prompt_len)),
        None if args.salt_mode == "none" else f"warmup-{run_id}",
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
    if args.storage_dir is not None:
        record["storage_snapshots"] = {
            "storage_dir": str(args.storage_dir),
            "pre_write": storage_dir_snapshot(args.storage_dir),
        }

    def capture_stage(stage: str) -> None:
        record["metrics"].setdefault("stage_counters", {})[stage] = (
            counter_snapshot(client.metrics())
        )
        if args.storage_dir is not None:
            record["storage_snapshots"][stage] = storage_dir_snapshot(
                args.storage_dir
            )

    if args.salt_mode == "none":
        trial_salts = [None] * args.num_trials
        cold_salts = [None] * args.num_trials
    else:
        trial_salts = [f"l2-{run_id}-{i:03d}" for i in range(args.num_trials)]
        cold_salts = [f"cold-{run_id}-{i:03d}" for i in range(args.num_trials)]

    print(f"== W：写入 {args.num_trials} 个相互隔离的 L2 候选 ==")
    writes = record["phases"]["W"] = []
    for i, salt in enumerate(trial_salts):
        writes.append(
            client.generate(
                trial_prompt_ids[i],
                salt,
                f"W-{run_id}-{i:03d}",
                args.max_new_tokens,
            )
        )
    print(f"W TTFT median: {summarize(writes)['ttft_median_s'] * 1000:.1f} ms")

    print(f"== 等待 {args.backup_wait:.1f}s，让 write_through 备份完成 ==")
    time.sleep(args.backup_wait)
    capture_stage("post_write")

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
                evict_ids
                if args.evict_content_mode == "shared"
                else build_exact_token_ids(
                    [7000 + i, 8000 + i, 9000 + i],
                    args.evict_request_tokens,
                ),
                None if args.salt_mode == "none" else f"evict-{run_id}-{i:04d}",
                f"E-{run_id}-{i:04d}",
                args.max_new_tokens,
            )
        )

    capture_stage("post_evict")

    if args.post_evict_wait > 0:
        # cache 模式下 E 的在途写回以保护位钉住 host staging 页；
        # 等队列 drain 完这些页才变成可驱逐，M 阶段 prefetch 才有
        # 分配空间。部分写可能被静默丢弃（永不 ack），故收敛判据用
        # 「acked 连续 N 次轮询不再增长」而非 acked>=enqueued。
        print(f"== 等待 backup drain，至多 {args.post_evict_wait:.0f}s ==")
        deadline = time.monotonic() + args.post_evict_wait
        last_acked = -1.0
        quiet_polls = 0
        while time.monotonic() < deadline:
            snap = counter_snapshot(client.metrics())
            enqueued = snap.get("sglang:hicache_backup_tokens_total")
            acked = snap.get("sglang:backuped_tokens_total")
            if acked is not None:
                if acked <= last_acked:
                    quiet_polls += 1
                else:
                    quiet_polls = 0
                    last_acked = acked
                if quiet_polls >= 4 or (
                    enqueued is not None and acked >= enqueued
                ):
                    print(
                        f"backup drain 收敛：enqueued={enqueued}, acked={acked}"
                    )
                    break
            time.sleep(2.0)
        else:
            print("警告：等待超时，backup 队列可能仍在 drain")
        capture_stage("post_drain")

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
                trial_prompt_ids[index]
                if phase == "R"
                else probe_ids[index],
                salt,
                f"{phase}-{run_id}-{index:03d}",
                args.max_new_tokens,
            )
            trial["results"][phase] = result
            validate_cache_result(
                phase,
                result,
                args.min_host_hit_ratio,
                storage_mode=args.expected_storage_backend is not None,
            )
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
    record["metrics"].setdefault("stage_counters", {})["post_measure"] = counters_after
    if args.storage_dir is not None:
        record["storage_snapshots"]["post_measure"] = storage_dir_snapshot(
            args.storage_dir
        )
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


def jsonable_args(args: argparse.Namespace) -> dict:
    return {
        key: str(value) if isinstance(value, Path) else value
        for key, value in vars(args).items()
    }


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
        "args": jsonable_args(args),
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
        record["failure_classification"] = classify_failure(exc, record)
        print(
            f"基准失败：{exc}（failure_classification={record['failure_classification']}）",
            file=sys.stderr,
        )
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
