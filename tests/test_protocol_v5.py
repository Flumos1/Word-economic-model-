"""Protocol v5 (D-151, option A+E): prospective data-availability amendment on top of frozen v4.

v5 changes only:
  * data-depth classes for the reconstructed 1990-1996 origins (REAL_TIME_RECONSTRUCTED_MARKET,
    RECONSTRUCTED_PIT, report class REAL_TIME_RECONSTRUCTED) and the admission class list;
  * the C4 extended window (windows.W1_EXT) and the target-specific C4 minimum (T1 12, T2 4);
  * the forward-registry cadence (monthly anchor + weekly vintage rows).
C1/C2/C5, targets, horizons, W1/W2/W3 endpoints, the comparator composition and v4 itself are
unchanged. v5 is prospective: it does not rewrite v4 and it cannot backdate a registry row.
"""
import hashlib
import json
import os
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

V4 = ROOT / "protocols" / "evaluation_protocol_v4.json"
V4_PIN = ROOT / "protocols" / "evaluation_protocol_v4.sha256"
V5 = ROOT / "protocols" / "evaluation_protocol_v5.json"
V5_PIN = ROOT / "protocols" / "evaluation_protocol_v5.sha256"
DOSSIER = ROOT / "protocols" / "pit_reconstruction.json"
NF_SIM = ROOT / "protocols" / "nf_sim_thresholds.json"
COMPOSITION = ROOT / "protocols" / "comparator_composition_2026-11-11.json"
EVALUATE = ROOT / "tools" / "evaluate.py"

IMMUTABLE = (
    "targets", "actuals", "estimation", "comparator_minimum", "primary_comparator_rule",
    "losses", "model_class_rules", "covid_handling", "drift_triggers", "comparison_unit",
    "origin_policy", "forecast_product", "universe", "protocol_id", "baselines", "multiplicity",
)
ALLOWED_CHANGED_KEYS = {
    "version", "status", "status_note", "supersedes", "decision_ids", "meta_gate", "amendment_rule",
    "windows", "admission", "data_depth", "changes_from_v4", "forward_registry", "reporting_requirements",
    "frozen_on",
}
ALLOWED_CHANGED_PATHS = (
    "windows.W1_EXT", "admission.criteria.C4", "admission.c4_window_and_minimum",
    "admission.admission_classes", "admission.reconstructed_caveat", "admission.reporting_requirements",
    "data_depth.reconstruction", "data_depth.classification_rule",
    "forward_registry.cadence", "forward_registry.cadence_note",
)


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def lf_sha256(path):
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def get(data, dotted):
    node = data
    for part in dotted.split("."):
        node = node[part]
    return node


def diff_paths(a, b, prefix="", out=None):
    out = [] if out is None else out
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            here = f"{prefix}.{key}" if prefix else key
            if key not in a or key not in b:
                out.append(here)
            else:
                diff_paths(a[key], b[key], here, out)
    elif a != b:
        out.append(prefix)
    return out


class TestProtocolV5(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v4 = load(V4)
        cls.v5 = load(V5)
        cls.pin_v5 = V5_PIN.read_text(encoding="utf-8").split()[0] if V5_PIN.exists() else None

    # -- identity and scope ---------------------------------------------------------------
    def test_identity(self):
        self.assertEqual(self.v5["protocol_id"], "AMFE-EVAL")
        self.assertIn(self.v5["version"], {"5.0.0", "5.0.1"})
        self.assertIn(self.v5["status"], {"PROPOSED", "FROZEN"})
        self.assertEqual(self.v5["supersedes"]["version"], "4.0.0")
        self.assertIn("D-151", self.v5["decision_ids"])
        self.assertIn("D-156", self.v5["decision_ids"])
        self.assertEqual(self.v5["meta_gate"]["reference"], "D-100")
        self.assertIn(self.v5["meta_gate"]["stage"], {"PROPOSED", "APPROVED"})
        self.assertTrue(any("D-151" in d.get("decision", "") for d in self.v5["meta_gate"]["owner_decisions"]))

    def test_v4_is_untouched(self):
        pin = V4_PIN.read_text(encoding="utf-8").split()[0]
        self.assertEqual(lf_sha256(V4), pin)
        self.assertEqual(self.v5["supersedes"]["sha256"], pin)

    def test_pin_when_frozen(self):
        if self.v5["status"] != "FROZEN":
            self.assertIsNone(self.pin_v5)
            return
        self.assertIsNotNone(self.pin_v5, "FROZEN v5 must have protocols/evaluation_protocol_v5.sha256")
        self.assertEqual(lf_sha256(V5), self.pin_v5)

    def test_only_documented_paths_changed(self):
        changed = diff_paths(self.v4, self.v5)
        for path in changed:
            root = path.split(".")[0]
            self.assertTrue(root in ALLOWED_CHANGED_KEYS or path in ALLOWED_CHANGED_PATHS,
                            f"undocumented change in v5: {path}")
        for key in IMMUTABLE:
            self.assertEqual(self.v4[key], self.v5[key], key)

    def test_windows_unchanged(self):
        for window in ("W1", "W2", "W3"):
            self.assertEqual(self.v4["windows"][window], self.v5["windows"][window], window)
        self.assertEqual(self.v5["windows"]["W1_EXT"]["start"], "1990-01")
        self.assertEqual(self.v5["windows"]["W1_EXT"]["end"], "2006-12")
        self.assertEqual(self.v5["windows"]["target_overrides"], self.v4["windows"]["target_overrides"])

    def test_criteria_texts_except_c4(self):
        for cid in ("C1", "C2", "C3", "C5", "C6"):
            a = next(c for c in self.v4["admission"]["criteria"] if c["id"] == cid)
            b = next(c for c in self.v5["admission"]["criteria"] if c["id"] == cid)
            self.assertEqual(a, b, cid)
        c4 = next(c for c in self.v5["admission"]["criteria"] if c["id"] == "C4")
        self.assertTrue(c4["gating"])
        self.assertIn("W1_EXT", c4["text"])

    # -- the new data classes -------------------------------------------------------------
    def test_reconstruction_classes(self):
        rec = self.v5["data_depth"]["reconstruction"]
        self.assertEqual(rec["id"], "PIT-RECON-1990-1996")
        self.assertEqual(rec["dossier"], "protocols/pit_reconstruction.json")
        self.assertIn("REAL_TIME_RECONSTRUCTED_MARKET", rec["classes"])
        self.assertIn("RECONSTRUCTED_PIT", rec["classes"])
        self.assertIn("GDPC1", rec["not_used"])
        self.assertEqual(rec["admission_classes"],
                         ["REAL_TIME", "REAL_TIME_TAPE", "REAL_TIME_RECONSTRUCTED"])
        self.assertIn("RECONSTRUCTED_PIT", rec["c4_classes"])
        self.assertEqual(rec["admission_classes"], self.v5["admission"]["admission_classes"])
        self.assertIn("C1/C2/C5", rec["sensitivity"])
        self.assertIn("carrier", rec["classes"]["RECONSTRUCTED_PIT"]["look_ahead"])

    def test_c4_extended_window_and_slice(self):
        block = self.v5["admission"]["c4_window_and_minimum"]
        self.assertEqual(block["min_recession_months"], 12)
        self.assertEqual(block["extended_window"], {"T1_CPI_YOY": "W1_EXT"})
        self.assertEqual(block["real_time_slice"]["start_origin"], "1996-12-31")
        self.assertEqual(block["real_time_slice"]["status"], "REPORTING_ONLY_NOT_GATING")
        self.assertEqual(block["comparator_on_w1_ext"]["members"],
                         ["AO", "AR_BIC", "BVAR_MINNESOTA", "RW"])
        self.assertIn("leave-one-episode-out", block["comparator_on_w1_ext"]["pooling"])
        self.assertIn("REAL_TIME-only", block["sensitivity"])

    def test_t2_is_not_changed(self):
        # v5 narrow scope: no reconstructed GDPC1 and no T2 C4 minimum change
        block = self.v5["admission"]["c4_window_and_minimum"]
        self.assertNotIn("T2_GDP_GROWTH", json.dumps(block))
        self.assertEqual(self.v5["data_depth"]["reconstruction"]["not_used"]["GDPC1"][:20],
                         "reconstructed GDP gr")
        self.assertEqual(self.v5["admission"]["recession_conditioning"],
                         self.v4["admission"]["recession_conditioning"])

    def test_caveat_is_machine_readable(self):
        caveat = self.v5["admission"]["reconstructed_caveat"]
        self.assertIn("conditional_data_class", caveat)
        self.assertIn("CONDITIONAL_DATA_CLASS", caveat)
        self.assertIn("C6", caveat)
        self.assertIn("ACTIVE", caveat)
        self.assertIn("stages_note", self.v5["admission"])

    # -- forward registry cadence ---------------------------------------------------------
    def test_forward_cadence(self):
        fr = self.v5["forward_registry"]
        self.assertIn("weekly", fr["cadence"])
        self.assertEqual(fr["c6_min_distinct_target_periods"], 4)
        self.assertEqual(fr["record"], self.v4["forward_registry"]["record"])
        self.assertEqual(fr["control_arms"], self.v4["forward_registry"]["control_arms"])
        self.assertEqual(fr["integrity"], self.v4["forward_registry"]["integrity"])
        # 5.0.1 (D-156): weekly rows carry vintage_week, the monthly anchor does not
        week = fr["vintage_week"]
        self.assertEqual(week["field"], "vintage_week")
        self.assertIn("vintage_week", week["identity"])
        self.assertIn("correlated", week["disclosure"])

    # -- frozen artifacts are unchanged ---------------------------------------------------
    def test_frozen_artifacts_unchanged(self):
        # v5 does not re-pin R3, NF-SIM or the composition: their pins remain those of v4.
        r3 = load(ROOT / "protocols" / "r3_specification.json")
        self.assertEqual(r3["frozen_protocol"]["version"], "4.0.0")
        self.assertTrue(NF_SIM.exists() and COMPOSITION.exists())
        self.assertIn("NF-SIM", json.dumps(self.v5["admission"]["threshold_rule"]))

    # -- harness supports the extended C4 and admission classes ---------------------------
    def test_harness_extended_c4(self):
        import evaluate as harness

        rng = np.random.default_rng(7)
        n, m = 80, 60
        shapes = np.linspace(-1.5, 1.5, 19)

        def window(size, good=True):
            actual = rng.normal(2.0, 1.0, size=size)
            noise = rng.normal(0.0, 1.0, size=size)
            cand_point = actual + (0.6 * noise if good else 1.5 * noise)
            comp_point = actual + noise
            recession = np.zeros(size, dtype=bool)
            recession[::6] = True
            return {"actual_primary": actual, "actual_secondary": actual,
                    "candidate": {"point": cand_point,
                                  "quantiles": cand_point[:, None] + shapes[None, :]},
                    "comparator": {"point": comp_point,
                                   "quantiles": comp_point[:, None] + shapes[None, :]},
                    "recession": recession}

        base = {"W1": window(n), "W2": window(m)}
        ext = {"W1": window(n + 20)}
        report = harness.evaluate_admission(
            "T1_CPI_YOY", 1, base, data_class="REAL_TIME_RECONSTRUCTED",
            c4_windows=ext, min_recession_origins=4,
            admission_classes=("REAL_TIME", "REAL_TIME_TAPE", "REAL_TIME_RECONSTRUCTED"),
            protocol_label="protocols/evaluation_protocol_v5.json",
            conditional_data_class=True)
        self.assertTrue(report["admission_allowed"])
        self.assertTrue(report["conditional_data_class"])
        self.assertIn(report["status_label"], {"CANDIDATE", "SHADOW_TEST_CONDITIONAL_DATA_CLASS"})
        self.assertEqual(report["protocol"], "protocols/evaluation_protocol_v5.json")
        c4 = report["criteria"]["C4"]
        self.assertEqual(c4["min_recession_origins"], 4)
        self.assertEqual(c4["extended"]["W1"]["source"], "extended_window")
        self.assertEqual(c4["extended"]["W1"]["n_origins"], n + 20)
        self.assertEqual(c4["extended"]["W2"]["source"], "standard_window")

        # v4 default path still refuses a reconstructed class
        report_v4 = harness.evaluate_admission("T1_CPI_YOY", 1, base,
                                               data_class="REAL_TIME_RECONSTRUCTED")
        self.assertFalse(report_v4["admission_allowed"])
        self.assertEqual(report_v4["status"], "CANDIDATE")

    def test_c4_mask_is_the_origin_month_for_every_horizon(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import run_real_evaluation as runner

        origins = ["1991-01-31", "1991-04-30", "2001-05-31", "2001-12-31"]
        for horizon in (1, 3, 6, 12):
            mask = runner.recession_mask("T1_CPI_YOY", origins, horizon)
            self.assertEqual(mask, [1, 0, 1, 0], horizon)
        self.assertIn("origin", runner.MASK_RULE)

    def test_reconstructed_root_fail_closed(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import run_real_evaluation as runner

        old = os.environ.get("AMFE_DATA_ROOT")
        try:
            os.environ["AMFE_DATA_ROOT"] = str(ROOT / "data" / "reconstructed")
            self.assertTrue(runner._is_reconstructed_root())
            with self.assertRaises(SystemExit):
                runner.main(["--target", "T2_GDP_GROWTH", "--horizon", "1",
                             "--candidate", "ENSEMBLE_RW_AR", "--max-origins", "1",
                             "--skip-candidate-registration",
                             "--out", str(ROOT / ".tmp-test" / "guard_t2.json")])
            os.environ["AMFE_DATA_ROOT"] = ""
            self.assertFalse(runner._is_reconstructed_root())
            with self.assertRaises(SystemExit):
                runner.main(["--target", "T1_CPI_YOY", "--horizon", "1",
                             "--candidate", "ENSEMBLE_RW_AR", "--extended-c4",
                             "--protocol", str(V5), "--max-origins", "1",
                             "--skip-candidate-registration",
                             "--out", str(ROOT / ".tmp-test" / "guard_ext.json")])
        finally:
            if old is None:
                os.environ.pop("AMFE_DATA_ROOT", None)
            else:
                os.environ["AMFE_DATA_ROOT"] = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
