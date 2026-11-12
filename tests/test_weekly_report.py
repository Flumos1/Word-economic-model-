"""Weekly integration report: structure, determinism, offline behaviour and degraded inputs.

The report is a deterministic snapshot of the committed state: the same repository files and the same
--date must produce byte-identical Markdown, no network is used, and missing or empty inputs (for
example the forward registry on the first day) must be reported as risks instead of crashing.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import weekly_report  # noqa: E402

AS_OF = "2026-11-12"


class ReportStructure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = weekly_report.build_markdown(ROOT, AS_OF)

    def test_all_sections_are_present(self):
        headings = (
            "# AMFE — недельный интеграционный отчёт",
            "## 1. Сводка",
            "## 2. Данные и винтажи",
            "## 3. Протокол, R3 и цепочка хешей",
            "## 4. Кандидаты и стадии допуска",
            "## 5. Критерии допуска C1–C6",
            "## 6. Журнал будущих прогнозов (forward registry)",
            "## 7. Риски",
            "## 8. Следующие шаги",
            "## 9. Источники и хеши",
        )
        for heading in headings:
            self.assertIn(heading, self.text, heading)

    def test_reports_the_repository_facts(self):
        for token in ("AMFE-WEEKLY-INTEGRATION-REPORT", AS_OF, "4.0.0", "FROZEN",
                      "ENSEMBLE_RW_AR", "REAL_TIME", "CANDIDATE", "178", "73",
                      "vintage_check", "R2"):
            self.assertIn(token, self.text, token)

    def test_criteria_and_data_sections_carry_results(self):
        for criterion in ("C1", "C2", "C3", "C4", "C5", "C6"):
            self.assertIn(f"| {criterion} |", self.text, criterion)
        self.assertIn("NOT_IMPLEMENTED", self.text)
        self.assertIn("NO_RESOLVED_FORECASTS", self.text)
        self.assertIn("Плотные винтажные наборы (ALFRED)", self.text)
        self.assertIn("Ворота R2", self.text)

    def test_the_report_does_not_assign_admission(self):
        self.assertIn("статусы кандидатов отчёт не присваивает", self.text)
        self.assertNotIn("ADMITTED", self.text)
        self.assertNotIn("статус присвоен", self.text)


class Determinism(unittest.TestCase):
    def test_same_date_produces_identical_bytes(self):
        first = weekly_report.build_markdown(ROOT, AS_OF)
        second = weekly_report.build_markdown(ROOT, AS_OF)
        self.assertEqual(first, second)

    def test_a_different_date_changes_the_report(self):
        current = weekly_report.build_markdown(ROOT, AS_OF)
        other = weekly_report.build_markdown(ROOT, "2026-11-19")
        self.assertIn(f"# AMFE — недельный интеграционный отчёт ({AS_OF})", current)
        self.assertIn("# AMFE — недельный интеграционный отчёт (2026-11-19)", other)
        self.assertNotEqual(current, other)

        def without_the_date_header(text: str) -> list:
            return [line for line in text.splitlines()
                    if not line.startswith("# AMFE — недельный интеграционный отчёт")
                    and not line.startswith("Дата отчёта (as_of):")]

        self.assertEqual(without_the_date_header(current), without_the_date_header(other))


class Offline(unittest.TestCase):
    def test_the_tool_does_not_import_network_modules(self):
        source = (ROOT / "tools" / "weekly_report.py").read_text(encoding="utf-8")
        for token in ("urllib", "requests", "http.client", "socket", "urlopen"):
            self.assertNotIn(token, source, token)


class DegradedInputs(unittest.TestCase):
    def test_missing_inputs_produce_a_report_with_risks(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = weekly_report.build_markdown(Path(tmp), AS_OF)
        for heading in ("## 1. Сводка", "## 7. Риски", "## 8. Следующие шаги"):
            self.assertIn(heading, text, heading)
        self.assertIn("нет файла", text)
        self.assertIn("Отчёт первой реальной оценки отсутствует", text)
        self.assertIn("Ворота R2 не пройдены", text)
        self.assertNotIn("<<", text)

    def test_empty_forward_registry_is_normal_first_day_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "registry").mkdir(parents=True)
            (root / "registry" / "forecasts.jsonl").write_text("", encoding="utf-8")
            text = weekly_report.build_markdown(root, AS_OF)
        self.assertIn("записей: **0**", text)
        self.assertIn("Журнал пуст", text)
        self.assertIn("C6 недоступен", text)

    def test_forward_registry_records_are_counted(self):
        records = [
            {"candidate_id": "RW", "target": "T1_CPI_YOY", "horizon": 1, "origin": "2026-11"},
            {"candidate_id": "RW", "target": "T1_CPI_YOY", "horizon": 3, "origin": "2026-12"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "registry").mkdir(parents=True)
            (root / "registry" / "forecasts.jsonl").write_text(
                "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
            text = weekly_report.build_markdown(root, AS_OF)
        self.assertIn("записей: **2**", text)
        self.assertIn("| RW | 2 |", text)
        self.assertIn("origin=2026-12", text)

    def test_broken_registry_jsonl_is_a_risk_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "registry").mkdir(parents=True)
            (root / "registry" / "forecasts.jsonl").write_text("not a json line\n", encoding="utf-8")
            text = weekly_report.build_markdown(root, AS_OF)
        self.assertIn("строка 1", text)
        self.assertIn("записей: **0**", text)


class CommandLine(unittest.TestCase):
    def test_out_writes_the_same_markdown_and_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "nested" / "weekly_report.md"
            code = weekly_report.main(["--date", AS_OF, "--out", str(out), "--root", str(ROOT)])
            self.assertEqual(code, 0)
            self.assertTrue(out.is_file())
            self.assertEqual(out.read_text(encoding="utf-8"),
                             weekly_report.build_markdown(ROOT, AS_OF))

    def test_a_bad_date_is_rejected(self):
        self.assertEqual(weekly_report.main(["--date", "2026-13-40", "--root", str(ROOT)]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
