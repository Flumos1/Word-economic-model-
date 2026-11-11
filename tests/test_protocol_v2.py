"""Draft evaluation protocol v2: links to v1, covers M1-M14, structurally valid.

While status is DRAFT the file may change. Once FROZEN it needs a pinned SHA-256
(evaluation_protocol_v2.sha256), exactly like v1.
"""
import hashlib, json, sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
V1_PINNED = ROOT / "protocols" / "evaluation_protocol_v1.sha256"
V2 = ROOT / "protocols" / "evaluation_protocol_v2.json"
V2_PINNED = ROOT / "protocols" / "evaluation_protocol_v2.sha256"


def load():
    return json.loads(V2.read_text(encoding="utf-8"))


def resolve(data, dotted):
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


class ProtocolV2(unittest.TestCase):
    def test_supersedes_exactly_the_frozen_v1(self):
        pinned = V1_PINNED.read_text().split()[0]
        self.assertEqual(load()["supersedes"]["sha256"], pinned)

    def test_status_and_freeze(self):
        p = load()
        self.assertIn(p["status"], {"DRAFT", "FROZEN"})
        if p["status"] == "FROZEN":
            data = V2.read_bytes().replace(b"\r\n", b"\n")
            self.assertEqual(hashlib.sha256(data).hexdigest(), V2_PINNED.read_text().split()[0])
            self.assertEqual(p["meta_gate"]["approval"], "APPROVED")
        else:
            self.assertFalse(V2_PINNED.exists(), "a DRAFT must not have a pinned hash")
            self.assertEqual(p["meta_gate"]["approval"], "PENDING")

    def test_every_change_m1_to_m14_is_mapped_to_existing_content(self):
        p = load()
        ids = [c["id"] for c in p["changes_from_v1"]]
        self.assertEqual(ids, [f"M{i}" for i in range(1, 15)])
        for change in p["changes_from_v1"]:
            self.assertTrue(change["paths"], change["id"])
            for path in change["paths"]:
                self.assertTrue(resolve(p, path), f"{change['id']}: {path} not found")

    def test_estimation_window_is_rolling_and_finite(self):
        e = load()["estimation"]
        self.assertEqual(e["scheme"], "rolling")
        self.assertGreater(e["window_months"], 0)
        self.assertGreater(e["window_quarters"], 0)
        self.assertEqual(e["recursive_variant"]["test"], "Clark-West")

    def test_windows_ordered_disjoint_and_no_selection_window(self):
        w = load()["windows"]
        self.assertNotIn("selection", w)
        order = ["W1", "W2", "W3"]
        self.assertLess(w["training_start"], w["W1"]["start"])
        for a, b in zip(order, order[1:]):
            self.assertLess(w[a]["end"], w[b]["start"], f"{a} overlaps {b}")
        self.assertIsNone(w["forward_registry"]["start"])

    def test_baselines_for_recession_target(self):
        p = load()
        t3 = [b for b in p["baselines"] if "T3_RECESSION" in b["applies_to"]]
        scored = {b["id"] for b in t3 if b.get("scored", True)}
        self.assertTrue({"PROBIT_SPREAD", "SAHM_RULE", "EQUAL_WEIGHT"} <= scored)
        spf = next(b for b in t3 if b["id"] == "SPF_NEG_GDP_PROB")
        self.assertFalse(spf["scored"])
        self.assertTrue(spf["event_mismatch"])
        self.assertNotIn("SPF_MEDIAN", {b["id"] for b in t3})

    def test_every_target_has_a_scored_baseline(self):
        p = load()
        for t in p["targets"]:
            self.assertTrue(any(t["id"] in b["applies_to"] and b.get("scored", True)
                                for b in p["baselines"]), t["id"])

    def test_criteria_and_gating(self):
        a = load()["admission"]
        ids = [c["id"] for c in a["criteria"]]
        self.assertEqual(ids, ["C1", "C2", "C3", "C4", "C5", "C6"])
        gating = {c["id"]: c["gating"] for c in a["criteria"]}
        self.assertFalse(gating["C3"])
        self.assertTrue(all(gating[i] for i in ("C1", "C2", "C4", "C5", "C6")))
        self.assertEqual(a["threshold_rule"]["value"], "min(0.97, q05_equal_methods)")
        self.assertEqual(a["T3_ceiling"], "LIMITED_DEPLOYMENT")
        self.assertGreaterEqual(a["long_horizon"]["min_independent_outcomes_for_ACTIVE"], 8)

    def test_noise_floor_study_sizes_match_the_windows(self):
        sizes = load()["admission"]["threshold_rule"]["noise_floor_study"]["sample_sizes"]
        # W1 = 17 years, W2 = 13 years
        self.assertEqual(sizes["monthly"], [13 * 12, 17 * 12])
        self.assertEqual(sizes["quarterly"], [13 * 4, 17 * 4])

    def test_data_depth_matches_what_was_verified(self):
        d = load()["data_depth"]["first_realtime_vintage"]
        self.assertEqual(d["FRED-MD"], "1999-08")
        self.assertEqual(d["FRED-QD"], "2018-05")
        self.assertEqual(d["ALFRED_CPIAUCSL"], "1972-07-21")

    def test_llm_results_cannot_count_when_contaminated(self):
        r = load()["model_class_rules"]
        self.assertIn("CONTAMINATED", r["llm_evidence"])
        self.assertIn("CONTAMINATED", r["agent_selection"])

    def test_scored_forecast_is_a_precise_distribution(self):
        f = load()["forecast_product"]
        self.assertIn("precise", f["distribution"])
        self.assertIn("scenario layer", f["imprecise_probabilities"])
        self.assertIn("never in a scored forecast", f["imprecise_probabilities"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
