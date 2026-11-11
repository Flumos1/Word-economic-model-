"""The frozen evaluation protocol must stay unchanged and structurally valid."""
import hashlib, json, sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "protocols" / "evaluation_protocol_v1.json"
PINNED = ROOT / "protocols" / "evaluation_protocol_v1.sha256"


def load():
    return json.loads(PROTOCOL.read_text(encoding="utf-8"))


class ProtocolV1(unittest.TestCase):
    def test_file_is_frozen(self):
        # Git on Windows may rewrite line endings; hash the LF form.
        data = PROTOCOL.read_bytes().replace(b"\r\n", b"\n")
        self.assertEqual(hashlib.sha256(data).hexdigest(), PINNED.read_text().split()[0],
                         "v1 is frozen: create evaluation_protocol_v2.json instead of editing v1")

    def test_windows_ordered_and_disjoint(self):
        w = load()["windows"]
        order = ["selection", "W1", "W2", "W3"]
        for a, b in zip(order, order[1:]):
            self.assertLess(w[a]["end"], w[b]["start"], f"{a} overlaps {b}")
        for name in order:
            self.assertLess(w[name]["start"], w[name]["end"])

    def test_every_target_has_horizons_and_baselines(self):
        p = load()
        ids = {t["id"] for t in p["targets"]}
        self.assertEqual(ids, {"T1_CPI_YOY", "T2_GDP_GROWTH", "T3_RECESSION"})
        for t in p["targets"]:
            self.assertTrue(t.get("horizons_months") or t.get("horizons_quarters"), t["id"])
            self.assertTrue(any(t["id"] in b["applies_to"] for b in p["baselines"]), t["id"])

    def test_admission_criteria_complete(self):
        a = load()["admission"]
        self.assertEqual([c["id"] for c in a["criteria_all_required"]],
                         ["C1", "C2", "C3", "C4", "C5", "C6"])
        self.assertEqual(a["T3_ceiling"], "LIMITED_DEPLOYMENT")

    def test_decisions_cover_closed_questions(self):
        p = load()
        self.assertEqual(p["closes_questions"], ["Q-001", "Q-002", "Q-003", "Q-004", "Q-005", "Q-006"])
        self.assertEqual(len(p["decision_ids"]), 6)

    def test_forward_registry_is_not_backdated(self):
        self.assertIsNone(load()["windows"]["forward_registry"]["start"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
