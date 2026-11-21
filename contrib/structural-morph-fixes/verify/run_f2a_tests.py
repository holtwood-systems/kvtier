"""Exercise the F2-a phase-split logic offline by exec-ing the patched
methods VERBATIM (ast.get_source_segment) from unified_radix_cache.py with
the real msgspec/PollTimer and stub collaborators. The full module import
needs compiled deps unavailable on this box.

Covers: query cap formula; poll arms the query guard only; a slow lookup
no longer consumes the transfer budget; each phase expires on its own
timer; the vote flag and the KV-landed branch still dominate; non-timeout
policies arm nothing.
"""
import ast
import sys
import time
import types
import unittest

sys.path.insert(0, "/tmp/sglang-main-audit/python")

import msgspec

from sglang.srt.utils.poll_timer import PollTimer

SRC = "/tmp/sglang-main-audit/python/sglang/srt/mem_cache/unified_radix_cache.py"
_source = open(SRC).read()


def extract(name):
    tree = ast.parse(_source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return ast.get_source_segment(_source, node)
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(_source, node)
    raise AssertionError(f"{name} not found")


ns = {"msgspec": msgspec, "PollTimer": PollTimer, "PrefetchOperation": object}
for name in (
    "_StoragePrefetchWait",
    "_storage_prefetch_query_cap",
    "_start_storage_prefetch_wait",
    "_start_storage_prefetch_load_wait",
    "_prefetch_deadline_expired",
):
    exec(extract(name), ns)

_StoragePrefetchWait = ns["_StoragePrefetchWait"]


class _Req:
    def __init__(self, rid):
        self.rid = rid


class _Cache:
    def __init__(self, policy="timeout"):
        self.prefetch_stop_policy = policy
        self.prefetch_timeout_base = 1.0
        # page_size=1, 0.25 s per Ki-token of pages.
        self.prefetch_timeout_per_page = 0.25 / 1024
        self.page_size = 1
        self._storage_prefetch_wait = {}
        for name in (
            "_storage_prefetch_query_cap",
            "_start_storage_prefetch_wait",
            "_start_storage_prefetch_load_wait",
            "_prefetch_deadline_expired",
        ):
            setattr(self, name, types.MethodType(ns[name], self))


class _Op:
    def __init__(self, pages=4223):
        self.handle = _Req("r1")
        self.stats_requested_tokens = pages
        self.hash_value = [None] * pages
        self.host_indices = None
        self.completed_tokens = 0


def _rewind(timer, seconds):
    timer.started_at = time.perf_counter() - seconds


class TestF2aPhaseSplit(unittest.TestCase):
    def test_query_cap_is_four_full_budgets(self):
        cache = _Cache()
        op = _Op()
        self.assertAlmostEqual(
            cache._storage_prefetch_query_cap(op), 4 * (1.0 + 4223 * 0.25 / 1024)
        )

    def test_poll_arms_only_the_query_guard(self):
        cache = _Cache()
        op = _Op()
        cache._start_storage_prefetch_wait(op)
        wait = cache._storage_prefetch_wait[op.handle]
        self.assertFalse(wait.timer.running)
        self.assertTrue(wait.query_timer.running)
        self.assertEqual(wait.timeout_s, 0.0)
        # Idempotent per request: a second poll keeps the first start.
        started = wait.query_timer.started_at
        cache._start_storage_prefetch_wait(op)
        self.assertIs(cache._storage_prefetch_wait[op.handle].query_timer, wait.query_timer)
        self.assertEqual(cache._storage_prefetch_wait[op.handle].query_timer.started_at, started)

    def test_slow_lookup_no_longer_consumes_the_load_budget(self):
        """The C2 signature: 2.5 s spent querying beat the old 2.03 s budget."""
        cache = _Cache()
        op = _Op()
        cache._start_storage_prefetch_wait(op)
        wait = cache._storage_prefetch_wait[op.handle]
        _rewind(wait.query_timer, 2.5)  # > 2.03 s, < 8.13 s cap
        self.assertFalse(cache._prefetch_deadline_expired(op))

    def test_query_guard_expires_on_its_own_cap(self):
        cache = _Cache()
        op = _Op()
        cache._start_storage_prefetch_wait(op)
        _rewind(cache._storage_prefetch_wait[op.handle].query_timer, 9.0)
        self.assertTrue(cache._prefetch_deadline_expired(op))

    def test_alloc_arms_the_transfer_budget(self):
        cache = _Cache()
        op = _Op()
        cache._start_storage_prefetch_load_wait(op)
        wait = cache._storage_prefetch_wait[op.handle]
        self.assertTrue(wait.timer.running)
        self.assertAlmostEqual(wait.timeout_s, 1.0 + 4223 * 0.25 / 1024)
        self.assertFalse(wait.query_timer.running)  # query phase already over
        self.assertFalse(cache._prefetch_deadline_expired(op))
        _rewind(wait.timer, 2.5)
        self.assertTrue(cache._prefetch_deadline_expired(op))

    def test_alloc_uses_the_truncated_span(self):
        cache = _Cache()
        op = _Op(pages=4223)
        op.hash_value = [None] * 128  # truncated to fit host capacity
        cache._start_storage_prefetch_load_wait(op)
        self.assertAlmostEqual(
            cache._storage_prefetch_wait[op.handle].timeout_s,
            1.0 + 128 * 0.25 / 1024,
        )

    def test_vote_flag_and_landed_kv_dominate(self):
        cache = _Cache()
        op = _Op()
        cache._start_storage_prefetch_wait(op)
        cache._storage_prefetch_wait[op.handle].expired = True
        self.assertTrue(cache._prefetch_deadline_expired(op))
        op.host_indices = object()
        op.completed_tokens = 4223  # KV landed
        self.assertFalse(cache._prefetch_deadline_expired(op))

    def test_non_timeout_policy_arms_nothing(self):
        cache = _Cache(policy="wait_complete")
        op = _Op()
        cache._start_storage_prefetch_wait(op)
        self.assertEqual(cache._storage_prefetch_wait, {})
        cache._start_storage_prefetch_load_wait(op)
        self.assertEqual(cache._storage_prefetch_wait, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
