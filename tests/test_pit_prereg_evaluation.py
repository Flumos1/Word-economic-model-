#!/usr/bin/env python3
"""Tests for tools/run_pit_prereg_evaluation.py (offline synthetic fixtures).

V2 re-release (docs/172, task-67) contract: every mandatory slice must be its own computation, the
preregistration pins must be checked under both pin spellings plus the canonical ``content_sha256``,
the coverage tree digest must be recomputed from the data root, ``w1_data_class`` is REAL_TIME, the
conditional-data-class run flag is separated from the conditional pass, and LOO 2007-09 / C4 W2 are
published.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import reconstruct_pit as rp  # noqa: E402
import run_pit_prereg_evaluation as rpe  # noqa: E402

FIRST_REAL = "1996-12-31"
#: V2 numbers that the re-release must reproduce (docs/172 §7).
REAL_TIME_ONLY_LOSS = 1.0672202
LOO_1990_91, LOO_2001, LOO_2007_09 = 0.5809326, 0.5705337, 0.8629
C4_W1_EXT_LOSS = 1.0774564457828197
C4_W2_LOSS = 0.9406


def write_json(path: Path, document: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, ensure_ascii=False, indent=1) + "\n"
    path.write_text(payload, encoding="utf-8", newline="\n")
    return rpe.sha256_file(path)


def month_ends(start_year: int, start_month: int, count: int) -> list[str]:
    out = []
    year, month = start_year, start_month
    for _ in range(count):
        if month == 12:
            last = date(year, 12, 31)
        else:
            last = date(year, month + 1, 1).toordinal() - 1
            last = date.fromordinal(last)
        out.append(last.isoformat())
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return out


W1_EXT_ORIGINS = month_ends(1990, 1, 204)      # 1990-01 .. 2006-12
W1_ORIGINS = month_ends(1996, 12, 121)         # 1996-12 .. 2006-12
W2_ORIGINS = month_ends(2007, 1, 156)          # 2007-01 .. 2019-12


def make_primary(tmp: Path, *, protocol_sha: str, candidate: str = "ENSEMBLE_INVERSE_MSE",
                 c4_status: str = "FAIL", reconstructed: int = 83,
                 real_time: int = 121, slice_status: str = "INCONCLUSIVE",
                 slice_loss: float = REAL_TIME_ONLY_LOSS, slice_recessions: int = 8,
                 w1_data_class: str = "REAL_TIME_RECONSTRUCTED") -> Path:
    origins = W1_EXT_ORIGINS[-int(reconstructed + real_time):]
    w1_used = origins[-int(real_time):]
    report = {
        "report_id": "AMFE-FIRST-REAL-EVALUATION",
        "candidate": candidate,
        "target": "T1_CPI_YOY",
        "horizon": 1,
        "data_class": "REAL_TIME_RECONSTRUCTED",
        "conditional_data_class": True,
        "protocol": {"file": "protocols/evaluation_protocol_v5.json", "version": "5.0.1",
                     "sha256": protocol_sha},
        "reconstruction": {"extended_c4": True, "first_real_vintage": FIRST_REAL,
                           "w1_origins_total": int(len(origins)),
                           "w1_origins_real_time": int(real_time),
                           "w1_origins_reconstructed": int(reconstructed)},
        "windows": {
            "W1_EXT": {"window": "W1_EXT", "origins": len(origins), "used_origins": origins,
                       "first_origin": origins[0], "last_origin": origins[-1],
                       "data_class": "REAL_TIME_RECONSTRUCTED"},
            "W1": {"window": "W1", "origins": int(real_time), "used_origins": w1_used,
                   "first_origin": w1_used[0], "last_origin": w1_used[-1],
                   "origin_floor": FIRST_REAL, "data_class": w1_data_class},
            "W2": {"window": "W2", "origins": len(W2_ORIGINS), "used_origins": W2_ORIGINS,
                   "first_origin": W2_ORIGINS[0], "last_origin": W2_ORIGINS[-1],
                   "data_class": "REAL_TIME"},
        },
        "admission": {
            "criteria": {
                "C1": {"pass": True},
                "C2": {"pass": True},
                "C4": {"per_window": {
                    "W1": {"RMSE": {"status": c4_status, "recession_origins": 16,
                                    "min_recession_origins": 12,
                                    "relative_loss_recession": C4_W1_EXT_LOSS,
                                    "mean_differential_recession": 0.02103046963944405,
                                    "conditional_gw_p": 7.435397984381002e-08,
                                    "favours_comparator_in_recession": True,
                                    "pass": c4_status == "PASS"}},
                    "W2": {"RMSE": {"status": "PASS", "recession_origins": 18,
                                    "min_recession_origins": 12,
                                    "relative_loss_recession": C4_W2_LOSS,
                                    "mean_differential_recession": -0.2920560308487014,
                                    "conditional_gw_p": 0.5462442828521263,
                                    "favours_comparator_in_recession": False,
                                    "pass": True}}},
                    "extended": {"W1": {"data_class": ("REAL_TIME_RECONSTRUCTED"
                                                       if reconstructed else "REAL_TIME"),
                                        "n_origins": len(origins), "source": "extended_window"}}},
                "C5": {"pass": True},
                "C6": {"status": "NO_RESOLVED_FORECASTS", "pass": False},
            },
            "status": "CANDIDATE" if c4_status != "PASS" else "SHADOW_TEST",
        },
        "c4_episodes": {
            "W1_EXT": {
                "1990_91": {"RMSE": {"origins": 8, "relative_loss_episode": 1.0991911,
                                     "relative_loss_leave_one_out": LOO_1990_91}},
                "2001": {"RMSE": {"origins": 8, "relative_loss_episode": 1.0672201,
                                  "relative_loss_leave_one_out": LOO_2001}},
            },
            "W2": {"2007_09": {"RMSE": {"origins": 19, "relative_loss_episode": 0.8739039,
                                        "relative_loss_leave_one_out": LOO_2007_09}}},
        },
        "c4_slices": {"real_time_window": {
            "window": "W1", "data_class": "REAL_TIME", "origins": int(real_time),
            "first_origin": w1_used[0], "origin_floor": FIRST_REAL,
            "recession_origins": int(slice_recessions),
            "losses": {"RMSE": {"status": slice_status, "recession_origins": int(slice_recessions),
                                "min_recession_origins": 12,
                                "relative_loss_recession": slice_loss,
                                "pass": slice_status == "PASS"}},
            "note": "C4 recomputed inside the extended run on the REAL_TIME origins only",
        }},
    }
    path = tmp / f"primary_{candidate}.json"
    write_json(path, report)
    return path


def make_standard(tmp: Path, name: str, *, protocol_sha: str,
                  candidate: str = "ENSEMBLE_INVERSE_MSE", origins: int = 121,
                  first_origin: str = FIRST_REAL, status: str = "INCONCLUSIVE",
                  loss=REAL_TIME_ONLY_LOSS, recessions: int = 8,
                  data_class: str = "REAL_TIME",
                  data_root: str = "data/raw") -> Path:
    used = W1_ORIGINS if origins == 121 else month_ends(1990, 1, origins)
    report = {
        "report_id": "AMFE-FIRST-REAL-EVALUATION",
        "candidate": candidate,
        "target": "T1_CPI_YOY",
        "horizon": 1,
        "data_class": data_class,
        "data_root": data_root,
        "protocol": {"file": "protocols/evaluation_protocol_v5.json", "version": "5.0.1",
                     "sha256": protocol_sha},
        "reconstruction": {"extended_c4": False, "first_real_vintage": FIRST_REAL,
                           "data_root": data_root,
                           "w1_origins_total": int(origins), "w1_origins_real_time": int(origins),
                           "w1_origins_reconstructed": 0},
        "windows": {
            "W1": {"window": "W1", "origins": int(origins), "used_origins": used,
                   "first_origin": first_origin, "last_origin": used[-1],
                   "data_class": data_class},
            "W2": {"window": "W2", "origins": len(W2_ORIGINS), "used_origins": W2_ORIGINS,
                   "data_class": "REAL_TIME"},
        },
        "admission": {"criteria": {"C1": {"pass": True}, "C2": {"pass": True},
                                   "C4": {"per_window": {"W1": {"RMSE": {
                                       "status": status, "recession_origins": int(recessions),
                                       "min_recession_origins": 12,
                                       "relative_loss_recession": loss,
                                       "pass": status == "PASS"}}}},
                                   "C5": {"pass": True}},
                      "status": "CANDIDATE"},
    }
    path = tmp / f"standard_{name}.json"
    write_json(path, report)
    return path


def make_data_root(tmp: Path) -> Path:
    """A tiny data root with a coverage audit whose stored digest is computed by the real tool."""
    data_root = tmp / "reconstructed"
    (data_root / "CPIAUCSL").mkdir(parents=True, exist_ok=True)
    (data_root / "CPIAUCSL" / "vintages.zip").write_bytes(b"PK\x03\x04synthetic-fixture")
    (data_root / "USREC" ).mkdir(parents=True, exist_ok=True)
    (data_root / "USREC" / "latest.csv").write_text("observation_date,USREC\n1990-01-01,0.0\n",
                                                    encoding="utf-8")
    digest = rp.tree_digest(data_root)
    coverage = data_root / "g4_coverage.json"
    write_json(coverage, {"schema": "amfe.pit_reconstruction_g4_coverage.v1",
                          "artifacts": {"data_root": "reconstructed", "tree_digest": digest}})
    return data_root


def make_prereg(tmp: Path, data_root: Path, *, pin_name: str = "prereg.sha256.txt",
                tree_digest: str | None = None) -> Path:
    protocol = tmp / "protocol_v5.json"
    write_json(protocol, {"version": "5.0.1"})
    dossier = tmp / "dossier.json"
    write_json(dossier, {"schema": "amfe.pit_reconstruction.v2"})
    coverage = data_root / "g4_coverage.json"
    audit = json.loads(coverage.read_text(encoding="utf-8"))
    prereg = {
        "schema": "amfe.pit_preregistration.v1",
        "preregistration_id": "AMFE-PIT-PREREG-0.3",
        "version": "0.3.0",
        "status": "PROPOSED",
        "protocol_basis": {"v5": {"file": protocol.name, "version": "5.0.1",
                                  "sha256": rpe.sha256_file(protocol)}},
        "data_basis": {
            "reconstruction_dossier": {"file": dossier.name,
                                       "sha256": rpe.sha256_file(dossier)},
            "coverage_audit": {
                "file": f"{data_root.name}/g4_coverage.json",
                "tree_digest": tree_digest or audit["artifacts"]["tree_digest"]},
        },
    }
    prereg["content_sha256"] = rpe.canonical_content_sha256(prereg)
    path = tmp / "prereg.json"
    write_json(path, prereg)
    if pin_name:
        (tmp / pin_name).write_text(rpe.sha256_file(path) + "\n", encoding="utf-8")
    return path


class PreregEvaluationV2Test(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.protocol = self.dir / "protocol_v5.json"
        write_json(self.protocol, {"version": "5.0.1"})
        self.data_root = make_data_root(self.dir)
        self.prereg = make_prereg(self.dir, self.data_root)
        self.protocol_sha = rpe.sha256_file(self.protocol)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ------------------------------------------------------------------ builders
    def primary(self, **kwargs) -> Path:
        kwargs.setdefault("protocol_sha", self.protocol_sha)
        return make_primary(self.dir, **kwargs)

    def standard(self, name: str, **kwargs) -> Path:
        kwargs.setdefault("protocol_sha", self.protocol_sha)
        return make_standard(self.dir, name, **kwargs)

    def build(self, primary: Path | None = None, *, raw: Path | None = None,
              recon: Path | None = None, prereg: Path | None = None) -> dict:
        primary = primary or self.primary()
        raw = raw or self.standard("raw", data_root="data/raw")
        recon = recon or self.standard("recon", data_root="data/reconstructed")
        payload = json.loads((prereg or self.prereg).read_text(encoding="utf-8"))
        return rpe.build_report(payload, prereg or self.prereg, [primary], root=self.dir,
                                standard_reports=[{"real_time_only": raw, "tape_free": recon}])

    # ------------------------------------------------------------------ 1. REAL_TIME-only slice
    def test_real_time_only_slice_is_the_clean_121_inconclusive_run(self):
        slices = self.build()["candidates"][0]["slices"]
        real = slices["real_time_only"]
        self.assertEqual(real["window"], "W1")
        self.assertEqual(real["origins"], 121)
        self.assertEqual(real["first_origin"], FIRST_REAL)
        self.assertEqual(real["data_class"], "REAL_TIME")
        self.assertEqual(real["c4_status"], "INCONCLUSIVE")
        self.assertEqual(real["recession_origins"], 8)
        self.assertAlmostEqual(real["relative_loss"], REAL_TIME_ONLY_LOSS, places=7)
        self.assertEqual(real["computation"], "standard_v5_run")

    # ------------------------------------------------------------------ 2. three separate slices
    def test_the_three_mandatory_slices_are_separate_computations(self):
        slices = self.build()["candidates"][0]["slices"]
        self.assertEqual(list(slices), ["real_time_only", "reconstructed_excluded", "tape_free"])
        self.assertEqual(slices["reconstructed_excluded"]["computation"],
                         "primary_run_real_time_subset")
        self.assertEqual(slices["real_time_only"]["computation"], "standard_v5_run")
        signatures = {(entry["computation"], entry["report_sha256"]) for entry in slices.values()}
        self.assertEqual(len(signatures), 3)
        for entry in slices.values():
            self.assertEqual(entry["origins"], 121)
            self.assertEqual(entry["data_class"], "REAL_TIME")
            self.assertIn("report_sha256", entry)

    def test_slice_aliases_fail_closed(self):
        primary = self.primary()
        raw = self.standard("raw")
        payload = json.loads(self.prereg.read_text(encoding="utf-8"))
        # the same standard report supplied twice: two slices would be one calculation
        with self.assertRaises(rpe.PreregError):
            rpe.build_report(payload, self.prereg, [primary], root=self.dir,
                             standard_reports=[{"real_time_only": raw, "tape_free": raw}])

    def test_missing_tape_free_slice_fails_closed(self):
        primary = self.primary()
        payload = json.loads(self.prereg.read_text(encoding="utf-8"))
        with self.assertRaises(rpe.PreregError):
            rpe.build_report(payload, self.prereg, [primary], root=self.dir,
                             standard_reports=[{"real_time_only": self.standard("raw")}])

    def test_primary_without_in_run_slice_fails_closed(self):
        report = json.loads(self.primary().read_text(encoding="utf-8"))
        report.pop("c4_slices")
        path = self.dir / "no_slice.json"
        write_json(path, report)
        with self.assertRaises(rpe.PreregError):
            self.build(primary=path)

    # ------------------------------------------------------------------ cache-replay guard
    def test_contaminated_standard_report_fails_closed(self):
        # the archive 204-origin "standard" run: cache replay of reconstructed origins (docs/172 §4)
        contaminated = self.standard("contaminated", origins=204, first_origin="1990-01-31",
                                     status="FAIL", loss=1.1057688402590469, recessions=16,
                                     data_root="data/raw")
        with self.assertRaises(rpe.PreregError):
            self.build(raw=contaminated)

    # ------------------------------------------------------------------ 3. LOO incl. 2007-09
    def test_leave_one_episode_out_covers_all_three_episodes(self):
        loo = self.build()["candidates"][0]["c4_leave_one_episode_out"]
        self.assertEqual(sorted(loo), ["1990_91", "2001", "2007_09"])
        self.assertAlmostEqual(loo["1990_91"]["relative_loss_leave_one_out"], LOO_1990_91, places=7)
        self.assertAlmostEqual(loo["2001"]["relative_loss_leave_one_out"], LOO_2001, places=7)
        self.assertAlmostEqual(loo["2007_09"]["relative_loss_leave_one_out"], LOO_2007_09, places=7)
        self.assertEqual(loo["1990_91"]["window"], "W1_EXT")
        self.assertEqual(loo["2007_09"]["window"], "W2")

    # ------------------------------------------------------------------ 4. C4 W2 published
    def test_c4_w2_is_published(self):
        candidate = self.build()["candidates"][0]
        self.assertEqual(candidate["c4_w2"]["status"], "PASS")
        self.assertEqual(candidate["c4_w2"]["recession_origins"], 18)
        self.assertAlmostEqual(candidate["c4_w2"]["relative_loss"], C4_W2_LOSS, places=7)
        self.assertEqual(candidate["criteria"]["C4_W2"], "PASS")
        self.assertAlmostEqual(candidate["c4_w1_ext"]["relative_loss"], C4_W1_EXT_LOSS, places=12)

    # ------------------------------------------------------------------ 5. per-origin C4 evidence
    def test_c4_w1_ext_loss_is_recomputed_from_the_primary_run(self):
        # the aggregate is taken from the fresh run's per-origin differentials, not from a constant
        report = self.build(self.primary(c4_status="FAIL",
                                         slice_loss=REAL_TIME_ONLY_LOSS))["candidates"][0]
        self.assertAlmostEqual(report["c4_w1_ext"]["relative_loss"], C4_W1_EXT_LOSS, places=12)
        self.assertEqual(report["c4_w1_ext"]["recession_origins"], 16)
        self.assertIn("mean_differential_recession", report["c4_w1_ext"])
        self.assertIn("conditional_gw_p", report["c4_w1_ext"])
        self.assertTrue(report["c4_w1_ext"]["favours_comparator_in_recession"])
        self.assertNotIn("used_origins", json.dumps(report, ensure_ascii=False))
        # a different per-origin outcome in the run changes the published aggregate
        changed = json.loads(self.primary(c4_status="FAIL").read_text(encoding="utf-8"))
        changed["admission"]["criteria"]["C4"]["per_window"]["W1"]["RMSE"][
            "relative_loss_recession"] = 1.2345
        path = self.dir / "changed_primary.json"
        write_json(path, changed)
        self.assertAlmostEqual(self.build(path)["candidates"][0]["c4_w1_ext"]["relative_loss"],
                               1.2345, places=12)

    # ------------------------------------------------------------------ 6. w1_data_class
    def test_w1_data_class_is_real_time(self):
        windows = self.build()["candidates"][0]["windows"]
        self.assertEqual(windows["w1_data_class"], "REAL_TIME")
        self.assertTrue(windows["w1_data_class_relabelled"])
        self.assertEqual(windows["w1_data_class_reported_by_run"], "REAL_TIME_RECONSTRUCTED")
        self.assertEqual(windows["w1_ext_data_class"], "REAL_TIME_RECONSTRUCTED")
        self.assertEqual(windows["w1_origins"], 121)
        self.assertEqual(windows["w1_ext_origins_total"], 204)

    def test_w1_with_reconstructed_origin_fails_closed(self):
        report = json.loads(self.primary().read_text(encoding="utf-8"))
        report["windows"]["W1"]["used_origins"] = W1_EXT_ORIGINS[:121]
        report["windows"]["W1"]["first_origin"] = W1_EXT_ORIGINS[0]
        path = self.dir / "leaky.json"
        write_json(path, report)
        with self.assertRaises(rpe.PreregError):
            self.build(primary=path)

    # ------------------------------------------------------------------ conditional flag split
    def test_conditional_data_class_is_the_pass_not_the_run_class(self):
        candidate = self.build()["candidates"][0]
        self.assertTrue(candidate["relies_on_reconstructed_origins"])
        self.assertFalse(candidate["criteria"]["conditional_data_class"])
        self.assertNotIn("conditional_data_class", candidate)
        label = self.build()["labels"][0]
        self.assertEqual(label["label"], "CANDIDATE")
        self.assertFalse(label["conditional_data_class"])

    def test_conditional_pass_is_flagged_when_c4_passes_on_reconstructed_origins(self):
        report = self.build(self.primary(c4_status="PASS"))["labels"][0]
        self.assertEqual(report["label"], "CONDITIONAL_DATA_CLASS")
        self.assertTrue(report["conditional_data_class"])

    def test_zero_reconstructed_origins_makes_a_pass_unconditional(self):
        labels = self.build(self.primary(c4_status="PASS", reconstructed=0, real_time=121))["labels"]
        self.assertEqual(labels[0]["label"], "PASS")
        self.assertFalse(labels[0]["conditional_data_class"])

    def test_c4_failure_is_not_a_pass(self):
        report = self.build(self.primary(c4_status="FAIL"))
        self.assertEqual(report["labels"][0]["label"], "CANDIDATE")

    # ------------------------------------------------------------------ 7. pins
    def test_prereg_pin_without_json_suffix_is_checked(self):
        pins = self.build()["pins"]
        self.assertIn("preregistration_file", pins)
        self.assertEqual(pins["preregistration_file"]["pin_file"], "prereg.sha256.txt")
        self.assertTrue(pins["preregistration_file"]["match"])
        self.assertTrue(self.build()["pins_ok"])

    def test_prereg_pin_json_suffix_is_also_supported(self):
        (self.dir / "prereg.sha256.txt").unlink()
        (self.dir / "prereg.json.sha256.txt").write_text(
            rpe.sha256_file(self.prereg) + "\n", encoding="utf-8")
        pins = self.build()["pins"]
        self.assertEqual(pins["preregistration_file"]["pin_file"], "prereg.json.sha256.txt")
        self.assertEqual(pins["preregistration_file"]["pin_files_checked"],
                         ["prereg.json.sha256.txt"])
        self.assertTrue(self.build()["pins_ok"])

    def test_both_pin_spellings_are_verified_when_present(self):
        (self.dir / "prereg.json.sha256.txt").write_text("d" * 64 + "\n", encoding="utf-8")
        with self.assertRaises(rpe.PreregError):
            self.build()

    def test_missing_prereg_pin_fails_closed(self):
        (self.dir / "prereg.sha256.txt").unlink()
        with self.assertRaises(rpe.PreregError):
            self.build()

    def test_prereg_content_sha256_is_recomputed(self):
        pins = self.build()["pins"]
        self.assertIn("preregistration_content", pins)
        self.assertTrue(pins["preregistration_content"]["match"])
        self.assertTrue(self.build()["preregistration"]["content_sha256_verified"])

    def test_prereg_content_sha256_mismatch_fails_closed(self):
        payload = json.loads(self.prereg.read_text(encoding="utf-8"))
        payload["content_sha256"] = "b" * 64
        write_json(self.prereg, payload)
        with self.assertRaises(rpe.PreregError):
            self.build()

    # ------------------------------------------------------------------ 8. coverage digest
    def test_coverage_tree_digest_is_recomputed_from_the_data_root(self):
        pins = self.build()["pins"]["coverage_audit"]
        self.assertTrue(pins["recomputed_from_data_root"])
        self.assertEqual(pins["tree_digest_recomputed"], pins["tree_digest_pin"])
        self.assertEqual(pins["tree_digest_recomputed"], pins["tree_digest_stored_in_audit"])

    def test_coverage_digest_ignores_a_stale_stored_field(self):
        # the audit file keeps its old (matching) digest, but the data root changes underneath it
        (self.data_root / "CPIAUCSL" / "vintages.zip").write_bytes(b"PK\x03\x04changed")
        payload = json.loads(self.prereg.read_text(encoding="utf-8"))
        with self.assertRaises(rpe.PreregError):
            rpe.build_report(payload, self.prereg, [self.primary()], root=self.dir,
                             standard_reports=[{"real_time_only": self.standard("raw"),
                                                "tape_free": self.standard(
                                                    "recon2", data_root="data/reconstructed")}])

    def test_coverage_pin_mismatch_fails_closed(self):
        prereg = make_prereg(self.dir, self.data_root, pin_name="prereg.sha256.txt",
                             tree_digest="a" * 64)
        with self.assertRaises(rpe.PreregError):
            self.build(prereg=prereg)

    # ------------------------------------------------------------------ report properties
    def test_report_is_deterministic(self):
        first = self.build()
        second = self.build()
        self.assertEqual(json.dumps(first, sort_keys=True, ensure_ascii=False),
                         json.dumps(second, sort_keys=True, ensure_ascii=False))

    def test_report_is_provisional_and_not_an_admission(self):
        report = self.build()
        self.assertEqual(report["status"], "PROVISIONAL")
        self.assertFalse(report["admission"])
        self.assertEqual(report["schema"], "amfe.pit_prereg_evaluation.v2")
        self.assertTrue(report["slices_complete"])

    def test_no_raw_origin_arrays_in_output(self):
        text = json.dumps(self.build(), ensure_ascii=False)
        self.assertNotIn("used_origins", text)
        self.assertNotIn("vintages", text)
        self.assertLess(len(text), 60000)

    def test_cli_check_mode_and_fail_exit_code(self):
        out = self.dir / "out.json"
        primary = self.primary()
        raw = self.standard("cli_raw", data_root="data/raw")
        recon = self.standard("cli_recon", data_root="data/reconstructed")
        argv = ["--prereg", str(self.prereg), "--report", str(primary),
                "--standard-report", str(raw), "--tape-free-report", str(recon),
                "--out", str(out), "--root", str(self.dir)]
        self.assertEqual(rpe.main(argv + ["--check"]), 0)
        self.assertFalse(out.exists())
        self.assertEqual(rpe.main(argv), 0)
        self.assertTrue(out.exists())
        written = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(written["candidates"][0]["candidate"], "ENSEMBLE_INVERSE_MSE")
        (self.dir / "prereg.sha256.txt").write_text("f" * 64 + "\n", encoding="utf-8")
        self.assertEqual(rpe.main(argv), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
