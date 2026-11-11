"""Draft evaluation protocol v3: links to frozen v2, answers every review finding (L1-L4,
V3-01..V3-17, R-01..R-17), keeps restricted and unverified sources out of the comparator, and
keeps the licence register outside the protocol file.

While status is DRAFT the file may change. Once FROZEN it needs a pinned SHA-256
(evaluation_protocol_v3.sha256) and an approved meta-gate, exactly like v1 and v2.
"""
import hashlib, json, sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
V2_PINNED = ROOT / "protocols" / "evaluation_protocol_v2.sha256"
V3 = ROOT / "protocols" / "evaluation_protocol_v3.json"
V3_PINNED = ROOT / "protocols" / "evaluation_protocol_v3.sha256"
REGISTER = ROOT / "protocols" / "licence_register.json"


def load():
    return json.loads(V3.read_text(encoding="utf-8"))


def register():
    return json.loads(REGISTER.read_text(encoding="utf-8"))


def status_by_id():
    return {e["id"]: e["status"] for e in register()["entries"]}


def resolve(data, dotted):
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def members_for(p, target, statuses):
    """Comparator members of a target whose inputs are all OPEN in the register."""
    out = []
    for b in p["baselines"]:
        if not b.get("comparator_member") or not b.get("scored", True) or target not in b["applies_to"]:
            continue
        inputs = b["inputs_by_target"][target]
        if all(statuses.get(i) == "OPEN" for i in inputs):
            out.append(b["id"])
    return out


def density_members_for(p, target, statuses):
    """Members that provide a predictive distribution and whose inputs are all OPEN."""
    out = []
    for b in p["baselines"]:
        if not b.get("comparator_member") or not b.get("scored", True) or b.get("point_only"):
            continue
        if target not in b["applies_to"]:
            continue
        inputs = b["inputs_by_target"].get(target, [])
        if inputs and all(statuses.get(i) == "OPEN" for i in inputs):
            out.append(b["id"])
    return out


class ProtocolV3(unittest.TestCase):
    def test_supersedes_exactly_the_frozen_v2(self):
        self.assertEqual(load()["supersedes"]["sha256"], V2_PINNED.read_text().split()[0])

    def test_status_and_freeze(self):
        p = load()
        self.assertIn(p["status"], {"DRAFT", "FROZEN"})
        if p["status"] == "FROZEN":
            data = V3.read_bytes().replace(b"\r\n", b"\n")
            self.assertEqual(hashlib.sha256(data).hexdigest(), V3_PINNED.read_text().split()[0])
            self.assertEqual(p["meta_gate"]["approval"], "APPROVED")
        else:
            self.assertFalse(V3_PINNED.exists(), "a DRAFT must not have a pinned hash")
            self.assertEqual(p["meta_gate"]["approval"], "PENDING")
            self.assertTrue(p["meta_gate"]["owner_confirmation_needed"])

    def test_every_change_is_mapped_to_existing_content(self):
        p = load()
        ids = [c["id"] for c in p["changes_from_v2"]]
        expected = (["L1", "L2", "L3", "L4"] + [f"V3-{i:02d}" for i in range(1, 18)]
                    + [f"R-{i:02d}" for i in range(1, 18)])
        self.assertEqual(ids, expected)
        for change in p["changes_from_v2"]:
            self.assertIn(change["disposition"], {"adopted", "adopted_needs_owner_confirmation",
                                                  "recorded_as_caveat", "no_change"}, change["id"])
            self.assertTrue(change["paths"], change["id"])
            for path in change["paths"]:
                self.assertTrue(resolve(p, path), f"{change['id']}: {path} not found")

    def test_register_is_separate_and_well_formed(self):
        p = load()
        self.assertNotIn("register", p["licence_policy"], "the register must live outside the frozen file")
        self.assertEqual(p["licence_policy"]["register_file"], "protocols/licence_register.json")
        reg = register()
        ids = [e["id"] for e in reg["entries"]]
        self.assertEqual(len(ids), len(set(ids)), "register ids must be unique")
        for e in reg["entries"]:
            self.assertIn(e["status"], {"OPEN", "RESTRICTED", "UNVERIFIED"}, e["id"])
            self.assertIn(e["level"], {"full", "extraction", "none"}, e["id"])
            self.assertTrue(e["date"], e["id"])
            if e["level"] != "none":
                self.assertTrue(e["url"], f"{e['id']}: an entry read from a page must cite its url")
            self.assertTrue(e["basis"], e["id"])

    def test_register_statuses_that_the_protocol_relies_on(self):
        s = status_by_id()
        self.assertEqual(s["ATLANTA_FED_GDPNOW"], "RESTRICTED")
        self.assertEqual(s["FRED_ALFRED_SERVICES"], "OPEN")
        self.assertEqual(s["ALFRED:CPIAUCSL"], "OPEN")
        self.assertEqual(s["FRED-MD_FRED-QD"], "UNVERIFIED")
        self.assertEqual(s["BLS_PRIMARY"], "OPEN")
        self.assertEqual(s["BEA_PRIMARY"], "UNVERIFIED")
        self.assertEqual(s["ALFRED:USREC"], "OPEN")
        fred = next(e for e in register()["entries"] if e["id"] == "FRED_ALFRED_SERVICES")
        text = " ".join(fred["conditions"])
        self.assertIn("manual download", text)
        self.assertIn("no training", text)

    def test_comparator_members_use_no_restricted_or_unverified_input_unless_flagged(self):
        p = load()
        s = status_by_id()
        for b in p["baselines"]:
            if not b.get("comparator_member"):
                continue
            for target, inputs in b["inputs_by_target"].items():
                self.assertIn(target, b["applies_to"], b["id"])
                for src in inputs:
                    self.assertIn(src, s, f"{b['id']}: {src} not in the register")
                    self.assertNotEqual(s[src], "RESTRICTED", f"{b['id']} uses a RESTRICTED source")
                    if s[src] == "UNVERIFIED":
                        self.assertIn("once its register entry is OPEN", b["note"], f"{b['id']}: {src}")

    def test_every_target_has_the_minimum_number_of_open_members_today(self):
        p = load()
        s = status_by_id()
        minimum = p["comparator_minimum"]["min_members"]
        self.assertEqual(minimum, 2)
        for t in p["targets"]:
            got = members_for(p, t["id"], s)
            self.assertGreaterEqual(len(got), minimum, f"{t['id']}: {got}")

    def test_gdpnow_is_not_used_anywhere(self):
        p = load()
        s = status_by_id()
        self.assertEqual(s["ATLANTA_FED_GDPNOW"], "RESTRICTED")
        for b in p["baselines"]:
            for inputs in b.get("inputs_by_target", {}).values():
                self.assertFalse([i for i in inputs if "GDPNOW" in i.upper()], b["id"])
        self.assertNotIn("NOWCAST_EXTERNAL", [b["id"] for b in p["baselines"]])
        self.assertNotIn("NOWCAST_EXTERNAL", p["forward_registry"]["control_arms"])
        self.assertTrue(p["licence_policy"]["gdpnow"]["use"].startswith("none"))
        self.assertIn("NOT PURSUED", p["licence_policy"]["gdpnow"]["permission_request"])

    def test_nyfed_nowcast_is_a_control_arm_only(self):
        p = load()
        arm = next(b for b in p["baselines"] if b["id"] == "NOWCAST_NYFED")
        self.assertFalse(arm["comparator_member"])
        self.assertNotIn("NOWCAST_NYFED", p["forward_registry"]["control_arms"])

    def test_own_nowcast_replaces_external_nowcasts(self):
        p = load()
        own = next(b for b in p["baselines"] if b["id"] == "OWN_NOWCAST_BRIDGE")
        self.assertEqual(own["applies_to"], ["T2_GDP_GROWTH"])
        self.assertEqual(own["horizons_quarters"], [0])
        self.assertTrue(own["comparator_member"])
        self.assertIn("hash", own["specification_required"])
        self.assertIn("OWN_NOWCAST_BRIDGE", p["licence_policy"]["gdpnow"]["replacement"])
        self.assertIn("substitute", p["licence_policy"])
        self.assertNotIn("approximation", p["licence_policy"])

    def test_no_baseline_of_any_kind_uses_a_restricted_source(self):
        p = load()
        s = status_by_id()
        for b in p["baselines"]:
            for inputs in b.get("inputs_by_target", {}).values():
                for src in inputs:
                    self.assertIn(src, s, f"{b['id']}: {src} not in the register")
                    self.assertNotEqual(s[src], "RESTRICTED", f"{b['id']} uses a RESTRICTED source")
                    if s[src] == "UNVERIFIED":
                        self.assertIn("once its register entry is OPEN", b.get("note", ""),
                                      f"{b['id']}: {src} is UNVERIFIED")

    def test_horizon_fields_are_lists(self):
        p = load()
        for b in p["baselines"]:
            self.assertNotIn("horizons", b, b["id"])
            if "horizons_quarters" in b:
                self.assertIsInstance(b["horizons_quarters"], list, b["id"])
                self.assertTrue(b["horizons_quarters"], b["id"])
                for h in b["horizons_quarters"]:
                    self.assertIsInstance(h, int, b["id"])

    def test_density_comparator_has_the_minimum_number_of_open_members(self):
        p = load()
        s = status_by_id()
        minimum = p["comparator_minimum"]["min_members"]
        for t in p["targets"]:
            got = density_members_for(p, t["id"], s)
            self.assertGreaterEqual(len(got), minimum, f"{t['id']}: {got}")

    def test_review_accounting_is_complete(self):
        p = load()
        text = p["meta_gate"]["independent_review"]
        self.assertIn("13 non-blocking", text)
        self.assertIn("docs/62", text)
        self.assertEqual([c["id"] for c in p["changes_from_v2"] if c["id"].startswith("V3-")],
                         [f"V3-{i:02d}" for i in range(1, 18)])
        self.assertIn("began before the origin", next(
            t for t in p["targets"] if t["id"] == "T3_RECESSION")["resolution"])

    def test_owner_decision_no_permissions_is_recorded(self):
        p = load()
        self.assertTrue(any("No permission is requested" in d["decision"]
                            for d in p["meta_gate"]["owner_decisions"]))
        self.assertIn("requests no permission", p["licence_policy"]["no_permissions"])
        self.assertIn("never committed", p["licence_policy"]["raw_data"])
        self.assertIn("private", p["licence_policy"]["privacy"])

    def test_point_only_baselines_and_density_comparator(self):
        p = load()
        spf = next(b for b in p["baselines"] if b["id"] == "SPF_MEDIAN")
        self.assertTrue(spf["point_only"])
        ids = {b["id"] for b in p["baselines"]}
        self.assertTrue({"EQUAL_WEIGHT_POINT", "EQUAL_WEIGHT_DENSITY"} <= ids)
        self.assertNotIn("EQUAL_WEIGHT", ids)
        self.assertIn("Averaging quantiles is not the pool", p["forecast_product"]["pool"])
        self.assertIn("0.01", p["forecast_product"]["log_score"])

    def test_windows_ordered_disjoint_and_covid_origins(self):
        w = load()["windows"]
        order = ["W1", "W2", "W3"]
        self.assertLess(w["training_start"], w["W1"]["start"])
        for a, b in zip(order, order[1:]):
            self.assertLess(w[a]["end"], w[b]["start"], f"{a} overlaps {b}")
        self.assertEqual(w["W3"]["end"], "2026-10")
        self.assertIsNone(w["forward_registry"]["start"])
        self.assertIn("2020-03 to 2021-02", w["origin_exclusion"]["covid_targets"])
        self.assertIn("origin and by the date of the target", w["defined_by"])

    def test_estimation_windows(self):
        e = load()["estimation"]
        self.assertEqual(e["scheme"], "rolling")
        self.assertEqual(e["window_months"], 180)
        self.assertEqual(e["T3"]["window_months"], 240)
        self.assertEqual(e["T3"]["sensitivity_windows_months"], [180, 360])
        self.assertIn("owner", e["T3"]["status"])

    def test_criteria_and_gating(self):
        a = load()["admission"]
        self.assertEqual([c["id"] for c in a["criteria"]], ["C1", "C2", "C3", "C4", "C5", "C6"])
        gating = {c["id"]: c["gating"] for c in a["criteria"]}
        self.assertFalse(gating["C3"])
        self.assertTrue(all(gating[i] for i in ("C1", "C2", "C4", "C5", "C6")))
        c4 = next(c for c in a["criteria"] if c["id"] == "C4")
        self.assertNotIn("T3_RECESSION", c4["applies_to"])
        self.assertIn("inconclusive", c4["text"])
        self.assertEqual(a["threshold_rule"]["value"], "min(0.97, q05_equal_methods)")
        self.assertEqual(a["T3_ceiling"], "LIMITED_DEPLOYMENT")
        self.assertEqual(a["long_horizon"]["min_independent_outcomes_for_ACTIVE"], 8)
        self.assertIn("CONTAMINATED", a["stages"]["CANDIDATE_to_SHADOW_TEST"])

    def test_t3_resolution_and_calibration(self):
        p = load()
        t3 = next(t for t in p["targets"] if t["id"] == "T3_RECESSION")
        self.assertIn("began before the origin", t3["resolution"])
        self.assertIn("not read as 0", t3["resolution"])
        self.assertNotIn("missing announcement is read as no recession", t3["resolution"])
        self.assertIn("peak exactly at t+h", t3["resolution"])
        self.assertIn("turning point after t+h is dated", t3["resolution"])
        self.assertIn("completeness deadline", t3["resolution"])
        self.assertIn("C4 does not apply", t3["c4_not_applicable"])
        c6 = next(c for c in p["admission"]["criteria"] if c["id"] == "C6")
        self.assertIn("calibration replaces coverage", c6["text"])

    def test_data_depth_facts(self):
        d = load()["data_depth"]
        v = d["first_realtime_vintage"]
        self.assertEqual(v["FRED-MD"], "1999-08")
        self.assertEqual(v["FRED-QD"], "2018-05")
        self.assertEqual(v["ALFRED_CPIAUCSL"], "1972-07-21")
        self.assertNotIn("GDPNow", " ".join(v))
        self.assertIn("REAL_TIME_TAPE", d["classification_rule"])
        self.assertIn("1990-2026", d["gate"])

    def test_llm_rules_and_registry_access(self):
        r = load()["model_class_rules"]
        self.assertIn("CONTAMINATED", r["llm_evidence"])
        self.assertIn("open to every candidate", r["registry_access"])
        self.assertIn("Training or fine-tuning on FRED", r["data"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
