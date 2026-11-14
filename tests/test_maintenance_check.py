"""Maintenance guard (tools/maintenance_check.py): freshness, alerts, fail-closed, exit codes.

The guard is the support-mode tripwire: it must report OK only when the journal is valid, the shadow
track and the generated artefacts are fresh and the track is not in ALERT.  Missing inputs must never
be reported as PASS (fail-closed), the status document must be deterministic apart from
``generated_at``, and the exit code must be 0 / 2 / 1 for OK / ALERT / ERROR.
"""
import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import maintenance_check  # noqa: E402

DATE = "2026-11-14"
FIXED = "2026-11-14T12:00:00Z"
ANOTHER = "2026-11-14T18:00:00Z"
JOURNAL = ROOT / "registry" / "forecasts.jsonl"


def stamp(days_ago: int) -> str:
    return f"{(date.fromisoformat(DATE) - timedelta(days=days_ago)).isoformat()}T09:00:00Z"


def write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_root(base: Path, *, shadow_age: int = 0, product_age: int = 0, bulletin_age: int = 0,
              track_status: str = "STARTING", alerts=None, journal_text: str | None = None,
              with_shadow: bool = True, with_product: bool = True, with_bulletin: bool = True) -> Path:
    """A temporary repository with a valid journal and the three generated artefacts."""
    root = Path(base) / "repo"
    (root / "registry").mkdir(parents=True, exist_ok=True)
    if journal_text is None:
        shutil.copy2(JOURNAL, root / "registry" / "forecasts.jsonl")
    else:
        (root / "registry" / "forecasts.jsonl").write_text(journal_text, encoding="utf-8")
    if with_shadow:
        write_json(root / "build" / "shadow" / "shadow_status.json", {
            "schema": "AMFE-SHADOW-TRACK/1.0.0",
            "generated_at": stamp(shadow_age),
            "date": stamp(shadow_age)[:10],
            "candidate": {"id": "ENSEMBLE_REGIME_V4"},
            "status": track_status,
            "alerts": alerts or [],
        })
    if with_product:
        write_json(root / "build" / "product" / "forecast_latest.json", {
            "schema_version": "1.0.0",
            "generated_at": stamp(product_age),
            "status": "CANDIDATE",
        })
    if with_bulletin:
        moment = stamp(bulletin_age)
        write_json(root / "build" / "bulletin" / f"{moment[:10]}.json", {
            "schema_version": "1.0.0",
            "date": moment[:10],
            "generated_at": moment,
            "status": "CANDIDATE",
        })
    return root


def run(root: Path, out: Path, generated_at: str = FIXED):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        code = maintenance_check.main(["--root", str(root), "--out-dir", str(out),
                                       "--date", DATE, "--generated-at", generated_at])
    path = Path(out) / "status.json"
    document = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    return code, document


def check_by_id(document: dict, check_id: str) -> dict:
    matches = [item for item in document["checks"] if item["id"] == check_id]
    if not matches:
        raise AssertionError(f"check {check_id} not found: {[c['id'] for c in document['checks']]}")
    return matches[0]


class MaintenanceCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def tmpdir(self, name: str) -> Path:
        path = self.base / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------------------------------- happy path
    def test_fresh_inputs_are_ok(self):
        root = make_root(self.tmpdir("ok"), shadow_age=0, product_age=0, bulletin_age=0)
        out = self.tmpdir("ok-out")
        code, document = run(root, out)
        self.assertEqual(code, 0)
        self.assertIsNotNone(document)
        self.assertEqual(document["status"], "OK")
        self.assertEqual(document["exit_code"], 0)
        self.assertEqual(document["schema"], maintenance_check.SCHEMA)
        self.assertEqual(len(document["checks"]), 5)
        self.assertEqual(document["counts"], {"checks": 5, "passed": 5, "failed": 0})
        self.assertEqual(document["findings"], [])
        for check in document["checks"]:
            self.assertEqual(check["status"], "PASS", check["id"])
            for key in ("id", "severity", "status", "detail", "evidence"):
                self.assertIn(key, check)
        self.assertEqual(document["track"]["status"], "STARTING")
        self.assertIn("journal", document["sources"])

    def test_freshness_boundary_ten_days(self):
        root = make_root(self.tmpdir("boundary-ok"), shadow_age=10)
        code, document = run(root, self.tmpdir("boundary-ok-out"))
        self.assertEqual(code, 0)
        self.assertEqual(check_by_id(document, "shadow.fresh")["evidence"]["age_days"], 10)
        root = make_root(self.tmpdir("boundary-fail"), shadow_age=11)
        code, document = run(root, self.tmpdir("boundary-fail-out"))
        self.assertEqual(code, 2)
        self.assertEqual(check_by_id(document, "shadow.fresh")["status"], "FAIL")

    # ---------------------------------------------------------------------------------- alerts
    def test_stale_shadow_is_an_alert(self):
        root = make_root(self.tmpdir("stale-shadow"), shadow_age=20)
        code, document = run(root, self.tmpdir("stale-shadow-out"))
        self.assertEqual(code, 2)
        self.assertEqual(document["status"], "ALERT")
        self.assertEqual(document["exit_code"], 2)
        check = check_by_id(document, "shadow.fresh")
        self.assertEqual(check["status"], "FAIL")
        self.assertEqual(check["evidence"]["age_days"], 20)
        self.assertTrue(any("shadow.fresh" in finding for finding in document["findings"]))

    def test_stale_build_artefacts_are_alerts(self):
        root = make_root(self.tmpdir("stale-build"), product_age=15, bulletin_age=20)
        code, document = run(root, self.tmpdir("stale-build-out"))
        self.assertEqual(code, 2)
        for check_id in ("product.fresh", "bulletin.fresh"):
            check = check_by_id(document, check_id)
            self.assertEqual(check["status"], "FAIL", check_id)
            self.assertGreater(check["evidence"]["age_days"], 14)
        self.assertEqual(check_by_id(document, "shadow.fresh")["status"], "PASS")

    def test_track_alert_is_exit_two(self):
        alerts = [{"code": "SUSPEND_LOSS", "severity": "ALERT",
                   "message": "cumulative relative loss 1.2 > 1.10 after 6 resolved forecasts"}]
        root = make_root(self.tmpdir("track-alert"), track_status="ALERT", alerts=alerts)
        code, document = run(root, self.tmpdir("track-alert-out"))
        self.assertEqual(code, 2)
        check = check_by_id(document, "track.alert")
        self.assertEqual(check["status"], "FAIL")
        self.assertEqual(check["evidence"]["track_status"], "ALERT")
        self.assertEqual(check["evidence"]["alert_codes"], ["SUSPEND_LOSS"])
        self.assertEqual(document["track"]["candidate"], "ENSEMBLE_REGIME_V4")

    def test_invalid_journal_is_an_alert(self):
        lines = [line for line in JOURNAL.read_text(encoding="utf-8").splitlines() if line.strip()]
        duplicated = "\n".join([lines[0], lines[0]]) + "\n"  # duplicate identity, no correction_of
        root = make_root(self.tmpdir("bad-journal"), journal_text=duplicated)
        code, document = run(root, self.tmpdir("bad-journal-out"))
        self.assertEqual(code, 2)
        check = check_by_id(document, "journal.valid")
        self.assertEqual(check["status"], "FAIL")
        self.assertTrue(check["evidence"]["problems"])

    # ------------------------------------------------------------------------------ fail-closed
    def test_missing_inputs_fail_closed(self):
        empty = self.tmpdir("empty")
        code, document = run(empty, self.tmpdir("empty-out"))
        self.assertEqual(code, 1)
        self.assertEqual(document["status"], "ERROR")
        self.assertEqual(document["exit_code"], 1)
        self.assertEqual(document["checks"], [])
        self.assertTrue(document["findings"])

        root = make_root(self.tmpdir("no-shadow"), with_shadow=False)
        code, document = run(root, self.tmpdir("no-shadow-out"))
        self.assertEqual(code, 1)
        self.assertEqual(document["status"], "ERROR")
        self.assertIn("shadow_status", document["findings"][0])

    def test_missing_bulletin_fails_closed(self):
        root = make_root(self.tmpdir("no-bulletin"), with_bulletin=False)
        code, document = run(root, self.tmpdir("no-bulletin-out"))
        self.assertEqual(code, 1)
        self.assertEqual(document["status"], "ERROR")
        self.assertIn("bulletin", document["findings"][0])

    # ---------------------------------------------------------------------------- invariants
    def test_determinism_except_generated_at(self):
        root = make_root(self.tmpdir("deterministic"))
        first = self.tmpdir("det-one")
        second = self.tmpdir("det-two")
        code, _ = run(root, first, FIXED)
        self.assertEqual(code, 0)
        code, _ = run(root, first, FIXED)
        self.assertEqual(code, 0)
        code, _ = run(root, second, ANOTHER)
        self.assertEqual(code, 0)
        left = (first / "status.json").read_text(encoding="utf-8")
        right = (second / "status.json").read_text(encoding="utf-8")
        self.assertEqual(left.replace(FIXED, "<generated_at>"),
                         right.replace(ANOTHER, "<generated_at>"))
        self.assertEqual(left, (first / "status.json").read_text(encoding="utf-8"))
        one = json.loads(left)
        two = json.loads(right)
        one["generated_at"] = two["generated_at"] = "<generated_at>"
        self.assertEqual(one, two)

    def test_cli_exit_codes(self):
        environment = {**__import__("os").environ, "PYTHONIOENCODING": "utf-8"}

        def invoke(root: Path, out: Path) -> subprocess.CompletedProcess:
            return subprocess.run(
                [sys.executable, str(ROOT / "tools" / "maintenance_check.py"),
                 "--root", str(root), "--out-dir", str(out),
                 "--date", DATE, "--generated-at", FIXED],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", env=environment)

        ok_root = make_root(self.tmpdir("cli-ok"))
        completed = invoke(ok_root, self.tmpdir("cli-ok-out"))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["status"], "OK")

        alert_root = make_root(self.tmpdir("cli-alert"), shadow_age=30)
        completed = invoke(alert_root, self.tmpdir("cli-alert-out"))
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["status"], "ALERT")

        completed = invoke(self.tmpdir("cli-empty"), self.tmpdir("cli-empty-out"))
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(json.loads(completed.stderr)["status"], "ERROR")

    def test_inputs_are_not_modified(self):
        import hashlib

        def digest(path: Path) -> str:
            return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()

        before = digest(JOURNAL)
        root = make_root(self.tmpdir("read-only"))
        run(root, self.tmpdir("read-only-out"))
        self.assertEqual(digest(JOURNAL), before)
        self.assertEqual(root.joinpath("registry", "forecasts.jsonl").read_text(encoding="utf-8"),
                         JOURNAL.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
