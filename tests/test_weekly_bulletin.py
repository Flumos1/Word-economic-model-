"""Weekly bulletin (tools/weekly_bulletin.py): schema, required blocks, consistency, determinism.

The bulletin is a derived publication artefact: every number must be traceable to the product feed,
the forward journal or the v5 report; it must never contain raw third-party observations, rebuilding
must be byte-identical apart from ``generated_at``, the tool must touch no input and it must fail
closed when the product pack is absent.
"""
import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import product_pack  # noqa: E402
import weekly_bulletin  # noqa: E402

FIXED = "2026-11-12T12:00:00Z"
ANOTHER = "2026-11-12T18:30:00Z"
DATE = "2026-11-12"

FEED = ROOT / "build" / "product" / "forecast_latest.json"
JOURNAL = ROOT / "registry" / "forecasts.jsonl"
REPORT = ROOT / "protocols" / "first_real_evaluation_report_v5.json"
REGIME = ROOT / "protocols" / "regime_candidate_evaluation_report.json"
PIN = ROOT / "protocols" / "evaluation_protocol_v5.sha256"
PROTOCOL_VERSION = json.loads(
    (ROOT / "protocols" / "evaluation_protocol_v5.json").read_text(encoding="utf-8"))["version"]
PROTOCOL_SHA = PIN.read_text(encoding="utf-8").split()[0]
JOURNAL_ROWS = len([line for line in JOURNAL.read_text(encoding="utf-8").splitlines() if line])
PROTOCOL = ROOT / "protocols" / "evaluation_protocol_v5.json"

SOURCE_FILES = (
    "protocols/evaluation_protocol_v5.json",
    "protocols/evaluation_protocol_v5.sha256",
    "protocols/first_real_evaluation_report_v5.json",
    "protocols/r3_specification.json",
    "protocols/nf_sim_thresholds.json",
    "protocols/nf_sim_thresholds.sha256",
    "protocols/comparator_composition_2026-11-11.json",
    "registry/forecasts.jsonl",
)
OPTIONAL_FILES = ("protocols/regime_candidate_evaluation_report.json",)


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def walk_keys(node, path=""):
    """Yield ``(path, key, value)`` for every mapping member of a JSON document."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield path, key, value
            yield from walk_keys(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk_keys(value, f"{path}[{index}]")


def minimal_root(base: Path, with_product: bool = False) -> Path:
    """A temporary repository with only the files the bulletin needs."""
    root = base / "repo"
    for relative in SOURCE_FILES + OPTIONAL_FILES:
        source = ROOT / relative
        if not source.is_file():
            continue
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    if with_product:
        target = root / "build" / "product" / "forecast_latest.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        if FEED.is_file():
            shutil.copy2(FEED, target)
        else:  # a clean checkout: build the feed once inside the temporary repository
            product_pack.main(["--root", str(root), "--out-dir", str(target.parent)])
    return root


class WeeklyBulletin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name)
        cls.root = minimal_root(cls.base, with_product=True)
        cls.out = cls.base / "bulletin"
        cls.hashes_before = {relative: product_pack.lf_sha256(ROOT / relative)
                             for relative in SOURCE_FILES}
        cls.code = weekly_bulletin.main(["--root", str(cls.root), "--out-dir", str(cls.out),
                                         "--generated-at", FIXED, "--date", DATE])
        cls.json_path = cls.out / f"{DATE}.json"
        cls.md_path = cls.out / f"{DATE}.md"
        cls.document = read_json(cls.json_path)
        cls.markdown = cls.md_path.read_text(encoding="utf-8")
        cls.feed = read_json(cls.root / "build" / "product" / "forecast_latest.json")
        cls.rows = [json.loads(line) for line in
                    (cls.root / "registry" / "forecasts.jsonl").read_text(encoding="utf-8")
                    .splitlines() if line.strip()]
        cls.report = read_json(cls.root / "protocols" / "first_real_evaluation_report_v5.json")
        cls.regime = read_json(cls.root / "protocols" / "regime_candidate_evaluation_report.json")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # ------------------------------------------------------------------------------- structure
    def test_exit_code_and_files(self):
        self.assertEqual(self.code, 0)
        self.assertTrue(self.md_path.is_file())
        self.assertTrue(self.json_path.is_file())

    def test_json_schema(self):
        required = {"schema_version", "bulletin_id", "generated_at", "date", "status",
                    "status_label", "conditional_data_class", "forecast", "changes",
                    "changes_note", "confirmed", "not_confirmed", "near_miss_candidate",
                    "versions", "hashes", "disclaimer", "limitations"}
        self.assertTrue(required.issubset(self.document), required - set(self.document))
        self.assertEqual(self.document["schema_version"], weekly_bulletin.SCHEMA_VERSION)
        self.assertEqual(self.document["bulletin_id"], weekly_bulletin.BULLETIN_ID)
        self.assertEqual(self.document["generated_at"], FIXED)
        self.assertEqual(self.document["status"], "CANDIDATE")
        self.assertEqual(self.document["status_label"], "CANDIDATE")
        self.assertTrue(self.document["conditional_data_class"])
        self.assertIsInstance(self.document["changes"], list)
        self.assertIsInstance(self.document["confirmed"], list)
        self.assertIsInstance(self.document["not_confirmed"], list)
        self.assertIsInstance(self.document["hashes"], dict)
        self.assertTrue(self.document["disclaimer"])
        for key in ("protocol", "r3", "nf_sim", "composition", "report", "journal"):
            self.assertIn(key, self.document["hashes"])
            self.assertEqual(len(self.document["hashes"][key]), 64, key)

    def test_forecast_block_schema(self):
        forecast = self.document["forecast"]
        for key in ("candidate", "target", "horizon", "origin", "point", "median",
                    "quantiles_5_to_95_step_5", "intervals", "controls", "pre_origin"):
            self.assertIn(key, forecast)
        self.assertIsInstance(forecast["point"], float)
        self.assertIsInstance(forecast["median"], float)
        self.assertEqual(len(forecast["quantiles_5_to_95_step_5"]), 19)
        self.assertTrue(all(b > a for a, b in zip(forecast["quantiles_5_to_95_step_5"],
                                                  forecast["quantiles_5_to_95_step_5"][1:])))
        self.assertEqual(set(forecast["intervals"]), {"50", "80", "90"})
        self.assertEqual(set(forecast["controls"]), set(product_pack.CONTROL_ARMS))

    def test_intervals_come_from_the_quantiles(self):
        quantiles = self.document["forecast"]["quantiles_5_to_95_step_5"]
        expected = {"50": (4, 14), "80": (1, 17), "90": (0, 18)}
        for level, (low, high) in expected.items():
            interval = self.document["forecast"]["intervals"][level]
            self.assertEqual(interval["level_percent"], int(level))
            self.assertAlmostEqual(interval["low"], quantiles[low], places=12)
            self.assertAlmostEqual(interval["high"], quantiles[high], places=12)
            self.assertLess(interval["low"], interval["high"])
        intervals = self.document["forecast"]["intervals"]
        self.assertLessEqual(intervals["90"]["low"], intervals["80"]["low"])
        self.assertLessEqual(intervals["80"]["low"], intervals["50"]["low"])
        self.assertGreaterEqual(intervals["90"]["high"], intervals["80"]["high"])
        self.assertGreaterEqual(intervals["80"]["high"], intervals["50"]["high"])

    # --------------------------------------------------------------------------- consistency
    def test_forecast_matches_the_feed_and_the_journal(self):
        forecast = self.document["forecast"]
        row = next(row for row in self.rows
                   if row["candidate_id"] == forecast["candidate"]
                   and row["target"] == forecast["target"]
                   and row["horizon"] == forecast["horizon"])
        self.assertEqual(forecast["origin"], row["origin"])
        self.assertEqual(forecast["point"], row["point"])
        self.assertEqual(forecast["quantiles_5_to_95_step_5"],
                         row["quantiles_5_to_95_step_5"])
        self.assertEqual(forecast["origin"], self.feed["origin"])
        self.assertEqual(forecast["point"], self.feed["point"])
        self.assertEqual(forecast["candidate"], self.feed["candidate"])
        self.assertEqual(forecast["target"], self.feed["target"])
        self.assertEqual(forecast["horizon"], self.feed["horizon"])
        self.assertEqual(forecast["median"], self.feed["quantiles_5_to_95_step_5"][9])
        self.assertTrue(forecast["pre_origin"])

    def test_controls_match_the_journal_control_arms(self):
        arms = {row["candidate_id"]: row["point"] for row in self.rows
                if row.get("control_arm") and row["origin"] == self.document["forecast"]["origin"]}
        for arm in product_pack.CONTROL_ARMS:
            self.assertAlmostEqual(self.document["forecast"]["controls"][arm]["point"],
                                   arms[arm], places=12)

    def test_status_and_hashes_match_the_feed(self):
        document, feed = self.document, self.feed
        self.assertEqual(document["status"], feed["status"])
        self.assertEqual(document["status_label"], feed["status_label"])
        self.assertEqual(document["conditional_data_class"], feed["conditional_data_class"])
        self.assertEqual(document["hashes"]["protocol"], feed["protocol"]["sha256"])
        self.assertEqual(document["hashes"]["report"], feed["source_hashes"]["report"])
        self.assertEqual(document["hashes"]["journal"], feed["source_hashes"]["journal"])
        self.assertEqual(document["versions"]["protocol"]["version"], PROTOCOL_VERSION)
        self.assertEqual(document["versions"]["protocol"]["status"], "FROZEN")
        self.assertEqual(document["versions"]["r3"]["version"], "1.0.1")
        self.assertEqual(document["versions"]["nf_sim"]["study_id"], "NF-SIM")
        self.assertEqual(document["hashes"]["protocol"],
                         product_pack.lf_sha256(PROTOCOL))
        self.assertEqual(document["versions"]["journal"]["rows"], len(self.rows))

    def test_confirmed_is_c1_c2_c5_pass(self):
        confirmed = {item["id"]: item for item in self.document["confirmed"]}
        self.assertEqual(list(confirmed), ["C1", "C2", "C5"])
        criteria = self.report["admission"]["criteria"]
        for key in ("C1", "C2", "C5"):
            self.assertEqual(confirmed[key]["status"], "PASS")
            self.assertIs(criteria[key]["pass"], True)
        c1 = confirmed["C1"]["evidence"]
        self.assertAlmostEqual(c1["W1"]["RMSE"]["relative_loss"],
                               criteria["C1"]["windows"]["W1"]["RMSE"]["relative_loss"],
                               places=12)
        self.assertAlmostEqual(c1["W1"]["CRPS"]["relative_loss"],
                               criteria["C1"]["windows"]["W1"]["CRPS"]["relative_loss"],
                               places=12)
        c2 = confirmed["C2"]["evidence"]
        self.assertAlmostEqual(c2["W1"]["CRPS"]["adjusted_p_value"], 0.0887, places=12)
        self.assertAlmostEqual(c2["W2"]["CRPS"]["adjusted_p_value"], 0.0084, places=12)

    def test_not_confirmed_c4_and_c6(self):
        items = {item["id"]: item for item in self.document["not_confirmed"]}
        self.assertEqual(set(items), {"C4", "C6"})
        c4 = items["C4"]
        self.assertEqual(c4["status"], "FAIL")
        self.assertAlmostEqual(c4["rmse"]["relative_loss"], 1.0774564457828197, places=12)
        self.assertTrue(c4["crps"]["pass"])
        self.assertAlmostEqual(c4["crps"]["relative_loss"], 0.9303087995012138, places=12)
        self.assertAlmostEqual(c4["rmse"]["episodes"]["1990-91"], 1.0991911009430804, places=12)
        self.assertAlmostEqual(c4["rmse"]["episodes"]["2001"], 1.067220161565827, places=12)
        c6 = items["C6"]
        self.assertEqual(c6["status"], "NOT_PASSED")
        self.assertEqual(c6["n_resolved"], 0)
        self.assertEqual(c6["min_forecasts"], 12)
        self.assertEqual(c6["min_distinct_target_periods"], 4)
        self.assertIn("0/12", c6["summary"])
        self.assertIn("0/4", c6["summary"])

    def test_near_miss_ensemble_regime(self):
        near_miss = self.document["near_miss_candidate"]
        self.assertEqual(near_miss["id"], "ENSEMBLE_REGIME")
        self.assertEqual(near_miss["status"], "NEAR_MISS")
        c4 = self.regime["admission"]["criteria"]["C4"]["per_window"]
        self.assertAlmostEqual(near_miss["c4_rmse"]["W1"],
                               c4["W1"]["RMSE"]["relative_loss_recession"], places=12)
        self.assertAlmostEqual(near_miss["c4_rmse"]["W2"],
                               c4["W2"]["RMSE"]["relative_loss_recession"], places=12)
        self.assertAlmostEqual(near_miss["c4_crps"]["W1"],
                               c4["W1"]["CRPS"]["relative_loss_recession"], places=12)
        self.assertAlmostEqual(near_miss["c4_crps"]["W2"],
                               c4["W2"]["CRPS"]["relative_loss_recession"], places=12)
        self.assertAlmostEqual(near_miss["c1_w2_rmse"],
                               self.regime["admission"]["criteria"]["C1"]["windows"]["W2"]["RMSE"]
                               ["relative_loss"], places=12)
        self.assertAlmostEqual(near_miss["c4_rmse"]["W1"], 0.9733747411033493, places=9)
        self.assertAlmostEqual(near_miss["c4_crps"]["W2"], 1.0132839353437832, places=9)

    def test_changes_say_first_issue(self):
        self.assertEqual(self.document["changes"], [])
        note = self.document["changes_note"]
        self.assertIn("Первый выпуск", note)
        self.assertIn(f"{JOURNAL_ROWS} строк", note)
        self.assertIn("2026-11", note)
        self.assertIn("Первый выпуск", self.markdown)
        self.assertIn("Изменений прогноза нет", self.markdown)

    # ---------------------------------------------------------------------------- markdown
    def test_markdown_has_required_blocks(self):
        for heading in ("# AMFE — недельный бюллетень", "## 1. Статус и замороженная цепочка",
                        "## 2. Последний прогноз", "## 3. Что подтверждено",
                        "## 4. Что не подтверждено",
                        "## 5. Изменение к предыдущей записи журнала",
                        "## 6. Дисклеймер и ограничения", "## 7. Источники"):
            self.assertIn(heading, self.markdown, heading)

    def test_markdown_has_the_required_numbers(self):
        text = self.markdown
        report_sha = self.document["hashes"]["report"][:8]
        journal_sha = self.document["hashes"]["journal"][:8]
        for token in ("CANDIDATE", "conditional_data_class", PROTOCOL_VERSION, "FROZEN", "1.0.1",
                      "NF-SIM", PROTOCOL_SHA[:8], "5191b062", "f057e2a5", "50da8670", report_sha,
                      journal_sha, "ENSEMBLE_INVERSE_MSE", "2026-11", "T1_CPI_YOY",
                      "3.6257", "3.2737", "4.1029", "2.7936", "4.5035", "2.4579", "4.7971",
                      "RW", "EQUAL_WEIGHT_POINT", "EQUAL_WEIGHT_DENSITY",
                      "C1 PASS", "C2 PASS", "C5 PASS", "1.0775", "1.099", "1.067", "0.9303",
                      "0/12", "0/4", "ENSEMBLE_REGIME", "0.9734", "0.9035", "1.0019",
                      "1.0133", "0.9705"):
            self.assertIn(token, text, token)

    def test_markdown_has_the_disclaimer(self):
        self.assertIn("не допущенная модель", self.markdown)
        self.assertIn("не инвестиционная рекомендация", self.markdown)
        self.assertIn("SHADOW_TEST", self.markdown)
        self.assertIn("ACTIVE", self.markdown)

    # ------------------------------------------------------------------------- licence hygiene
    def test_no_raw_third_party_values(self):
        forbidden_keys = {"actual", "actuals", "actual_primary", "actual_secondary",
                          "observations", "observation", "raw", "raw_values", "series_values",
                          "values"}
        for path, key, _value in walk_keys(self.document):
            self.assertNotIn(key.lower(), forbidden_keys, f"raw-data key at {path}")
        text = self.markdown + json.dumps(self.document, ensure_ascii=False)
        for token in ("actual_primary", "actual_secondary", "data/raw", "data_log.json",
                      "first publication value", "\"observations\"", "\"actuals\""):
            self.assertNotIn(token, text, token)

    # ----------------------------------------------------------------- invariants / determinism
    def test_inputs_are_not_modified(self):
        for relative, digest in self.hashes_before.items():
            self.assertEqual(product_pack.lf_sha256(ROOT / relative), digest, relative)

    def test_determinism_except_generated_at(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            args = ["--root", str(self.root), "--date", DATE]
            self.assertEqual(weekly_bulletin.main(args + ["--out-dir", first,
                                                          "--generated-at", FIXED]), 0)
            self.assertEqual(weekly_bulletin.main(args + ["--out-dir", first,
                                                          "--generated-at", FIXED]), 0)
            self.assertEqual(weekly_bulletin.main(args + ["--out-dir", second,
                                                          "--generated-at", ANOTHER]), 0)
            for name in (f"{DATE}.md", f"{DATE}.json"):
                left = (Path(first) / name).read_text(encoding="utf-8")
                right = (Path(second) / name).read_text(encoding="utf-8")
                self.assertEqual(left.replace(FIXED, "<generated_at>"),
                                 right.replace(ANOTHER, "<generated_at>"), name)
                fresh = (Path(first) / name).read_text(encoding="utf-8")
                self.assertEqual(left, fresh, name)
            one = read_json(Path(second) / f"{DATE}.json")
            two = read_json(self.json_path)
            one["generated_at"] = two["generated_at"] = "<generated_at>"
            self.assertEqual(one, two)

    # --------------------------------------------------------------------------- fail-closed
    def test_fails_closed_without_the_product_pack(self):
        with tempfile.TemporaryDirectory() as base:
            root = minimal_root(Path(base), with_product=False)
            out = Path(base) / "bulletin"
            with contextlib.redirect_stderr(io.StringIO()):
                code = weekly_bulletin.main(["--root", str(root), "--out-dir", str(out),
                                             "--generated-at", FIXED, "--date", DATE,
                                             "--no-build"])
            self.assertEqual(code, 2)
            self.assertFalse((out / f"{DATE}.md").exists())
            self.assertFalse((out / f"{DATE}.json").exists())

    def test_fails_closed_on_an_empty_root(self):
        with tempfile.TemporaryDirectory() as empty, contextlib.redirect_stderr(io.StringIO()):
            code = weekly_bulletin.main(["--root", empty, "--out-dir",
                                         str(Path(empty) / "bulletin"), "--no-build"])
            self.assertEqual(code, 2)

    def test_builds_the_product_pack_when_it_is_missing(self):
        with tempfile.TemporaryDirectory() as base:
            root = minimal_root(Path(base), with_product=False)
            out = Path(base) / "bulletin"
            code = weekly_bulletin.main(["--root", str(root), "--out-dir", str(out),
                                         "--generated-at", FIXED, "--date", DATE])
            self.assertEqual(code, 0)
            self.assertTrue((root / "build" / "product" / "forecast_latest.json").is_file())
            self.assertTrue((out / f"{DATE}.md").is_file())
            self.assertTrue((out / f"{DATE}.json").is_file())

    def test_cli_runs_as_a_script(self):
        with tempfile.TemporaryDirectory() as base:
            root = minimal_root(Path(base), with_product=True)
            out = Path(base) / "bulletin"
            completed = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "weekly_bulletin.py"),
                 "--root", str(root), "--out-dir", str(out),
                 "--generated-at", FIXED, "--date", DATE],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertTrue(summary["ok"])
            self.assertEqual(summary["status"], "CANDIDATE")
            self.assertEqual(summary["date"], DATE)
            self.assertEqual(sorted(summary["files"]), sorted([f"{DATE}.md", f"{DATE}.json"]))
            for name in (f"{DATE}.md", f"{DATE}.json"):
                self.assertTrue((out / name).is_file(), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
