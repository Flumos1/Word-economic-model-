"""Check a reference list against Crossref: first-author surname, year (+-1), title overlap.

Usage: python -I -X utf8 tools/verify_references.py DOC.md OUT.json [START_MARK END_MARK]

Reads the lines that start with "- " and contain a year in parentheses between
START_MARK and END_MARK (defaults: "## 8. " and "## 9. "). Verifies that a work with
matching metadata EXISTS; it does not check that a claim matches the source text.
Statuses: OK (author, year and title all match), PARTIAL (two of three), WEAK (one or
none, typical for books, reports, standards and preprints), NO_YEAR, ERROR:*.
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter

MAILTO = "flumos@gmail.com"


def words(text):
    return {w for w in re.findall(r"[a-zа-я0-9]{4,}", text.lower())}


def first_item(entry):
    # an entry may list several works separated by ';' - verify the first one
    return re.split(r";\s+(?=[A-Z(])", entry)[0]


def lookup(item, surname, year):
    query = urllib.parse.urlencode({"query.bibliographic": item[:300], "rows": 3, "mailto": MAILTO})
    for _ in range(4):
        try:
            with urllib.request.urlopen("https://api.crossref.org/works?" + query, timeout=60) as response:
                found = json.load(response)["message"]["items"]
            break
        except Exception as exc:
            error = "ERROR:" + type(exc).__name__
            time.sleep(3)
    else:
        return error, None
    best = None
    for work in found:
        title = " ".join(work.get("title", [""]))
        authors = [a.get("family", "") for a in work.get("author", []) + work.get("editor", [])]
        issued = (work.get("issued", {}).get("date-parts") or [[None]])[0][0]
        overlap = len(words(title) & words(item)) / max(1, len(words(title)))
        author_ok = any(surname.lower() == a.lower() for a in authors)
        year_ok = issued is not None and abs(issued - year) <= 1
        score = (overlap >= 0.6) + author_ok + year_ok
        if best is None or score > best[0]:
            best = (score, title, issued, work.get("DOI"), author_ok, year_ok, round(overlap, 2))
    status = "OK" if best and best[0] == 3 else ("PARTIAL" if best and best[0] == 2 else "WEAK")
    return status, best


def main():
    doc, out = sys.argv[1], sys.argv[2]
    start = sys.argv[3] if len(sys.argv) > 3 else "## 8. "
    end = sys.argv[4] if len(sys.argv) > 4 else "## 9. "
    section = open(doc, encoding="utf-8").read().split(start)[1].split(end)[0]
    results = []
    for line in section.splitlines():
        if not (line.startswith("- ") and re.search(r"\((19|20)\d\d", line)):
            continue
        entry = line[2:].strip()
        item = first_item(entry)
        match = re.search(r"\(((?:19|20)\d\d)", item) or re.search(r"\(((?:19|20)\d\d)", entry)
        if not match:
            results.append({"entry": item[:140], "status": "NO_YEAR", "match": None})
            continue
        surname = re.match(r"[★\s]*([A-Za-zÀ-ÿ'\-]+)", item.replace("★", "")).group(1)
        status, best = lookup(item, surname, int(match.group(1)))
        results.append({"entry": item[:140], "status": status, "match": best})
        time.sleep(0.25)
    json.dump(results, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(Counter(r["status"] for r in results), len(results))


if __name__ == "__main__":
    main()
