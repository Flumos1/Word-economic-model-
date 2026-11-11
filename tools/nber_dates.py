#!/usr/bin/env python3
"""NBER business-cycle announcement dates: peak/trough reference dates + announcement dates.

Source: NBER Business Cycle Dating Committee announcements page
(https://www.nber.org/research/business-cycle-dating/business-cycle-dating-committee-announcements).
The NBER data page states: "Permission to copy is granted, provided attribution of source is given."
The tool therefore stores metadata only (reference dates and announcement dates), with the source URL
and the attribution in protocols/nber_dates.json; no NBER page text is copied beyond the short
announcement titles needed to justify each date.

What is extracted, per announcement listed on the page:
  * announcement_date - the date of the NBER committee announcement (the "determination" date);
  * type - "peak", "trough", or "memo" (committees memos carry no cycle determination);
  * cycle_date - the month (YYYY-MM) of the peak/trough that the announcement determines;
  * peak / trough - the reference-date fields, filled only for the type they belong to;
  * title and detail source_url - the announcement page on nber.org (attribution and verification).

Also written: reference_dates - peak/trough pairs with the announcement date of each determination,
which is the series a model needs to avoid using a recession date before it was announced.

The JSON carries two hashes: source_page_sha256 (the downloaded page) and announcements_sha256
(canonical JSON of the extracted announcements), so a change in either is detectable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_URL = ("https://www.nber.org/research/business-cycle-dating/"
              "business-cycle-dating-committee-announcements")
DETAIL_BASE = "https://www.nber.org"
USER_AGENT = "AMFE-research/0.1 (+https://github.com/Flumos1/Word-economic-model-)"
PERMISSION = ("NBER data page: 'Permission to copy is granted, provided attribution of source is "
              "given.' (https://www.nber.org/research/data/us-business-cycle-expansions-and-contractions)")
ATTRIBUTION = ("Source: National Bureau of Economic Research (NBER), Business Cycle Dating Committee "
               "announcements, https://www.nber.org")
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]
MONTH_RE = "|".join(MONTHS)

ANNOUNCEMENT_RE = re.compile(
    r'<p>\s*<a[^>]*href="(/news/business-cycle-dating-committee-announcement-[^"]+)"[^>]*>'
    r'([A-Z][a-z]+ \d{1,2}, \d{4})</a>(.*?)</p>',
    re.S)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        if resp.status != 200:
            raise RuntimeError(f"{url}: HTTP {resp.status}")
        return resp.read()


def parse_announcements(html: str) -> list:
    """Announcement rows from the NBER committee announcements page."""
    out = []
    for m in ANNOUNCEMENT_RE.finditer(html):
        href, date_text, rest = m.group(1), m.group(2), m.group(3)
        title = re.sub(r"<[^>]+>", " ", rest)
        title = re.sub(r"&nbsp;|\s+", " ", title).strip()
        try:
            ann = datetime.strptime(date_text, "%B %d, %Y").date().isoformat()
        except ValueError:
            continue
        low = title.lower()
        kind = "peak" if "peak" in low else "trough" if "trough" in low else "memo"
        cycle = None
        cm = re.search(rf"({MONTH_RE})\s+(\d{{4}})", title)
        if cm and kind in ("peak", "trough"):
            cycle = f"{cm.group(2)}-{MONTHS.index(cm.group(1)) + 1:02d}"
        out.append({
            "announcement_date": ann,
            "type": kind,
            "cycle_date": cycle,
            "peak": cycle if kind == "peak" else None,
            "trough": cycle if kind == "trough" else None,
            "title": title,
            "source_url": DETAIL_BASE + href,
        })
    # the page lists announcements newest first
    out.sort(key=lambda a: a["announcement_date"], reverse=True)
    return out


def reference_dates(announcements: list) -> list:
    """Peak/trough pairs with the announcement date of each determination."""
    peaks = [a for a in announcements if a["type"] == "peak" and a["cycle_date"]]
    troughs = [a for a in announcements if a["type"] == "trough" and a["cycle_date"]]
    pairs = []
    for peak in peaks:
        later = [t for t in troughs
                 if t["cycle_date"] > peak["cycle_date"]
                 and t["announcement_date"] >= peak["announcement_date"]]
        trough = min(later, key=lambda t: t["cycle_date"]) if later else None
        pairs.append({
            "peak": peak["cycle_date"],
            "peak_announcement": peak["announcement_date"],
            "trough": trough["cycle_date"] if trough else None,
            "trough_announcement": trough["announcement_date"] if trough else None,
        })
    pairs.sort(key=lambda p: p["peak"])
    return pairs


def canonical_sha(announcements: list) -> str:
    blob = json.dumps(announcements, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Extract NBER business-cycle announcement dates")
    ap.add_argument("--out", default=str(ROOT / "protocols" / "nber_dates.json"))
    ap.add_argument("--html", default=None, help="use a saved page instead of downloading it")
    args = ap.parse_args(argv)

    raw_html = Path(args.html).read_bytes() if args.html else fetch(SOURCE_URL)
    html = raw_html.decode("utf-8", "replace")
    announcements = parse_announcements(html)
    if not announcements:
        print(json.dumps({"error": "no announcements parsed", "source_url": SOURCE_URL}))
        return 2
    coverage = {"first_announcement": announcements[-1]["announcement_date"],
                "last_announcement": announcements[0]["announcement_date"],
                "announcements": len(announcements),
                "peaks": sum(1 for a in announcements if a["type"] == "peak"),
                "troughs": sum(1 for a in announcements if a["type"] == "trough")}
    doc = {
        "dataset": "NBER Business Cycle Dating Committee announcements",
        "tool": "tools/nber_dates.py",
        "source_url": SOURCE_URL,
        "retrieved_at": utc_now(),
        "permission": PERMISSION,
        "attribution": ATTRIBUTION,
        "announcements": announcements,
        "reference_dates": reference_dates(announcements),
        "coverage": coverage,
        "source_page_sha256": hashlib.sha256(raw_html).hexdigest(),
        "announcements_sha256": canonical_sha(announcements),
        "limitations": [
            "only the announcements listed on the NBER announcements page are extracted; older "
            "committee announcements from before 1990 are not on that page",
            "the page lists announcements newest first; the extraction relies on the page markup "
            "(<p><a href='/news/business-cycle-dating-committee-announcement-...'>date</a>text</p>)",
            "memos carry no peak/trough determination (type='memo', cycle_date=null)",
        ],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(out), "announcements": coverage["announcements"],
                      "source_page_sha256": doc["source_page_sha256"][:16],
                      "announcements_sha256": doc["announcements_sha256"][:16]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
