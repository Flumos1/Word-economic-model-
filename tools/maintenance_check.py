#!/usr/bin/env python3
"""AMFE product maintenance guard: is the supporting loop alive and healthy?

Support mode (P2, 10-20% effort) means the product contour must keep publishing every week without
anyone re-deriving results: the forward journal stays append-only and valid, the shadow track stays
fresh, and the generated product/bulletin artefacts are rebuilt on schedule.  This tool checks exactly
that and nothing else:

  * ``journal.valid``    - ``registry/forecasts.jsonl`` parses and passes the append-only validation
                           (``tools/registry.py``: required fields, identities, corrections);
  * ``shadow.fresh``     - ``build/shadow/shadow_status.json`` is at most 10 days old;
  * ``product.fresh``    - ``build/product/forecast_latest.json`` is at most 14 days old;
  * ``bulletin.fresh``    - the latest ``build/bulletin/<date>.json`` is at most 14 days old;
  * ``track.alert``      - the shadow track is not in ``ALERT`` (suspension thresholds, docs/119).

It writes ``build/maintenance/status.json`` and returns:

  * ``0`` - OK (everything is fresh and the track has no alert);
  * ``2`` - ALERT: a check failed (stale artefact, invalid journal, track ALERT) - a human must act;
  * ``1`` - ERROR / fail-closed: a required input is missing or unreadable, so health cannot be
    claimed.  No check is reported as PASS on missing inputs.

The tool is read-only except for its own status file: it never writes the journal, the registry, the
protocols, ``src/`` or the product artefacts, never resolves forecasts and never contains raw
third-party observations.  Freshness is derived from the artefacts' own ``generated_at``/``date``
fields (not from file mtimes), so the same inputs and the same ``--date`` produce byte-identical
documents apart from ``generated_at``.

Usage:
    python tools/maintenance_check.py
    python tools/maintenance_check.py --date 2026-11-14 --generated-at 2026-11-14T12:00:00Z
    python tools/maintenance_check.py --root /path/to/repo --out-dir build/maintenance
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import registry  # noqa: E402  (append-only journal validation, read-only)

SCHEMA = "AMFE-MAINTENANCE/1.0.0"
JOURNAL_FILE = "registry/forecasts.jsonl"
SHADOW_STATUS_FILE = "build/shadow/shadow_status.json"
PRODUCT_FEED_FILE = "build/product/forecast_latest.json"
BULLETIN_DIR = "build/bulletin"
OUT_DIR = "build/maintenance"

SHADOW_MAX_AGE_DAYS = 10
BUILD_MAX_AGE_DAYS = 14
TRACK_STATUSES = ("STARTING", "ON_TRACK", "WARN", "ALERT", "GATE_PASS")

EXIT_OK, EXIT_ERROR, EXIT_ALERT = 0, 1, 2


class MaintenanceError(RuntimeError):
    """A required input is missing or unreadable: health cannot be claimed (fail-closed)."""


# --------------------------------------------------------------------------------------- generic
def read_json(path: Path, label: str) -> dict:
    path = Path(path)
    if not path.is_file():
        raise MaintenanceError(f"{label}: нет файла {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MaintenanceError(f"{label}: не удалось прочитать {path}: {exc!r}") from exc
    if not isinstance(document, dict):
        raise MaintenanceError(f"{label}: ожидался JSON-объект")
    return document


def _reference_date(value: str | None) -> str:
    value = value or _utc_now()[:10]
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise MaintenanceError(f"--date: ожидался YYYY-MM-DD, получено {value!r}") from exc
    return value


def _validate_timestamp(value: str | None) -> str:
    value = value or _utc_now()
    try:
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise MaintenanceError(f"--generated-at: ожидался ISO 8601, получено {value!r}") from exc
    return str(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _moment(document: dict, label: str) -> date:
    """The artefact's own timestamp (``generated_at`` first, then ``date``); fail-closed if unusable."""
    for key in ("generated_at", "date"):
        value = document.get(key)
        if isinstance(value, str) and len(value) >= 10:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
            except ValueError:
                continue
    raise MaintenanceError(f"{label}: нет корректного поля generated_at/date")


def _age_days(reference: str, moment: date) -> int:
    return max(0, (date.fromisoformat(reference) - moment).days)


def _check(check_id: str, severity: str, passed: bool, detail: str, evidence: dict) -> dict:
    return {"id": check_id, "severity": severity, "status": "PASS" if passed else "FAIL",
            "detail": detail, "evidence": evidence}


# ---------------------------------------------------------------------------------------- inputs
def _latest_bulletin(root: Path) -> tuple[Path, dict]:
    directory = Path(root) / BULLETIN_DIR
    if not directory.is_dir():
        raise MaintenanceError(f"bulletin: нет каталога {BULLETIN_DIR}/")
    candidates: list[tuple[str, Path, dict]] = []
    for path in sorted(directory.glob("*.json")):
        document = read_json(path, f"bulletin {path.name}")
        moment = _moment(document, f"bulletin {path.name}")
        candidates.append((moment.isoformat(), path, document))
    if not candidates:
        raise MaintenanceError(f"bulletin: в {BULLETIN_DIR}/ нет ни одного выпуска JSON")
    _, path, document = max(candidates, key=lambda item: (item[0], item[1].name))
    return path, document


def collect(root: Path, reference: str) -> dict:
    """Load and validate every maintenance input; raise MaintenanceError when health is unknowable."""
    root = Path(root)
    journal_path = root / JOURNAL_FILE
    if not journal_path.is_file():
        raise MaintenanceError(f"journal: нет файла {JOURNAL_FILE}")
    try:
        rows, problems = registry.validate_journal(journal_path)
    except registry.RegistryError as exc:
        raise MaintenanceError(f"journal: {exc}") from exc

    shadow_path = root / SHADOW_STATUS_FILE
    shadow = read_json(shadow_path, "shadow_status")
    if not isinstance(shadow.get("status"), str) or not shadow["status"].strip():
        raise MaintenanceError("shadow_status: нет поля status")
    if shadow["status"] not in TRACK_STATUSES:
        raise MaintenanceError(f"shadow_status: неизвестный статус трека {shadow['status']!r}")
    alerts = shadow.get("alerts")
    if alerts is None:
        alerts = []
    if not isinstance(alerts, list):
        raise MaintenanceError("shadow_status: alerts должен быть списком")
    for alert in alerts:
        if not isinstance(alert, dict) or not alert.get("severity"):
            raise MaintenanceError("shadow_status: элемент alerts без severity")
    shadow_moment = _moment(shadow, "shadow_status")

    product_path = root / PRODUCT_FEED_FILE
    product = read_json(product_path, "product feed")
    product_moment = _moment(product, "product feed")

    bulletin_path, bulletin = _latest_bulletin(root)
    bulletin_moment = _moment(bulletin, f"bulletin {bulletin_path.name}")

    return {"root": root, "journal_path": journal_path, "rows": rows, "problems": problems,
            "shadow_path": shadow_path, "shadow": shadow, "shadow_moment": shadow_moment,
            "product_path": product_path, "product": product, "product_moment": product_moment,
            "bulletin_path": bulletin_path, "bulletin": bulletin,
            "bulletin_moment": bulletin_moment,
            "reference": reference, "alerts": alerts}


# ---------------------------------------------------------------------------------------- checks
def build_document(root: Path = ROOT, reference: str | None = None,
                   generated_at: str | None = None) -> dict:
    reference = _reference_date(reference)
    generated_at = _validate_timestamp(generated_at)
    collected = collect(Path(root), reference)
    checks: list[dict] = []

    problems = collected["problems"]
    checks.append(_check(
        "journal.valid", "blocker", not problems,
        (f"журнал валиден: строк {len(collected['rows'])}" if not problems else
         f"журнал НЕ валиден: проблем {len(problems)}"),
        {"file": JOURNAL_FILE, "rows": len(collected["rows"]), "problems": problems[:10]}))

    shadow_age = _age_days(reference, collected["shadow_moment"])
    checks.append(_check(
        "shadow.fresh", "blocker", shadow_age <= SHADOW_MAX_AGE_DAYS,
        f"shadow_status.json: {shadow_age} дн. (порог ≤ {SHADOW_MAX_AGE_DAYS})",
        {"file": SHADOW_STATUS_FILE, "generated_at": str(collected["shadow"].get("generated_at")),
         "age_days": shadow_age, "max_age_days": SHADOW_MAX_AGE_DAYS}))

    product_age = _age_days(reference, collected["product_moment"])
    checks.append(_check(
        "product.fresh", "blocker", product_age <= BUILD_MAX_AGE_DAYS,
        f"forecast_latest.json: {product_age} дн. (порог ≤ {BUILD_MAX_AGE_DAYS})",
        {"file": PRODUCT_FEED_FILE, "generated_at": str(collected["product"].get("generated_at")),
         "age_days": product_age, "max_age_days": BUILD_MAX_AGE_DAYS}))

    bulletin_age = _age_days(reference, collected["bulletin_moment"])
    checks.append(_check(
        "bulletin.fresh", "blocker", bulletin_age <= BUILD_MAX_AGE_DAYS,
        f"последний бюллетень {collected['bulletin_path'].name}: {bulletin_age} дн. "
        f"(порог ≤ {BUILD_MAX_AGE_DAYS})",
        {"file": str(collected["bulletin_path"].relative_to(collected["root"])).replace("\\", "/"),
         "generated_at": str(collected["bulletin"].get("generated_at")),
         "age_days": bulletin_age, "max_age_days": BUILD_MAX_AGE_DAYS}))

    track_alert = collected["shadow"]["status"] == "ALERT" or any(
        str(alert.get("severity")).upper() == "ALERT" for alert in collected["alerts"])
    codes = [str(alert.get("code")) for alert in collected["alerts"]
             if str(alert.get("severity")).upper() == "ALERT"]
    checks.append(_check(
        "track.alert", "blocker", not track_alert,
        (f"трек в ALERT: {', '.join(codes) or 'см. alerts'}" if track_alert else
         f"трека ALERT нет (статус {collected['shadow']['status']})"),
        {"track_status": collected["shadow"]["status"],
         "alert_codes": codes,
         "alerts": [{"code": alert.get("code"), "severity": alert.get("severity")}
                    for alert in collected["alerts"]]}))

    findings = [f"{item['id']}: {item['detail']}" for item in checks if item["status"] == "FAIL"]
    status = "ALERT" if findings else "OK"
    document = {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "date": reference,
        "reference_date": reference,
        "status": status,
        "exit_code": EXIT_ALERT if findings else EXIT_OK,
        "checks": checks,
        "counts": {"checks": len(checks),
                   "passed": sum(1 for item in checks if item["status"] == "PASS"),
                   "failed": len(findings)},
        "findings": findings,
        "track": {"status": collected["shadow"]["status"],
                  "candidate": ((collected["shadow"].get("candidate") or {}).get("id")),
                  "alerts": [{"code": alert.get("code"), "severity": alert.get("severity"),
                              "message": alert.get("message")}
                             for alert in collected["alerts"]]},
        "sources": {"journal": JOURNAL_FILE,
                    "shadow_status": SHADOW_STATUS_FILE,
                    "product_feed": PRODUCT_FEED_FILE,
                    "bulletin": str(collected["bulletin_path"].relative_to(
                        collected["root"])).replace("\\", "/")},
        "rules": {"shadow_max_age_days": SHADOW_MAX_AGE_DAYS,
                  "build_max_age_days": BUILD_MAX_AGE_DAYS,
                  "alert_exit_code": EXIT_ALERT,
                  "error_exit_code": EXIT_ERROR,
                  "reference": "docs/119 (shadow track), docs/124 (support checklist)"},
    }
    return document


def error_document(error: str, reference: str, generated_at: str) -> dict:
    return {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "date": reference,
        "reference_date": reference,
        "status": "ERROR",
        "exit_code": EXIT_ERROR,
        "checks": [],
        "counts": {"checks": 0, "passed": 0, "failed": 0},
        "findings": [error],
        "track": None,
        "sources": {},
        "rules": {"shadow_max_age_days": SHADOW_MAX_AGE_DAYS,
                  "build_max_age_days": BUILD_MAX_AGE_DAYS,
                  "alert_exit_code": EXIT_ALERT,
                  "error_exit_code": EXIT_ERROR,
                  "reference": "docs/119 (shadow track), docs/124 (support checklist)"},
    }


def write_status(document: dict, out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "status.json"
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8", newline="\n")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="AMFE maintenance guard: journal, shadow track, product/bulletin freshness")
    parser.add_argument("--root", default=str(ROOT), help="корень репозитория")
    parser.add_argument("--out-dir", default=None, help=f"каталог вывода (по умолчанию {OUT_DIR})")
    parser.add_argument("--date", default=None, help="дата проверки YYYY-MM-DD (по умолчанию сегодня UTC)")
    parser.add_argument("--generated-at", default=None,
                        help="ISO 8601 дата-время снимка; по умолчанию текущее UTC (для детерминизма)")
    args = parser.parse_args(argv)
    root = Path(args.root)
    out_dir = Path(args.out_dir) if args.out_dir else root / OUT_DIR
    try:
        reference = _reference_date(args.date)
        generated_at = _validate_timestamp(args.generated_at)
    except MaintenanceError as error:
        print(json.dumps({"ok": False, "status": "ERROR", "error": str(error)},
                         ensure_ascii=False), file=sys.stderr)
        return EXIT_ERROR
    try:
        document = build_document(root, reference=reference, generated_at=generated_at)
    except MaintenanceError as error:
        document = error_document(str(error), reference, generated_at)
        write_status(document, out_dir)
        print(json.dumps({"ok": False, "status": document["status"], "exit_code": EXIT_ERROR,
                          "error": str(error), "out": str(out_dir / "status.json").replace("\\", "/")},
                         ensure_ascii=False), file=sys.stderr)
        return EXIT_ERROR
    path = write_status(document, out_dir)
    print(json.dumps({
        "ok": document["status"] == "OK",
        "status": document["status"],
        "exit_code": document["exit_code"],
        "date": document["date"],
        "checks": document["counts"],
        "findings": document["findings"],
        "out": str(path).replace("\\", "/"),
    }, ensure_ascii=False))
    return document["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
