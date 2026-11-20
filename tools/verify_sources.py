#!/usr/bin/env python3
"""Verify data sources: URL reachability, licence/terms evidence, coverage/fields markers (G6-prep).

The tool is deliberately conservative:

* it **never** turns a source into ``VERIFIED`` on its own — a normal run only *checks* and
  appends evidence to ``protocols/source_verification_log.jsonl``;
* ``--apply`` promotes a source from ``CATALOGUED`` to ``ACCESS_VERIFIED`` **only** if the same
  run produced ``VERIFIED_CANDIDATE`` evidence for it (HTTP 200, licence/terms page found,
  coverage and field markers found) and the source is explicitly allowed to be promoted.
  It never promotes to ``VERIFIED``: that status requires actual data ingestion;
* HTTP 403, HTML stubs ("Access denied", a JavaScript shell), timeouts and missing markers are
  recorded as ``NOT_VERIFIED``/``OFFLINE`` — never as PASS;
* the evidence log is append-only; existing lines are never rewritten;
* the registry is written atomically and re-validated with ``amfe.data.source_registry``.

Verification scope is explicitly **source-page level**: the tool confirms that the public page and
its terms/licence page exist and mention the declared coverage/fields; it does **not** ingest data
and does not turn CATALOGUED into "observed data".  ``docs/197`` documents this limitation.

Usage
-----
    python tools/verify_sources.py --check                  # check + append evidence, no registry write
    python tools/verify_sources.py --check --dry-run        # check, print plan, write nothing
    python tools/verify_sources.py --apply                  # check + evidence + promote eligible sources
    python tools/verify_sources.py --offline --check        # record an honest offline attempt
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.data import source_registry as registry_module  # noqa: E402

SCHEMA = "AMFE-SOURCE-VERIFICATION/1.0.0"
LOG_SCHEMA = "AMFE-SOURCE-VERIFICATION-LOG/1.0.0"
TOOL_VERSION = "1.0.0"
DEFAULT_REGISTRY = ROOT / "protocols" / "source_registry.json"
DEFAULT_LOG = ROOT / "protocols" / "source_verification_log.jsonl"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
VERDICTS = ("VERIFIED_CANDIDATE", "NOT_VERIFIED", "OFFLINE", "BLOCKED")
STUB_PATTERNS = (
    "access denied", "403 forbidden", "request blocked", "are you a robot", "captcha",
    "enable javascript", "javascript is required", "just a moment", "checking your browser",
)


class VerificationError(RuntimeError):
    """The tool refuses to continue (bad config, bad registry, inconsistent evidence)."""


#: Per-source checks.  Markers are lowercase substrings of the public pages (verified on
#: 2026-11-20); the tool reports exactly which markers it found.
SOURCE_CHECKS: dict[str, dict] = {
    "EIA": {
        "terms_url": "https://www.eia.gov/about/copyrights_reuse.php",
        "license_markers": ("public domain", "copyright"),
        "coverage_markers": ("monthly", "annual"),
        "field_markers": ("energy", "price"),
        "min_bytes": 5000, "allow_promotion": False,
    },
    "WORLD_BANK": {
        "terms_url": "https://www.worldbank.org/en/about/legal/terms-and-conditions",
        "license_markers": ("license", "attribution", "copyright"),
        "coverage_markers": ("monthly", "annual"),
        "field_markers": ("commodity", "price", "energy"),
        "min_bytes": 5000, "allow_promotion": False,
    },
    "OECD_ICIO": {
        "terms_url": "https://www.oecd.org/en/about/terms-conditions.html",
        "license_markers": ("terms", "licence", "conditions"),
        "coverage_markers": ("input-output", "annual"),
        "field_markers": ("inter-country", "industry"),
        "min_bytes": 5000, "allow_promotion": False,
    },
    "UN_COMTRADE": {
        "terms_url": "https://comtradeplus.un.org/en/license-agreement",
        "license_markers": ("license", "terms", "copyright"),
        "coverage_markers": ("monthly", "annual", "trade"),
        "field_markers": ("trade", "exports", "imports"),
        "min_bytes": 5000, "allow_promotion": True,
    },
    "EUROSTAT": {
        "terms_url": "https://ec.europa.eu/eurostat/help/copyright-notice",
        "license_markers": ("copyright", "reuse", "licence", "license"),
        "coverage_markers": ("monthly", "annual", "trade"),
        "field_markers": ("trade", "goods", "imports", "exports"),
        "min_bytes": 5000, "allow_promotion": True,
    },
    "USDA_PSD": {
        "terms_url": "https://www.fas.usda.gov/about-fas/legal-information",
        "license_markers": ("public domain", "terms"),
        "coverage_markers": ("monthly", "annual", "production"),
        "field_markers": ("psd", "production", "supply"),
        "min_bytes": 5000, "allow_promotion": True,
    },
    "FAO_FAOSTAT": {
        "terms_url": "https://www.fao.org/contact-us/terms/en/",
        "license_markers": ("cc by-nc-sa", "license", "licence"),
        "coverage_markers": ("annual", "production", "trade"),
        "field_markers": ("crops", "food", "trade"),
        "min_bytes": 5000, "allow_promotion": True,
    },
    "FRED_ALFRED": {
        "terms_url": "https://fred.stlouisfed.org/legal/",
        "license_markers": ("terms", "copyright"),
        "coverage_markers": ("monthly", "annual", "series"),
        "field_markers": ("series", "data"),
        "min_bytes": 5000, "allow_promotion": True,
    },
    "BLS": {
        "terms_url": "https://www.bls.gov/developers/faqs.htm",
        "license_markers": ("public domain", "copyright"),
        "coverage_markers": ("monthly", "annual"),
        "field_markers": ("employment", "prices", "data"),
        "min_bytes": 5000, "allow_promotion": True,
    },
    "OPEC": {
        "terms_url": "https://www.opec.org/opec_web/en/publications/338.htm",
        "license_markers": ("terms", "publications"),
        "coverage_markers": ("monthly", "annual", "oil"),
        "field_markers": ("oil", "supply", "demand"),
        "min_bytes": 5000, "allow_promotion": True,
    },
}


# ----------------------------------------------------------------------------------- fetching
def fetch(url: str, *, timeout: float = 20.0, limit: int = 2_000_000,
          context=None) -> dict:
    """Fetch one URL; returns status/text/hash or an ``error`` (never raises for network errors)."""
    context = context if context is not None else ssl.create_default_context()
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9",
                      "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            data = response.read(limit)
            charset = "utf-8"
            match = re.search(r"charset=([\w-]+)", response.headers.get("content-type", "") or "")
            if match:
                charset = match.group(1)
            try:
                text = data.decode(charset, "replace")
            except LookupError:
                text = data.decode("utf-8", "replace")
            return {"url": url, "status": int(response.status),
                    "final_url": str(response.geturl()),
                    "content_type": response.headers.get("content-type", ""),
                    "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                    "text": text, "error": None,
                    "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1)}
    except urllib.error.HTTPError as error:
        return {"url": url, "status": int(error.code), "final_url": str(error.geturl() or url),
                "content_type": "", "bytes": 0, "sha256": None, "text": "", "error": None,
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1)}
    except Exception as error:  # timeout, DNS, TLS, connection reset, ...
        return {"url": url, "status": None, "final_url": url, "content_type": "", "bytes": 0,
                "sha256": None, "text": "", "error": f"{type(error).__name__}: {error}",
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1)}


def _markers(text: str, markers) -> dict:
    lowered = (text or "").lower()
    return {marker: (marker in lowered) for marker in markers}


def _stub_reason(page: dict, min_bytes: int) -> str | None:
    if page["error"]:
        return f"fetch error: {page['error']}"
    if page["status"] is None:
        return "no HTTP status (offline?)"
    if not 200 <= page["status"] < 300:
        return f"HTTP {page['status']}"
    if page["bytes"] < int(min_bytes):
        return f"page too small for a real data page ({page['bytes']} bytes < {min_bytes})"
    lowered = page["text"].lower()
    for pattern in STUB_PATTERNS:
        if pattern in lowered:
            return f"HTML stub/blocked page marker: {pattern!r}"
    return None


# ------------------------------------------------------------------------------------ checking
def check_source(source: dict, config: dict, *, fetch=fetch, checked_at: str,
                 timeout: float = 20.0, offline: bool = False) -> dict:
    """Check one source and return a deterministic evidence entry (no registry changes)."""
    source_id = source["source_id"]
    page_url = source["url"]
    entry = {
        "schema": SCHEMA, "tool_version": TOOL_VERSION, "source_id": source_id,
        "checked_at": checked_at, "status_before": source.get("status"),
        "page_url": page_url, "terms_url": config.get("terms_url"),
        "offline": bool(offline),
    }
    if offline:
        entry.update({"verdict": "OFFLINE", "page": {"status": None, "error": "offline mode"},
                      "terms": {"status": None, "error": "offline mode"},
                      "checks": {"network_attempted": False},
                      "reasons": ["offline mode: no network request was made"]})
        return entry
    page = fetch(page_url, timeout=timeout)
    entry["page"] = {key: page[key] for key in ("status", "final_url", "content_type", "bytes",
                                                "sha256", "error", "elapsed_ms")}
    stub = _stub_reason(page, int(config.get("min_bytes", 5000)))
    license_found = _markers(page.get("text", ""), config["license_markers"])
    coverage_found = _markers(page.get("text", ""), config["coverage_markers"])
    fields_found = _markers(page.get("text", ""), config["field_markers"])

    terms = {"status": None, "error": "not fetched"}
    terms_license = {}
    if config.get("terms_url"):
        terms = fetch(config["terms_url"], timeout=timeout)
        entry["terms"] = {key: terms[key] for key in ("status", "final_url", "content_type",
                                                      "bytes", "sha256", "error", "elapsed_ms")}
        terms_license = _markers(terms.get("text", ""), config["license_markers"])
    else:
        entry["terms"] = terms

    reasons: list[str] = []
    if stub:
        reasons.append(stub)
    if page["error"]:
        verdict = "OFFLINE"
    elif page["status"] is not None and not 200 <= page["status"] < 300:
        verdict = "NOT_VERIFIED"
        reasons.append(f"page HTTP {page['status']}")
    elif not any(license_found.values()) and not any(terms_license.values()):
        verdict = "NOT_VERIFIED"
        reasons.append("licence/terms markers not found on the page or the terms page")
    elif sum(coverage_found.values()) < min(2, len(coverage_found)):
        verdict = "NOT_VERIFIED"
        reasons.append(f"coverage markers missing: found {sum(coverage_found.values())}/"
                       f"{len(coverage_found)}")
    elif sum(fields_found.values()) < min(2, len(fields_found)):
        verdict = "NOT_VERIFIED"
        reasons.append(f"field markers missing: found {sum(fields_found.values())}/"
                       f"{len(fields_found)}")
    elif stub:
        verdict = "NOT_VERIFIED"
    elif not (200 <= (terms.get("status") or 0) < 300) and not any(license_found.values()):
        verdict = "NOT_VERIFIED"
        reasons.append(f"terms page HTTP {terms.get('status')}")
    else:
        verdict = "VERIFIED_CANDIDATE"
    entry["checks"] = {
        "page_http_ok": bool(page["status"] and 200 <= page["status"] < 300),
        "page_not_stub": stub is None,
        "license_markers_page": license_found,
        "license_markers_terms": terms_license,
        "license_found": bool(any(license_found.values()) or any(terms_license.values())),
        "coverage_markers": coverage_found,
        "coverage_ok": sum(coverage_found.values()) >= min(2, len(coverage_found)),
        "field_markers": fields_found,
        "fields_ok": sum(fields_found.values()) >= min(2, len(fields_found)),
        "terms_http_ok": bool(terms.get("status") and 200 <= terms["status"] < 300),
        "allow_promotion": bool(config.get("allow_promotion")),
    }
    entry["verdict"] = verdict
    entry["reasons"] = reasons
    return entry


def check_sources(document: dict, *, source_ids=None, fetch=fetch, checked_at: str,
                  timeout: float = 20.0, offline: bool = False) -> list[dict]:
    """Check the requested sources (default: every source that has a check configuration)."""
    registry_module.validate_document(document)
    sources = {source["source_id"]: source for source in document["sources"]}
    wanted = [str(value) for value in (source_ids or [])]
    if wanted:
        unknown = sorted(set(wanted) - set(sources))
        if unknown:
            raise VerificationError(f"unknown source_id(s): {unknown}; "
                                    f"known: {sorted(sources)}")
        selected = wanted
    else:
        selected = [source_id for source_id in sources if source_id in SOURCE_CHECKS]
    entries = []
    for source_id in selected:
        config = SOURCE_CHECKS.get(source_id)
        if config is None:
            raise VerificationError(f"no check configuration for source {source_id!r}")
        entries.append(check_source(sources[source_id], config, fetch=fetch,
                                    checked_at=checked_at, timeout=timeout, offline=offline))
    return entries


# ----------------------------------------------------------------------------------- evidence
def append_log(path: Path, entries: list[dict]) -> int:
    """Append evidence lines to the JSONL log (append-only; returns the number of new lines)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    return len(entries)


def load_log(path: Path) -> list[dict]:
    path = Path(path)
    if not path.is_file():
        return []
    entries = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError as error:
            raise VerificationError(f"{path}:{line_number}: invalid JSON: {error}") from error
        if not isinstance(entry, dict):
            raise VerificationError(f"{path}:{line_number}: expected a JSON object")
        entries.append(entry)
    return entries


# ----------------------------------------------------------------------------------- registry
def plan_promotions(document: dict, entries: list[dict]) -> list[dict]:
    """Which CATALOGUED sources can be promoted on this evidence (dry-run plan)."""
    by_id = {source["source_id"]: source for source in document["sources"]}
    plan = []
    for entry in entries:
        source = by_id.get(entry["source_id"])
        if source is None:
            continue
        if source.get("status") != "CATALOGUED":
            continue
        if entry.get("verdict") != "VERIFIED_CANDIDATE":
            continue
        if not entry.get("checks", {}).get("allow_promotion"):
            continue
        plan.append({"source_id": entry["source_id"],
                     "from_status": source.get("status"), "to_status": "ACCESS_VERIFIED",
                     "verdict": entry["verdict"],
                     "page_url": entry.get("page_url"),
                     "page_sha256": (entry.get("page") or {}).get("sha256"),
                     "terms_sha256": (entry.get("terms") or {}).get("sha256"),
                     "evidence_checked_at": entry.get("checked_at")})
    return plan


def _evidence_note(entry: dict) -> str:
    page = entry.get("page") or {}
    terms = entry.get("terms") or {}
    return (f"Access/terms verification {entry.get('checked_at')}: HTTP "
            f"{page.get('status')} for {entry.get('page_url')} "
            f"(page sha256 {str(page.get('sha256'))[:16]}…), "
            f"terms {entry.get('terms_url')} HTTP {terms.get('status')} "
            f"(sha256 {str(terms.get('sha256'))[:16]}…); coverage/field markers confirmed. "
            f"Dataset ingestion NOT performed: ACCESS_VERIFIED (access evidence, NOT VERIFIED, "
            f"not observed data); evidence: protocols/source_verification_log.jsonl.")


def apply_promotions(document: dict, evidence: list[dict]) -> tuple[dict, list[dict]]:
    """Return a new registry document with eligible CATALOGUED->VERIFIED promotions."""
    registry_module.validate_document(document)
    plan = plan_promotions(document, evidence)
    updated = json.loads(json.dumps(document))
    by_id = {source["source_id"]: source for source in updated["sources"]}
    evidence_by_id = {entry["source_id"]: entry for entry in evidence}
    applied = []
    for item in plan:
        source = by_id[item["source_id"]]
        entry = evidence_by_id[item["source_id"]]
        notes = str(source.get("notes") or "").strip()
        source["status"] = "ACCESS_VERIFIED"
        source["retrieved_at"] = None  # access evidence only: no data retrieval
        source["notes"] = (notes + " | " if notes else "") + _evidence_note(entry)
        applied.append(item)
    registry_module.validate_document(updated)
    return updated, applied


def write_registry(document: dict, path: Path, *, indent: int = 2) -> None:
    """Atomically write the registry document (tmp file + replace)."""
    registry_module.validate_document(document)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, ensure_ascii=False, indent=indent) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=str(path.parent),
                                     prefix=".registry-", suffix=".tmp", delete=False) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    temporary.replace(path)


# -------------------------------------------------------------------------------------- CLI
def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify data sources: URL, licence, coverage")
    parser.add_argument("--check", action="store_true", help="check and append evidence (no registry write)")
    parser.add_argument("--apply", action="store_true", help="check + evidence + promote eligible sources")
    parser.add_argument("--dry-run", action="store_true", help="check and print the plan; write nothing")
    parser.add_argument("--offline", action="store_true", help="record an honest offline attempt")
    parser.add_argument("--source", action="append", default=None, help="source_id (repeatable)")
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    parser.add_argument("--log", default=str(DEFAULT_LOG))
    parser.add_argument("--checked-at", default=None, help="fixed ISO timestamp (determinism)")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--json", action="store_true", help="print the evidence entries as JSON")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not (args.check or args.apply or args.dry_run):
        args.check = True
    registry_path = Path(args.registry)
    log_path = Path(args.log)
    try:
        document = registry_module.load(registry_path)
        checked_at = args.checked_at or _now()
        entries = check_sources(document, source_ids=args.source, checked_at=checked_at,
                                timeout=float(args.timeout), offline=bool(args.offline))
        plan = plan_promotions(document, entries)
        written_log = 0
        applied: list[dict] = []
        if not args.dry_run:
            written_log = append_log(log_path, entries)
            if args.apply and plan:
                updated, applied = apply_promotions(document, entries)
                write_registry(updated, registry_path)
        summary = {"status": "OK", "schema": SCHEMA, "checked_at": checked_at,
                   "sources_checked": len(entries), "dry_run": bool(args.dry_run),
                   "verdicts": {entry["source_id"]: entry["verdict"] for entry in entries},
                   "promotion_plan": plan, "promoted": applied,
                   "evidence_lines_written": written_log, "log": str(log_path),
                   "registry": str(registry_path),
                   "counts_before": registry_module.counts(document),
                   "counts_after": registry_module.counts(
                       registry_module.load(registry_path) if applied else document)}
        if args.json:
            print(json.dumps({"summary": summary, "entries": entries}, ensure_ascii=False,
                             sort_keys=True, indent=2))
        else:
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    except (VerificationError, registry_module.SourceRegistryError) as error:
        print(json.dumps({"status": "BLOCKED", "error": str(error)}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
