#!/usr/bin/env python3
"""Tests of the optional World Bank WGI calibration mode for block 15 (docs/248).

The WGI mode is *additive*: built-in ``EXAMPLE-*`` profiles, defaults, the simulator and every
existing report must stay unchanged.  The pinned public slice (``data/public/worldbank_wgi_2025.*``)
is CC BY 4.0 and contains only the six WGI governance estimates for 20 large economies, one latest
year; the block's reports carry the converted 0–100 indicators and sanitized provenance, never the
raw WGI values.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.world import institutions as ins  # noqa: E402

CSV_PATH = ROOT / "data" / "public" / "worldbank_wgi_2025.csv"
META_PATH = ROOT / "data" / "public" / "worldbank_wgi_2025.meta.json"
INDICATOR_KEYS = list(ins.INDICATOR_KEYS)
COLUMNS = ["country_iso3", "country_name", "year", "indicator_code", "indicator_name", "value"]


def distinct_values(offset: float = 0.0) -> dict:
    """Distinctive synthetic WGI estimates (so raw values can be searched for in reports)."""
    values = [-2.3456789, 0.1534567, 1.2345678, -0.7564321, 2.3123456, -1.1234567]
    return {key: values[index] + offset for index, key in enumerate(INDICATOR_KEYS)}


def build_profile(**kwargs):
    payload = dict(distinct_values())
    payload.update(kwargs.pop("values", {}))
    return ins.profile_from_wgi(payload, iso3=kwargs.pop("iso3", "USA"),
                                year=kwargs.pop("year", 2025), **kwargs)


def read_csv_rows() -> list[dict]:
    with CSV_PATH.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


class ScaleTests(unittest.TestCase):
    def test_scale_endpoints(self):
        self.assertAlmostEqual(ins.wgi_score(-2.5), 0.0, places=12)
        self.assertAlmostEqual(ins.wgi_score(0.0), 50.0, places=12)
        self.assertAlmostEqual(ins.wgi_score(2.5), 100.0, places=12)
        scores = [ins.wgi_score(value) for value in (-2.0, -1.0, 0.0, 1.0, 2.0)]
        self.assertEqual(scores, sorted(scores))

    def test_clipping_at_the_block_bounds(self):
        self.assertEqual(ins.wgi_score(-9.0), 0.0)
        self.assertEqual(ins.wgi_score(-2.500001), 0.0)
        self.assertEqual(ins.wgi_score(3.0), 100.0)
        self.assertEqual(ins.wgi_score(2.500001), 100.0)
        self.assertGreaterEqual(ins.wgi_score(-1e6), 0.0)
        self.assertLessEqual(ins.wgi_score(1e6), 100.0)

    def test_non_finite_values_fail_closed(self):
        for bad in (float("nan"), float("inf"), None, "x"):
            with self.assertRaises(ins.InstitutionError):
                ins.wgi_score(bad)


class ProfileTests(unittest.TestCase):
    def test_profile_source_scores_and_pointer(self):
        profile = build_profile()
        self.assertEqual(profile.source, ins.WGI_SOURCE)
        values = distinct_values()
        expected = {key: ins.wgi_score(values[key]) for key in INDICATOR_KEYS}
        self.assertEqual(profile.indicators.as_dict(), expected)
        for score in profile.indicators.as_tuple():
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 100.0)
        self.assertGreaterEqual(profile.score(), 0.0)
        self.assertLessEqual(profile.score(), 100.0)
        to_dict = profile.to_dict()
        self.assertEqual(to_dict["source"], "WGI_CALIBRATED")
        self.assertEqual(to_dict["classification"], "WGI_CALIBRATED")
        self.assertEqual(to_dict["data_status"], "WGI_CALIBRATED_OPEN_DATA")
        self.assertIs(to_dict["not_calibrated"], True)

    def test_raw_values_are_kept_only_in_the_profile_provenance(self):
        values = distinct_values()
        profile = build_profile(values=values)
        raw = profile.wgi_provenance["raw_values"]
        for key in INDICATOR_KEYS:
            self.assertAlmostEqual(raw[key], values[key], places=15)
        self.assertEqual(profile.wgi_provenance["iso3"], "USA")
        self.assertEqual(profile.wgi_provenance["year"], 2025)
        self.assertNotIn("raw_values", profile.to_dict()["wgi_provenance"])
        self.assertIs(profile.to_dict()["wgi_provenance"]["raw_values_in_report"], False)
        text = ins.canonical_json(profile.to_dict())
        for value in values.values():
            self.assertNotIn(repr(value), text)

    def test_missing_unknown_and_invalid_inputs_fail_closed(self):
        with self.assertRaises(ins.InstitutionError):
            ins.profile_from_wgi({key: 0.0 for key in INDICATOR_KEYS[:-1]},
                                 iso3="USA", year=2025)
        with self.assertRaises(ins.InstitutionError):
            ins.profile_from_wgi({**{key: 0.0 for key in INDICATOR_KEYS}, "extra": 0.0},
                                 iso3="USA", year=2025)
        with self.assertRaises(ins.InstitutionError):
            ins.profile_from_wgi({key: float("nan") for key in INDICATOR_KEYS},
                                 iso3="USA", year=2025)
        with self.assertRaises(ins.InstitutionError):
            ins.profile_from_wgi({key: 0.0 for key in INDICATOR_KEYS}, iso3="US", year=2025)
        with self.assertRaises(ins.InstitutionError):
            ins.profile_from_wgi({key: 0.0 for key in INDICATOR_KEYS}, iso3="USA", year=1900)
        with self.assertRaises(ins.InstitutionError):
            ins.profile_from_wgi({key: 0.0 for key in INDICATOR_KEYS}, iso3="USA", year=True)

    def test_classmethod_and_module_function_agree(self):
        values = distinct_values()
        one = ins.InstitutionProfile.from_wgi(values, iso3="DEU", year=2024)
        two = ins.profile_from_wgi(values, iso3="DEU", year=2024)
        self.assertEqual(one.to_dict(), two.to_dict())
        self.assertEqual(one.name, "WGI-DEU-2024")
        self.assertEqual(one.wgi_provenance["indicator_codes"], ins.WGI_INDICATOR_CODES)

    def test_determinism(self):
        first = build_profile()
        second = build_profile()
        self.assertEqual(first.to_dict(), second.to_dict())
        scenario = ins.default_scenarios()[0]
        self.assertEqual(ins.scenario_overlay(first, scenario)["digest"],
                         ins.scenario_overlay(second, scenario)["digest"])

    def test_config_roundtrip_and_wgi_provenance_required(self):
        profile = build_profile()
        config = {
            "name": profile.name,
            "indicators": profile.indicators.as_dict(),
            "source": "WGI_CALIBRATED",
            "note": profile.note,
            "wgi_provenance": dict(profile.wgi_provenance),
        }
        restored = ins.profile_from_config(config)
        self.assertEqual(restored.source, "WGI_CALIBRATED")
        self.assertEqual(restored.to_dict(), profile.to_dict())
        with self.assertRaises(ins.InstitutionError):
            ins.profile_from_config({"name": "WGI-USA-2025",
                                     "indicators": profile.indicators.as_dict(),
                                     "source": "WGI_CALIBRATED"})
        with self.assertRaises(ins.InstitutionError):
            ins.profile_from_config({"name": "EXAMPLE-MID",
                                     "indicators": profile.indicators.as_dict(),
                                     "source": "ILLUSTRATIVE",
                                     "wgi_provenance": dict(profile.wgi_provenance)})

    def test_example_profiles_are_unchanged(self):
        self.assertEqual(tuple(ins._EXAMPLE_VALUES), ("EXAMPLE-LOW", "EXAMPLE-MID",
                                                      "EXAMPLE-HIGH", "EXAMPLE-POLARIZED"))
        for profile in ins.example_profiles():
            expected = ins._EXAMPLE_VALUES[profile.name]
            self.assertEqual(profile.source, "ILLUSTRATIVE")
            self.assertEqual(profile.indicators.as_tuple(), tuple(float(value) for value in expected))
            payload = profile.to_dict()
            self.assertEqual(payload["classification"], "ASSUMPTION_BASED")
            self.assertNotIn("wgi_provenance", payload)
            self.assertNotIn("data_status", payload)
            self.assertEqual(payload["source"], "ILLUSTRATIVE")
        self.assertEqual(ins.find_example_profile("example-mid").name, "EXAMPLE-MID")

    def test_defaults_and_global_status_are_unchanged(self):
        self.assertEqual(ins.DATA_STATUS, "ASSUMPTION_BASED")
        self.assertIs(ins.NOT_CALIBRATED, True)
        self.assertIs(ins.ADMISSION, False)
        self.assertIs(ins.GATING, False)
        for value in ins.DEFAULT_WEIGHTS.values():
            self.assertAlmostEqual(value, 1.0 / 6.0, places=12)


class ReportTests(unittest.TestCase):
    def test_scenario_report_checks_and_provenance(self):
        profile = build_profile()
        report = ins.scenario_overlay(profile, ins.default_scenarios()[0])
        self.assertEqual(report["status"], "RESEARCH_ONLY")
        self.assertIs(report["admission"], False)
        self.assertEqual(report["data_status"], "WGI_CALIBRATED_OPEN_DATA")
        self.assertIs(report["not_calibrated"], True)
        self.assertTrue(report["checks"]["score_bounds"])
        self.assertTrue(report["checks"]["indicator_bounds"])
        self.assertTrue(report["checks"]["no_raw_data"])
        self.assertFalse(report["checks"]["gating"])
        self.assertEqual(report["provenance"]["indicator_source"], "WGI_CALIBRATED")
        self.assertEqual(report["provenance"]["calibration"], "WGI_OPEN_DATA")
        self.assertIs(report["provenance"]["data_read"], True)
        self.assertIs(report["provenance"]["country_table"], True)
        self.assertIn("wgi_provenance", report)
        self.assertEqual(report["wgi_provenance"]["iso3"], "USA")
        self.assertEqual(report["wgi_provenance"]["year"], 2025)
        self.assertIs(report["wgi_provenance"]["raw_values_in_report"], False)
        self.assertNotIn("raw_values", report["wgi_provenance"])

    def test_raw_values_never_reach_the_report(self):
        values = distinct_values()
        profile = build_profile(values=values)
        report = ins.scenario_overlay(profile, ins.default_scenarios()[-1])
        text = ins.canonical_json(report)
        for value in values.values():
            self.assertNotIn(repr(value), text)
        self.assertEqual(ins.forbidden_keys(report), [])
        self.assertNotIn("raw_values", report["wgi_provenance"])
        self.assertIs(report["wgi_provenance"]["raw_values_in_report"], False)

    def test_run_all_index_for_wgi_profile(self):
        profile = build_profile()
        result = ins.run_all(profile)
        index = result["index"]
        self.assertEqual(index["data_status"], "WGI_CALIBRATED_OPEN_DATA")
        self.assertIn("wgi_provenance", index)
        self.assertTrue(index["checks"]["all_no_raw_data"])
        self.assertTrue(index["checks"]["all_score_bounds"])
        self.assertIs(index["admission"], False)
        again = ins.run_all(build_profile())
        self.assertEqual(index["digest"], again["index"]["digest"])
        examples = ins.run_all(ins.find_example_profile("EXAMPLE-MID"))
        self.assertEqual(examples["index"]["data_status"], ins.DATA_STATUS)
        self.assertNotIn("wgi_provenance", examples["index"])

    def test_scenario_modifier_keeps_the_research_only_frame(self):
        profile = build_profile()
        report = ins.scenario_overlay(profile, ins.default_scenarios()[0])
        modifier = ins.as_scenario_modifier(report)
        self.assertEqual(modifier["profile_source"], "WGI_CALIBRATED")
        self.assertEqual(modifier["data_status"], "WGI_CALIBRATED_OPEN_DATA")
        self.assertIs(modifier["not_calibrated"], True)
        self.assertIs(modifier["gating"], False)
        self.assertIs(modifier["causal_claim"], False)
        example_report = ins.scenario_overlay(ins.find_example_profile("EXAMPLE-MID"),
                                              ins.default_scenarios()[0])
        example_modifier = ins.as_scenario_modifier(example_report)
        self.assertEqual(example_modifier["data_status"], ins.DATA_STATUS)


class DataPinTests(unittest.TestCase):
    def test_csv_is_pinned_and_compact(self):
        self.assertTrue(CSV_PATH.is_file(), CSV_PATH)
        raw = CSV_PATH.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM is not allowed")
        self.assertNotIn(b"\r\n", raw, "the pinned CSV must use LF")
        rows = read_csv_rows()
        self.assertGreaterEqual(len(rows), 100)
        self.assertLessEqual(len(rows), 200)
        self.assertEqual(rows[0].keys(), dict.fromkeys(COLUMNS).keys())
        self.assertEqual(len({row["country_iso3"] for row in rows}), 20)
        self.assertEqual(len({row["indicator_code"] for row in rows}), 6)
        self.assertEqual({row["year"] for row in rows}, {"2025"})
        for row in rows:
            self.assertTrue(math.isfinite(float(row["value"])), row)
        # deterministic ordering: ISO3 alphabetical, then the declared indicator order
        ordered_codes = [ins.WGI_INDICATOR_CODES[ins.INDICATOR_KEYS[index]]
                         for index in range(6)]
        by_country: dict[str, list[str]] = {}
        for row in rows:
            by_country.setdefault(row["country_iso3"], []).append(row["indicator_code"])
        self.assertEqual(sorted(by_country), list(by_country))
        for codes in by_country.values():
            self.assertEqual(codes, ordered_codes)

    def test_meta_matches_the_csv_and_has_provenance(self):
        self.assertTrue(META_PATH.is_file(), META_PATH)
        meta = json.loads(META_PATH.read_text(encoding="utf-8"))
        csv_bytes = CSV_PATH.read_bytes()
        self.assertEqual(meta["schema"], "AMFE-PUBLIC-WGI/1.0.0")
        self.assertIn("CC BY 4.0", meta["license"])
        self.assertIn("attribution", meta["license"])
        self.assertEqual(meta["publisher"], "World Bank")
        self.assertTrue(meta["source_url"].startswith("https://api.worldbank.org/v2/"))
        self.assertEqual(set(meta["source_urls"]), set(ins.INDICATOR_KEYS))
        self.assertEqual(meta["retrieved_at"], "2026-11-26")
        self.assertEqual(meta["observations"], 120)
        self.assertEqual(meta["coverage"]["observations"], 120)
        self.assertEqual(meta["coverage"]["countries"], 20)
        self.assertEqual(meta["coverage"]["missing_cells"], 0)
        self.assertIn("WGI", meta["unit"])
        self.assertEqual(meta["file"], "data/public/worldbank_wgi_2025.csv")
        self.assertEqual(meta["file_bytes"], len(csv_bytes))
        self.assertEqual(meta["file_sha256"], hashlib.sha256(csv_bytes).hexdigest())
        codes = {item["key"]: item["indicator_code"] for item in meta["indicators"]}
        self.assertEqual(codes, ins.WGI_INDICATOR_CODES)
        self.assertEqual(len(meta["indicators"]), 6)
        self.assertEqual(meta["observed_range"]["min"], min(float(row["value"]) for row in read_csv_rows()))
        self.assertEqual(meta["observed_range"]["max"], max(float(row["value"]) for row in read_csv_rows()))

    def test_profile_from_a_pinned_country_row(self):
        rows = [row for row in read_csv_rows() if row["country_iso3"] == "USA"]
        self.assertEqual(len(rows), 6)
        by_code = {row["indicator_code"]: float(row["value"]) for row in rows}
        values = {key: by_code[ins.WGI_INDICATOR_CODES[key]] for key in INDICATOR_KEYS}
        profile = ins.profile_from_wgi(values, iso3="USA", year=2025)
        self.assertEqual(profile.source, "WGI_CALIBRATED")
        for key in INDICATOR_KEYS:
            self.assertAlmostEqual(profile.indicators.as_dict()[key], ins.wgi_score(values[key]),
                                   places=12)
        report = ins.scenario_overlay(profile, ins.default_scenarios()[0])
        self.assertTrue(report["checks"]["no_raw_data"])
        self.assertTrue(report["checks"]["score_bounds"])
        self.assertEqual(report["wgi_provenance"]["iso3"], "USA")

    def test_wgi_indicator_metadata(self):
        metadata = ins.wgi_indicators_metadata()
        self.assertEqual(len(metadata), 6)
        self.assertEqual({item["key"] for item in metadata}, set(ins.INDICATOR_KEYS))
        for item in metadata:
            self.assertEqual(item["indicator_code"], ins.WGI_INDICATOR_CODES[item["key"]])
            self.assertEqual(item["scale"], [ins.WGI_SCALE_MIN, ins.WGI_SCALE_MAX])


if __name__ == "__main__":
    unittest.main(verbosity=2)
