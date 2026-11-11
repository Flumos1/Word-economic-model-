"""NF-SIM threshold table: completeness, hash, threshold rule and consistency with protocol v4."""
import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "protocols" / "nf_sim_thresholds.json"
PIN = ROOT / "protocols" / "nf_sim_thresholds.sha256"
CONFIG = ROOT / "protocols" / "nf_sim_config.json"
PROTOCOL = ROOT / "protocols" / "evaluation_protocol_v4.json"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


class NfSimThresholds(unittest.TestCase):
    def test_table_is_complete_and_hashed(self):
        table = load(TABLE)
        self.assertEqual(table["study_id"], "NF-SIM")
        self.assertEqual(table["replications"], 10000)
        self.assertIn("min(0.97", table["threshold_rule"])
        digest = hashlib.sha256(TABLE.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        self.assertEqual(digest, PIN.read_text().split()[0])
        self.assertEqual(table["config_sha256"],
                         hashlib.sha256(CONFIG.read_bytes().replace(b"\r\n", b"\n")).hexdigest())

    def test_every_protocol_combination_is_covered(self):
        table = load(TABLE)
        keys = {(e["target"], e["horizon"], e["loss"], e["window"]) for e in table["entries"]}
        self.assertEqual(len(keys), len(table["entries"]), "duplicate keys")
        self.assertEqual(len(keys), 44, keys)
        expected = set()
        for target, horizons in (("T1_CPI_YOY", [1, 3, 6, 12]), ("T2_GDP_GROWTH", [0, 1, 2, 4])):
            for h in horizons:
                for loss in ("RMSE", "CRPS"):
                    for w in ("W1", "W2"):
                        expected.add((target, h, loss, w))
        for h in (3, 6, 12):
            for loss in ("Brier", "log_score"):
                for w in ("W1", "W2"):
                    expected.add(("T3_RECESSION", h, loss, w))
        self.assertEqual(keys, expected)

    def test_threshold_rule_and_effective_lengths(self):
        table = load(TABLE)
        protocol = load(PROTOCOL)
        periods = {"T1_CPI_YOY": {"W1": 204, "W2": 156},
                   "T2_GDP_GROWTH": {"W1": 68, "W2": 52},
                   "T3_RECESSION": {"W1": 121, "W2": 156}}
        self.assertEqual(protocol["version"], "4.0.0")
        for e in table["entries"]:
            self.assertEqual(e["threshold"],
                             round(min(0.97, e["q05_single"], e["q05_combination"]), 4), e)
            self.assertGreater(e["threshold"], 0.0)
            self.assertLessEqual(e["threshold"], 0.97)
            self.assertEqual(e["n_periods"], periods[e["target"]][e["window"]], e)
            hh = max(1, e["horizon"])
            self.assertEqual(e["effective_length"], max(12, e["n_periods"] - hh - (hh - 1)), e)

    def test_thresholds_are_not_trivially_loose(self):
        table = load(TABLE)
        thresholds = [e["threshold"] for e in table["entries"]]
        # a q05 far below 0.5 would make C1 meaningless; the study must stay in a sane band
        self.assertGreater(min(thresholds), 0.5)
        self.assertLessEqual(max(thresholds), 0.97)
        self.assertLess(sum(thresholds) / len(thresholds), 0.97)


if __name__ == "__main__":
    unittest.main(verbosity=2)
