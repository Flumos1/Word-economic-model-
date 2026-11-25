"""Licence hygiene of the repository: no third-party raw data in git, metadata-only artifacts,
report hygiene and licence-register coverage of the sources used by the code.

Offline and read-only. The audit that motivates every check is docs/84_Лицензионный_аудит_2_2026-11-12.md.

Known finding F-1 (owner decision pending): protocols/first_real_evaluation_report.json embeds the
evaluation actual levels (`windows.W*.actual_primary` / `actual_secondary`) as third-party values.
The finding is listed in KNOWN_REPORT_RAW_VALUES and documented in docs/84; one test is marked
expectedFailure while the finding is open. When the report is cleaned, that test turns into an
unexpected success (visible failure) and KNOWN_REPORT_RAW_VALUES must be emptied - this is deliberate.

Run: python tests/test_licence_hygiene.py
"""
import hashlib
import json
import re
import subprocess
import unittest
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
PROTOCOLS = ROOT / "protocols"
REGISTER = PROTOCOLS / "licence_register.json"
DATA_LOG = PROTOCOLS / "data_log.json"
GATE = PROTOCOLS / "r2_gate_report.json"
SPF = PROTOCOLS / "spf_metadata.json"
NBER = PROTOCOLS / "nber_dates.json"
FIRST_EVAL = PROTOCOLS / "first_evaluation_report.json"
FIRST_REAL = PROTOCOLS / "first_real_evaluation_report.json"
PLAN = PROTOCOLS / "data_plan.json"
R3 = PROTOCOLS / "r3_specification.json"
GITIGNORE = ROOT / ".gitignore"
AUDIT_DOC = ROOT / "docs" / "84_Лицензионный_аудит_2_2026-11-12.md"

RAW_SUFFIXES = {".csv", ".zip", ".xlsx", ".xls", ".parquet", ".dta", ".feather", ".gz"}
# own data only: the forward registry of the project's own forecasts (0 bytes today)
ALLOWED_TRACKED_RAW = {
    "registry/forecasts.jsonl",
    # EIA Brent monthly series committed in wave 24 (public domain, citation requested):
    # the series has a committed .meta.json provenance file and a licence-register entry.
    "data/oil/brent_monthly_eia.csv",
    "data/oil/brent_monthly_eia.meta.json",
    # Small public aggregates committed on 2026-11-25 with full provenance sidecars
    # (source URL, licence, retrieval date, coverage, sha256) — docs/236, docs/205.
    "data/public/eurostat_hicp_monthly.csv",
    "data/public/eurostat_hicp_monthly.meta.json",
    "data/public/un_comtrade_deu_hs27_2023.csv",
    "data/public/un_comtrade_deu_hs27_2023.meta.json",
}
NUMERIC_ARRAY_MIN = 10
CONSTANT_ARRAYS_ALLOWED = {("protocols/r3_specification.json", "quantile_grid")}
# docs/84 finding F-1; remove an item only together with the cleanup of the report
KNOWN_REPORT_RAW_VALUES = set()  # F-1/F-2 fixed on 2026-11-12: reports carry digests only
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISO_MONTH = re.compile(r"^\d{4}-\d{2}$")
STATUSES = {"OPEN", "RESTRICTED", "UNVERIFIED"}


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def tracked_files():
    """Paths in the git index; None when git is unavailable (the test then skips)."""
    if not (ROOT / ".git").exists():
        return None
    try:
        result = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"],
                                capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - environment dependent
        return None
    # file names can be non-ascii (Cyrillic docs); never let the locale decide the decoding
    text = result.stdout.decode("utf-8", "surrogateescape")
    return [item for item in text.split("\0") if item]


def numeric_arrays(obj, path="$", depth=0, out=None):
    """(path, length, sample) of lists that are almost entirely numbers and long enough to be a series."""
    if out is None:
        out = []
    if depth > 10:
        return out
    if isinstance(obj, dict):
        for key, value in obj.items():
            numeric_arrays(value, f"{path}.{key}", depth + 1, out)
    elif isinstance(obj, list):
        numbers = [x for x in obj if isinstance(x, (int, float)) and not isinstance(x, bool)]
        if len(obj) >= NUMERIC_ARRAY_MIN and len(numbers) >= 0.9 * len(obj):
            out.append((path, len(obj), obj[:3]))
        for index, value in enumerate(obj[:2]):
            numeric_arrays(value, f"{path}[{index}]", depth + 1, out)
    return out


def iter_keys(obj, prefix="$"):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield f"{prefix}.{key}"
            yield from iter_keys(value, f"{prefix}.{key}")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from iter_keys(value, f"{prefix}[{index}]")


class GitTree(unittest.TestCase):
    def test_index_has_no_raw_third_party_files(self):
        files = tracked_files()
        if files is None:
            self.skipTest("git index is not available")
        offenders = []
        for name in files:
            if name.startswith("data/"):
                if name not in ALLOWED_TRACKED_RAW:
                    offenders.append(name)
            elif Path(name).suffix.lower() in RAW_SUFFIXES and name not in ALLOWED_TRACKED_RAW:
                offenders.append(name)
        self.assertEqual(offenders, [], "third-party raw data must never be committed (docs/84 F-1, F-2)")

    def test_public_aggregates_carry_full_provenance(self):
        """Every committed aggregate under data/public must be reproducible and licensed."""
        files = tracked_files()
        if files is None:
            self.skipTest("git index is not available")
        aggregates = [name for name in files
                      if name.startswith("data/public/") and name.endswith(".csv")]
        self.assertTrue(aggregates, "expected at least one pinned public aggregate")
        for name in aggregates:
            meta_name = name[: -len(".csv")] + ".meta.json"
            self.assertIn(meta_name, files, f"{name}: provenance sidecar is not committed")
            meta = load(ROOT / meta_name)
            self.assertEqual(str(meta.get("file", "").replace("\\", "/")), name)
            self.assertEqual(str(meta.get("file_sha256", "")),
                             hashlib.sha256((ROOT / name).read_bytes()).hexdigest(),
                             f"{name}: sidecar hash must match the committed bytes")
            for field in ("source_url", "license", "retrieved_at", "coverage"):
                self.assertTrue(str(meta.get(field, "")).strip(), f"{name}: missing {field}")
            self.assertRegex(str(meta.get("retrieved_at", "")), r"^\d{4}-\d{2}-\d{2}$")

    def test_data_directories_are_gitignored(self):
        text = GITIGNORE.read_text(encoding="utf-8")
        for line in ("/data/raw/", "/data/external/"):
            self.assertIn(line, text)
        self.assertIn("never committed", text)
        self.assertIn("licence_policy", text)

    def test_protocols_and_tools_carry_no_workbooks(self):
        files = tracked_files()
        if files is None:
            self.skipTest("git index is not available")
        offenders = [f for f in files
                     if f.startswith(("protocols/", "tools/")) and Path(f).suffix.lower() in RAW_SUFFIXES]
        self.assertEqual(offenders, [])


class MetadataOnlyArtifacts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = load(PLAN)
        cls.register = load(REGISTER)
        cls.ids = {e["id"]: e for e in cls.register["entries"]}

    def test_data_log_is_metadata_only(self):
        log = load(DATA_LOG)
        self.assertEqual(log["tool"], "tools/fetch_data.py")
        self.assertTrue(log["no_human_download"])
        self.assertEqual(log["failures"], [])
        self.assertTrue(log["entries"], "the log of the last retrieval must not be empty")
        for entry in log["entries"]:
            self.assertIn("url", entry)
            self.assertRegex(entry["sha256"], SHA256)
            self.assertTrue(entry["url"].startswith(("https://fred.stlouisfed.org/",
                                                     "https://alfred.stlouisfed.org/")))
        for key in iter_keys(log):
            leaf = key.rsplit(".", 1)[-1].lower()
            self.assertNotIn(leaf, {"values", "observations", "rows_values"},
                             f"data_log must not carry series rows: {key}")
        self.assertEqual(numeric_arrays(log), [], "data_log must not carry value arrays")

    def test_spf_metadata_is_metadata_only(self):
        doc = load(SPF)
        self.assertFalse(doc["raw_files_committed"])
        self.assertTrue(doc["values_location"].startswith("data/raw/"), doc["values_location"])
        self.assertIn("never committed", doc["publication_rule"])
        self.assertIn(doc["licence_register_entry"], self.ids)
        self.assertIn("philadelphiafed.org", doc["source_url"])
        self.assertEqual(numeric_arrays(doc), [])
        text = json.dumps(doc)
        self.assertNotIn("INFCPI1YR\": [", text, "SPF values must not be committed")
        self.assertFalse((PROTOCOLS / "spf_median.json").exists())
        for variable in doc["variables"]:
            self.assertRegex(variable["values_sha256"], SHA256)
            self.assertNotIn("values", variable)

    def test_nber_dates_are_dates_and_provenance_only(self):
        doc = load(NBER)
        self.assertIn("attribution", doc["permission"])
        self.assertTrue(doc["attribution"].startswith("Source: National Bureau of Economic Research"))
        self.assertRegex(doc["source_page_sha256"], SHA256)
        self.assertEqual(len(doc["announcements"]), doc["coverage"]["announcements"])
        for announcement in doc["announcements"]:
            self.assertRegex(announcement["announcement_date"], ISO_DATE)
            self.assertIn(announcement["type"], {"peak", "trough", "memo"})
            if announcement["cycle_date"] is None:
                self.assertEqual(announcement["type"], "memo", announcement)
            else:
                self.assertRegex(announcement["cycle_date"], ISO_MONTH)
        for row in doc["reference_dates"]:
            self.assertRegex(row["peak"], ISO_MONTH)
            self.assertRegex(row["trough"], ISO_MONTH)
        self.assertEqual(numeric_arrays(doc), [])

    def test_gate_report_values_are_bounded_spot_checks(self):
        doc = load(GATE)
        pairs = doc["pairs"]
        self.assertGreaterEqual(len(pairs), 30, "the R2 gate needs at least 30 spot-check pairs")
        self.assertLessEqual(len(pairs), 500, "gate evidence must stay a bounded spot check, not a series")
        for pair in pairs:
            # F-2 fixed: digests and a flag instead of the third-party values
            self.assertEqual(set(pair) >= {"series", "period", "release", "initial_release_sha256",
                                           "vintage_value_sha256", "equal_within_tolerance"}, True, pair)
            self.assertRegex(pair["initial_release_sha256"], SHA256)
            self.assertRegex(pair["vintage_value_sha256"], SHA256)
            self.assertNotIn("initial_release", pair)
            self.assertNotIn("vintage_value", pair)
        self.assertEqual(numeric_arrays(doc), [], "no observation arrays in the gate report")
        self.assertTrue(doc["passed"])
        self.assertEqual(doc["totals"]["failures"], 0)
        self.assertGreaterEqual(doc["totals"]["matched_pairs_pre_2000"], 10)

    def test_first_evaluation_report_is_aggregates_only(self):
        doc = load(FIRST_EVAL)
        self.assertEqual(numeric_arrays(doc), [])
        for pair in doc["pairs"]:
            for key in ("candidate_loss", "comparator_loss", "relative_loss", "threshold"):
                self.assertIsInstance(pair[key], (int, float), key)


class ReportHygiene(unittest.TestCase):
    def violations(self):
        found = []
        for path in sorted(PROTOCOLS.glob("*.json")):
            key = f"protocols/{path.name}"
            for match in numeric_arrays(load(path)):
                if (key, match[0].lstrip("$.")) in CONSTANT_ARRAYS_ALLOWED:
                    continue
                found.append((key, match[0].lstrip("$.")))
        return sorted(found)

    def test_known_violation_is_documented_and_fixed(self):
        self.assertTrue(AUDIT_DOC.exists(), "docs/84 must record the findings")
        text = AUDIT_DOC.read_text(encoding="utf-8")
        self.assertIn("F-1", text)
        self.assertIn("дайджест", text)
        self.assertEqual(KNOWN_REPORT_RAW_VALUES, set(), "F-1 is fixed: reports must not embed actual values")
        doc = load(FIRST_REAL)
        for window_name in ("W1", "W2"):
            window = doc["windows"][window_name]
            self.assertNotIn("actual_primary", window, window_name)
            self.assertNotIn("actual_secondary", window, window_name)
            self.assertIn("actual_primary_sha256", window, window_name)
            self.assertIn("actual_secondary_sha256", window, window_name)

    def test_only_the_known_violation_exists(self):
        found = {tuple(item) for item in self.violations()}
        unexpected = found - KNOWN_REPORT_RAW_VALUES
        self.assertEqual(sorted(unexpected), [],
                         "committed artifacts must carry derived statistics only (licence_policy.intended_use)")

    def test_reports_contain_only_derived_statistics(self):
        found = self.violations()
        self.assertEqual(found, [], f"third-party values embedded in committed reports: {found}")


class RegisterCoverage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.register = load(REGISTER)
        cls.ids = {e["id"]: e for e in cls.register["entries"]}
        cls.statuses = {e["id"]: e["status"] for e in cls.register["entries"]}

    def test_register_is_well_formed(self):
        ids = [e["id"] for e in self.register["entries"]]
        self.assertEqual(len(ids), len(set(ids)), "register ids must be unique")
        for entry in self.register["entries"]:
            self.assertIn(entry["status"], STATUSES, entry["id"])
            self.assertIn(entry["level"], {"full", "extraction", "none"}, entry["id"])
            self.assertRegex(entry["date"], ISO_DATE)
            self.assertTrue(entry["basis"], entry["id"])
            if entry["level"] != "none":
                self.assertTrue(entry["url"], f"{entry['id']}: an entry read from a page must cite its url")
                for url in entry["url"]:
                    self.assertEqual(urlparse(url).scheme, "https", entry["id"])

    def test_data_plan_series_are_registered_and_open(self):
        for series in load(PLAN)["series"]:
            self.assertIn(series["register"], self.ids, series["id"])
            self.assertEqual(self.statuses[series["register"]], "OPEN", series["id"])

    def test_r3_inputs_are_registered_and_not_restricted(self):
        spec = load(R3)
        for model in spec["models"]:
            for inputs in model.get("inputs_by_target", {}).values():
                for source in inputs:
                    self.assertIn(source, self.statuses, f"{model['id']}: {source} is not in the register")
                    if model.get("comparator_member") and model.get("status") != "PROPOSED_BLOCKED_ON_REGISTER":
                        self.assertNotEqual(self.statuses[source], "RESTRICTED",
                                            f"{model['id']} uses a RESTRICTED source")

    def test_sources_used_by_tools_are_registered(self):
        # tool -> (marker inside the tool, register entry, domain that entry must cite)
        expectations = {
            "tools/fetch_data.py": ("alfred.stlouisfed.org", "FRED_ALFRED_SERVICES",
                                    "alfred.stlouisfed.org"),
            "tools/verify_data_gate.py": ("fredgraph.csv", "FRED_ALFRED_SERVICES",
                                          "fred.stlouisfed.org"),
            "tools/fetch_spf.py": ("philadelphiafed.org", "PHILADELPHIA_FED",
                                   "philadelphiafed.org"),
            "tools/nber_dates.py": ("nber.org", "NBER_CYCLE_DATES", "nber.org"),
        }
        register_urls = [url for entry in self.register["entries"] for url in entry.get("url", [])]
        for tool, (marker, entry_id, domain) in expectations.items():
            text = (ROOT / tool).read_text(encoding="utf-8")
            self.assertTrue(marker in text, f"{tool}: source marker {marker} disappeared")
            self.assertIn(entry_id, self.ids, tool)
            self.assertEqual(self.statuses[entry_id], "OPEN", tool)
            self.assertTrue(any(domain in url for url in register_urls),
                            f"{entry_id}: the register must cite {domain}")

    def test_every_used_source_is_open(self):
        used = {series["register"] for series in load(PLAN)["series"]}
        spec = load(R3)
        for model in spec["models"]:
            for inputs in model.get("inputs_by_target", {}).values():
                used.update(inputs)
        self.assertNotIn("ATLANTA_FED_GDPNOW", used)
        restricted = {source for source in used if self.statuses[source] == "RESTRICTED"}
        self.assertEqual(restricted, set())

    def test_register_states_the_raw_data_rule_and_the_new_uses(self):
        self.assertIn("raw_data_rule", self.register)
        rule = self.register["raw_data_rule"]
        self.assertIn("never committed", rule)
        self.assertIn("derived statistics", self.register["intended_use"])
        self.assertIn("never raw third-party data", self.register["intended_use"])

        fred = " ".join(self.ids["FRED_ALFRED_SERVICES"]["conditions"])
        for token in ("CHANNEL:", "STORAGE:", "ML:", "RETRIEVAL RISK:", "TERMS HASH:"):
            self.assertIn(token, fred)

        spf = " ".join(self.ids["PHILADELPHIA_FED"]["conditions"])
        self.assertIn("SPF VALUES OUTSIDE GIT", spf)
        self.assertIn("RTDSM", " ".join(self.ids["PHILADELPHIA_FED_RTDSM"]["conditions"]))

        nber = " ".join(self.ids["NBER_CYCLE_DATES"]["conditions"])
        self.assertIn("attribution", nber.lower())

    def test_audit_document_is_referenced(self):
        self.assertTrue(AUDIT_DOC.exists())
        self.assertTrue(any("docs/84" in item for item in self.register["audit"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
