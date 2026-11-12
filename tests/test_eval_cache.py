"""Forecast cache (tools/eval_cache.py): PIT/version key, identity, invalidation, maintenance.

Offline and fast: most tests use synthetic repository roots. The two integration tests exercise
``tools/run_real_evaluation.build_window`` on the real local PIT archive and are skipped when the
private ``data/raw`` directory is absent (CI without restored artefacts).
"""
import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import eval_cache  # noqa: E402

LOCAL_DATA = (ROOT / "data" / "raw" / "CPIAUCSL" / "latest.csv").exists()


def make_fake_root(base: Path) -> Path:
    """Minimal repository root: frozen files, forecast source stubs and one data file."""
    root = base / "repo"
    (root / "protocols").mkdir(parents=True, exist_ok=True)
    for relative in eval_cache.FROZEN_FILES.values():
        source = ROOT / relative
        if source.is_file():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for directory in eval_cache.SOURCE_DIRS:
        folder = root / directory
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "model_stub.py").write_text("VERSION = 1\n", encoding="utf-8")
    data = root / "data" / "raw"
    data.mkdir(parents=True, exist_ok=True)
    (data / "SERIES.csv").write_text("observation_date,value\n2020-01-01,1.0\n", encoding="utf-8")
    return root


def arrays_equal(left, right) -> bool:
    if isinstance(left, dict) and isinstance(right, dict):
        if set(left) != set(right):
            return False
        return all(arrays_equal(left[key], right[key]) for key in left)
    if isinstance(left, np.ndarray) or isinstance(right, np.ndarray):
        return np.array_equal(np.asarray(left), np.asarray(right))
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(arrays_equal(a, b) for a, b in zip(left, right))
    return left == right


class Fingerprint(unittest.TestCase):
    def test_fingerprint_structure(self):
        fingerprint = eval_cache.build_fingerprint(ROOT)
        self.assertEqual(fingerprint["cache_schema"], eval_cache.CACHE_SCHEMA)
        for name in ("protocol_v4", "r3_specification", "nf_sim_thresholds",
                     "comparator_composition", "nber_dates", "spf_metadata"):
            self.assertIn(name, fingerprint["versions"])
            current = fingerprint["versions"][name]
            if current is not None:
                self.assertRegex(current, r"^[0-9a-f]{64}$")
        self.assertRegex(fingerprint["source_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(fingerprint["data_sha256"], r"^[0-9a-f]{64}$")
        self.assertGreater(fingerprint["source_files"], 0)

    def test_fingerprint_is_deterministic(self):
        self.assertEqual(eval_cache.build_fingerprint(ROOT), eval_cache.build_fingerprint(ROOT))


class CacheKey(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.fake = make_fake_root(Path(cls.tmp.name))
        cls.fingerprint = eval_cache.build_fingerprint(cls.fake)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def key(self, **overrides):
        values = {"kind": "model", "name": "AO", "target": "T2_GDP_GROWTH", "horizon": 0,
                  "origin": "2000-01-31"}
        values.update(overrides)
        return eval_cache.cache_key(self.fingerprint, **values)

    def test_key_is_deterministic(self):
        self.assertEqual(self.key(), self.key())
        self.assertRegex(self.key(), r"^[0-9a-f]{64}$")

    def test_key_depends_on_the_prediction_identity(self):
        base = self.key()
        for overrides in ({"origin": "2000-02-29"}, {"target": "T1_CPI_YOY"},
                          {"horizon": 1}, {"name": "AR_BIC"}, {"kind": "candidate"},
                          {"mode": "latest"}, {"series_id": "CPIAUCSL"}):
            self.assertNotEqual(base, self.key(**overrides), overrides)

    def test_key_is_pit_safe_origin_bound(self):
        keys = {self.key(origin=origin) for origin in
                ("1990-03-31", "1991-03-31", "2000-03-31", "2020-03-31")}
        self.assertEqual(len(keys), 4)

    def test_data_change_invalidates_the_key(self):
        data_file = self.fake / "data" / "raw" / "SERIES.csv"
        before = eval_cache.build_fingerprint(self.fake)
        data_file.write_text("observation_date,value\n2020-01-01,2.0\n", encoding="utf-8")
        after = eval_cache.build_fingerprint(self.fake)
        self.assertNotEqual(before["data_sha256"], after["data_sha256"])
        self.assertNotEqual(eval_cache.cache_key(before, kind="model", name="AO",
                                                 target="T2_GDP_GROWTH", horizon=0,
                                                 origin="2000-01-31"),
                            eval_cache.cache_key(after, kind="model", name="AO",
                                                 target="T2_GDP_GROWTH", horizon=0,
                                                 origin="2000-01-31"))

    def test_source_change_invalidates_the_key(self):
        source = self.fake / eval_cache.SOURCE_DIRS[0] / "model_stub.py"
        before = eval_cache.build_fingerprint(self.fake)
        source.write_text("VERSION = 2\n", encoding="utf-8")
        after = eval_cache.build_fingerprint(self.fake)
        self.assertNotEqual(before["source_sha256"], after["source_sha256"])

    def test_frozen_version_change_invalidates_the_key(self):
        protocol = self.fake / eval_cache.FROZEN_FILES["protocol_v4"]
        before = eval_cache.build_fingerprint(self.fake)
        protocol.write_text(protocol.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        after = eval_cache.build_fingerprint(self.fake)
        self.assertNotEqual(before["versions"]["protocol_v4"], after["versions"]["protocol_v4"])


class CacheBehaviour(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.tmp.name) / "cache"
        self.cache = eval_cache.ForecastCache(self.cache_dir, root=ROOT)

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_returns_the_stored_payload_on_a_hit(self):
        calls = []

        def compute():
            calls.append(1)
            return {"point": 1.5, "quantiles": [0.1, 0.2, 0.3], "data_class": "REAL_TIME"}

        payload, hit = self.cache.forecast(compute, kind="model", name="AO",
                                           target="T2_GDP_GROWTH", horizon=0,
                                           origin="2000-01-31")
        self.assertFalse(hit)
        self.assertEqual(len(calls), 1)

        def should_not_run():
            raise AssertionError("cache miss on a warm cache")

        cached, hit = self.cache.forecast(should_not_run, kind="model", name="AO",
                                          target="T2_GDP_GROWTH", horizon=0,
                                          origin="2000-01-31")
        self.assertTrue(hit)
        self.assertEqual(cached, payload)
        self.assertEqual(self.cache.hits, 1)
        self.assertEqual(self.cache.misses, 1)

    def test_payload_is_json_normalised(self):
        payload, _ = self.cache.forecast(
            lambda: {"point": np.float64(1.5), "quantiles": np.array([0.1, 0.2])},
            kind="model", name="AO", target="T2_GDP_GROWTH", horizon=0, origin="2000-01-31")
        self.assertIsInstance(payload["point"], float)
        self.assertEqual(payload["quantiles"], [0.1, 0.2])
        self.assertIsInstance(payload["quantiles"], list)

    def test_clear_and_stats(self):
        for origin in ("2000-01-31", "2000-02-29"):
            self.cache.forecast(lambda: {"point": 1.0}, kind="model", name="AO",
                                target="T2_GDP_GROWTH", horizon=0, origin=origin)
        stats = self.cache.stats()
        self.assertEqual(stats["entries"], 2)
        self.assertGreater(stats["bytes"], 0)
        self.assertEqual(self.cache.clear(), 2)
        self.assertEqual(self.cache.stats()["entries"], 0)

    def test_corrupt_entry_is_a_miss_not_a_crash(self):
        self.cache.forecast(lambda: {"point": 1.0}, kind="model", name="AO",
                            target="T2_GDP_GROWTH", horizon=0, origin="2000-01-31")
        key = self.cache.key(kind="model", name="AO", target="T2_GDP_GROWTH", horizon=0,
                             origin="2000-01-31")
        self.cache._entry_path(key).write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.cache.get(key))
        self.assertGreaterEqual(self.cache.corrupt, 1)
        payload, hit = self.cache.forecast(lambda: {"point": 2.0}, kind="model", name="AO",
                                           target="T2_GDP_GROWTH", horizon=0, origin="2000-01-31")
        self.assertFalse(hit)
        self.assertEqual(payload["point"], 2.0)

    def test_prune_removes_entries_of_another_fingerprint(self):
        self.cache.forecast(lambda: {"point": 1.0}, kind="model", name="AO",
                            target="T2_GDP_GROWTH", horizon=0, origin="2000-01-31")
        self.cache._fingerprint = dict(self.cache.fingerprint())
        changed = dict(self.cache._fingerprint)
        changed["data_sha256"] = "0" * 64
        self.cache._fingerprint = changed  # simulate a different data archive fingerprint
        self.assertEqual(self.cache.stats()["entries"], 1)
        self.assertGreaterEqual(self.cache.prune(), 1)
        self.assertEqual(self.cache.stats()["entries"], 0)

    def test_cli_stats_and_clear(self):
        self.cache.forecast(lambda: {"point": 1.0}, kind="model", name="AO",
                            target="T2_GDP_GROWTH", horizon=0, origin="2000-01-31")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = eval_cache.main(["stats", "--dir", str(self.cache_dir), "--root", str(ROOT)])
        self.assertEqual(code, 0)
        stats = json.loads(buffer.getvalue())
        self.assertEqual(stats["entries"], 1)
        with contextlib.redirect_stdout(io.StringIO()):
            code = eval_cache.main(["clear", "--dir", str(self.cache_dir), "--root", str(ROOT)])
        self.assertEqual(code, 0)
        self.assertEqual(self.cache.stats()["entries"], 0)


class Integration(unittest.TestCase):
    """The real pipeline: a cached window must equal an uncached one element by element."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = eval_cache.ForecastCache(Path(self.tmp.name) / "cache", root=ROOT)

    def tearDown(self):
        self.tmp.cleanup()

    @unittest.skipUnless(LOCAL_DATA, "data/raw is not downloaded locally")
    def test_cached_window_matches_uncached_window(self):
        import run_real_evaluation as runner

        plain = runner.build_window("T1_CPI_YOY", 1, "W2", "ENSEMBLE_RW_AR", None, "auto",
                                    max_origins=3)
        cached = runner.build_window("T1_CPI_YOY", 1, "W2", "ENSEMBLE_RW_AR", None, "auto",
                                     max_origins=3, cache=self.cache)
        warm = runner.build_window("T1_CPI_YOY", 1, "W2", "ENSEMBLE_RW_AR", None, "auto",
                                   max_origins=3, cache=self.cache)
        self.assertEqual(plain["origins"], cached["origins"])
        self.assertGreater(plain["origins"], 0)
        for key in ("actual_primary", "actual_secondary", "recession"):
            self.assertTrue(arrays_equal(plain[key], cached[key]), key)
        for role in ("candidate", "comparator"):
            self.assertTrue(arrays_equal(plain[role], cached[role]), role)
        self.assertEqual(plain["vintage_dates"], cached["vintage_dates"])
        self.assertEqual(plain["data_class"], cached["data_class"])
        # every member/candidate prediction of the second run came from the cache
        self.assertEqual(self.cache.misses, plain["origins"] * (1 + len(
            runner.members_for(json.loads(runner.COMPOSITION.read_text(encoding="utf-8")),
                               "T1_CPI_YOY", "CRPS", 1))))
        self.assertTrue(arrays_equal(cached, warm), "a warm cache must return the same window")
        self.assertGreater(self.cache.hits, 0)

    @unittest.skipUnless(LOCAL_DATA, "data/raw is not downloaded locally")
    def test_cached_window_report_layer_is_identical(self):
        import run_real_evaluation as runner

        plain = runner.build_window("T1_CPI_YOY", 1, "W2", "ENSEMBLE_RW_AR", None, "auto",
                                    max_origins=2)
        cached = runner.build_window("T1_CPI_YOY", 1, "W2", "ENSEMBLE_RW_AR", None, "auto",
                                     max_origins=2, cache=self.cache)
        self.assertEqual(runner._sanitized_window(plain), runner._sanitized_window(cached))


class Offline(unittest.TestCase):
    def test_cache_module_has_no_network_imports(self):
        source = (ROOT / "tools" / "eval_cache.py").read_text(encoding="utf-8")
        for token in ("import urllib", "import requests", "import socket", "http.client",
                      "urlopen", "import numpy"):
            self.assertNotIn(token, source, token)


if __name__ == "__main__":
    unittest.main(verbosity=2)
