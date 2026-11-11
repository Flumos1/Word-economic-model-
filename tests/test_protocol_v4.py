"""Protocol v4: two pre-registered owner decisions on top of the frozen v3.

(1) D-131 narrows the T3 evaluation window W1 to 1996-12, because ALFRED vintages of
    GS10/TB3MS start 1996-12-03 and before that date the T3 comparator has a single
    REAL_TIME member.
(2) D-132 replaces the human download step with automated retrieval through the project
    tool and CI within the published terms; raw third-party files stay out of the repo.

Integrity test: v4 must be the frozen v3 plus exactly these documented changes.
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
    "licence_policy", "windows", "admission", "data_depth", "changes_from_v3",
}
ALLOWED_CHANGED_PATHS = (
    "licence_policy.data_channel", "licence_policy.raw_data",
    "windows.target_overrides", "admission.window_note", "admission.criteria",
    "data_depth.retrieval", "data_depth.vintage_sources",
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
    """Paths (dotted) where two JSON structures differ."""
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
            if top in {"licence_policy", "windows", "admission", "data_depth"}:
                self.assertTrue(any(path == p or path.startswith(p) for p in ALLOWED_CHANGED_PATHS),
                                f"change outside the two owner decisions: {path}")
        # everything not listed above must be byte-identical in meaning
        for key in ("targets", "actuals", "estimation", "baselines", "comparator_minimum",
                    "primary_comparator_rule", "losses", "multiplicity", "model_class_rules",
                    "covid_handling", "forward_registry", "drift_triggers", "comparison_unit",
                    "origin_policy", "forecast_product", "universe", "protocol_id"):
            self.assertEqual(v3[key], v4[key], f"{key} must not change in v4")

    def test_t3_window_override(self):
        v4 = load(V4)
        w = v4["windows"]
        self.assertEqual(w["target_overrides"]["T3_RECESSION"]["W1_start"], "1996-12")
        self.assertLess(w["target_overrides"]["T3_RECESSION"]["W1_start"], w["W1"]["end"])
        self.assertGreater(w["target_overrides"]["T3_RECESSION"]["W1_start"], w["W1"]["start"])
        self.assertEqual(list(w["target_overrides"]), ["T3_RECESSION"], "no other target may change its window")
        note = v4["admission"]["window_note"]
        self.assertIn("T3", note)
        self.assertIn("1996-12", note)
        for crit in v4["admission"]["criteria"]:
            if crit["id"] in ("C1", "C2", "C5"):
                self.assertIn("admission.window_note", crit["text"], crit["id"])
        self.assertIn("data availability", v4["windows"]["target_overrides"]["T3_RECESSION"]["reason"])

    def test_t3_comparator_meets_the_minimum_on_the_narrowed_w1(self):
        v4 = load(V4)
        statuses = status_by_id()
        members = []
        for b in v4["baselines"]:
            if not b.get("comparator_member") or not b.get("scored", True):
                continue
            if "T3_RECESSION" not in b["applies_to"]:
                continue
            inputs = b["inputs_by_target"]["T3_RECESSION"]
            if all(statuses.get(i) == "OPEN" for i in inputs):
                members.append(b["id"])
        self.assertGreaterEqual(len(members), v4["comparator_minimum"]["min_members"], members)
        self.assertIn("PROBIT_SPREAD", members)
        self.assertIn("SAHM_RULE", members)

    def test_automated_retrieval_policy(self):
        v4 = load(V4)
        ch = v4["licence_policy"]["data_channel"]
        text = " ".join(ch.values())
        for phrase in ("automated", "rate-limit", "no human download", "no FRED API key"):
            self.assertIn(phrase, text)
        self.assertNotIn("manual download", text.lower())
        self.assertIn("No step of the data pipeline requires a human download", ch["automation"])
        self.assertIn("replaced by the project model", ch["automation"])
        retrieval = v4["data_depth"]["retrieval"]
        self.assertEqual(retrieval["tool"], "tools/fetch_data.py")
        self.assertEqual(retrieval["workflow"], ".github/workflows/data-refresh.yml")
        self.assertEqual(retrieval["data_log"], "protocols/data_log.json")
        # the promised tools exist
        self.assertTrue(TOOL.exists(), "tools/fetch_data.py is promised by the protocol")
        self.assertTrue(PLAN.exists(), "protocols/data_plan.json is promised by the protocol")
        self.assertTrue(WORKFLOW.exists(), "the data-refresh workflow is promised by the protocol")
        self.assertIn("never", v4["licence_policy"]["raw_data"].lower())

    def test_dataset_plan_is_registered_and_open(self):
        plan = load(PLAN)
        statuses = status_by_id()
        for series in plan["series"]:
            self.assertIn(series["register"], statuses, series["register"])
            self.assertEqual(statuses[series["register"]], "OPEN", series["register"])
            self.assertTrue(series["url"].startswith("https://fred.stlouisfed.org/"), series["url"])

    def test_owner_decisions_are_recorded(self):
        v4 = load(V4)
        decisions = " ".join(d["decision"] for d in v4["meta_gate"]["owner_decisions"])
        self.assertIn("D-131", decisions)
        self.assertIn("D-132", decisions)
        self.assertIn("D-131", v4["decision_ids"])
        self.assertIn("D-132", v4["decision_ids"])
        self.assertEqual(v4["meta_gate"]["owner_confirmation_needed"], [])
        self.assertIn("D-133", v4["meta_gate"]["on_approval"])

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
