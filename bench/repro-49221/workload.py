#!/usr/bin/env python3
"""repro-49221 七相位负载客户端：L3 prefetch 命中页在 admission 前被逐。

复刻 issue #49221 的负载形状（seed → serial → burst16-a/b → burst32 →
repeat32-a/b，非 seed 相位前 sleep 5s + /flush_cache；32 请求、互异
cache_salt、同一 prompt 文本、prefix 6100、max_new_tokens=1、温度 0），
复用 repro-31600 的 SGLangClient / counter_snapshot（含 reason 维度）/
storage_dir_snapshot / backup drain 收敛轮询。

设计文档：docs/bench-design/2026-11-20-repro-49221.md（§4 负载形状、
§5 门禁、§8 观测信号、附录 A 骨架）。纯客户端。

结果包 schema（schema_version=1）：
  benchmark="sglang-hicache-admission-shortfall"
  phases[]: {name, concurrency, requests[]{rid,salt,ttft_s,total_s,
            cached_tokens,cached_tokens_details{...}},
            counters_before/after（含 reason 键）, unfulfilled_deltas,
            wall_s, ttft 摘要}
  storage_snapshots: pre_seed / post_seed / post_final（隔离 B）
  cold_probe: flush 后独立 salt + 独立内容的全冷证据（G0-3）
  server / args / environment
"""

import argparse
import importlib.util
import json
import platform
import socket
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

_MODULE_PATH = Path(__file__).resolve().parent.parent / "repro-31600" / "workload.py"
_SPEC = importlib.util.spec_from_file_location("hicache_l2_workload", _MODULE_PATH)
workload = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(workload)

_TOKEN_SEED = "hicache admission shortfall probe"
SGLangClient = workload.SGLangClient
counter_snapshot = workload.counter_snapshot
storage_dir_snapshot = workload.storage_dir_snapshot
safe_server_snapshot = workload.safe_server_snapshot
build_exact_token_ids = workload.build_exact_token_ids


class AdminAwareClient(SGLangClient):
    """main 设置 --admin-api-key 后，/flush_cache 等端点也要求 Bearer。"""

    admin_key: str | None = None

    def flush_cache(self) -> None:
        headers = (
            {"Authorization": f"Bearer {self.admin_key}"} if self.admin_key else {}
        )
        response = self.session.post(
            f"{self.base_url}/flush_cache",
            params={"timeout": 30},
            timeout=40,
            headers=headers,
        )
        response.raise_for_status()

_PHASE_SETS = {
    # (phase, concurrency)；issue 七相位结构（设计 §4 / 附录 A）
    "judgment": [
        ("seed", 1),
        ("serial", 1),
        ("burst16-a", 16),
        ("burst16-b", 16),
        ("burst32", 32),
        ("repeat32-a", 32),
        ("repeat32-b", 32),
    ],
    # 探索臂剂效梯度（P3）
    "gradient": [
        ("seed", 1),
        ("serial", 1),
        ("burst4", 4),
        ("burst8", 8),
        ("burst16", 16),
        ("burst32", 32),
    ],
}


def unfulfilled_deltas(before: dict, after: dict) -> dict:
    prefix = 'sglang:storage_prefetch_unfulfilled_tokens_total{reason="'
    deltas = {}
    for key, value in after.items():
        if key.startswith(prefix):
            reason = key[len(prefix) :].rstrip('"}')
            deltas[reason] = value - (before.get(key) or 0.0)
    return deltas


def wait_backup_drain(client: SGLangClient, timeout: float) -> dict:
    """seed 后等 L3 写入完成：acked 追平 enqueued 或连续 4 次静默。"""
    deadline = time.monotonic() + timeout
    last_acked = -1.0
    quiet_polls = 0
    final = {}
    while time.monotonic() < deadline:
        snap = counter_snapshot(client.metrics())
        enqueued = snap.get("sglang:hicache_backup_tokens_total")
        acked = snap.get("sglang:backuped_tokens_total")
        final = {"enqueued": enqueued, "acked": acked}
        if acked is not None:
            if acked <= last_acked:
                quiet_polls += 1
            else:
                quiet_polls = 0
                last_acked = acked
            if quiet_polls >= 4 or (enqueued is not None and acked >= enqueued):
                final["converged"] = True
                return final
        time.sleep(2.0)
    final["converged"] = False
    return final


def summarize_phase(requests: list[dict]) -> dict:
    ttfts = [r["ttft_s"] for r in requests]
    cached = [r.get("cached_tokens") or 0 for r in requests]
    storage = [
        (r.get("cached_tokens_details") or {}).get("storage", 0) for r in requests
    ]
    return {
        "n": len(requests),
        "ttft_median_s": statistics.median(ttfts),
        "ttft_mean_s": statistics.fmean(ttfts),
        "cached_tokens_sum": sum(cached),
        "cached_tokens_median": statistics.median(cached),
        "storage_hit_requests": sum(1 for s in storage if s > 0),
        "storage_tokens_sum": sum(storage),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=30021)
    parser.add_argument("--phase-set", choices=sorted(_PHASE_SETS), default="judgment")
    parser.add_argument("--requests", type=int, default=32)
    parser.add_argument("--prefix-tokens", type=int, default=6100)
    parser.add_argument("--max-new-tokens", type=int, default=1)
    parser.add_argument("--drain-timeout", type=float, default=240.0)
    parser.add_argument("--pre-flush-sleep", type=float, default=5.0)
    parser.add_argument("--seed", type=int, default=49221)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--expected-storage-backend", default="file")
    parser.add_argument("--storage-dir", type=Path, default=None)
    parser.add_argument("--sglang-commit", default=None)
    parser.add_argument("--output-dir", type=Path, default=Path("results"))
    parser.add_argument(
        "--profile-start-phase",
        default=None,
        help="V2 剖析：进入该相位前 POST /start_profile（如 serial）",
    )
    parser.add_argument(
        "--profile-end-phase",
        default=None,
        help="V2 剖析：该相位结束后 POST /stop_profile（如 burst16-a）",
    )
    parser.add_argument("--profile-output-dir", default="/data/prof/repro-49221")
    parser.add_argument(
        "--profile-admin-key",
        default=None,
        help="main 的 /start_profile 等 admin 端点需要 --admin-api-key；"
        "此处传同一 key 作 Bearer 头",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.requests <= 0 or args.prefix_tokens <= 0 or args.max_new_tokens <= 0:
        raise ValueError("requests/prefix-tokens/max-new-tokens 必须为正")
    if args.sglang_commit is None or len(args.sglang_commit) != 40 or any(
        char not in "0123456789abcdefABCDEF" for char in args.sglang_commit
    ):
        raise ValueError("--sglang-commit 必须是 40 位十六进制 commit SHA")
    if args.storage_dir is not None and args.host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("--storage-dir 仅在服务端位于本机时可用")


def main() -> int:
    args = parse_args()
    try:
        validate_args(args)
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2

    started_at = datetime.now(timezone.utc)
    run_id = started_at.strftime("%Y%m%dT%H%M%SZ")
    record = {
        "schema_version": 1,
        "benchmark": "sglang-hicache-admission-shortfall",
        "run_id": run_id,
        "started_at_utc": started_at.isoformat(),
        "environment": {
            "client_hostname": socket.gethostname(),
            "client_platform": platform.platform(),
            "client_python": platform.python_version(),
            "server_host": args.host,
            "local_gpu_inventory": workload.local_gpu_inventory(args.host),
        },
        "args": {
            key: str(value) if isinstance(value, Path) else value
            for key, value in vars(args).items()
        },
        "phases": [],
        "status": "running",
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = args.output_dir / f"results-{run_id}.json"

    exit_code = 0
    try:
        run_phases(args, record)
        record["status"] = "complete"
    except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
        record["status"] = "failed"
        record["error"] = str(exc)
        exit_code = 1
        print(f"负载失败：{exc}", file=sys.stderr)
    finally:
        record["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        output_path.write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"结果包：{output_path}")
    return exit_code


def run_phases(args: argparse.Namespace, record: dict) -> None:
    base_url = f"http://{args.host}:{args.port}"
    client = AdminAwareClient(base_url, args.timeout)
    client.admin_key = args.profile_admin_key
    client.health()

    server_info = client.get_json("/server_info")
    model_info = client.get_json("/model_info")
    snapshot = safe_server_snapshot(server_info, model_info)
    record["server"] = snapshot
    workload.validate_configuration(snapshot, args.expected_storage_backend)

    seed_ids, max_model_len = client.tokenize(_TOKEN_SEED)
    if args.prefix_tokens + args.max_new_tokens >= max_model_len:
        raise RuntimeError(
            f"请求长度 {args.prefix_tokens} 超出模型上下文限制 {max_model_len}"
        )
    prompt_ids = build_exact_token_ids(seed_ids, args.prefix_tokens)
    # 32 个互异 salt、同一 prompt 文本（issue 形状）；跨相位复用同 salt 集，
    # 使 serial/burst 的 re-match 落在同一命名空间。
    salt_stem = f"shortfall-{uuid.uuid4().hex[:8]}"
    salts = [f"{salt_stem}-{i:02d}" for i in range(args.requests)]

    if args.storage_dir is not None:
        record["storage_snapshots"] = {
            "storage_dir": str(args.storage_dir),
            "pre_seed": storage_dir_snapshot(args.storage_dir),
        }

    phases = _PHASE_SETS[args.phase_set]
    profile_events = []
    for phase, conc in phases:
        if phase != "seed":
            time.sleep(args.pre_flush_sleep)
            client.flush_cache()
        if args.profile_start_phase == phase:
            headers = (
                {"Authorization": f"Bearer {args.profile_admin_key}"}
                if args.profile_admin_key
                else {}
            )
            client.session.post(
                f"{base_url}/start_profile",
                json={
                    "output_dir": args.profile_output_dir,
                    "activities": ["CPU", "GPU"],
                },
                headers=headers,
                timeout=60,
            ).raise_for_status()
            profile_events.append({"event": "start", "phase": phase, "t": time.time()})
            print(f"[profile] start @ {phase} -> {args.profile_output_dir}")
        before = counter_snapshot(client.metrics())
        wall_start = time.monotonic()
        with ThreadPoolExecutor(max_workers=conc) as pool:
            requests_list = list(
                pool.map(
                    lambda i: client.generate(
                        prompt_ids,
                        salts[i],
                        f"{phase}-{record['run_id']}-{i:02d}",
                        args.max_new_tokens,
                    ),
                    range(args.requests),
                )
            )
        wall = time.monotonic() - wall_start
        drain = wait_backup_drain(client, args.drain_timeout)
        after = counter_snapshot(client.metrics())
        entry = {
            "phase": phase,
            "concurrency": conc,
            "requests": requests_list,
            "summary": summarize_phase(requests_list),
            "unfulfilled_deltas": unfulfilled_deltas(before, after),
            "counters_after": after,
            "drain": drain,
            "wall_s": wall,
        }
        record["phases"].append(entry)
        print(
            f"[{phase}] conc={conc} wall={wall:.1f}s cached_sum="
            f"{entry['summary']['cached_tokens_sum']} storage_hits="
            f"{entry['summary']['storage_hit_requests']}/{args.requests} "
            f"ttft_med={entry['summary']['ttft_median_s']:.3f}s "
            f"unfulfilled={entry['unfulfilled_deltas']} drain={drain}"
        )
        if args.storage_dir is not None and phase == "seed":
            record["storage_snapshots"]["post_seed"] = storage_dir_snapshot(
                args.storage_dir
            )
        if args.profile_end_phase == phase:
            headers = (
                {"Authorization": f"Bearer {args.profile_admin_key}"}
                if args.profile_admin_key
                else {}
            )
            client.session.post(
                f"{base_url}/stop_profile", headers=headers, timeout=120
            ).raise_for_status()
            profile_events.append({"event": "stop", "phase": phase, "t": time.time()})
            print(f"[profile] stop @ {phase}")
    if profile_events:
        record["profile_events"] = profile_events

    # 冷探针（G0-3）：flush 后独立 salt + 独立内容必须全冷（证明 flush 清了
    # L1/L2；serial 的 storage>0 反证 flush 未清 L3）
    time.sleep(args.pre_flush_sleep)
    client.flush_cache()
    probe_ids = build_exact_token_ids(
        [15000, 16000, 17000], args.prefix_tokens
    )
    probe = client.generate(
        probe_ids,
        f"cold-{record['run_id']}",
        f"cold-{record['run_id']}",
        args.max_new_tokens,
    )
    record["cold_probe"] = probe
    print(
        f"[cold-probe] cached_tokens_details={probe['cached_tokens_details']}"
    )
    if args.storage_dir is not None:
        record["storage_snapshots"]["post_final"] = storage_dir_snapshot(
            args.storage_dir
        )


if __name__ == "__main__":
    sys.exit(main())
