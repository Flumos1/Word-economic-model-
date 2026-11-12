"""Forward-registry journal (protocol v4): schema, append-only discipline, duplicates, T3 rows."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import registry  # noqa: E402

REPO_JOURNAL = ROOT / "registry" / "forecasts.jsonl"
README = ROOT / "registry" / "README.md"

QUANTILES = [round(0.05 * i, 2) for i in range(1, 20)]


def good_record(**overrides):
    record = {
        "candidate_id": "AMFE_AR_BIC_v1",
        "code_commit": "a" * 40,
        "data_vintage_hash": "b" * 64,
        "target": "T1_CPI_YOY",
        "horizon": 3,
        "origin": "2026-10",
        "quantiles_5_to_95_step_5": [2.0 + 0.1 * i for i in range(19)],
        "point": 2.9,
        "scoring_rule_commit": "c" * 40,
        "registered_at": "2026-11-11T12:00:00Z",
    }
    record.update(overrides)
    return record


def t3_record(**overrides):
    record = {
        "candidate_id": "AMFE_PROBIT_v1",
        "code_commit": "d" * 40,
        "data_vintage_hash": "e" * 64,
        "target": "T3_RECESSION",
        "horizon": 6,
        "origin": "2026-10",
        "event_probability": 0.31,
        "scoring_rule_commit": "f" * 40,
        "registered_at": "2026-11-11T12:00:00Z",
    }
    record.update(overrides)
    return record


class RegistryJournal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = Path(self.tmp.name) / "forecasts.jsonl"
        self.journal.touch()

    def tearDown(self):
        self.tmp.cleanup()

    def test_repository_journal_is_open_valid_and_not_backdated(self):
        self.assertTrue(REPO_JOURNAL.exists(), "registry/forecasts.jsonl must be committed")
        self.assertTrue(README.exists(), "registry/README.md must explain the commit decision")
        rows, problems = registry.validate_journal(REPO_JOURNAL)
        self.assertEqual(problems, [])
        self.assertGreaterEqual(len(rows), 5, "the journal was opened with the first 2026-11 rows")
        self.assertGreater(REPO_JOURNAL.stat().st_size, 0, "the journal is open, not empty")
        status = registry.journal_status(REPO_JOURNAL)
        self.assertEqual(status["rows"], len(rows))
        self.assertIsNotNone(status["first_registered_at"])
        self.assertIsNotNone(status["forward_registry_start"])
        # the registry start is the date of the first committed row; later rows are append-only
        first_day = min(row["registered_at"][:10] for row in rows)
        self.assertEqual(status["forward_registry_start"], first_day)
        self.assertEqual(status["first_registered_at"][:10], first_day)

    def test_readme_states_the_commit_decision(self):
        text = README.read_text(encoding="utf-8")
        for phrase in ("append-only", "forecasts.jsonl", "метаданные", "Сырые", "correction_of",
                       "forward_registry.start"):
            self.assertIn(phrase, text)

    def test_valid_records_append(self):
        info = registry.append_record(good_record(), self.journal)
        self.assertTrue(info["appended"])
        registry.append_record(t3_record(), self.journal)
        rows, problems = registry.validate_journal(self.journal)
        self.assertEqual(problems, [])
        self.assertEqual(len(rows), 2)
        lines = self.journal.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0]), rows[0])
        status = registry.journal_status(self.journal)
        self.assertEqual(status["rows"], 2)
        self.assertEqual(status["forward_registry_start"], "2026-11-11")
        self.assertEqual({(p["target"], p["horizon"]) for p in status["pairs"]},
                         {("T1_CPI_YOY", 3), ("T3_RECESSION", 6)})

    def test_duplicate_is_rejected_and_journal_untouched(self):
        registry.append_record(good_record(), self.journal)
        before = self.journal.read_bytes()
        with self.assertRaises(registry.RegistryError) as caught:
            registry.append_record(good_record(), self.journal)
        self.assertIn("duplicate", str(caught.exception))
        self.assertEqual(self.journal.read_bytes(), before)

    def test_correction_is_a_new_row_referencing_the_old_one(self):
        registry.append_record(good_record(), self.journal)
        rows, _ = registry.validate_journal(self.journal)
        digest = registry.record_digest(rows[0])
        corrected = good_record(point=3.05, correction_of=digest,
                                registered_at="2026-11-12T09:00:00Z",
                                notes="point revised, source table corrected")
        registry.append_record(corrected, self.journal)
        rows, problems = registry.validate_journal(self.journal)
        self.assertEqual(problems, [])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["correction_of"], digest)
        # a correction that references nothing is rejected
        with self.assertRaises(registry.RegistryError):
            registry.append_record(good_record(origin="2026-09", correction_of="9" * 64),
                                   self.journal)

    def test_no_edit_or_delete_api(self):
        forbidden = [name for name in dir(registry)
                     if any(word in name.lower() for word in ("update", "delete", "remove", "edit"))
                     and callable(getattr(registry, name))]
        self.assertEqual(forbidden, [], "the journal is append-only by construction")

    def test_schema_rejections(self):
        bad_cases = {
            "target unknown": good_record(target="T9_UNKNOWN"),
            "horizon not allowed": good_record(horizon=2),
            "horizon wrong type": good_record(horizon="3"),
            "commit malformed": good_record(code_commit="xyz"),
            "vintage malformed": good_record(data_vintage_hash="abc"),
            "quantile count": good_record(quantiles_5_to_95_step_5=QUANTILES[:18]),
            "quantiles not increasing": good_record(
                quantiles_5_to_95_step_5=list(reversed(QUANTILES))),
            "point missing": good_record(point=None),
            "origin format": good_record(origin="2026/10"),
            "unknown field": good_record(extra_field=1),
            "T3 quantiles": t3_record(quantiles_5_to_95_step_5=QUANTILES),
            "T3 probability range": t3_record(event_probability=1.4),
            "T3 point": t3_record(point=0.1),
            "bad registered_at": good_record(registered_at="not-a-date"),
        }
        for label, record in bad_cases.items():
            self.assertNotEqual(registry.validate_record(record), [], label)

    def test_canonical_lines_and_lf(self):
        registry.append_record(good_record(), self.journal)
        raw = self.journal.read_bytes()
        self.assertNotIn(b"\r\n", raw)
        self.assertTrue(raw.endswith(b"\n"))
        line = raw.decode("utf-8").splitlines()[0]
        self.assertEqual(line, registry.canonical_json(json.loads(line)))
        self.assertIn('"candidate_id":"AMFE_AR_BIC_v1"', line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
