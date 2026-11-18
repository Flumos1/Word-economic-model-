#!/usr/bin/env python3
"""Structural and consistency tests for ``protocols/horizon_matrix_v5.json`` (task-58).

The matrix is a reporting artefact: it must contain no raw forecasts/actuals, must use the honest
status vocabulary (CANDIDATE/FAIL/INCONCLUSIVE/NOT_RUN), must cover every cell of the protocol grid
and must agree with the reports it points at (docs/176).

Run either way (the project uses unittest, not pytest):

    python -m unittest tests.test_horizon_matrix_v5
    python tests/test_horizon_matrix_v5.py
"""
from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "protocols" / "horizon_matrix_v5.json"
PROTOCOL = ROOT / "protocols" / "evaluation_protocol_v5.json"
CANDIDATES = ROOT / "protocols" / "candidates_2026-11-11.json"
DOC = ROOT / "docs" / "176_Матрица_горизонтов_v5.md"
STATUSES = {"CANDIDATE", "FAIL", "INCONCLUSIVE", "NOT_RUN"}
CRITERIA = ("C1", "C2", "C4", "C5")
CRITERION_VOCAB = {"pass", "fail", "not_applicable", "not_run"}
REQUIRED = ("target", "horizon", "candidate", "protocol", "data_class", "origins",
            "criteria", "metrics", "overall_status", "reason", "sources")
FORBIDDEN_KEYS = {"actual_primary", "actual_secondary", "quantiles", "probability", "point",
                  "used_origins", "vintage_dates", "recession", "member_forecasts",
                  "actual_values", "forecast_values"}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class HorizonMatrixV5Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not MATRIX.exists():  # pragma: no cover - build first
            raise unittest.SkipTest(f"missing {MATRIX}; run tools/build_horizon_matrix_v5.py")
        cls.matrix = _load(MATRIX)
        cls.rows = cls.matrix["rows"]
        cls.protocol = _load(PROTOCOL)
        cls.candidates = _load(CANDIDATES)["candidates"]

    # ------------------------------------------------------------------ structure
    def test_matrix_exists_and_parses(self) -> None:
        self.assertEqual(self.matrix["matrix_id"], "AMFE-HORIZON-MATRIX-V5")
        self.assertEqual(self.matrix["schema_version"], "1.0.0")
        self.assertTrue(self.rows, "matrix has no rows")
        self.assertRegex(self.matrix["generated_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_protocol_digest_matches_file(self) -> None:
        block = self.matrix["protocol"]
        self.assertEqual(block["file"], "protocols/evaluation_protocol_v5.json")
        self.assertEqual(block["version"], self.protocol["version"])
        self.assertEqual(block["sha256"], _sha(PROTOCOL))
        self.assertEqual(self.matrix["legacy_protocol"]["sha256"],
                         _sha(ROOT / "protocols" / "evaluation_protocol_v4.json"))

    def test_every_row_has_required_fields(self) -> None:
        for index, row in enumerate(self.rows):
            missing = [field for field in REQUIRED if field not in row]
            self.assertFalse(missing, f"row {index} missing {missing}")
            self.assertIsInstance(row["horizon"], int, row)
            self.assertTrue(row["reason"].strip(), f"row {index} empty reason")
            self.assertIsInstance(row["origins"], dict, row)
            self.assertIsInstance(row["sources"], list, row)

    def test_status_vocabulary(self) -> None:
        for index, row in enumerate(self.rows):
            self.assertIn(row["overall_status"], STATUSES, f"row {index}: {row['overall_status']}")
            self.assertEqual(sorted(row["criteria"]), sorted(CRITERIA), f"row {index}")
            for name, value in row["criteria"].items():
                self.assertIn(value, CRITERION_VOCAB, f"row {index} {name}={value}")
            if row["overall_status"] == "NOT_RUN":
                self.assertEqual(set(row["criteria"].values()), {"not_run"}, f"row {index}")
                self.assertFalse(row["metrics"], f"row {index} NOT_RUN must carry no metrics")
            else:
                self.assertNotIn("not_run", row["criteria"].values(), f"row {index}")

    def test_no_raw_values_or_arrays(self) -> None:
        def walk(value, path="root"):
            if isinstance(value, dict):
                self.assertFalse(set(value) & FORBIDDEN_KEYS, f"raw key at {path}")
                for key, item in value.items():
                    walk(item, f"{path}.{key}")
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    self.assertIsInstance(item, dict, f"array of values at {path}[{index}]")
                    walk(item, f"{path}[{index}]")
            else:
                self.assertIsInstance(value, (str, int, float, bool, type(None)), path)

        # rows and every nested aggregate: dicts, strings, numbers and nulls only -- no bare arrays
        for index, row in enumerate(self.rows):
            walk(row, f"rows[{index}]")

        def no_numeric_arrays(value, path="root"):
            if isinstance(value, dict):
                for key, item in value.items():
                    no_numeric_arrays(item, f"{path}.{key}")
            elif isinstance(value, list):
                for item in value:
                    self.assertFalse(isinstance(item, (int, float)) and not isinstance(item, bool),
                                     f"numeric array at {path}")
                    no_numeric_arrays(item, f"{path}[]")

        no_numeric_arrays(self.matrix)

    # ------------------------------------------------------------------ honesty
    def test_every_not_run_has_reason(self) -> None:
        missing = [row for row in self.rows
                   if row["overall_status"] == "NOT_RUN" and len(row["reason"].strip()) < 40]
        self.assertFalse(missing, f"{len(missing)} NOT_RUN rows without a substantive reason")
        self.assertTrue(any(row["overall_status"] == "NOT_RUN" for row in self.rows),
                        "matrix must declare its gaps explicitly")

    def test_not_run_metadata_is_honest(self) -> None:
        for row in self.rows:
            if row["overall_status"] != "NOT_RUN":
                continue
            self.assertIsNone(row["data_class"], row)
            self.assertEqual(row["origins"], {}, row)
            self.assertTrue(row["sources"], row)

    def test_sources_exist_and_digests_match(self) -> None:
        for row in self.rows:
            self.assertTrue(row["sources"], row)
            for source in row["sources"]:
                path = ROOT / source["file"]
                self.assertTrue(path.exists(), source)
                self.assertEqual(_sha(path), source["sha256"], source)

    # ------------------------------------------------------------------ coverage
    def test_cells_cover_protocol_grid(self) -> None:
        expected = set()
        for target in self.protocol["targets"]:
            horizons = target.get("horizons_months") or target.get("horizons_quarters") or []
            for horizon in horizons:
                for candidate in self.candidates:
                    if target["id"] in candidate["targets"]:
                        expected.add((target["id"], int(horizon), candidate["id"]))
        actual = {(row["target"], row["horizon"], row["candidate"]) for row in self.rows}
        self.assertEqual(actual, expected, "matrix cells != protocol targets x horizons x candidates")
        self.assertEqual(len(self.rows), len(expected), "duplicate cells")

    def test_totals_match_coverage(self) -> None:
        coverage = self.matrix["coverage"]
        self.assertEqual(coverage["rows"], len(self.rows))
        counted = {status: sum(1 for row in self.rows if row["overall_status"] == status)
                   for status in STATUSES}
        self.assertEqual(coverage["status_counts"], counted)
        self.assertEqual(coverage["evaluated"], len(self.rows) - counted["NOT_RUN"])
        self.assertEqual(coverage["not_run"], counted["NOT_RUN"])
        for target, horizons in coverage["cells"].items():
            for horizon, candidates in horizons.items():
                self.assertEqual(
                    len(candidates),
                    sum(1 for row in self.rows
                        if row["target"] == target and row["horizon"] == int(horizon)),
                    (target, horizon))

    def test_protocol_labels_and_data_classes(self) -> None:
        for row in self.rows:
            if row["overall_status"] == "NOT_RUN":
                self.assertEqual(row["protocol"], "v4.0.0", row)
                continue
            self.assertIn(row["protocol"], {"v5.0.1", "v4.0.0"}, row)
            if row["protocol"] == "v4.0.0":
                self.assertEqual(row["data_class"], "REAL_TIME", row)
                continue
            # v5 rows: the report-level class must agree with the C4 window actually used
            extended = ((row["metrics"].get("C4") or {}).get("extended") or {}).get("W1") or {}
            expected = ("REAL_TIME_RECONSTRUCTED"
                        if extended.get("source") == "extended_window" else "REAL_TIME")
            self.assertEqual(row["data_class"], expected, row)

    # ------------------------------------------------------------------ targeted facts
    def test_v5_t1_h1_reference(self) -> None:
        inverse = [row for row in self.rows if row["target"] == "T1_CPI_YOY"
                   and row["horizon"] == 1 and row["candidate"] == "ENSEMBLE_INVERSE_MSE"
                   and row["protocol"] == "v5.0.1"]
        self.assertEqual(len(inverse), 1, "v5 T1 h=1 reference row for ENSEMBLE_INVERSE_MSE")
        row = inverse[0]
        self.assertEqual(row["overall_status"], "CANDIDATE")
        self.assertEqual(row["criteria"]["C1"], "pass")
        self.assertEqual(row["criteria"]["C2"], "pass")
        self.assertIn("W1_EXT", row["origins"])
        self.assertGreaterEqual(row["origins"]["W1_EXT"], 200)
        self.assertTrue(row["metrics"]["C4"]["applies"])
        self.assertTrue(row["metrics"]["C4"]["extended"]["W1"]["source"] == "extended_window")

    def test_spf_t2_h0_evaluated_higher_horizons_not_run(self) -> None:
        zero = [row for row in self.rows if row["target"] == "T2_GDP_GROWTH"
                and row["horizon"] == 0 and row["candidate"] == "ENSEMBLE_T2_SPF"]
        self.assertEqual(len(zero), 1, "T2 h=0 SPF pair")
        self.assertEqual(zero[0]["overall_status"], "CANDIDATE")
        self.assertNotEqual(zero[0]["criteria"]["C1"], "not_run")
        for horizon in (1, 2, 4):
            row = [r for r in self.rows if r["target"] == "T2_GDP_GROWTH"
                   and r["horizon"] == horizon and r["candidate"] == "ENSEMBLE_T2_SPF"][0]
            self.assertEqual(row["overall_status"], "NOT_RUN", row)
            self.assertIn("SPF", row["reason"])
            self.assertIn("h=1/2/4", row["reason"])

    def test_t1_h12_evaluated_for_three_candidates(self) -> None:
        rows = [row for row in self.rows if row["target"] == "T1_CPI_YOY" and row["horizon"] == 12]
        self.assertEqual(len(rows), 5, rows)
        evaluated = {row["candidate"]: row for row in rows if row["overall_status"] != "NOT_RUN"}
        self.assertEqual(set(evaluated), {"ENSEMBLE_ADAPTIVE", "ENSEMBLE_EQUAL",
                                          "ENSEMBLE_INVERSE_MSE"}, sorted(evaluated))
        for candidate, row in evaluated.items():
            self.assertEqual(row["protocol"], "v5.0.1", (candidate, row["protocol"]))
            self.assertEqual(row["data_class"], "REAL_TIME_RECONSTRUCTED", (candidate, row))
            self.assertEqual(row["overall_status"], "CANDIDATE", (candidate, row))
            # C4 for T1 v5 is evaluated on the extended reconstructed window
            self.assertEqual(row["metrics"]["C4"]["extended"]["W1"]["source"],
                             "extended_window", candidate)
            self.assertGreaterEqual(row["origins"]["W1_EXT"], 190, candidate)
            self.assertGreater(row["origins"]["W1"], 0, candidate)
            self.assertGreaterEqual(row["origins"]["W1_EXT"] - row["origins"]["W1"], 50, candidate)
            self.assertEqual(sorted(row["metrics"]["C1"]["windows"]), ["W1", "W2"], candidate)
            for window, losses in row["metrics"]["C1"]["windows"].items():
                for loss, entry in losses.items():
                    self.assertIsNotNone(entry["threshold"], (candidate, window, loss))
        # the pass is explicitly partial: BAYES and RW_AR stay NOT_RUN with a dated reason
        not_run = [row for row in rows if row["overall_status"] == "NOT_RUN"]
        self.assertEqual({row["candidate"] for row in not_run},
                         {"ENSEMBLE_BAYES", "ENSEMBLE_RW_AR"})
        for row in not_run:
            self.assertIn("task-58", row["reason"])
            self.assertIn("time-box", row["reason"])
            self.assertIn("ENSEMBLE_INVERSE_MSE", row["reason"])
        doc = DOC.read_text(encoding="utf-8") if DOC.exists() else ""
        self.assertIn("T1 h=12: результаты", doc)

    def test_t3_statuses_come_from_v4_and_v5_rerun_is_not_run(self) -> None:
        rows = [row for row in self.rows if row["target"] == "T3_RECESSION"]
        self.assertEqual(len(rows), 12, "4 candidates x 3 horizons")
        self.assertEqual(sum(1 for row in rows if row["overall_status"] != "NOT_RUN"), 6)
        for row in rows:
            if row["overall_status"] == "NOT_RUN":
                self.assertIn("T3", row["reason"])
                continue
            self.assertEqual(row["protocol"], "v4.0.0", row)
            self.assertEqual(row["overall_status"], "CANDIDATE", row)
            self.assertEqual(row["criteria"]["C4"], "not_applicable", row)
        gaps = self.matrix["v5_reruns_not_performed"]
        self.assertTrue(any("T3_RECESSION" in gap["scope"] and gap["status"] == "NOT_RUN"
                            for gap in gaps), gaps)

    # ------------------------------------------------------------------ documentation
    def test_docs_176_covers_matrix_and_gaps(self) -> None:
        self.assertTrue(DOC.exists(), f"missing {DOC}")
        text = DOC.read_text(encoding="utf-8")
        for token in ("T1_CPI_YOY", "T2_GDP_GROWTH", "T3_RECESSION", "NOT_RUN", "INCONCLUSIVE"):
            self.assertIn(token, text, token)
        self.assertRegex(text, r"h\s*=\s*12")
        self.assertIn("56", text)
        self.assertIn(str(self.matrix["coverage"]["not_run"]), text)
        self.assertIn(str(self.matrix["coverage"]["evaluated"]), text)
        self.assertIn("T1 h=12: результаты", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
