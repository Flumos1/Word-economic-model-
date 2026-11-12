#!/usr/bin/env python3
"""Check the terms hashes of the licence register against the documents and the live pages.

Two modes:

  * ``--offline`` (default) - no network. Recomputes the ``terms-passages-v1`` fingerprint of every
    terms source from the excerpts in ``docs/90_Условия_источников_хеши.md`` and
    ``docs/97_Лицензии_ряды_и_terms.md`` and compares it with ``terms_sha256`` in
    ``protocols/licence_register.json``. This is what ``tests/test_check_terms.py`` exercises.
  * ``--online`` - intended for CI. Downloads every ``terms_url`` of the terms sources, strips the
    HTML, normalises the text and checks that all cited excerpts are still present. The sha256 of the
    downloaded text is printed for the audit log.

Limitation: the registered hash is a fingerprint of the cited excerpts, not a byte hash of the HTML,
because text extraction is not byte-stable (markup, cookie banners, ordering). ``--online`` therefore
checks that the cited wording is still on the page; it does not compare a byte hash. A mismatch means
the terms changed: a new dated register entry (or a dated note) and a new audit are required before
the next evaluation. This tool is not legal advice.

Exit code: 0 when every checked source is OK, 1 on a mismatch, missing excerpt/document or fetch error.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTER = ROOT / "protocols" / "licence_register.json"
METHOD = "terms-passages-v1"
USER_AGENT = "AMFE-licence-check/0.1 (research project; terms.hash.check)"
BLOCK = re.compile(r"<!-- terms-passages: ([A-Za-z0-9_:]+) -->[ \t]*\n```text terms-passages\n(.*?)\n```", re.S)
STATUS_OK, STATUS_MISMATCH, STATUS_MISSING, STATUS_ERROR = "OK", "MISMATCH", "MISSING", "ERROR"


def load_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def normalise(text: str) -> str:
    """The terms-passages-v1 rule: NFC, whitespace runs -> one ASCII space, strip."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def fingerprint(passages) -> str:
    """sha256 over the normalised excerpts joined by one newline, in the listed order."""
    return hashlib.sha256("\n".join(normalise(p) for p in passages).encode("utf-8")).hexdigest()


def parse_passage_blocks(path: Path) -> dict:
    """``{source_id: [normalised excerpt, ...]}`` from a document with terms-passages fenced blocks."""
    out = {}
    if not Path(path).exists():
        return out
    for match in BLOCK.finditer(Path(path).read_text(encoding="utf-8")):
        out[match.group(1)] = [line.strip() for line in match.group(2).split("\n") if line.strip()]
    return out


def terms_sources(register: dict) -> list:
    """All register entries that carry a terms fingerprint, in register order."""
    sources = []
    for entry in register.get("entries", []):
        if not entry.get("terms_sha256"):
            continue
        sources.append({
            "id": entry["id"],
            "sha256": entry["terms_sha256"],
            "retrieved_at": entry.get("terms_retrieved_at"),
            "urls": entry.get("terms_urls", []),
            "passages": entry.get("terms_passages"),
            "docs": entry.get("terms_docs"),
        })
    return sources


def load_document_blocks(sources, root: Path = ROOT) -> dict:
    """``{docs_path: {source_id: passages}}`` for every document the sources point to."""
    cache = {}
    for source in sources:
        docs = source.get("docs")
        if not docs:
            continue
        path = Path(root) / docs
        cache.setdefault(docs, parse_passage_blocks(path))
    return cache


def check_offline(register: dict | None = None, document_blocks: dict | None = None,
                  root: Path = ROOT) -> dict:
    """Compare the register hashes with the excerpts in the committed documents. No network."""
    register = register if register is not None else load_json(Path(root) / "protocols" / "licence_register.json")
    sources = terms_sources(register)
    blocks = document_blocks if document_blocks is not None else load_document_blocks(sources, root)
    report = {"mode": "offline", "method": METHOD, "checked": 0, "ok": 0,
              "sources": [], "unexpected": [], "result": STATUS_ERROR}
    for source in sources:
        item = {"id": source["id"], "docs": source.get("docs"), "status": STATUS_ERROR}
        docs = source.get("docs")
        passages = blocks.get(docs, {}).get(source["id"]) if docs else None
        expected = source["sha256"]
        item["expected"] = expected
        if not docs or passages is None:
            item["status"] = STATUS_MISSING
            item["reason"] = "document or terms-passages block not found"
        else:
            computed = fingerprint(passages)
            item["computed"] = computed
            item["passages"] = len(passages)
            if source.get("passages") is not None and len(passages) != source["passages"]:
                item["status"] = STATUS_MISMATCH
                item["reason"] = f"{len(passages)} excerpts in the document, {source['passages']} registered"
            elif computed != expected:
                item["status"] = STATUS_MISMATCH
                item["reason"] = "recomputed hash differs from terms_sha256"
            else:
                item["status"] = STATUS_OK
        report["checked"] += 1
        if item["status"] == STATUS_OK:
            report["ok"] += 1
        else:
            report["unexpected"].append(source["id"])
        report["sources"].append(item)
    report["result"] = STATUS_OK if report["ok"] == report["checked"] and report["checked"] else STATUS_MISMATCH
    return report


def html_to_text(raw: str) -> str:
    """Best-effort text extraction for the online check: drop script/style, strip tags, unescape."""
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text)


def fetch_text(url: str, user_agent: str = USER_AGENT, timeout: int = 60, retries: int = 2) -> str:
    """Download a terms page and return its text (HTML is reduced to text). Network mode only."""
    last = None
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(url, headers={"User-Agent": user_agent})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
            return html_to_text(raw) if "<" in raw else raw
        except Exception as exc:  # network and HTTP errors are reported, never raised to the caller loop
            last = exc
    raise RuntimeError(f"could not fetch {url}: {last!r}")


def check_online(register: dict | None = None, document_blocks: dict | None = None,
                 fetcher=fetch_text, root: Path = ROOT) -> dict:
    """Fetch every terms URL and verify that all cited excerpts are still present."""
    register = register if register is not None else load_json(Path(root) / "protocols" / "licence_register.json")
    sources = terms_sources(register)
    blocks = document_blocks if document_blocks is not None else load_document_blocks(sources, root)
    report = {"mode": "online", "method": METHOD, "checked": 0, "ok": 0,
              "sources": [], "unexpected": [], "result": STATUS_ERROR}
    for source in sources:
        item = {"id": source["id"], "urls": source.get("urls", []), "status": STATUS_ERROR, "pages": []}
        docs = source.get("docs")
        passages = blocks.get(docs, {}).get(source["id"]) if docs else None
        if not passages:
            item["status"] = STATUS_MISSING
            item["reason"] = "document or terms-passages block not found"
        else:
            expected = [normalise(p) for p in passages]
            missing = []
            for url in source.get("urls", []):
                page = {"url": url}
                try:
                    text = fetcher(url)
                except Exception as exc:
                    item["status"] = STATUS_ERROR
                    item["reason"] = str(exc)[:300]
                    page["status"] = STATUS_ERROR
                    page["error"] = str(exc)[:300]
                    item["pages"].append(page)
                    continue
                page["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
                normalised = normalise(text)
                absent = [i for i, excerpt in enumerate(expected) if excerpt not in normalised]
                page["status"] = STATUS_OK if not absent else STATUS_MISMATCH
                page["missing"] = absent
                missing.extend((url, i) for i in absent)
                page["chars"] = len(text)
                item["pages"].append(page)
            if not item["pages"]:
                item["status"] = STATUS_ERROR
                item["reason"] = "no terms_url in the register entry"
            elif any(page.get("status") == STATUS_ERROR for page in item["pages"]):
                item["status"] = STATUS_ERROR
                item["reason"] = item.get("reason") or "a terms page could not be fetched"
            elif missing:
                item["status"] = STATUS_MISMATCH
                item["reason"] = "cited excerpt(s) no longer found on the page"
            else:
                item["status"] = STATUS_OK
            item["missing"] = [{"url": url, "excerpt": i} for url, i in missing]
        report["checked"] += 1
        if item["status"] == STATUS_OK:
            report["ok"] += 1
        else:
            report["unexpected"].append(source["id"])
        report["sources"].append(item)
    report["result"] = STATUS_OK if report["ok"] == report["checked"] and report["checked"] else STATUS_MISMATCH
    return report


def filter_report(report: dict, source_id: str) -> dict:
    """Keep one source in a report (for ``--source``)."""
    report = dict(report)
    report["sources"] = [item for item in report["sources"] if item["id"] == source_id]
    report["checked"] = len(report["sources"])
    report["ok"] = sum(1 for item in report["sources"] if item["status"] == STATUS_OK)
    report["unexpected"] = [item["id"] for item in report["sources"] if item["status"] != STATUS_OK]
    report["result"] = STATUS_OK if report["checked"] and report["ok"] == report["checked"] else STATUS_MISMATCH
    return report


def format_report(report: dict) -> str:
    lines = [f"mode={report['mode']} method={report['method']} "
             f"checked={report['checked']} ok={report['ok']} result={report['result']}"]
    for item in report["sources"]:
        lines.append(f"{item['status']:<8} {item['id']:<24} docs={item.get('docs')}")
        if item["status"] != STATUS_OK:
            lines.append(f"         reason: {item.get('reason', '')}")
            for page in item.get("pages", []):
                if page.get("status") != STATUS_OK:
                    lines.append(f"         {page.get('status')} {page.get('url')} missing={page.get('missing')}")
    if report.get("unexpected"):
        lines.append("unexpected: " + ", ".join(report["unexpected"]))
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check terms hashes and, optionally, the live terms pages")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="check against the committed excerpts (default)")
    mode.add_argument("--online", action="store_true", help="download the terms pages and check the excerpts")
    parser.add_argument("--source", help="only this register id")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    register = load_json(REGISTER)
    report = check_online(register, fetcher=fetch_text) if args.online else check_offline(register)
    if args.source:
        report = filter_report(report, args.source)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else format_report(report))
    return 0 if report["result"] == STATUS_OK else 1


if __name__ == "__main__":
    sys.exit(main())
