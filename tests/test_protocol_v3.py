"""Draft evaluation protocol v3: links to frozen v2, covers L1-L3 and R-01..R-17, keeps the
primary comparator free of restricted data.

While status is DRAFT the file may change. Once FROZEN it needs a pinned SHA-256
(evaluation_protocol_v3.sha256) and an approved meta-gate, exactly like v1 and v2.
"""
import hashlib, json, sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
V2_PINNED = ROOT / "protocols" / "evaluation_protocol_v2.sha256"
V3 = ROOT / "protocols" / "evaluation_protocol_v3.json"
V3_PINNED = ROOT / "protocols" / "evaluation_protocol_v3.sha256"


def load():
    return json.loads(V3.read_text(encoding="utf-8"))


def resolve(data, dotted):
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


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

    def test_every_change_is_mapped_to_existing_content(self):
        p = load()
        ids = [c["id"] for c in p["changes_from_v2"]]
        self.assertEqual(ids, ["L1", "L2", "L3"] + [f"R-{i:02d}" for i in range(1, 18)])
        for change in p["changes_from_v2"]:
            self.assertIn(change["disposition"], {"adopted", "adopted_needs_owner_confirmation",
                                                  "recorded_as_caveat", "no_change"}, change["id"])
            self.assertTrue(change["paths"], change["id"])
            for path in change["paths"]:
                self.assertTrue(resolve(p, path), f"{change['id']}: {path} not found")

    def test_primary_comparator_has_no_restricted_or_unverified_member(self):
        p = load()
        status = {k: v["status"] for k, v in p["licence_policy"]["register"].items() if isinstance(v, dict)}
        members = [b for b in p["baselines"] if b.get("comparator_member")]
        self.assertTrue(members)
        for b in members:
            sources = b.get("licence_sources")
            self.assertTrue(sources, b["id"])
            for src in sources:
                self.assertIn(src, status, f"{b['id']}: {src}")
                if status[src] == "RESTRICTED":
                    self.fail(f"{b['id']} draws on a RESTRICTED source")
                # an UNVERIFIED source is allowed only with the explicit "once its sources are OPEN" condition
                if status[src] == "UNVERIFIED":
                    self.assertIn("once its sources are OPEN", b["note"], f"{b['id']}: {src}")

    def test_gdpnow_is_a_control_arm_only(self):
        p = load()
        arm = next(b for b in p["baselines"] if b["id"] == "NOWCAST_EXTERNAL")
        self.assertFalse(arm["comparator_member"])
        self.assertEqual(p["licence_policy"]["register"]["ATLANTA_FED_GDPNOW"]["status"], "RESTRICTED")
        self.assertIn("ATLANTA_FED_GDPNOW", arm["licence_sources"])
        self.assertIn("NOWCAST_EXTERNAL", p["forward_registry"]["control_arms"])
        self.assertNotIn("NOWCAST_EXTERNAL", p["primary_comparator_rule"].split("Members:")[0])

    def test_licence_policy_forbids_committing_raw_archives(self):
        lp = load()["licence_policy"]
        self.assertIn("never committed", lp["raw_data"])
        self.assertIn("PENDING", lp["gdpnow"]["permission_request"])

    def test_point_only_baselines_do_not_enter_density_losses(self):
        p = load()
        spf = next(b for b in p["baselines"] if b["id"] == "SPF_MEDIAN")
        self.assertTrue(spf["point_only"])
        ids = {b["id"] for b in p["baselines"]}
        self.assertTrue({"EQUAL_WEIGHT_POINT", "EQUAL_WEIGHT_DENSITY"} <= ids)
        self.assertNotIn("EQUAL_WEIGHT", ids)

    def test_every_target_has_a_scored_baseline_and_a_comparator(self):
        p = load()
        for t in p["targets"]:
            self.assertTrue(any(t["id"] in b["applies_to"] and b.get("scored", True)
                                and b.get("comparator_member") for b in p["baselines"]), t["id"])

    def test_windows_ordered_disjoint(self):
        w = load()["windows"]
        order = ["W1", "W2", "W3"]
        self.assertLess(w["training_start"], w["W1"]["start"])
        for a, b in zip(order, order[1:]):
            self.assertLess(w[a]["end"], w[b]["start"], f"{a} overlaps {b}")
        self.assertEqual(w["W3"]["end"], "2026-10")
        self.assertIsNone(w["forward_registry"]["start"])

    def test_estimation_windows(self):
        e = load()["estimation"]
        self.assertEqual(e["scheme"], "rolling")
        self.assertEqual(e["window_months"], 180)
        self.assertEqual(e["T3"]["window_months"], 240)
        self.assertIn("owner", e["T3"]["status"])

    def test_criteria_and_gating(self):
        a = load()["admission"]
        self.assertEqual([c["id"] for c in a["criteria"]], ["C1", "C2", "C3", "C4", "C5", "C6"])
        gating = {c["id"]: c["gating"] for c in a["criteria"]}
        self.assertFalse(gating["C3"])
        self.assertTrue(all(gating[i] for i in ("C1", "C2", "C4", "C5", "C6")))
        self.assertEqual(a["threshold_rule"]["value"], "min(0.97, q05_equal_methods)")
        self.assertEqual(a["T3_ceiling"], "LIMITED_DEPLOYMENT")
        self.assertEqual(a["long_horizon"]["min_independent_outcomes_for_ACTIVE"], 8)

    def test_data_depth_facts_unchanged(self):
        d = load()["data_depth"]["first_realtime_vintage"]
        self.assertEqual(d["FRED-MD"], "1999-08")
        self.assertEqual(d["FRED-QD"], "2018-05")
        self.assertEqual(d["ALFRED_CPIAUCSL"], "1972-07-21")
        self.assertEqual(d["GDPNow_live"], "2014Q2")

    def test_llm_rules_and_registry_access(self):
        r = load()["model_class_rules"]
        self.assertIn("CONTAMINATED", r["llm_evidence"])
        self.assertIn("open to every candidate", r["registry_access"])

    def test_owner_confirmation_list_is_present_while_draft(self):
        p = load()
        if p["status"] == "DRAFT":
            self.assertTrue(p["meta_gate"]["owner_confirmation_needed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
