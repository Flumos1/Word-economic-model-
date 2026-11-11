"""R3 specification: fixes the baselines of the frozen evaluation protocol v3, the project's own
nowcast, the predictive distributions, the comparator procedure, the data manifest for R2 and the
NF-SIM threshold study - all before any candidate is evaluated.

The specification is PROPOSED. It becomes binding only when its own hash, the code hashes and the
NF-SIM threshold table are frozen together; a candidate evaluated before that would break D-100.
"""
import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "protocols" / "evaluation_protocol_v3.json"
SPEC = ROOT / "protocols" / "r3_specification.json"
REGISTER = ROOT / "protocols" / "licence_register.json"


def load_protocol():
    return json.loads(PROTOCOL.read_text(encoding="utf-8"))


def load_spec():
    return json.loads(SPEC.read_text(encoding="utf-8"))


def status_by_id():
    reg = json.loads(REGISTER.read_text(encoding="utf-8"))
    return {e["id"]: e["status"] for e in reg["entries"]}


def lf_sha256(path):
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


class R3Specification(unittest.TestCase):
    def test_protocol_is_frozen_and_linked_by_hash(self):
        p = load_protocol()
        s = load_spec()
        self.assertEqual(p["status"], "FROZEN")
        self.assertEqual(p["meta_gate"]["approval"], "APPROVED")
        self.assertEqual(s["frozen_protocol"]["sha256"], lf_sha256(PROTOCOL))
        self.assertEqual(s["frozen_protocol"]["version"], p["version"])

    def test_status_and_freeze_discipline(self):
        s = load_spec()
        self.assertEqual(s["status"], "PROPOSED")
        for phrase in ("hash of this file", "code hashes", "NF-SIM threshold table"):
            self.assertIn(phrase, s["freeze_rule"])
        self.assertIn("does not permit evaluating candidates", s["freeze_rule"])

    def test_models_cover_every_protocol_baseline(self):
        p = load_protocol()
        s = load_spec()
        protocol_ids = {b["id"] for b in p["baselines"]}
        spec_ids = {m["id"] for m in s["models"]} | {d["id"] for d in s["derived_comparators"]}
        self.assertEqual(spec_ids, protocol_ids)

    def test_inputs_and_targets_match_the_frozen_protocol(self):
        p = load_protocol()
        s = load_spec()
        baselines = {b["id"]: b for b in p["baselines"]}
        for model in s["models"]:
            base = baselines[model["id"]]
            self.assertEqual(sorted(model["applies_to"]), sorted(base["applies_to"]), model["id"])
            if "inputs_by_target" in base:
                self.assertEqual(model["inputs_by_target"], base["inputs_by_target"], model["id"])
            if base.get("point_only"):
                self.assertTrue(model.get("point_only"), model["id"])
            self.assertEqual(bool(model.get("comparator_member")), bool(base.get("comparator_member")), model["id"])

    def test_horizons_and_quantile_grid(self):
        p = load_protocol()
        s = load_spec()
        self.assertEqual(s["quantile_grid"], [round(0.05 * i, 2) for i in range(1, 20)])
        for target in p["targets"]:
            spec = s["targets"][target["id"]]
            for key in ("horizons_months", "horizons_quarters"):
                if key in target:
                    self.assertEqual(spec["horizons"], target[key], target["id"])
        own = next(m for m in s["models"] if m["id"] == "OWN_NOWCAST_BRIDGE")
        self.assertEqual(own["horizons_quarters"], [0])

    def test_windows_match_the_frozen_protocol(self):
        p = load_protocol()
        s = load_spec()
        self.assertEqual(s["windows"]["training_start"], p["windows"]["training_start"])
        for w in ("W1", "W2", "W3"):
            self.assertEqual(s["windows"][w], [p["windows"][w]["start"], p["windows"][w]["end"]], w)
        self.assertEqual(s["windows"]["covid_targets"][0], "2020-03")
        self.assertEqual(s["windows"]["covid_targets"][1], "2021-02")

    def test_every_input_is_in_the_licence_register_and_none_is_restricted(self):
        s = load_spec()
        statuses = status_by_id()
        for model in s["models"]:
            for target, inputs in model.get("inputs_by_target", {}).items():
                for src in inputs:
                    self.assertIn(src, statuses, f"{model['id']}: {src} not in the register")
                    if model.get("comparator_member") and model.get("status") != "PROPOSED_BLOCKED_ON_REGISTER":
                        self.assertNotEqual(statuses[src], "RESTRICTED", f"{model['id']} uses RESTRICTED")
        self.assertEqual(statuses["FRED-MD_FRED-QD"], "UNVERIFIED")
        blocked = next(m for m in s["models"] if m["id"] == "DFM_FREDMD")
        self.assertEqual(blocked["status"], "PROPOSED_BLOCKED_ON_REGISTER")

    def test_hashes_are_declared_and_still_open(self):
        s = load_spec()
        for model in s["models"]:
            self.assertIn("code_hash", model, model["id"])
            self.assertIn("spec_hash", model, model["id"])
            self.assertIsNone(model["code_hash"], f"{model['id']}: no code before freezing")
        self.assertIn("code hashes of every model", " ".join(s["acceptance_checklist"]))

    def test_own_nowcast_is_specified_as_a_substitute(self):
        s = load_spec()
        own = next(m for m in s["models"] if m["id"] == "OWN_NOWCAST_BRIDGE")
        text = own["point"]
        for phrase in ("dynamic factor model", "bridge regression", "published description",
                       "without parameters, data or forecasts"):
            self.assertIn(phrase, text)
        self.assertIn("unknown", own["report"])
        self.assertIn("admission bar may be lower", own["report"])
        self.assertEqual(own["horizons_quarters"], [0])

    def test_comparator_procedure(self):
        s = load_spec()
        c = s["comparator_procedure"]
        self.assertIn("At least 2 members", c["minimum"])
        self.assertIn("SHA-256", c["composition_record"])
        self.assertIn("never a researcher choice", c["no_fitting"])
        self.assertIn("fixed composition", c["fixed_composition_sensitivity"])

    def test_nf_sim_is_defined(self):
        s = load_spec()
        n = s["nf_sim"]
        self.assertEqual(n["threshold_rule"], "min(0.97, q05_equal_methods)")
        self.assertEqual(n["replications"], 10000)
        self.assertEqual(n["output_table"]["keys"], ["target", "horizon", "loss", "window"])
        self.assertIn("after the R2 data gate", n["timing"])
        self.assertIn("stricter of the two thresholds", n["combination_variant"])
        self.assertGreaterEqual(len(n["dgp"]), 3)

    def test_data_manifest_covers_every_series_used(self):
        p = load_protocol()
        s = load_spec()
        statuses = status_by_id()
        manifest = {e["register"] for e in s["data_manifest"]["series"]}
        for entry in s["data_manifest"]["series"]:
            self.assertIn(entry["register"], statuses, entry["register"])
        used = set()
        for target in p["targets"]:
            for series in target["series"]:
                used.add(series if series == "NBER_CYCLE_DATES" else "ALFRED:" + series)
        for b in p["baselines"]:
            for inputs in b.get("inputs_by_target", {}).values():
                used.update(inputs)
        self.assertEqual(used - manifest, set(), f"manifest misses {used - manifest}")
        gate = s["data_manifest"]["gate"]
        self.assertIn("30", gate["spot_check"])
        self.assertIn("10", gate["spot_check"])
        self.assertIn("PSEUDO_REAL_TIME", gate["failure_rule"])

    def test_acceptance_checklist_is_complete(self):
        s = load_spec()
        text = " ".join(s["acceptance_checklist"]).lower()
        for phrase in ("r2 gate", "nf-sim", "code hashes", "comparator composition",
                       "test suite", "forward registry"):
            self.assertIn(phrase, text)
        self.assertTrue(s["open_items"])
        self.assertTrue(any("SPF" in item for item in s["open_items"]))

    def test_r2_documentary_audit_facts_are_recorded(self):
        s = load_spec()
        statuses = status_by_id()
        self.assertEqual(statuses["BEA_PRIMARY"], "OPEN")
        self.assertEqual(statuses["PHILADELPHIA_FED_RTDSM"], "OPEN")
        manifest = {e["register"]: e["depth"] for e in s["data_manifest"]["series"]}
        self.assertIn("1996-12-03", manifest["ALFRED:GS10"])
        self.assertIn("1996-12-03", manifest["ALFRED:TB3MS"])
        self.assertIn("2014-01-27", manifest["ALFRED:T10Y3M"])
        self.assertIn("1960-03-15", manifest["ALFRED:UNRATE"])
        self.assertIn("PHILADELPHIA_FED_RTDSM", manifest)
        spf = next(m for m in s["models"] if m["id"] == "SPF_MEDIAN")
        self.assertEqual(spf["status"], "PROPOSED_T2_ONLY")
        self.assertFalse(spf["scored_by_target"]["T1_CPI_YOY"])
        self.assertTrue(spf["scored_by_target"]["T2_GDP_GROWTH"])
        self.assertIn("100*ln(1+g/100)", spf["point"])
        self.assertIn("scored for that target", s["comparator_procedure"]["rule"])
        text = " ".join(s["open_items"])
        self.assertIn("1996-12-03", text)
        self.assertIn("single REAL_TIME member", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
