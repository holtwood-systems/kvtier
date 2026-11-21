"""Run the patched F1 test file in the offline venv (datasets stubbed)."""
import sys
import types
import unittest

sys.path.insert(0, "/tmp/sglang-main-audit/python")
ds = types.ModuleType("datasets")
ds.load_dataset = lambda *a, **k: None
sys.modules["datasets"] = ds

loader = unittest.TestLoader()
suite = loader.discover(
    "/tmp/sglang-main-audit/test/registered/unit/mem_cache",
    pattern="test_hicache_file_batch_exists.py",
)
runner = unittest.TextTestRunner(verbosity=2)
result = runner.run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
