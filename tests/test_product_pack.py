"""Product pack (tools/product_pack.py): schema, statuses, consistency, no raw data, determinism.

The pack is a derived artefact: every number in it must be traceable to the forward registry, the v5
report or the frozen protocol, it must never contain raw third-party observations, rebuilding must be
byte-identical (apart from ``generated_at``) and the tool must not touch the append-only journal.
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

FIXED = "2026-11-12T12:00:00Z"
ANOTHER = "2026-11-12T18:30:00Z"

REPORT = ROOT / "protocols" / "first_real_evaluation_report_v5.json"
PROTOCOL = ROOT / "protocols" / "evaluation_protocol_v5.json"
PROTOCOL_PIN = ROOT / "protocols" / "evaluation_protocol_v5.sha256"
JOURNAL = ROOT / "registry" / "forecasts.jsonl"
REQUIRED_FILES = ("model_card.md", "forecast_latest.json", "dashboard.html",
                  "dashboard.md", "README.md")
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


def minimal_root(base: Path) -> Path:
    """A temporary repository with only the files the pack needs."""
    root = base / "repo"
    for relative in SOURCE_FILES:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    return root


class ProductPack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / "product"
        cls.hashes_before = {relative: product_pack.lf_sha256(ROOT / relative)
                             for relative in SOURCE_FILES}
        cls.code = product_pack.main(["--out-dir", str(cls.out), "--generated-at", FIXED])
        cls.feed = read_json(cls.out / "forecast_latest.json")
        cls.texts = {name: (cls.out / name).read_text(encoding="utf-8")
                     for name in REQUIRED_FILES if name != "forecast_latest.json"}
        cls.rows = [json.loads(line) for line in
                    JOURNAL.read_text(encoding="utf-8").splitlines() if line.strip()]
        cls.report = read_json(REPORT)
        cls.protocol = read_json(PROTOCOL)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # ------------------------------------------------------------------------------ structure
    def test_exit_code_and_files(self):
        self.assertEqual(self.code, 0)
        for name in REQUIRED_FILES:
            self.assertTrue((self.out / name).is_file(), name)

    def test_feed_schema(self):
        required = {
            "schema_version", "generated_at", "status", "status_label",
            "conditional_data_class", "protocol", "candidate", "target", "horizon", "origin",
            "point", "quantiles_5_to_95_step_5", "controls", "c4_status", "c6_status",
            "limitations", "source_hashes",
        }
        self.assertTrue(required.issubset(self.feed), required - set(self.feed))
        self.assertEqual(self.feed["schema_version"], product_pack.SCHEMA_VERSION)
        self.assertEqual(self.feed["generated_at"], FIXED)
        self.assertIsInstance(self.feed["point"], float)
        self.assertIsInstance(self.feed["limitations"], list)
        self.assertTrue(self.feed["limitations"])
        for key in ("pass", "rmse_relative_loss", "episodes"):
            self.assertIn(key, self.feed["c4_status"])
        for key in ("n_resolved", "min_forecasts", "min_distinct_target_periods"):
            self.assertIn(key, self.feed["c6_status"])

    def test_quantiles_are_19_and_strictly_increasing(self):
        quantiles = self.feed["quantiles_5_to_95_step_5"]
        self.assertEqual(len(quantiles), 19)
        self.assertTrue(all(isinstance(q, (int, float)) and not isinstance(q, bool)
                            for q in quantiles))
        self.assertTrue(all(b > a for a, b in zip(quantiles, quantiles[1:])))
        self.assertTrue(any(q != 0 for q in quantiles))

    def test_status_fields_match_the_report(self):
        admission = self.report["admission"]
        self.assertEqual(self.feed["status"], admission["status"])
        self.assertEqual(self.feed["status_label"], admission["status_label"])
        self.assertEqual(self.feed["conditional_data_class"],
                         admission["conditional_data_class"])
        self.assertEqual(self.feed["candidate"], self.report["candidate"])
        self.assertEqual(self.feed["target"], self.report["target"])
        self.assertEqual(self.feed["horizon"], self.report["horizon"])
        self.assertEqual(self.feed["status"], "CANDIDATE")
        self.assertTrue(self.feed["conditional_data_class"])

    # --------------------------------------------------------------------------- consistency
    def test_forecast_is_the_journal_row(self):
        row = next(row for row in self.rows
                   if row["candidate_id"] == self.feed["candidate"]
                   and row["target"] == self.feed["target"]
                   and row["horizon"] == self.feed["horizon"])
        self.assertEqual(self.feed["origin"], row["origin"])
        self.assertEqual(self.feed["point"], row["point"])
        self.assertEqual(self.feed["quantiles_5_to_95_step_5"],
                         row["quantiles_5_to_95_step_5"])

    def test_controls_are_the_journal_control_arms(self):
        arms = {}
        for row in self.rows:
            if row.get("control_arm") and row["origin"] == self.feed["origin"]:
                arms[row["candidate_id"]] = row["point"]
        self.assertEqual(set(self.feed["controls"]), set(product_pack.CONTROL_ARMS))
        for arm, value in self.feed["controls"].items():
            self.assertEqual(value["point"], arms[arm], arm)

    def test_c4_matches_the_report(self):
        admission = self.report["admission"]
        c4 = admission["criteria"]["C4"]
        w1_rmse = c4["per_window"]["W1"]["RMSE"]
        w1_crps = c4["per_window"]["W1"]["CRPS"]
        status = self.feed["c4_status"]
        self.assertEqual(status["pass"], c4["pass"])
        self.assertFalse(status["pass"])
        self.assertAlmostEqual(status["rmse_relative_loss"],
                               w1_rmse["relative_loss_recession"], places=12)
        self.assertAlmostEqual(status["rmse_relative_loss"], 1.0774564457828197, places=9)
        self.assertAlmostEqual(status["crps_relative_loss"],
                               w1_crps["relative_loss_recession"], places=12)
        self.assertTrue(status["crps_pass"])
        self.assertFalse(status["rmse_pass"])
        episodes = {episode["episode"]: episode for episode in status["episodes"]}
        self.assertAlmostEqual(episodes["1990-91"]["rmse_relative_loss"],
                               self.report["c4_episodes"]["W1_EXT"]["1990_91"]["RMSE"]
                               ["relative_loss_episode"], places=12)
        self.assertAlmostEqual(episodes["2001"]["rmse_relative_loss"],
                               self.report["c4_episodes"]["W1_EXT"]["2001"]["RMSE"]
                               ["relative_loss_episode"], places=12)

    def test_c6_matches_the_protocol_and_the_report(self):
        admission = self.report["admission"]
        c6 = self.feed["c6_status"]
        self.assertEqual(c6["n_resolved"], 0)
        self.assertEqual(c6["min_forecasts"], 12)
        self.assertEqual(c6["min_distinct_target_periods"],
                         self.protocol["forward_registry"]["c6_min_distinct_target_periods"])
        self.assertEqual(c6["status"], admission["criteria"]["C6"]["status"])
        self.assertFalse(c6["pass"])

    def test_source_hashes_and_pins(self):
        hashes = self.feed["source_hashes"]
        self.assertEqual(set(hashes), {"protocol", "report", "journal"})
        self.assertEqual(hashes["protocol"], product_pack.lf_sha256(PROTOCOL))
        self.assertEqual(hashes["protocol"], PROTOCOL_PIN.read_text(encoding="utf-8").split()[0])
        self.assertEqual(hashes["report"], product_pack.lf_sha256(REPORT))
        self.assertEqual(hashes["journal"], product_pack.lf_sha256(JOURNAL))
        self.assertEqual(self.feed["protocol"]["version"], self.protocol["version"])
        self.assertEqual(self.feed["protocol"]["sha256"], hashes["protocol"])

    # ------------------------------------------------------------------------- licence hygiene
    def test_no_raw_third_party_values_in_the_feed(self):
        forbidden_keys = {"actual", "actuals", "actual_primary", "actual_secondary",
                          "observations", "observation", "raw", "raw_values", "series_values",
                          "values"}
        for path, key, _value in walk_keys(self.feed):
            self.assertNotIn(key.lower(), forbidden_keys, f"raw-data key at {path}")
        text = json.dumps(self.feed, ensure_ascii=False)
        for token in ("actual_primary", "actual_secondary", "\"observations\"", "\"actuals\""):
            self.assertNotIn(token, text)

    def test_documents_have_no_raw_data_markers(self):
        for name in ("model_card.md", "dashboard.md", "dashboard.html", "README.md"):
            text = self.texts[name]
            for token in ("actual_primary", "actual_secondary", "\"observations\"",
                          "data_log.json", "first publication value"):
                self.assertNotIn(token, text, f"{name} contains {token}")

    # ---------------------------------------------------------------------------- dashboard
    def test_dashboard_is_static_and_has_no_external_resources(self):
        page = self.texts["dashboard.html"]
        self.assertIn('<meta charset="utf-8">', page)
        self.assertIn("<html", page)
        self.assertNotIn("<script", page.lower())
        for token in ("http://", "https://", "cdn.", "react", "vue.", "chart.js"):
            self.assertNotIn(token, page.lower(), token)
        self.assertIn("CANDIDATE", page)
        self.assertIn("C6", page)
        self.assertIn("1.0775", page)
        self.assertIn("41d6823e", page)

    def test_dashboard_markdown_has_the_required_blocks(self):
        text = self.texts["dashboard.md"]
        for heading in ("## 1. Статус", "## 2. Последний прогноз и интервалы",
                        "## 3. Что не подтверждено", "## 4. C6-счётчик"):
            self.assertIn(heading, text)
        self.assertIn("0 / 12", text)
        self.assertIn("CANDIDATE", text)

    # --------------------------------------------------------------------------- model card
    def test_model_card_required_content(self):
        text = self.texts["model_card.md"]
        for token in ("## 1. Назначение", "## 2. Версии и хеши", "## 3. Что подтверждено",
                      "## 4. Что НЕ подтверждено", "## 6. Ограничения", "Дата снимка",
                      "CANDIDATE", "conditional_data_class", "1.0775", "1.099", "1.067",
                      "0.9303", "41d6823e", "1.0.1", "NF-SIM", "C6", "docs/107", "docs/110"):
            self.assertIn(token, text, token)

    def test_readme_states_it_is_not_admission(self):
        text = self.texts["README.md"]
        self.assertIn("не допуск", text)
        self.assertIn("python tools/product_pack.py", text)
        self.assertIn("C4", text)
        self.assertIn("C6", text)

    # ------------------------------------------------------------------- invariants / determinism
    def test_journal_and_frozen_inputs_are_not_modified(self):
        for relative, digest in self.hashes_before.items():
            self.assertEqual(product_pack.lf_sha256(ROOT / relative), digest, relative)

    def test_determinism_except_generated_at(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            self.assertEqual(product_pack.main(["--out-dir", first, "--generated-at", FIXED]), 0)
            self.assertEqual(product_pack.main(["--out-dir", first, "--generated-at", FIXED]), 0)
            self.assertEqual(product_pack.main(["--out-dir", second, "--generated-at", ANOTHER]), 0)
            for name in REQUIRED_FILES:
                left = (Path(first) / name).read_text(encoding="utf-8")
                right = (Path(second) / name).read_text(encoding="utf-8")
                self.assertEqual(left.replace(FIXED, "<generated_at>"),
                                 right.replace(ANOTHER, "<generated_at>"), name)
            for name in REQUIRED_FILES:
                one = (self.out / name).read_text(encoding="utf-8")
                two = (Path(first) / name).read_text(encoding="utf-8")
                self.assertEqual(one, two, name)

    def test_fails_closed_on_a_broken_root(self):
        with tempfile.TemporaryDirectory() as empty, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(product_pack.main(
                ["--root", empty, "--out-dir", str(Path(empty) / "out")]), 2)

    def test_fails_closed_on_a_pin_mismatch(self):
        with tempfile.TemporaryDirectory() as base:
            root = minimal_root(Path(base))
            pin = root / "protocols" / "evaluation_protocol_v5.sha256"
            pin.write_text("0" * 64 + "  evaluation_protocol_v5.json\n", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                code = product_pack.main(
                    ["--root", str(root), "--out-dir", str(root / "build" / "product")])
            self.assertEqual(code, 2)

    def test_cli_runs_as_a_script(self):
        with tempfile.TemporaryDirectory() as out:
            completed = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "product_pack.py"),
                 "--out-dir", out, "--generated-at", FIXED],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertTrue(summary["ok"])
            self.assertEqual(summary["status"], "CANDIDATE")
            self.assertEqual(len(summary["files"]), len(REQUIRED_FILES))
            for name in REQUIRED_FILES:
                self.assertTrue((Path(out) / name).is_file(), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
