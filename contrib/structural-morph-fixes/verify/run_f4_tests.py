"""Run the F4 collector test body standalone: imports only
metrics_collector (the full test file also imports unified_radix_cache,
which needs compiled deps unavailable here). Test bodies are verbatim from
test/registered/unit/observability/test_stat_loggers_di.py; doubles are
copied from the same file.
"""
import sys
import types
import unittest

sys.path.insert(0, "/tmp/sglang-main-audit/python")
ds = types.ModuleType("datasets")
ds.load_dataset = lambda *a, **k: None
sys.modules["datasets"] = ds

from sglang.srt.observability.metrics_collector import StorageMetricsCollector


class _BoundRecordingMetric:
    def __init__(self, metric, labels):
        self.metric = metric
        self.labels = labels

    def inc(self, value=1):
        self.metric.increments.append((self.labels, value))


class _RecordingMetric:
    def __init__(self, *args, name=None, labelnames=(), **kwargs):
        self.name = name if name is not None else args[0]
        self.labelnames = tuple(labelnames)
        self.increments = []

    def labels(self, *values, **labels):
        if values:
            labels = dict(zip(self.labelnames, values, strict=True))
        return _BoundRecordingMetric(self, labels)


class _RecordingStorageMetricsCollector(StorageMetricsCollector):
    _counter_cls = _RecordingMetric
    _histogram_cls = _RecordingMetric


class TestDeadlineCutLookupMetric(unittest.TestCase):
    def test_deadline_cut_lookup_metric_counts_events_without_token_dimension(
        self,
    ):
        """A lookup cut before its verdict is booked must not need tokens.

        The counter exists because such a cut books no hit/unfulfilled tokens
        at all; it stays event-denominated so it cannot pollute the
        hit = fulfilled + sum(unfulfilled) identity.
        """
        labels = {"model_name": "test"}
        collector = _RecordingStorageMetricsCollector(labels=labels)

        collector.log_storage_prefetch_deadline_cut_lookup()
        collector.log_storage_prefetch_deadline_cut_lookup()

        self.assertEqual(
            collector.storage_prefetch_deadline_cut_lookups_total.increments,
            [(labels, 1), (labels, 1)],
        )
        self.assertEqual(
            collector.storage_prefetch_deadline_cut_lookups_total.labelnames,
            tuple(labels.keys()),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
