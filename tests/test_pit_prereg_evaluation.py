#!/usr/bin/env python3
"""Tests for tools/run_pit_prereg_evaluation.py (offline synthetic fixtures)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import run_pit_prereg_evaluation as rpe  # noqa: E402


def write_json(path: Path, document: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, ensure_ascii=False, indent=1) + "\n"
    path.write_text(payload, encoding="utf-8", newline="\n")
    return rpe.sha256_file(path)


def make_reports(tmp: Path, *, c4_status: str = "PASS", reconstructed: int = 83,
                 protocol_sha: str, candidate: str = "ENSEMBLE_INVERSE_MSE") -> Path:
    report = {
        "candidate": candidate,
        "target": "T1_CPI_YOY",
        "horizon": 1,
        "data_class": "REAL_TIME_RECONSTRUCTED",
        "conditional_data_class": c4_status == "PASS",
        "protocol": {"file": "protocols/evaluation_protocol_v5.json", "version": "5.0.1",
                     "sha256": protocol_sha},
        "reconstruction": {"w1_origins_total": 204, "w1_origins_real_time": 204 - reconstructed,
                           "w1_origins_reconstructed": reconstructed,
                           "first_real_vintage": "1996-12-31"},
        "windows": {
            "W1_EXT": {"window": "W1_EXT", "origins": 204, "used_origins": list(range(204)),
                       "data_class": "REAL_TIME_RECONSTRUCTED"},
            "W1": {"window": "W1", "origins": 121, "used_origins": list(range(121)),
                   "data_class": "REAL_TIME"},
            "W2": {"window": "W2", "origins": 156, "used_origins": list(range(156)),
                   "data_class": "REAL_TIME"},
        },
        "admission": {
            "criteria": {
                "C1": {"pass": True},
                "C2": {"pass": True},
                "C4": {"per_window": {"W1": {"RMSE": {"status": c4_status,
                                                       "relative_loss_recession": 1.08,
                                                       "recession_origins": 8}}},
                       "extended": {"W1": {"data_class": ("REAL_TIME_RECONSTRUCTED"
                                                         if reconstructed else "REAL_TIME"),
                                          "n_origins": 204, "source": "extended_window"}}},
                "C5": {"pass": True},
                "C6": {"status": "NO_RESOLVED_FORECASTS", "pass": False},
            },
            "status": "CANDIDATE",
        },
        "c4_episodes": {"W1_EXT": {"1990_91": {"RMSE": {"origins": 8, "relative_loss_leave_one_out": 0.58}},
                                    "2001": {"RMSE": {"origins": 8, "relative_loss_leave_one_out": 0.57}}}},
    }
    path = tmp / "report.json"
    write_json(path, report)
    return path


def make_prereg(tmp: Path, protocol_sha: str) -> Path:
    protocol = tmp / "protocol_v5.json"
    write_json(protocol, {"version": "5.0.1"})
    protocol_sha = rpe.sha256_file(protocol)
    coverage = tmp / "coverage.json"
    write_json(coverage, {"data_root": {"tree_digest": "a" * 64}})
    dossier = tmp / "dossier.json"
    write_json(dossier, {"schema": "amfe.pit_reconstruction.v2"})
    prereg = {
        "preregistration_id": "AMFE-PIT-PREREG-0.3",
        "version": "0.3.0",
        "status": "PROPOSED",
        "protocol_basis": {
            "v5": {"file": str(protocol.relative_to(tmp)), "version": "5.0.1", "sha256": protocol_sha},
        },
        "data_basis": {
            "reconstruction_dossier": {"file": str(dossier.relative_to(tmp)),
                                       "sha256": rpe.sha256_file(dossier)},
            "coverage_audit": {"file": str(coverage.relative_to(tmp)), "tree_digest": "a" * 64},
        },
        "content_sha256": "b" * 64,
    }
    path = tmp / "prereg.json"
    write_json(path, prereg)
    (tmp / "prereg.json.sha256.txt").write_text(rpe.sha256_file(path) + "\n", encoding="utf-8")
    return path


class PreregEvaluationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.protocol = self.dir / "protocol_v5.json"
        write_json(self.protocol, {"version": "5.0.1"})
        self.prereg = make_prereg(self.dir, rpe.sha256_file(self.protocol))

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, **kwargs):
        prereg = json.loads(self.prereg.read_text(encoding="utf-8"))
        return rpe.build_report(prereg, self.prereg, [self.report(**kwargs)], root=self.dir)

    def report(self, **kwargs):
        return make_reports(self.dir, protocol_sha=rpe.sha256_file(self.protocol), **kwargs)

    def test_pins_and_conditional_pass_label(self):
        report = self.build(c4_status="PASS", reconstructed=83)
        self.assertTrue(report["pins_ok"])
        self.assertFalse(report["admission"])
        self.assertEqual(report["status"], "PROVISIONAL")
        label = report["labels"][0]
        self.assertEqual(label["label"], "CONDITIONAL_DATA_CLASS")
        self.assertTrue(label["conditional_data_class"])
        self.assertAlmostEqual(report["candidates"][0]["reconstructed_origin_share"], 83 / 204, places=6)

    def test_c4_failure_is_not_a_pass(self):
        report = self.build(c4_status="FAIL")
        self.assertEqual(report["labels"][0]["label"], "CANDIDATE")

    def test_zero_reconstructed_origins_makes_it_unconditional(self):
        report = self.build(c4_status="PASS", reconstructed=0)
        self.assertEqual(report["labels"][0]["label"], "PASS")
        self.assertFalse(report["labels"][0]["conditional_data_class"])

    def test_protocol_sha_mismatch_fails_closed(self):
        report = json.loads(self.report(c4_status="PASS").read_text(encoding="utf-8"))
        report["protocol"]["sha256"] = "c" * 64
        path = self.dir / "bad_report.json"
        write_json(path, report)
        prereg = json.loads(self.prereg.read_text(encoding="utf-8"))
        with self.assertRaises(rpe.PreregError):
            rpe.build_report(prereg, self.prereg, [path], root=self.dir)

    def test_prereg_pin_mismatch_fails_closed(self):
        (self.dir / "prereg.json.sha256.txt").write_text("d" * 64 + "\n", encoding="utf-8")
        prereg = json.loads(self.prereg.read_text(encoding="utf-8"))
        with self.assertRaises(rpe.PreregError):
            rpe.build_report(prereg, self.prereg, [self.report()], root=self.dir)

    def test_coverage_digest_mismatch_fails_closed(self):
        coverage = self.dir / "coverage.json"
        write_json(coverage, {"data_root": {"tree_digest": "e" * 64}})
        prereg = json.loads(self.prereg.read_text(encoding="utf-8"))
        with self.assertRaises(rpe.PreregError):
            rpe.build_report(prereg, self.prereg, [self.report()], root=self.dir)

    def test_report_is_deterministic(self):
        first = self.build()
        second = self.build()
        self.assertEqual(json.dumps(first, sort_keys=True, ensure_ascii=False),
                         json.dumps(second, sort_keys=True, ensure_ascii=False))

    def test_no_raw_origin_arrays_in_output(self):
        report = self.build()
        text = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("used_origins", text)
        self.assertNotIn("vintages", text)
        self.assertLess(len(text), 40000)

    def test_cli_check_mode_and_fail_exit_code(self):
        out = self.dir / "out.json"
        code = rpe.main(["--prereg", str(self.prereg), "--report", str(self.report()),
                         "--out", str(out), "--check", "--root", str(self.dir)])
        self.assertEqual(code, 0)
        self.assertFalse(out.exists())
        (self.dir / "prereg.json.sha256.txt").write_text("f" * 64 + "\n", encoding="utf-8")
        code = rpe.main(["--prereg", str(self.prereg), "--report", str(self.report()),
                         "--out", str(out), "--root", str(self.dir)])
        self.assertEqual(code, 2)
        self.assertFalse(out.exists())

    def test_w1_w2_data_classes_are_read_from_the_report(self):
        report = self.build()
        item = report["candidates"][0]
        self.assertEqual(item["windows"]["w1_data_class"], "REAL_TIME")
        self.assertEqual(item["windows"]["w2_data_class"], "REAL_TIME")
        self.assertEqual(item["windows"]["w1_ext_data_class"], "REAL_TIME_RECONSTRUCTED")
        self.assertEqual(item["slices"]["real_time_only"]["window"], "W1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
