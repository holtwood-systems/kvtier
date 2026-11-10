import importlib.util
import argparse
import tempfile
import unittest
from pathlib import Path


_MODULE_PATH = Path(__file__).with_name("workload.py")
_SPEC = importlib.util.spec_from_file_location("hicache_l2_workload", _MODULE_PATH)
workload = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(workload)


class FakeResponse:
    def __init__(self, lines):
        self.lines = lines

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def raise_for_status(self):
        return None

    def iter_lines(self, decode_unicode=False):
        return iter(self.lines)


class FakeSession:
    def __init__(self, response):
        self.response = response

    def post(self, *args, **kwargs):
        return self.response


class WorkloadTest(unittest.TestCase):
    def test_formal_run_requires_a_commit_sha(self):
        args = argparse.Namespace(
            num_trials=1,
            prefix_tokens=1,
            tail_tokens=1,
            max_new_tokens=1,
            evict_request_tokens=1,
            evict_factor=1.1,
            evict_total_tokens=None,
            backup_wait=0,
            min_host_hit_ratio=0.9,
            sglang_commit=None,
            storage_dir=None,
            dry_run=False,
        )
        with self.assertRaises(ValueError):
            workload.validate_args(args)

    def test_formal_run_accepts_a_valid_commit_sha(self):
        args = argparse.Namespace(
            num_trials=1,
            prefix_tokens=1,
            tail_tokens=1,
            max_new_tokens=1,
            evict_request_tokens=1,
            evict_factor=1.1,
            evict_total_tokens=None,
            backup_wait=0,
            min_host_hit_ratio=0.9,
            sglang_commit="0123456789abcdef0123456789abcdef01234567",
            storage_dir=None,
            dry_run=False,
        )
        workload.validate_args(args)

    def test_exact_token_ids(self):
        self.assertEqual(
            workload.build_exact_token_ids([1, 2, 3], 8),
            [1, 2, 3, 1, 2, 3, 1, 2],
        )

    def test_measurement_plan_is_reproducible_and_balanced(self):
        first = workload.build_measurement_plan(8, 31600)
        second = workload.build_measurement_plan(8, 31600)
        self.assertEqual(first, second)
        self.assertEqual(first.count(["R", "P"]), 4)
        self.assertEqual(first.count(["P", "R"]), 4)

    def test_metric_total_aggregates_labels_and_timestamp(self):
        metrics = (
            'sglang:load_back_tokens_total{pool="kv"} 12\n'
            'sglang:load_back_tokens_total{pool="draft"} 3 123456\n'
        )
        self.assertEqual(
            workload.metric_total(metrics, "sglang:load_back_tokens_total"), 15
        )

    def test_cache_validation_distinguishes_host_and_cold(self):
        reload = {
            "rid": "R",
            "input_token_count": 100,
            "cached_tokens_details": {"device": 0, "host": 95, "storage": 0},
        }
        cold = {
            "rid": "P",
            "input_token_count": 100,
            "cached_tokens_details": {"device": 0, "host": 0, "storage": 0},
        }
        workload.validate_cache_result("R", reload, 0.9)
        workload.validate_cache_result("P", cold, 0.9)
        with self.assertRaises(RuntimeError):
            workload.validate_cache_result("P", reload, 0.9)

    def test_counter_delta_treats_unmaterialized_before_as_zero(self):
        before = {name: None for name in workload._COUNTER_NAMES}
        after = {name: 5.0 for name in workload._COUNTER_NAMES}
        self.assertEqual(
            workload.counter_deltas(before, after),
            {name: 5.0 for name in workload._COUNTER_NAMES},
        )

    def test_generate_parses_token_event_and_cache_details(self):
        event = (
            b'data: {"text":"x","meta_info":{"completion_tokens":1,'
            b'"prompt_tokens":8,"cached_tokens":7,'
            b'"cached_tokens_details":{"device":0,"host":7}}}'
        )
        client = workload.SGLangClient("http://unused", 1)
        client.session = FakeSession(FakeResponse([event, b"data: [DONE]"]))
        result = client.generate([1] * 8, "salt", "rid", 1)
        self.assertEqual(result["output_token_events"], 1)
        self.assertEqual(
            result["cached_tokens_details"],
            {"device": 0, "host": 7, "storage": 0},
        )

    def test_generate_rejects_malformed_sse_json(self):
        client = workload.SGLangClient("http://unused", 1)
        client.session = FakeSession(
            FakeResponse([b"data: {bad-json}", b"data: [DONE]"])
        )
        with self.assertRaises(RuntimeError):
            client.generate([1], "salt", "rid", 1)

    def test_storage_dir_requires_localhost(self):
        args = argparse.Namespace(
            num_trials=1,
            prefix_tokens=1,
            tail_tokens=1,
            max_new_tokens=1,
            evict_request_tokens=1,
            evict_factor=1.1,
            evict_total_tokens=None,
            backup_wait=0,
            min_host_hit_ratio=0.9,
            sglang_commit="0123456789abcdef0123456789abcdef01234567",
            storage_dir=Path("/tmp/hicache"),
            host="10.0.0.1",
            dry_run=False,
        )
        with self.assertRaises(ValueError):
            workload.validate_args(args)

    def test_storage_dir_snapshot_lists_only_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.bin").write_bytes(b"x" * 10)
            (root / "b.bin.tmp.1.2.abc").write_bytes(b"y" * 5)
            (root / "notes.txt").write_text("ignore me")
            entries = workload.storage_dir_snapshot(root)
        self.assertIsNotNone(entries)
        names = [e["name"] for e in entries]
        self.assertEqual(names, ["a.bin", "b.bin.tmp.1.2.abc"])
        self.assertEqual(entries[0]["size"], 10)
        self.assertIsNone(workload.storage_dir_snapshot(root / "missing"))

    def test_bin_bytes_growth(self):
        before = [{"name": "a.bin", "size": 100, "mtime": 0.0}]
        after = before + [{"name": "b.bin", "size": 50, "mtime": 1.0}]
        self.assertEqual(workload.bin_bytes_growth(before, after), 50)
        self.assertIsNone(workload.bin_bytes_growth(None, after))

    def _failed_record(self):
        return {
            "phases": {"W": [{"rid": "W"}]},
            "metrics": {
                "counters_before": {"sglang:backuped_tokens_total": 0.0},
                "stage_counters": {},
            },
        }

    def test_classify_config_failure(self):
        self.assertEqual(
            workload.classify_failure(RuntimeError("bad"), {"phases": {}}),
            "config",
        )

    def test_classify_not_backed_up(self):
        record = self._failed_record()
        record["metrics"]["stage_counters"] = {
            "post_write": {"sglang:backuped_tokens_total": 0.0},
            "post_evict": {"sglang:backuped_tokens_total": 0.0},
        }
        self.assertEqual(
            workload.classify_failure(
                RuntimeError("未形成足够的分层命中"), record
            ),
            "not_backed_up",
        )

    def test_classify_index_evicted(self):
        record = self._failed_record()
        record["metrics"]["stage_counters"] = {
            "post_write": {"sglang:backuped_tokens_total": 4096.0},
            "post_evict": {"sglang:backuped_tokens_total": 8192.0},
            "post_measure": {"sglang:load_back_tokens_total": 0.0},
        }
        self.assertEqual(
            workload.classify_failure(
                RuntimeError("未形成足够的分层命中"), record
            ),
            "index_evicted",
        )

    def test_classify_storage_read_failed(self):
        record = self._failed_record()
        self.assertEqual(
            workload.classify_failure(
                RuntimeError("Short read for key"), record
            ),
            "storage_read_failed",
        )

    def test_classify_transfer_anomaly(self):
        record = self._failed_record()
        record["metrics"]["stage_counters"] = {
            "post_write": {"sglang:backuped_tokens_total": 4096.0},
            "post_evict": {"sglang:backuped_tokens_total": 4096.0},
            "post_measure": {"sglang:load_back_tokens_total": 100.0},
        }
        self.assertEqual(
            workload.classify_failure(RuntimeError("x"), record),
            "transfer_anomaly",
        )


if __name__ == "__main__":
    unittest.main()
