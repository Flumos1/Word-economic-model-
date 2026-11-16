#!/usr/bin/env python3
"""Offline data template for the AMFE 2.0 finance layer (docs/145, docs/149).

The tool never touches the network.  It contains:

1. **Source specifications** for the three vendors the finance layer will use later - Yahoo Finance
   (equities/indices/ETFs), FRED (rates) and CoinGecko (crypto).  A specification is metadata only:
   fields, frequency, licence, point-in-time caveats, endpoint template.  Nothing is downloaded.
2. **A local CSV adapter** that validates a file against a specification: ISO dates, strictly
   increasing calendar, numeric values, positive prices, no duplicate dates, a maximum gap between
   observations, and a hard **point-in-time guard** - rows dated after ``--as-of`` are rejected
   instead of being silently dropped.
3. **The schema/licence registry** and a **template writer** (``--write-template``) that emits the
   exact CSV headers the adapter expects, so a user can drop in real exports without guessing.
4. **A self-test** (``--self-test``) that round-trips the templates through the adapter in memory
   and checks the PIT guard, determinism and the fail-closed paths.

Rules encoded (docs/149):

  * no network, ever: the vendors are specifications, not clients (``NETWORK_ALLOWED = False``);
  * a dataset is accepted only if it is strictly increasing and free of non-finite values;
  * point-in-time: the adapter refuses data after the decision date instead of trimming it, because
    silent trimming is exactly how look-ahead leaks into a backtest;
  * licences: only derived aggregates leave the project; raw vendor files are not committed;
  * deterministic: the same CSV bytes give the same SHA-256, row count and summary.

CLI::

    python tools/finance_data.py --list
    python tools/finance_data.py --spec yahoo
    python tools/finance_data.py --schema
    python tools/finance_data.py --write-template build/finance_templates
    python tools/finance_data.py --load quotes.csv --source yahoo --value-column close \\
        --as-of 2026-11-15 --json
    python tools/finance_data.py --self-test --json
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

from amfe.markets_finance.contracts import canonical_sha256  # noqa: E402

#: Hard rule of this module: the finance data tool is offline by design.
NETWORK_ALLOWED = False


class DatasetError(ValueError):
    """A local dataset is missing, malformed, not point-in-time or otherwise inadmissible."""


@dataclass(frozen=True)
class SourceSpec:
    """Metadata-only vendor specification (never a client; the tool is offline)."""

    source_id: str
    provider: str
    content: str
    frequency: str
    fields: tuple[str, ...]
    date_column: str
    value_column: str
    dataset_ids: tuple[str, ...]
    endpoint_template: str
    licence: str
    pit_caveats: tuple[str, ...]
    value_kind: str = "price"  # price | rate | index

    def to_dict(self) -> dict:
        return {"source_id": self.source_id, "provider": self.provider, "content": self.content,
                "frequency": self.frequency, "fields": list(self.fields),
                "date_column": self.date_column, "value_column": self.value_column,
                "dataset_ids": list(self.dataset_ids),
                "endpoint_template": self.endpoint_template, "licence": self.licence,
                "pit_caveats": list(self.pit_caveats), "value_kind": self.value_kind,
                "network_allowed": NETWORK_ALLOWED}


SOURCES: dict[str, SourceSpec] = {
    "yahoo": SourceSpec(
        source_id="yahoo", provider="Yahoo Finance (public chart endpoint)",
        content="equity/index/ETF daily quotes", frequency="daily",
        fields=("date", "open", "high", "low", "close", "adj_close", "volume"),
        date_column="date", value_column="close",
        dataset_ids=("SPY", "AAPL", "MSFT", "IWDA.AS"),
        endpoint_template=("https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
                           "?period1={start}&period2={end}&interval=1d"),
        licence=("Yahoo Terms of Service; personal/research use, redistribution restricted; "
                 "raw files are not committed"),
        pit_caveats=("adj_close is retroactively adjusted for splits/dividends: an unadjusted "
                     "price from today is not the price known on that date",
                     "delisted tickers disappear from the vendor list: survivorship bias",
                     "the endpoint may return repaired bars: store the retrieval date"),
        value_kind="price"),
    "fred": SourceSpec(
        source_id="fred", provider="Federal Reserve Economic Data (FRED)",
        content="US rates and macro series", frequency="daily/monthly",
        fields=("date", "value"), date_column="date", value_column="value",
        dataset_ids=("DGS3M", "DGS10", "DFF", "T10Y2Y", "CPIAUCSL"),
        endpoint_template=("https://api.stlouisfed.org/fred/series/observations?series_id={series}"
                           "&api_key={key}&file_type=json&observation_start={start}"),
        licence=("FRED Terms of Use; US government data mostly public domain; attribution "
                 "required; raw files are not committed"),
        pit_caveats=("FRED serves the latest vintage: CPI/GDP are revised after publication, so a "
                     "backtest needs ALFRED real-time vintages (or a stored vintage tape)",
                     "holidays are missing values, not zeros: the adapter rejects gaps > max_gap",
                     "series can be discontinued or rebased: pin the series id and retrieval date"),
        value_kind="rate"),
    "coingecko": SourceSpec(
        source_id="coingecko", provider="CoinGecko public API",
        content="crypto daily prices and volumes", frequency="daily (24/7 market)",
        fields=("timestamp", "price_usd", "market_cap_usd", "volume_usd"),
        date_column="timestamp", value_column="price_usd",
        dataset_ids=("bitcoin", "ethereum", "solana"),
        endpoint_template=("https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
                           "?vs_currency=usd&days={days}"),
        licence=("CoinGecko API Terms; free tier rate limits and attribution; raw files are not "
                 "committed"),
        pit_caveats=("24/7 market: the daily close is a UTC midnight snapshot, not an exchange "
                     "close",
                     "exchange outages and stablecoin depegs create extreme returns; no "
                     "halt/adjustment metadata",
                     "dead coins are dropped from the API: survivorship bias in any backtest"),
        value_kind="price"),
}

#: Ordered CSV schema the adapter enforces for every source.
SCHEMA: dict[str, dict] = {
    "date_column": "date",
    "value_column": "value (per source: close for yahoo, value for FRED, price_usd for CoinGecko)",
    "date_format": "ISO-8601 (YYYY-MM-DD)",
    "ordering": "strictly increasing, no duplicates",
    "values": "finite numeric; positive for price sources",
    "max_gap_days": 10,
    "point_in_time": "no row dated after --as-of (rejected, never trimmed)",
    "extra_columns": "ignored after validation (open/high/low/volume may be present)",
}

LICENCES = {source_id: {"licence": spec.licence, "pit_caveats": list(spec.pit_caveats)}
            for source_id, spec in SOURCES.items()}


@dataclass(frozen=True)
class LocalDataset:
    """A validated local CSV snapshot with provenance and a content digest."""

    source_id: str
    path: str
    value_column: str
    dates: tuple[str, ...]
    values: np.ndarray
    sha256: str
    bytes: int
    as_of: str | None
    rows: int
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"schema": "AMFE-FINANCE-LOCAL-DATASET/1.0.0", "source_id": self.source_id,
                "path": self.path, "value_column": self.value_column,
                "rows": int(self.rows), "first_date": self.dates[0],
                "last_date": self.dates[-1], "sha256": self.sha256, "bytes": int(self.bytes),
                "as_of": self.as_of, "warnings": list(self.warnings),
                "summary": {"min": float(np.min(self.values)),
                            "max": float(np.max(self.values)),
                            "mean": float(np.mean(self.values)),
                            "last": float(self.values[-1])},
                "provenance": {"source_id": self.source_id,
                               "licence": SOURCES[self.source_id].licence,
                               "data_class": "LOCAL_CSV",
                               "network_allowed": NETWORK_ALLOWED}}


def describe(source_id: str) -> dict:
    if source_id not in SOURCES:
        raise DatasetError(f"unknown source {source_id!r}; available: {sorted(SOURCES)}")
    return SOURCES[source_id].to_dict()


def list_sources() -> list[dict]:
    return [describe(source_id) for source_id in SOURCES]


def schema_document() -> dict:
    return {"schema": "AMFE-FINANCE-CSV-SCHEMA/1.0.0", "sources": {
        source_id: {"fields": list(spec.fields), "date_column": spec.date_column,
                    "value_column": spec.value_column, "value_kind": spec.value_kind,
                    "frequency": spec.frequency}
        for source_id, spec in SOURCES.items()}, "rules": SCHEMA,
        "licences": LICENCES, "network_allowed": NETWORK_ALLOWED}


def _parse_iso(value: str, row_number: int, column: str) -> str:
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError as error:
        raise DatasetError(f"row {row_number}: {column} is not an ISO date: {value!r}") from error


def load_local_csv(path: Path | str, source_id: str, value_column: str | None = None,
                   date_column: str | None = None, as_of: str | None = None,
                   max_gap_days: int = 10, min_rows: int = 3,
                   strict: bool = True) -> LocalDataset:
    """Validate a local CSV against a source specification; fail closed on any violation."""
    if source_id not in SOURCES:
        raise DatasetError(f"unknown source {source_id!r}; available: {sorted(SOURCES)}")
    spec = SOURCES[source_id]
    location = Path(path)
    if not location.is_file():
        raise DatasetError(f"dataset is missing: {location}")
    try:
        payload = location.read_bytes()
    except OSError as error:
        raise DatasetError(f"cannot read {location}: {error}") from error
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise DatasetError(f"{location} is not UTF-8: {error}") from error
    try:
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = tuple(reader.fieldnames or ())
        rows = list(reader)
    except csv.Error as error:
        raise DatasetError(f"{location} is not a well-formed CSV: {error}") from error
    if not rows:
        raise DatasetError(f"{location} has no data rows")
    date_column = date_column or spec.date_column
    value_column = value_column or spec.value_column
    for column in (date_column, value_column):
        if column not in fieldnames:
            raise DatasetError(f"{location}: column {column!r} is missing; the source "
                               f"{source_id} requires {list(spec.fields)}")
    if len(rows) < min_rows:
        raise DatasetError(f"{location}: {len(rows)} rows, at least {min_rows} are required")
    if max_gap_days <= 0:
        raise DatasetError(f"max_gap_days must be positive, got {max_gap_days}")
    dates: list[str] = []
    values: list[float] = []
    warnings: list[str] = []
    for number, row in enumerate(rows, start=2):
        raw_date = (row.get(date_column) or "").strip()
        if not raw_date:
            raise DatasetError(f"row {number}: empty {date_column}")
        stamp = _parse_iso(raw_date, number, date_column)
        raw_value = (row.get(value_column) or "").strip()
        if raw_value in ("", ".", "NA", "NaN", "null"):
            raise DatasetError(f"row {number}: missing {value_column} value (gaps are not imputed)")
        try:
            value = float(raw_value.replace(",", ""))
        except ValueError as error:
            raise DatasetError(f"row {number}: {value_column} is not numeric: {raw_value!r}") \
                from error
        if not np.isfinite(value):
            raise DatasetError(f"row {number}: {value_column} is not finite: {raw_value!r}")
        if spec.value_kind == "price" and value <= 0.0:
            raise DatasetError(f"row {number}: price must be positive, got {value}")
        dates.append(stamp)
        values.append(value)
    for previous, current in zip(dates, dates[1:]):
        if current <= previous:
            raise DatasetError(f"{location}: dates must be strictly increasing "
                               f"({previous} -> {current})")
    for previous, current in zip(dates, dates[1:]):
        gap = (date.fromisoformat(current) - date.fromisoformat(previous)).days
        if gap > max_gap_days and strict:
            raise DatasetError(f"{location}: {gap}-day gap before {current} exceeds "
                               f"max_gap_days={max_gap_days}")
    if as_of is not None:
        cutoff = _parse_iso(as_of, 0, "as_of")
        later = [stamp for stamp in dates if stamp > cutoff]
        if later:
            raise DatasetError(f"{location}: {len(later)} row(s) are after as_of={cutoff} "
                               f"(first {later[0]}): rejecting instead of trimming (PIT guard)")
    array = np.asarray(values, dtype=float)
    if not np.all(np.isfinite(array)):
        raise DatasetError(f"{location}: non-finite values after parsing")
    file_sha = hashlib.sha256(payload).hexdigest()
    return LocalDataset(source_id=source_id, path=str(location).replace("\\", "/"),
                        value_column=value_column, dates=tuple(dates), values=array,
                        sha256=file_sha, bytes=len(payload), as_of=as_of, rows=len(dates),
                        warnings=tuple(warnings))


TEMPLATES: dict[str, str] = {
    "yahoo": ("date,open,high,low,close,adj_close,volume\n"
              "2026-11-10,100.00,101.20,99.50,100.80,100.80,1200000\n"
              "2026-11-11,100.80,102.10,100.40,101.70,101.70,1100000\n"
              "2026-11-12,101.70,102.50,101.00,102.30,102.30,900000\n"),
    "fred": ("date,value\n"
             "2026-11-10,4.05\n"
             "2026-11-11,4.03\n"
             "2026-11-12,4.04\n"),
    "coingecko": ("timestamp,price_usd,market_cap_usd,volume_usd\n"
                  "2026-11-10,65000.0,1280000000000,24000000000\n"
                  "2026-11-11,65800.0,1295000000000,25000000000\n"
                  "2026-11-12,65100.0,1282000000000,23000000000\n"),
}


def write_templates(directory: Path | str) -> list[str]:
    """Write the exact CSV headers the adapter expects; returns the written paths."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for source_id, payload in TEMPLATES.items():
        path = target / f"{source_id}_template.csv"
        path.write_text(payload, encoding="utf-8", newline="\n")
        written.append(str(path).replace("\\", "/"))
    return written


def self_test() -> dict:
    """Round-trip the templates through the adapter and exercise the fail-closed paths."""
    checks: dict[str, bool] = {}
    with tempfile.TemporaryDirectory() as tmp:
        paths = write_templates(Path(tmp))
        checks["templates_written"] = len(paths) == len(TEMPLATES)
        loaded = load_local_csv(paths[0], "yahoo", as_of="2026-11-12")
        checks["template_loads"] = (loaded.rows == 3 and loaded.value_column == "close"
                                    and loaded.dates[0] == "2026-11-10")
        checks["pit_guard_rejects_future"] = _raises(
            DatasetError, load_local_csv, paths[0], "yahoo", as_of="2026-11-11")
        checks["deterministic_digest"] = loaded.sha256 == load_local_csv(
            paths[0], "yahoo", as_of="2026-11-12").sha256
        broken = Path(tmp) / "broken.csv"
        broken.write_text("date,close\n2026-11-12,10\n2026-11-11,11\n", encoding="utf-8")
        checks["rejects_unordered"] = _raises(DatasetError, load_local_csv, broken, "yahoo")
        negative = Path(tmp) / "negative.csv"
        negative.write_text("date,close\n2026-11-10,-1\n2026-11-11,2\n", encoding="utf-8")
        checks["rejects_negative_price"] = _raises(DatasetError, load_local_csv, negative, "yahoo")
        gap = Path(tmp) / "gap.csv"
        gap.write_text("date,close\n2026-01-01,10\n2026-03-01,11\n2026-03-02,12\n", encoding="utf-8")
        checks["rejects_large_gap"] = _raises(DatasetError, load_local_csv, gap, "yahoo")
        checks["unknown_source"] = _raises(DatasetError, load_local_csv, paths[0], "nasdaq")
        checks["offline"] = NETWORK_ALLOWED is False
    return {"schema": "AMFE-FINANCE-DATA-SELFTEST/1.0.0",
            "ok": all(checks.values()), "checks": checks,
            "sha256": canonical_sha256(checks)}


def _raises(error_type, function, *args, **kwargs) -> bool:
    try:
        function(*args, **kwargs)
    except error_type:
        return True
    return False


# ------------------------------------------------------------------------------------------ CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline finance data template: vendor specs, local CSV adapter, PIT guard, "
                    "licences (no network)")
    parser.add_argument("--list", action="store_true", help="list the vendor specifications")
    parser.add_argument("--spec", default=None, help="print one vendor specification (yahoo/fred/"
                                                     "coingecko)")
    parser.add_argument("--schema", action="store_true", help="print the CSV schema and licences")
    parser.add_argument("--write-template", default=None, metavar="DIR",
                        help="write the CSV templates into DIR")
    parser.add_argument("--load", default=None, metavar="CSV", help="validate a local CSV")
    parser.add_argument("--source", default="yahoo", help="source id for --load (default yahoo)")
    parser.add_argument("--value-column", default=None, help="override the value column")
    parser.add_argument("--date-column", default=None, help="override the date column")
    parser.add_argument("--as-of", default=None, help="point-in-time cutoff (YYYY-MM-DD)")
    parser.add_argument("--max-gap-days", type=int, default=10, help="maximum gap in days")
    parser.add_argument("--self-test", action="store_true", help="run the offline self-test")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    return parser


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1))


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.list:
            payload = {"sources": [{"source_id": item["source_id"], "content": item["content"],
                                    "frequency": item["frequency"], "licence": item["licence"]}
                                   for item in list_sources()]}
        elif args.spec:
            payload = describe(args.spec)
        elif args.schema:
            payload = schema_document()
        elif args.write_template:
            payload = {"written": write_templates(args.write_template)}
        elif args.load:
            dataset = load_local_csv(args.load, args.source, value_column=args.value_column,
                                     date_column=args.date_column, as_of=args.as_of,
                                     max_gap_days=args.max_gap_days)
            payload = dataset.to_dict()
        elif args.self_test:
            payload = self_test()
        else:
            payload = {"usage": "see --help", "sources": sorted(SOURCES)}
        if args.json:
            _print_json(payload)
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=1))
        if payload.get("ok") is False:
            return 1
        return 0
    except DatasetError as error:
        print(json.dumps({"status": "BLOCKED_DATA", "error": str(error)}, ensure_ascii=False),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
