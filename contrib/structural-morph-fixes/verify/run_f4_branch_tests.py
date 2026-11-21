"""Replay the patched check_prefetch_progress branch and the new helper,
both extracted VERBATIM (ast.get_source_segment) from the patched
unified_radix_cache.py, against stub collaborators. Verifies:
  - the counter/WARNING fires exactly on deadline-cut-without-accounting;
  - an already-recorded hit does not fire it (unfulfilled path kept);
  - a non-deadline miss still walks poll_miss; control flow is unchanged.

Runs offline (full module import needs compiled deps absent on this box).
"""
import ast
import logging
import sys
import types
import unittest

sys.path.insert(0, "/tmp/sglang-main-audit/python")

SRC = "/tmp/sglang-main-audit/python/sglang/srt/mem_cache/unified_radix_cache.py"


def extract(name, kind="function"):
    tree = ast.parse(open(SRC).read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            seg = ast.get_source_segment(open(SRC).read(), node)
            return seg
    raise AssertionError(f"{name} not found")


class _Counter:
    def __init__(self):
        self.deadline_cut_lookups = 0

    def log_storage_prefetch_deadline_cut_lookup(self):
        self.deadline_cut_lookups += 1


class _Retries:
    def __init__(self):
        self.misses = []

    def poll_miss(self, rid, *a):
        self.misses.append(rid)


class _Cache:
    """Stub `self` for the extracted code."""

    def __init__(self, recorded_hit=None):
        self.page_size = 1
        self.enable_storage_metrics = True
        self.storage_metrics_collector = _Counter()
        self._storage_prefetch_hit_remaining_by_reqid = {}
        if recorded_hit is not None:
            self._storage_prefetch_hit_remaining_by_reqid[recorded_hit[0]] = recorded_hit[1]
        self.finished = []
        self.revoked = []
        self.terminated = []
        self.storage_prefetch_retries = _Retries()
        self.cache_controller = types.SimpleNamespace(
            terminate_prefetch=lambda op: self.terminated.append(op)
        )

    def _finish_storage_prefetch(self, request, fulfilled_tokens, reason):
        self.finished.append((request, fulfilled_tokens, reason))

    def revoke_pending_prefetch(self, request):
        self.revoked.append(request)


class _Req:
    def __init__(self, rid):
        self.rid = rid


class _Op:
    def __init__(self, rid, deadline_expired, host_indices=None, pages=4223):
        self.handle = _Req(rid)
        self.deadline_expired = deadline_expired
        self.host_indices = host_indices
        self.hash_value = list(range(pages))


BRANCH = extract("check_prefetch_progress").split(
    "        self.cache_controller.terminate_prefetch(operation)", 1
)[1]
# Method-body lines carry 8 spaces of indent; rewrap in a plain function
# (the tail ends with `return True`).
import textwrap

BRANCH = (
    "def _replay(self, operation, request):\n"
    + textwrap.indent(textwrap.dedent(BRANCH), "    ")
)

HELPER = extract("_log_storage_prefetch_deadline_cut_lookup")


def replay(cache, operation):
    ns = {
        "logger": logging.getLogger("replay"),
        "PrefetchOperation": object,
    }
    exec(BRANCH, ns)
    return ns["_replay"](cache, operation, operation.handle)


def bind_helper(cache):
    ns = {
        "logger": logging.getLogger("replay"),
        "PrefetchOperation": object,
    }
    exec(HELPER, ns)
    import types as t

    return t.MethodType(ns["_log_storage_prefetch_deadline_cut_lookup"], cache)


class TestF4Branch(unittest.TestCase):
    def test_cut_without_accounting_counts_and_warns(self):
        cache = _Cache()
        cache._log_storage_prefetch_deadline_cut_lookup = bind_helper(cache)
        op = _Op("req-cut", deadline_expired=True)
        with self.assertLogs("replay", level="WARNING") as logs:
            replay(cache, op)
        self.assertEqual(cache.storage_metrics_collector.deadline_cut_lookups, 1)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("req-cut", logs.output[0])
        self.assertIn("4223", logs.output[0])
        self.assertEqual(cache.finished, [(op.handle, 0, "deadline")])
        self.assertEqual(cache.revoked, [op.handle])
        self.assertEqual(cache.storage_prefetch_retries.misses, [])

    def test_cut_with_recorded_hit_does_not_count(self):
        cache = _Cache()
        op = _Op("req-hit", deadline_expired=True)
        cache._storage_prefetch_hit_remaining_by_reqid[op.handle] = 4223
        cache._log_storage_prefetch_deadline_cut_lookup = bind_helper(cache)
        replay(cache, op)
        self.assertEqual(cache.storage_metrics_collector.deadline_cut_lookups, 0)
        self.assertEqual(cache.finished, [(op.handle, 0, "deadline")])

    def test_plain_miss_still_walks_poll_miss(self):
        cache = _Cache()
        op = _Op("req-miss", deadline_expired=False)
        cache._log_storage_prefetch_deadline_cut_lookup = bind_helper(cache)
        replay(cache, op)
        self.assertEqual(cache.storage_metrics_collector.deadline_cut_lookups, 0)
        self.assertEqual(cache.finished, [])
        self.assertEqual(cache.storage_prefetch_retries.misses, ["req-miss"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
