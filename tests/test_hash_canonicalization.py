#!/usr/bin/env python3
"""Tests of the canonical report write / digest contract (task-69, docs/101 §7).

On Windows ``Path.write_text`` translates every ``"\\n"`` to ``"\\r\\n"``, so the SHA-256 of the
in-memory payload stopped matching the SHA-256 of the file that was actually written.  Eighteen
tools wrote their reports that way; four of them returned ``summary["sha256"]`` of the *payload*,
which is why ``summary["sha256"] != sha256_file(report_path)`` on Windows.

The contract pinned here is:

* a report is written as UTF-8 bytes with LF endings on every platform (``write_bytes`` or an
  explicit ``newline="\\n"``), so the digest of the payload equals ``sha256_file(path)``;
* ``summary["bytes"]`` equals the real file size and ``len(payload.encode("utf-8"))``;
* numbers and content are untouched — the report round-trips through JSON unchanged.

The report builders are exercised with the structurally faithful fixtures of the sibling suites
(``test_sector_exposure``, ``test_forecast_world_bridge``, ``test_quantity_channel``,
``test_market_bridge``), so no real ``build/`` or ``data/`` artefact is read or written: the suite
is offline and hermetic.  ``tools/cgra.py``, named by task-69, does not exist in this checkout; the
case is recorded as a skip instead of being silently dropped.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import forecast_world_bridge as fw  # noqa: E402
import market_bridge as mb  # noqa: E402
import quantity_bridge as qb  # noqa: E402
import sector_exposure as se  # noqa: E402
import test_forecast_world_bridge as fw_fixtures  # noqa: E402
import test_market_bridge as mb_fixtures  # noqa: E402
import test_quantity_channel as qb_fixtures  # noqa: E402
import test_sector_exposure as se_fixtures  # noqa: E402

GENERATED_AT = "2026-11-18T00:00:00Z"
REPORT_TOOLS = ("sector_exposure", "forecast_world_bridge", "quantity_bridge", "market_bridge")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_canonical(path: Path, text: str) -> str:
    """The canonical form: UTF-8 bytes with LF endings, digest over exactly those bytes."""
    data = text.encode("utf-8")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(data)
    return sha256_bytes(data)


class CanonicalWriteTest(unittest.TestCase):
    """Platform-independent reproduction of the defect and of the canonical write."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_crlf_translation_breaks_the_payload_digest(self):
        payload = '{\n "a": 1\n}\n'
        # Exactly what Path.write_text() produces on Windows: one extra byte per line.
        translated = payload.replace("\n", "\r\n").encode("utf-8")
        path = self.root / "crlf.json"
        path.write_bytes(translated)
        self.assertNotEqual(sha256_bytes(translated), sha256_bytes(payload.encode("utf-8")))
        self.assertEqual(sha256_bytes(path.read_bytes()), sha256_bytes(translated))
        self.assertEqual(path.stat().st_size, len(payload.encode("utf-8")) + payload.count("\n"))

    def test_canonical_write_matches_the_digest_and_never_writes_crlf(self):
        payload = '{"отрасль": "энергетика", "delta": -0.5}\n'
        path = self.root / "canonical.json"
        digest = write_canonical(path, payload)
        self.assertEqual(digest, sha256_bytes(path.read_bytes()))
        self.assertEqual(digest, hashlib.sha256(payload.encode("utf-8")).hexdigest())
        self.assertNotIn(b"\r\n", path.read_bytes())
        self.assertEqual(path.read_bytes(), payload.encode("utf-8"))
        self.assertEqual(path.stat().st_size, len(payload.encode("utf-8")))
        self.assertEqual(path.read_text(encoding="utf-8"), payload)

    def test_canonical_write_is_stable_across_repeated_writes(self):
        payload = '{"a": [1.0, 2.5]}\n'
        first = write_canonical(self.root / "a.json", payload)
        second = write_canonical(self.root / "b.json", payload)
        self.assertEqual(first, second)
        self.assertEqual((self.root / "a.json").read_bytes(), (self.root / "b.json").read_bytes())


class ReportContractMixin:
    """The shared assertion: the reported digest is the digest of the written file."""

    def assert_canonical_report(self, module, outcome: dict) -> None:
        path = Path(outcome["report_path"])
        self.assertTrue(path.is_file(), f"отчёт не записан: {path}")
        self.assertEqual(module.sha256_file(path), outcome["sha256"],
                         "summary['sha256'] не совпадает с SHA-256 файла")
        self.assertEqual(outcome["bytes"], path.stat().st_size)
        self.assertEqual(outcome["bytes"], len(path.read_bytes()))
        self.assertNotIn(b"\r\n", path.read_bytes(), "отчёт записан с CRLF")
        # Content is unchanged: the file parses to exactly the payload, only written canonically.
        # (The payload is re-serialised first: json.dumps turns tuples into arrays.)
        self.assertEqual(json.loads(path.read_bytes()),
                         json.loads(json.dumps(outcome["report"], ensure_ascii=False)))
        summary = Path(outcome["summary_path"])
        self.assertTrue(summary.is_file(), f"сводка не записана: {summary}")
        self.assertTrue(summary.read_bytes())
        self.assertNotIn(b"\r\n", summary.read_bytes(), "сводка записана с CRLF")


class SectorExposureHashTest(ReportContractMixin, unittest.TestCase):
    def test_sector_exposure_report_hash_is_the_file_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = se_fixtures.Fixture(root)
            outcome = se.build_report(
                forecast_world=fixture.forecast_world, price_bridge=fixture.price_bridge,
                dataset=se_fixtures.DATASET, scenario_id="inflation_plus_1pp",
                pass_through=0.6, cost_mode="price_bridge", top_n=5, demo=False,
                out_dir=root / "out", generated_at=se_fixtures.GENERATED_AT)
            self.assert_canonical_report(se, outcome)


class ForecastWorldBridgeHashTest(ReportContractMixin, unittest.TestCase):
    def test_forecast_world_bridge_report_hash_is_the_file_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = fw_fixtures.Fixture(root)
            saved = (fw.DATASETS, fw.QUANTITY_REPORT)
            fw.DATASETS = {"figaro": {"label": "fixture", "index": fixture.index,
                                      "reports": fixture.reports, "quantity_key": "figaro"}}
            fw.QUANTITY_REPORT = fixture.root / "quantity_report.json"
            try:
                outcome = fw.build_report(
                    feeds=[Path(item) for item in fixture.feeds.values()],
                    dataset="figaro", pass_through=0.6, channel="combined", horizon="long",
                    price_template="import_plus20", growth_template="energy_minus5", top_n=5,
                    out_dir=root / "out", generated_at=GENERATED_AT)
            finally:
                fw.DATASETS, fw.QUANTITY_REPORT = saved
            self.assert_canonical_report(fw, outcome)


class QuantityBridgeHashTest(ReportContractMixin, unittest.TestCase):
    def test_quantity_bridge_report_hash_is_the_file_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            qb_fixtures.synthetic_dataset(root / "figaro" / "figaro_2019" / "dataset.json")
            outcome = qb.build_report(root, datasets=("figaro",), pass_throughs=(0.6,),
                                      out_dir=root / "out", generated_at=GENERATED_AT)
            self.assert_canonical_report(qb, outcome)


class MarketBridgeHashTest(ReportContractMixin, unittest.TestCase):
    def test_market_bridge_report_hash_is_the_file_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "figaro" / "figaro_2019"
            folder.mkdir(parents=True)
            mb_fixtures.write_dataset(folder, n_countries=2, n_industries=3)
            outcome = mb.build_report(root, datasets=("figaro",), pass_throughs=(0.6,),
                                      out_dir=root / "out", generated_at=GENERATED_AT)
            self.assert_canonical_report(mb, outcome)


class CgraToolTest(ReportContractMixin, unittest.TestCase):
    """task-69 names ``tools/cgra.py``; this checkout has no such module (docs/101 §7)."""

    def test_cgra_report_hash_is_the_file_hash(self):
        spec = importlib.util.find_spec("cgra")
        if spec is None or spec.origin is None:
            self.skipTest("tools/cgra.py отсутствует в этой копии репозитория; контракт проверен "
                          "на sector_exposure, forecast_world_bridge, quantity_bridge, market_bridge")
        source = Path(spec.origin).read_text(encoding="utf-8")
        self.assertNotIn("report_path.write_text", source)
        self.assertIn('write_bytes(', source,
                      "cgra.py должен писать байты канонически (write_bytes / newline='\\n')")


class WriteGuardTest(unittest.TestCase):
    """Static guards over tools/: the pattern must not come back."""

    def test_no_tool_writes_text_without_an_explicit_newline(self):
        offenders = []
        for path in sorted((ROOT / "tools").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "write_text"
                        and "newline" not in {keyword.arg for keyword in node.keywords}):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [],
                         "write_text() без newline='\\n' пишет CRLF на Windows: "
                         + ", ".join(offenders))

    def test_report_tools_write_the_exact_bytes_they_hash(self):
        for name in REPORT_TOOLS:
            source = (ROOT / "tools" / f"{name}.py").read_text(encoding="utf-8")
            self.assertIn('report_path.write_bytes(payload.encode("utf-8"))', source,
                          f"{name}.py: отчёт должен писаться теми же байтами, что и хешируются")
            self.assertNotIn("report_path.write_text", source, f"{name}.py пишет отчёт через CRLF")


if __name__ == "__main__":
    unittest.main(verbosity=2)
