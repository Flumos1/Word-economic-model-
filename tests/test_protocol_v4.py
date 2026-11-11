"""Protocol v4: three pre-registered owner decisions on top of the frozen v3.

D-131: the T3 evaluation window W1 starts 1996-12, and PROBIT_SPREAD uses only the
       GS10-TB3MS spread (T10Y3M dropped: its ALFRED vintages start 2014-01-27), so both
       T3 comparator members are REAL_TIME over the narrowed window.
D-132: the human download step is replaced by automated retrieval - latest and vintage
       files through the public CSV endpoint, the NBER chronology through USREC, and every
       source the pipeline uses is in protocols/data_plan.json. Raw files stay out of git.

Integrity: v4 must be the frozen v3 plus exactly these documented changes.
"""
import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
V3 = ROOT / "protocols" / "evaluation_protocol_v3.json"
V3_PINNED = ROOT / "protocols" / "evaluation_protocol_v3.sha256"
V4 = ROOT / "protocols" / "evaluation_protocol_v4.json"
V4_PINNED = ROOT / "protocols" / "evaluation_protocol_v4.sha256"
REGISTER = ROOT / "protocols" / "licence_register.json"
TOOL = ROOT / "tools" / "fetch_data.py"
PLAN = ROOT / "protocols" / "data_plan.json"
WORKFLOW = ROOT / ".github" / "workflows" / "data-refresh.yml"

ALLOWED_CHANGED_KEYS = {
    "version", "status", "supersedes", "decision_ids", "meta_gate", "amendment_rule",
    "licence_policy", "windows", "admission", "data_depth", "changes_from_v3", "baselines", "multiplicity", "forward_registry",
}
ALLOWED_CHANGED_PATHS = (
    "licence_policy.data_channel", "licence_policy.raw_data",
    "windows.target_overrides", "admission.window_note", "admission.criteria",
    "data_depth.retrieval", "data_depth.vintage_sources", "baselines",
    "data_depth.automation_coverage", "data_depth.nber_rule", "data_depth.first_realtime_vintage",
    "data_depth.nber_source_id", "multiplicity.primary", "forward_registry.control_arms",
)

IMMUTABLE = (
    "targets", "actuals", "estimation", "comparator_minimum", "primary_comparator_rule",
    "losses", "model_class_rules", "covid_handling",
    "drift_triggers", "comparison_unit", "origin_policy", "forecast_product", "universe",
    "protocol_id",
)


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def lf_sha256(path):
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def status_by_id():
    reg = json.loads(REGISTER.read_text(encoding="utf-8"))
    return {e["id"]: e["status"] for e in reg["entries"]}


def resolve(data, dotted):
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def diff_paths(a, b, prefix=""):
    out = []
    if type(a) is not type(b):
        return [prefix or "<root>"]
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b)):
            p = f"{prefix}.{key}" if prefix else key
            if key not in a or key not in b:
                out.append(p)
            else:
                out.extend(diff_paths(a[key], b[key], p))
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(prefix)
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                out.extend(diff_paths(x, y, f"{prefix}[{i}]"))
    else:
        if a != b:
            out.append(prefix)
    return out


class ProtocolV4(unittest.TestCase):
    def test_v4_supersedes_exactly_the_frozen_v3(self):
        v3 = load(V3)
        v4 = load(V4)
        self.assertEqual(v3["status"], "FROZEN")
        self.assertEqual(v4["supersedes"]["sha256"], lf_sha256(V3))
        self.assertEqual(v4["supersedes"]["sha256"], V3_PINNED.read_text().split()[0])
        self.assertEqual(v4["supersedes"]["version"], v3["version"])

    def test_status_and_freeze_branch(self):
        v4 = load(V4)
        data = V4.read_bytes().replace(b"\r\n", b"\n")
        if v4["status"] == "FROZEN":
            self.assertTrue(V4_PINNED.exists(), "a frozen protocol needs a pinned hash")
            self.assertEqual(hashlib.sha256(data).hexdigest(), V4_PINNED.read_text().split()[0])
            self.assertEqual(v4["meta_gate"]["approval"], "APPROVED")
        else:
            self.assertEqual(v4["status"], "DRAFT")
            self.assertEqual(v4["meta_gate"]["approval"], "PENDING")
            self.assertFalse(V4_PINNED.exists(), "a draft must not have a pinned hash")

    def test_only_the_documented_changes_differ_from_v3(self):
        v3 = load(V3)
        v4 = load(V4)
        changed = diff_paths(v3, v4)
        for path in changed:
            top = path.split(".")[0].split("[")[0]
            self.assertIn(top, ALLOWED_CHANGED_KEYS, f"unexpected change outside the v4 amendment: {path}")
            if top in {"licence_policy", "windows", "admission", "data_depth", "baselines"}:
                self.assertTrue(any(path == p or path.startswith(p) for p in ALLOWED_CHANGED_PATHS),
                                f"change outside the owner decisions: {path}")
        for key in IMMUTABLE:
            self.assertEqual(v3[key], v4[key], f"{key} must not change in v4")

    def test_v3_baselines_untouched_except_the_two_documented_changes(self):
        v3b = {b["id"]: b for b in load(V3)["baselines"]}
        v4b = {b["id"]: b for b in load(V4)["baselines"]}
        self.assertEqual(set(v3b), set(v4b))
        for bid, b in v4b.items():
            if bid in ("PROBIT_SPREAD", "SPF_MEDIAN"):
                continue
            self.assertEqual(b, v3b[bid], f"{bid} must not change")
        probit = v4b["PROBIT_SPREAD"]
        self.assertEqual(probit["inputs_by_target"]["T3_RECESSION"],
                         ["ALFRED:GS10", "ALFRED:TB3MS", "NBER_CYCLE_DATES"])
        self.assertIn("T10Y3M series is not used", probit["point"])
        self.assertIn("2014-01-27", probit["point"])
        spf = v4b["SPF_MEDIAN"]
        self.assertFalse(spf["comparator_member"], "SPF is not a comparator member before R2")
        self.assertIn("R2", spf["available_from"])
        self.assertIn("never scored", spf["note"])

    def test_t1_t2_comparator_minimum_holds_without_spf(self):
        v4 = load(V4)
        statuses = status_by_id()
        for target in ("T1_CPI_YOY", "T2_GDP_GROWTH"):
            members = []
            for b in v4["baselines"]:
                if not b.get("comparator_member") or not b.get("scored", True):
                    continue
                if target not in b["applies_to"]:
                    continue
                inputs = b["inputs_by_target"].get(target, [])
                if inputs and all(statuses.get(i) == "OPEN" for i in inputs):
                    members.append(b["id"])
            self.assertGreaterEqual(len(members), v4["comparator_minimum"]["min_members"], target)
            self.assertNotIn("SPF_MEDIAN", members)

    def test_nber_is_represented_in_the_automated_plan(self):
        v4 = load(V4)
        dd = v4["data_depth"]
        self.assertIn("ALFRED:USREC", dd["nber_source_id"])
        self.assertIn("ALFRED:USREC", dd["automation_coverage"])
        plan = load(PLAN)
        registers = {s["register"] for s in plan["series"]}
        self.assertIn("ALFRED:USREC", registers)
        self.assertNotIn("PHILADELPHIA_FED", registers, "SPF is not automated yet, so not used")

    def test_t3_window_override(self):
        w = load(V4)["windows"]
        self.assertEqual(w["target_overrides"]["T3_RECESSION"]["W1_start"], "1996-12")
        self.assertLess(w["target_overrides"]["T3_RECESSION"]["W1_start"], w["W1"]["end"])
        self.assertGreater(w["target_overrides"]["T3_RECESSION"]["W1_start"], w["W1"]["start"])
        self.assertEqual(list(w["target_overrides"]), ["T3_RECESSION"], "no other target may change its window")
        note = load(V4)["admission"]["window_note"]
        self.assertIn("T3", note)
        self.assertIn("1996-12", note)
        for crit in load(V4)["admission"]["criteria"]:
            if crit["id"] in ("C1", "C2", "C5"):
                self.assertIn("admission.window_note", crit["text"], crit["id"])

    def test_t3_comparator_is_real_time_on_the_narrowed_w1(self):
        v4 = load(V4)
        statuses = status_by_id()
        first = v4["data_depth"]["first_realtime_vintage"]
        key = {"ALFRED:GS10": "ALFRED_GS10", "ALFRED:TB3MS": "ALFRED_TB3MS",
               "ALFRED:UNRATE": "ALFRED_UNRATE"}
        members = []
        for b in v4["baselines"]:
            if not b.get("comparator_member") or not b.get("scored", True):
                continue
            if "T3_RECESSION" not in b["applies_to"]:
                continue
            inputs = b["inputs_by_target"]["T3_RECESSION"]
            if not all(statuses.get(i) == "OPEN" for i in inputs):
                continue
            dates = [first[key[i]] for i in inputs if i in key]
            for d in dates:
                self.assertLessEqual(d, "1996-12-31", f"{b['id']}: {d} is later than the T3 W1 start")
            members.append(b["id"])
        self.assertGreaterEqual(len(members), v4["comparator_minimum"]["min_members"], members)
        self.assertIn("PROBIT_SPREAD", members)
        self.assertIn("SAHM_RULE", members)

    def test_first_vintage_facts_and_nber_rule(self):
        dd = load(V4)["data_depth"]
        first = dd["first_realtime_vintage"]
        self.assertEqual(first["ALFRED_GS10"], "1996-12-03")
        self.assertEqual(first["ALFRED_TB3MS"], "1996-12-03")
        self.assertEqual(first["ALFRED_T10Y3M"], "2014-01-27")
        self.assertIn("USREC", dd["nber_rule"])
        self.assertIn("published before the origin", dd["nber_rule"])
        self.assertIn("Every source that the current pipeline uses", dd["automation_coverage"])
        self.assertIn("not automated is not used", dd["automation_coverage"])

    def test_automated_retrieval_policy(self):
        v4 = load(V4)
        ch = v4["licence_policy"]["data_channel"]
        text = " ".join(ch.values())
        for phrase in ("automated", "rate-limit", "human download", "download-data form",
                       "requires or permits a human download"):
            if phrase == "download-data form":
                self.assertIn(phrase, text)
            elif phrase == "requires or permits a human download":
                self.assertIn(phrase, ch["automation"])
            else:
                self.assertIn(phrase, text)
        self.assertNotIn("manual download", text.lower())
        retrieval = v4["data_depth"]["retrieval"]
        self.assertEqual(retrieval["tool"], "tools/fetch_data.py")
        self.assertEqual(retrieval["workflow"], ".github/workflows/data-refresh.yml")
        self.assertEqual(retrieval["data_log"], "protocols/data_log.json")
        self.assertIn("last_observation", retrieval["log_fields"])
        self.assertIn("vintage_check", retrieval["log_fields"])
        self.assertIn("download-data", retrieval["modes"])
        self.assertTrue(TOOL.exists())
        self.assertTrue(PLAN.exists())
        self.assertTrue(WORKFLOW.exists())
        workflow_text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("--include-vintages", workflow_text)
        self.assertIn("download", TOOL.read_text(encoding="utf-8"))

    def test_dataset_plan_covers_the_r2_gate(self):
        plan = load(PLAN)
        statuses = status_by_id()
        pairs = 0
        pre_2000 = 0
        for s in plan["series"]:
            self.assertIn(s["register"], statuses, s["register"])
            self.assertEqual(statuses[s["register"]], "OPEN", s["register"])
            self.assertTrue(s["url"].startswith("https://fred.stlouisfed.org/"), s["url"])
            for date in s.get("vintages", []):
                pairs += 1
                if date < "2000-01-01":
                    pre_2000 += 1
        self.assertGreaterEqual(pairs, 30, "the R2 gate needs at least 30 (series, date) pairs")
        self.assertGreaterEqual(pre_2000, 10, "at least 10 pairs must be before 2000")
        self.assertIn("USREC", {s["id"] for s in plan["series"]})

    def test_owner_decisions_are_recorded(self):
        v4 = load(V4)
        decisions = " ".join(d["decision"] for d in v4["meta_gate"]["owner_decisions"])
        self.assertIn("D-131", decisions)
        self.assertIn("D-132", decisions)
        self.assertIn("T10Y3M", decisions)
        self.assertIn("D-131", v4["decision_ids"])
        self.assertIn("D-132", v4["decision_ids"])
        self.assertEqual(v4["meta_gate"]["owner_confirmation_needed"], [])
        confirmations = " ".join(c["decision"] for c in v4["meta_gate"]["owner_confirmations"])
        self.assertIn("меняй правила", confirmations)

    def test_changes_from_v3(self):
        v4 = load(V4)
        changes = v4["changes_from_v3"]
        self.assertEqual([c["id"] for c in changes], ["W4-01", "W4-02"])
        for c in changes:
            self.assertEqual(c["disposition"], "adopted")
            self.assertTrue(c["paths"])
            for path in c["paths"]:
                self.assertTrue(resolve(v4, path), f"{c['id']}: {path} not found")


if __name__ == "__main__":
    unittest.main(verbosity=2)
