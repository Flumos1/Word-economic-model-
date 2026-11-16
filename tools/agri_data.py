#!/usr/bin/env python3
"""Offline source inventory and data schema for the agri archetype (docs/148).

This tool is deliberately **offline**: it does not download anything, does not write raw data into
the repository and does not contact USDA/FAO/World Bank/COMTRADE.  It documents which public
sources the soybean pilot would need, under which licence, with which fields and update cadence,
and validates a manual export against the schema (fail-closed).  Raw third-party data stay outside
the repository; only this template and derived results are committed.

Usage::

    python tools/agri_data.py --describe
    python tools/agri_data.py --template build/agri_schema.json
    python tools/agri_data.py --check data/private/agri_records.json
    python tools/agri_data.py --help
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DOCS = "docs/148_Агро_соя.md"

# Public sources needed by the soybean pilot.  ``access`` describes the intended manual/offline
# acquisition; this tool never performs network calls.
SOURCES = (
    {
        "source_id": "USDA_PSD",
        "name": "USDA Production, Supply and Distribution (PSD)",
        "provider": "United States Department of Agriculture, Foreign Agricultural Service",
        "url": "https://apps.fas.usda.gov/psdonline/",
        "license": "public domain (U.S. Government work)",
        "license_note": ("U.S. federal government data are not subject to copyright in the United "
                         "States; attribution is good practice. Verify the current FAS terms "
                         "before redistribution."),
        "content": "production, area, yield, beginning/ending stocks, crush, exports, imports by "
                   "country/commodity/marketing year",
        "commodities": ["soybeans", "soybean meal", "soybean oil"],
        "frequency": "monthly WASDE updates; annual marketing-year tables",
        "access": "manual CSV/Excel export or bulk file; offline cache outside the repository",
        "required_fields": ["commodity", "region", "marketing_year", "production_mt", "area_mha",
                            "yield_t_ha", "stocks_mt", "crush_mt", "exports_mt", "imports_mt"],
    },
    {
        "source_id": "USDA_FAS_EXPORT_SALES",
        "name": "USDA FAS Export Sales / GAIN reports",
        "provider": "United States Department of Agriculture, Foreign Agricultural Service",
        "url": "https://apps.fas.usda.gov/export-sales/",
        "license": "public domain (U.S. Government work)",
        "license_note": "U.S. federal government work; attribution recommended.",
        "content": "weekly export sales/commitments, country destinations, policy and crop reports",
        "commodities": ["soybeans", "soybean meal"],
        "frequency": "weekly",
        "access": "manual export; offline cache outside the repository",
        "required_fields": ["commodity", "destination", "week", "volume_mt", "source_id"],
    },
    {
        "source_id": "FAO_FAOSTAT",
        "name": "FAO FAOSTAT food and agriculture statistics",
        "provider": "Food and Agriculture Organization of the United Nations",
        "url": "https://www.fao.org/faostat/",
        "license": "CC BY-NC-SA 3.0 IGO (non-commercial; share-alike)",
        "license_note": ("Non-commercial use only: commercial redistribution needs a separate FAO "
                         "agreement. Attribution and share-alike are required; this is stricter "
                         "than the other sources and must be tracked per row."),
        "content": "production, area harvested, yield, trade, food balance sheets",
        "commodities": ["soybeans", "soybean meal", "soybean oil"],
        "frequency": "annual",
        "access": "manual bulk download; offline cache outside the repository",
        "required_fields": ["commodity", "region", "year", "production_mt", "area_mha",
                            "yield_t_ha", "source_id", "license"],
    },
    {
        "source_id": "WORLD_BANK",
        "name": "World Bank Commodity Markets (Pink Sheet) / WDI",
        "provider": "World Bank",
        "url": "https://www.worldbank.org/en/research/commodity-markets",
        "license": "CC BY 4.0",
        "license_note": "Attribution required; commercial reuse permitted under CC BY 4.0.",
        "content": "monthly commodity prices (soybeans, soybean meal, soybean oil), freight-related "
                   "indicators, macro series for African importers",
        "commodities": ["soybeans", "soybean meal", "soybean oil"],
        "frequency": "monthly (prices), annual (WDI)",
        "access": "manual Excel/CSV download; offline cache outside the repository",
        "required_fields": ["commodity", "month", "price_usd_t", "source_id"],
    },
    {
        "source_id": "UN_COMTRADE",
        "name": "UN Comtrade bilateral trade statistics",
        "provider": "United Nations Statistics Division",
        "url": "https://comtradeplus.un.org/",
        "license": "UN Comtrade terms of use (free for non-commercial use, attribution; "
                   "premium API/limits apply)",
        "license_note": ("Free tier is rate-limited and for non-commercial use; bulk/commercial "
                         "use requires the UN Comtrade subscription. Track the terms per extract."),
        "content": "bilateral exports/imports by HS code (soybean HS 1201, meal HS 2304, oil HS "
                   "1507), trade values and quantities",
        "commodities": ["soybeans", "soybean meal", "soybean oil"],
        "frequency": "monthly/annual",
        "access": "manual query/export; offline cache outside the repository",
        "required_fields": ["reporter", "partner", "hs_code", "year", "month", "value_usd",
                            "quantity_mt", "source_id", "license"],
    },
)

SCHEMA = {
    "schema_id": "AMFE-AGRI-DATA-TEMPLATE",
    "version": "0.1.0",
    "format": "JSON list of flat records (one record per source/commodity/region/period)",
    "required_fields": ["source_id", "commodity", "region", "period", "period_type", "license",
                        "vintage", "source_class"],
    "optional_fields": ["production_mt", "area_mha", "yield_t_ha", "stocks_mt", "crush_mt",
                        "exports_mt", "imports_mt", "price_usd_t", "freight_usd_t",
                        "trade_value_usd", "notes"],
    "period_type_values": ["month", "marketing_year", "calendar_year", "week"],
    "commodity_values": ["soybeans", "soybean_meal", "soybean_oil"],
    "units": {"production_mt": "million tonnes", "area_mha": "million hectares",
              "yield_t_ha": "tonnes per hectare", "price_usd_t": "USD per tonne",
              "freight_usd_t": "USD per tonne", "trade_value_usd": "USD"},
    "rules": [
        "raw data are never committed: store them outside the repository (data/private, F:)",
        "every record carries its licence and source_id; incompatible licences must not be mixed",
        "source_class: observed (official data), estimated (derived), assumption (this prototype)",
        "vintage is the publication date or download date, not the period",
        "non-finite or missing required fields fail validation; no silent filling",
    ],
}

DISCLAIMER = (
    "Template only. This tool does not download data and does not contain any third-party values. "
    "The soybean pilot parameters in src/amfe/markets_agri are assumptions, not observations; the "
    "output is a research sensitivity range, not a forecast.")


class AgriDataError(ValueError):
    """Invalid source inventory or data record: fail closed."""


def describe_sources() -> dict:
    """Serialisable source inventory + schema + disclaimer (no network, no raw data)."""
    return {"tool": "tools/agri_data.py", "offline": True, "docs": DOCS,
            "sources": [dict(source) for source in SOURCES],
            "source_ids": [source["source_id"] for source in SOURCES],
            "schema": dict(SCHEMA), "disclaimer": DISCLAIMER}


def source_by_id(source_id: str) -> dict:
    for source in SOURCES:
        if source["source_id"] == source_id:
            return dict(source)
    raise AgriDataError(f"unknown source_id {source_id!r}")


def _required_license(sources_by_id: dict, source_id: str) -> str:
    return sources_by_id[source_id]["license"]


def validate_record(record: dict) -> dict:
    """Validate one flat data record against the template; returns a normalised copy."""
    if not isinstance(record, dict):
        raise AgriDataError("record must be a mapping")
    for field_name in SCHEMA["required_fields"]:
        if field_name not in record or record[field_name] in (None, ""):
            raise AgriDataError(f"record is missing required field {field_name!r}")
    sources_by_id = {source["source_id"]: source for source in SOURCES}
    source_id = str(record["source_id"])
    if source_id not in sources_by_id:
        raise AgriDataError(f"unknown source_id {source_id!r}")
    commodity = str(record["commodity"])
    if commodity not in SCHEMA["commodity_values"]:
        raise AgriDataError(f"unknown commodity {commodity!r}")
    period_type = str(record["period_type"])
    if period_type not in SCHEMA["period_type_values"]:
        raise AgriDataError(f"unknown period_type {period_type!r}")
    license_text = str(record["license"])
    if not license_text.strip():
        raise AgriDataError("license must be a non-empty string")
    expected = _required_license(sources_by_id, source_id)
    if license_text != expected:
        raise AgriDataError(
            f"license {license_text!r} does not match the registered source licence {expected!r} "
            f"for {source_id}; mixing licences is not allowed")
    for numeric_field in ("production_mt", "area_mha", "yield_t_ha", "stocks_mt", "crush_mt",
                          "exports_mt", "imports_mt", "price_usd_t", "freight_usd_t",
                          "trade_value_usd"):
        if numeric_field not in record or record[numeric_field] is None:
            continue
        value = float(record[numeric_field])
        if not math.isfinite(value) or value < 0.0:
            raise AgriDataError(f"{numeric_field} must be finite and non-negative")
    normalised = dict(record)
    normalised["source_class"] = str(record.get("source_class", "observed"))
    if normalised["source_class"] not in ("observed", "estimated", "assumption"):
        raise AgriDataError(f"unknown source_class {normalised['source_class']!r}")
    return normalised


def validate_records(records) -> dict:
    """Validate a list of records (the offline export); fail-closed on the first bad record."""
    if not isinstance(records, list) or not records:
        raise AgriDataError("records must be a non-empty list")
    checked = [validate_record(record) for record in records]
    return {"n_records": len(checked), "sources": sorted({row["source_id"] for row in checked}),
            "commodities": sorted({row["commodity"] for row in checked}),
            "licenses": sorted({row["license"] for row in checked}), "records": checked}


def template_document() -> dict:
    return describe_sources()


def write_template(path: Path) -> Path:
    """Write the source/schema template as JSON (paths outside the repository in normal use)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(template_document(), ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8", newline="\n")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline agri data-source inventory and schema (no network, no raw data)")
    parser.add_argument("--describe", action="store_true", help="print the source inventory")
    parser.add_argument("--template", default=None, help="write the JSON template to this path")
    parser.add_argument("--check", default=None, help="validate an offline JSON list of records")
    args = parser.parse_args(argv)
    if not (args.describe or args.template or args.check):
        parser.print_help()
        return 0
    if args.describe:
        document = describe_sources()
        document["sources"] = [{key: source[key] for key in
                                ("source_id", "name", "license", "frequency")}
                               for source in document["sources"]]
        print(json.dumps(document, ensure_ascii=False, indent=1))
    if args.template:
        try:
            path = write_template(Path(args.template))
        except OSError as exc:
            raise SystemExit(f"cannot write the template: {exc}") from exc
        print(f"template written to {path}")
    if args.check:
        path = Path(args.check)
        if not path.is_file():
            raise SystemExit(f"records file not found: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"cannot read records file {path}: {exc}") from exc
        try:
            result = validate_records(payload)
        except AgriDataError as exc:
            raise SystemExit(f"records failed validation: {exc}") from exc
        summary = {key: value for key, value in result.items() if key != "records"}
        print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
