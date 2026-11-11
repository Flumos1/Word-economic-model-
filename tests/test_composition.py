"""Primary-comparator composition (protocol v4): register rule, OPEN sources, minimum, hash."""
import hashlib
import json
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import composition  # noqa: E402

DATE = "2026-11-11"
OUT = ROOT / "protocols" / f"comparator_composition_{DATE}.json"
PIN = ROOT / "protocols" / f"comparator_composition_{DATE}.sha256"
PROTOCOL = ROOT / "protocols" / "evaluation_protocol_v4.json"
REGISTER = ROOT / "protocols" / "licence_register.json"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


class ComparatorComposition(unittest.TestCase):
    def setUp(self):
        self.document = load(OUT)
        self.protocol = load(PROTOCOL)
        self.statuses = composition.resolve_statuses(load(REGISTER), DATE)

    def test_document_is_canonical_and_hashed(self):
        self.assertEqual(self.document["evaluation_date"], DATE)
        self.assertEqual(OUT.read_bytes(),
                         composition.canonical_text(self.document).encode("utf-8"))
        digest = hashlib.sha256(OUT.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        self.assertEqual(PIN.read_text(encoding="utf-8").split()[0], digest)
        self.assertIn(OUT.name, PIN.read_text(encoding="utf-8"))
        # keys are sorted in the file
        raw = OUT.read_text(encoding="utf-8")
        self.assertLess(raw.index('"document_id"'), raw.index('"targets"'))
        ok, message, rebuilt = composition.check_document(DATE)
        self.assertTrue(ok, message)
        self.assertEqual(rebuilt, digest)

    def test_protocol_and_register_are_pinned(self):
        self.assertEqual(self.document["protocol"]["version"], self.protocol["version"])
        self.assertEqual(self.document["protocol"]["sha256"], composition.lf_sha256(PROTOCOL))
        self.assertEqual(self.document["licence_register"]["sha256"],
                         composition.lf_sha256(REGISTER))
        self.assertEqual(self.document["rule"], self.protocol["primary_comparator_rule"])

    def test_every_member_is_open_and_scored(self):
        baselines = {b["id"]: b for b in self.protocol["baselines"]}
        for target, block in self.document["targets"].items():
            for loss, loss_block in block["losses"].items():
                self.assertTrue(loss_block["members"], f"{target}/{loss}: empty member list")
                for member in loss_block["members"]:
                    base = baselines[member["id"]]
                    self.assertTrue(base.get("comparator_member"), member["id"])
                    self.assertTrue(base.get("scored", True), member["id"])
                    self.assertIn(target, base["applies_to"], member["id"])
                    for source, status in member["inputs"].items():
                        self.assertEqual(status, "OPEN", f"{member['id']}: {source} is {status}")
                        self.assertEqual(self.statuses[source], "OPEN", source)

    def test_minimum_two_members_per_loss_and_horizon(self):
        minimum = self.protocol["comparator_minimum"]["min_members"]
        self.assertEqual(self.document["minimum_members"], minimum)
        for target, block in self.document["targets"].items():
            self.assertTrue(block["eligible_for_admission"], target)
            for loss, loss_block in block["losses"].items():
                self.assertTrue(loss_block["minimum_met"], f"{target}/{loss}")
                for horizon, by_h in loss_block["by_horizon"].items():
                    self.assertGreaterEqual(by_h["count"], minimum, f"{target}/{loss}/h{horizon}")
                    self.assertTrue(by_h["minimum_met"])
                    self.assertEqual(sorted(by_h["members"]), by_h["members"])

    def test_expected_membership_and_exclusions(self):
        targets = self.document["targets"]
        t1 = targets["T1_CPI_YOY"]["losses"]
        self.assertEqual({m["id"] for m in t1["RMSE"]["members"]},
                         {"AO", "AR_BIC", "BVAR_MINNESOTA", "RW"})
        # DFM is blocked by the UNVERIFIED register entry of its panel
        excluded = {e["id"]: e for e in targets["T1_CPI_YOY"]["excluded"]}
        self.assertIn("DFM_FREDMD", excluded)
        self.assertEqual(excluded["DFM_FREDMD"]["inputs"]["FRED-MD_FRED-QD"], "UNVERIFIED")
        # SPF was never a comparator member: T1 has no monthly y/y SPF forecast
        self.assertIn("SPF_MEDIAN", excluded)
        self.assertIn("comparator_member", excluded["SPF_MEDIAN"]["reason"])
        t3 = targets["T3_RECESSION"]["losses"]
        for loss in ("Brier", "log_score"):
            members = [m["id"] for m in t3[loss]["members"]]
            self.assertEqual(members, ["PROBIT_SPREAD", "SAHM_RULE"])
        for target in targets.values():
            member_ids = set()
            for loss_block in target["losses"].values():
                member_ids |= {m["id"] for m in loss_block["members"]}
            for spf in ("SPF_MEDIAN", "SPF_NEG_GDP_PROB", "NOWCAST_NYFED"):
                self.assertNotIn(spf, member_ids, spf)

    def test_t2_horizon_zero_has_the_own_nowcast(self):
        t2 = self.document["targets"]["T2_GDP_GROWTH"]["losses"]["RMSE"]["by_horizon"]
        self.assertIn("OWN_NOWCAST_BRIDGE", t2["0"]["members"])
        for horizon in ("1", "2", "4"):
            self.assertNotIn("OWN_NOWCAST_BRIDGE", t2[horizon]["members"])
            self.assertGreaterEqual(t2[horizon]["count"], 2)

    def test_distribution_members_exclude_point_only(self):
        for target, block in self.document["targets"].items():
            if "CRPS" not in block["losses"]:
                continue
            density = {m["id"] for m in block["losses"]["CRPS"]["members"]}
            point = {m["id"] for m in block["losses"]["RMSE"]["members"]}
            self.assertEqual(density, point, target)
            self.assertTrue(all(m["provides_distribution"] for m in block["losses"]["CRPS"]["members"]))

    def test_register_dated_resolution(self):
        register = load(REGISTER)
        entries = [e for e in register["entries"] if e["id"] == "FRED-MD_FRED-QD"]
        self.assertTrue(entries, "the panel entry must exist")
        first_date = min(e["date"] for e in entries)
        self.assertEqual(composition.resolve_statuses(register, first_date)["FRED-MD_FRED-QD"],
                         "UNVERIFIED")
        day_before = (date.fromisoformat(first_date) - timedelta(days=1))
        earlier = composition.resolve_statuses(register, day_before.isoformat())
        self.assertNotIn("FRED-MD_FRED-QD", earlier,
                         "an entry dated after the evaluation date must not resolve")
        # a journal is append-only: a later dated entry replaces the status, an earlier one does not
        journal = {"entries": [
            {"id": "X", "date": "2026-01-01", "status": "UNVERIFIED"},
            {"id": "X", "date": "2026-06-01", "status": "OPEN"},
            {"id": "X", "date": "2026-12-01", "status": "RESTRICTED"},
        ]}
        self.assertEqual(composition.resolve_statuses(journal, "2026-03-01")["X"], "UNVERIFIED")
        self.assertEqual(composition.resolve_statuses(journal, "2026-06-01")["X"], "OPEN")
        self.assertEqual(composition.resolve_statuses(journal, "2026-11-11")["X"], "OPEN")


if __name__ == "__main__":
    unittest.main(verbosity=2)
