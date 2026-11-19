#!/usr/bin/env python3
"""Simulator UI MVP (tools/simulator_ui.py): scenarios, fail-closed API, no raw, HTML page.

The tests exercise the app directly (fast, no sockets) and then prove the same app is served over
``http.server``.  Numbers are only checked for *shape, sign and provenance*: the demo is a
RESEARCH_ONLY translation of the existing models, so the tests must not freeze model values.
"""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import simulator_ui as sim  # noqa: E402

ARTIFACT = ROOT / "build" / "geopolitics" / "report.json"
HORMUZ = {"scenario": "hormuz_base", "price": 75.0, "shock": 100.0, "country": "US"}


def payload_of(response: sim.Response) -> dict:
    """Serialise a response payload the same way the HTTP adapter does."""
    return json.loads(json.dumps(response.payload, ensure_ascii=False, sort_keys=True))


class Contracts(unittest.TestCase):
    """The document envelope: research status, aggregate-only, no raw data."""

    @classmethod
    def setUpClass(cls):
        cls.app = sim.SimulatorApp(geopolitics_artifact=ARTIFACT)
        cls.base = cls.app.run(dict(HORMUZ))

    def test_health_contract(self):
        health = self.app.health()
        self.assertEqual(health["schema"], sim.SCHEMA)
        self.assertEqual(health["status"], "RESEARCH_ONLY")
        self.assertIs(health["admission"], False)
        self.assertIs(health["causal_claim"], False)
        self.assertEqual([item["id"] for item in health["scenarios"]],
                         ["hormuz_base", "hormuz_stress", "gas_shock", "geopolitics"])
        self.assertGreaterEqual(len(health["countries"]), 10)
        self.assertIn("US", [item["code"] for item in health["countries"]])
        self.assertTrue(health["checks"]["oil_module"])
        self.assertTrue(health["checks"]["aggregate_only"])

    def test_result_envelope_is_research_only(self):
        document = self.base
        self.assertEqual(document["schema"], sim.SCHEMA)
        self.assertEqual(document["status"], "RESEARCH_ONLY")
        self.assertEqual(document["scenario"]["status"], "RESEARCH_ONLY")
        self.assertIs(document["admission"], False)
        self.assertIs(document["causal_claim"], False)
        self.assertIn("не прогноз", document["disclaimer"])
        for key in ("cards", "prices", "cpi", "risk", "charts", "table", "sectors", "country",
                    "checks", "assumptions", "limitations", "provenance", "digest"):
            self.assertIn(key, document)

    def test_hormuz_cards_table_and_paths(self):
        document = self.base
        keys = [card["key"] for card in document["cards"]]
        self.assertEqual(keys[:6], ["brent_peak", "gasoline_peak", "diesel_peak", "cpi_headline",
                                    "household", "risk"])
        self.assertGreater(document["prices"]["peak"], 0)
        self.assertLessEqual(len(document["prices"]["path"]), sim.MAX_PATH_POINTS)
        self.assertEqual(len(document["prices"]["path"]), 6)
        self.assertEqual(len(document["table"]["rows"]), 6)
        self.assertEqual([column["key"] for column in document["table"]["columns"]],
                         ["month", "brent", "gasoline", "diesel", "cpi", "household"])
        self.assertTrue(document["cpi"]["available"])
        self.assertGreater(document["cpi"]["headline_pp"], 0)
        self.assertIn(document["risk"]["band"], {"low", "elevated", "high", "severe"})
        self.assertEqual(len(document["charts"]), 2)
        self.assertTrue(all(chart["kind"] == "line" for chart in document["charts"]))

    def test_checks_pass_and_path_is_bounded(self):
        for key, value in self.base["checks"].items():
            self.assertIs(value, True, msg=f"check {key} failed")
        self.assertEqual(self.base["checks"]["path_months"], True)
        self.assertLessEqual(len(self.base["assumptions"]["items"]), 20)
        self.assertGreaterEqual(self.base["assumptions"]["count"], 10)

    def test_stress_scenario_adds_stress_blocks(self):
        stress = self.app.run({**HORMUZ, "scenario": "hormuz_stress"})
        keys = [card["key"] for card in stress["cards"]]
        self.assertIn("stress_diesel", keys)
        self.assertIn("stress_jet", keys)
        self.assertIn("stress", stress["details"])
        self.assertGreaterEqual(stress["risk"]["score_0_100"], self.base["risk"]["score_0_100"])
        self.assertTrue(all(stress["details"]["stress"]["checks"].values()))

    def test_gas_scenario_contract(self):
        document = self.app.run({"scenario": "gas_shock", "price": 34.0, "shock": 45.0,
                                 "country": "DE"})
        self.assertEqual(document["scenario"]["family"], "gas")
        self.assertEqual(document["prices"]["unit"], "USD/MMBtu")
        self.assertEqual(len(document["prices"]["path"]), 6)
        self.assertTrue(all(value > 0 for value in document["prices"]["path"]))
        self.assertTrue(all(value is True for value in document["checks"].values()))
        self.assertFalse(document["cpi"]["available"])
        self.assertFalse(document["risk"]["available"])
        self.assertFalse(document["sectors"]["available"])
        self.assertFalse(document["country"]["available"])
        self.assertIn("storage", document["details"])
        self.assertGreater(document["details"]["storage"]["end_bcm"], 0)

    def test_geopolitics_scenario_contract(self):
        document = self.app.run({"scenario": "geopolitics", "country": "US",
                                 "geo_scenario": "CHK-ORMUZ-2026"})
        self.assertEqual(document["scenario"]["family"], "geopolitics")
        self.assertTrue(document["sectors"]["available"])
        self.assertEqual(len(document["sectors"]["rows"]), 10)
        self.assertEqual(document["charts"][0]["kind"], "bar")
        self.assertTrue(document["country"]["available"])
        self.assertGreater(len(document["country"]["rows"]), 0)
        self.assertEqual(document["details"]["dataset"]["dataset_id"], "figaro_2019")
        self.assertIn("build/geopolitics/report.json",
                      document["provenance"]["identity"])
        self.assertFalse(document["prices"]["available"])
        self.assertFalse(document["cpi"]["available"])

    def test_geopolitics_unknown_scenario_is_refused(self):
        with self.assertRaises(sim.BadRequest) as caught:
            self.app.run({"scenario": "geopolitics", "geo_scenario": "NOPE-1"})
        self.assertEqual(caught.exception.code, "UNKNOWN_SCENARIO")
        self.assertTrue(caught.exception.details["available"])

    def test_no_raw_data_and_no_paths(self):
        text = json.dumps(self.base, ensure_ascii=False).lower()
        for forbidden in ("observations", "source_rows", "data_points", "vintage_dates",
                          "third_party_data", "actual_values", "forecast_values",
                          "\"raw\"", "raw_data"):
            self.assertNotIn(forbidden, text)
        self.assertNotIn("https://", text)
        self.assertNotIn("/users/", text)
        self.assertNotIn("\\\\", text)
        for chart in self.base["charts"]:
            for line in chart["lines"]:
                self.assertLessEqual(len(line["values"]), sim.MAX_PATH_POINTS)

    def test_country_filter(self):
        us = self.base["country"]
        de = self.app.run({**HORMUZ, "country": "DE"})["country"]
        unknown = self.app.run({**HORMUZ, "country": "ZZ"})
        self.assertTrue(us["available"])
        self.assertTrue(any("Бензин" in row["label"] for row in us["rows"]))
        self.assertTrue(de["available"])
        self.assertFalse(any("Бензин" in row["label"] for row in de["rows"]))
        self.assertTrue(any("MRIO" in row["label"] for row in de["rows"]))
        self.assertFalse(unknown["country"]["available"])
        self.assertEqual(unknown["status"], "RESEARCH_ONLY")

    def test_determinism_and_digest(self):
        first = json.dumps(self.app.run(dict(HORMUZ)), ensure_ascii=False, sort_keys=True)
        second = json.dumps(self.app.run(dict(HORMUZ)), ensure_ascii=False, sort_keys=True)
        self.assertEqual(first, second)
        document = self.app.run(dict(HORMUZ))
        digest = sim._digest({key: value for key, value in document.items() if key != "digest"})
        self.assertEqual(document["digest"], digest)
        other = self.app.run({**HORMUZ, "price": 95.0})
        self.assertNotEqual(other["digest"], document["digest"])

    def test_scenario_sensitivity(self):
        cheap = self.app.run({**HORMUZ, "price": 60.0})
        dear = self.app.run({**HORMUZ, "price": 110.0})
        self.assertGreater(dear["prices"]["peak"], cheap["prices"]["peak"])
        self.assertGreater(dear["cards"][1]["value"], cheap["cards"][1]["value"])
        closed = self.app.run({**HORMUZ, "shock": 100.0})
        partial = self.app.run({**HORMUZ, "shock": 40.0})
        self.assertLess(partial["prices"]["peak"], closed["prices"]["peak"])


class Validation(unittest.TestCase):
    """Fail-closed parameter handling: nothing is fixed up silently."""

    @classmethod
    def setUpClass(cls):
        cls.app = sim.SimulatorApp(geopolitics_artifact=ARTIFACT)

    def test_unknown_scenario(self):
        with self.assertRaises(sim.BadRequest) as caught:
            self.app.run({"scenario": "moon_landing"})
        self.assertEqual(caught.exception.code, "UNKNOWN_SCENARIO")

    def test_out_of_range_and_bad_values(self):
        cases = [
            ({"scenario": "hormuz_base", "price": -5}, "PARAM_OUT_OF_RANGE"),
            ({"scenario": "hormuz_base", "shock": 150}, "PARAM_OUT_OF_RANGE"),
            ({"scenario": "gas_shock", "price": 0}, "PARAM_OUT_OF_RANGE"),
            ({"scenario": "gas_shock", "shock": 400}, "PARAM_OUT_OF_RANGE"),
            ({"scenario": "hormuz_base", "price": "много"}, "INVALID_PARAM"),
            ({"scenario": "hormuz_base", "price": True}, "INVALID_PARAM"),
            ({"scenario": "hormuz_base", "shock": float("nan")}, "INVALID_PARAM"),
            ({"scenario": "hormuz_base", "price": None}, "INVALID_PARAM"),
        ]
        for case, code in cases:
            with self.assertRaises(sim.BadRequest, msg=str(case)) as caught:
                self.app.run(case)
            self.assertEqual(caught.exception.code, code, msg=str(case))

    def test_bad_country_and_scenario_id(self):
        for case, code in (
            ({"scenario": "hormuz_base", "country": "USA!"}, "INVALID_COUNTRY"),
            ({"scenario": "hormuz_base", "country": 12}, "INVALID_COUNTRY"),
            ({"scenario": "hormuz_base", "country": "U"}, "INVALID_COUNTRY"),
            ({"scenario": "geopolitics", "geo_scenario": "bad id"}, "INVALID_SCENARIO_ID"),
            ({"scenario": "geopolitics", "geo_scenario": 7}, "INVALID_SCENARIO_ID"),
        ):
            with self.assertRaises(sim.BadRequest, msg=str(case)) as caught:
                self.app.run(case)
            self.assertEqual(caught.exception.code, code, msg=str(case))

    def test_numeric_strings_are_accepted(self):
        document = self.app.run({"scenario": "hormuz_base", "price": "80,0", "shock": "100"})
        self.assertEqual(document["parameters"]["values"]["price"], 80.0)
        self.assertEqual(document["parameters"]["values"]["shock"], 100.0)

    def test_invalid_body(self):
        with self.assertRaises(sim.BadRequest) as caught:
            self.app.run(["not", "an", "object"])
        self.assertEqual(caught.exception.code, "INVALID_BODY")
        broken = self.app.dispatch("POST", "/api/run", b"{oops")
        self.assertEqual(broken.status, 400)
        self.assertEqual(broken.payload["code"], "INVALID_JSON")
        empty = self.app.dispatch("POST", "/api/run", b"")
        self.assertEqual(empty.status, 400)
        self.assertEqual(empty.payload["code"], "EMPTY_BODY")

    def test_aggregate_guard_blocks_raw_and_paths(self):
        for bad in ({"raw": [1, 2, 3]}, {"note": "C:\\Users\\me\\data.csv"},
                    {"note": "https://example.com/x"}, {"deep": {"observations": []}}):
            with self.assertRaises(sim.PolicyViolation, msg=str(bad)):
                sim._scan_aggregate(bad, "test")
        with self.assertRaises(sim.PolicyViolation):
            sim._scan_aggregate({"big": list(range(sim.MAX_LIST_ITEMS + 1))}, "test")
        sim._scan_aggregate({"ok": "build/geopolitics/report.json", "n": 1.5}, "test")

    def test_artifact_required_mode_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "absent.json"
            strict = sim.SimulatorApp(geopolitics_artifact=missing, geopolitics_mode="artifact")
            with self.assertRaises(sim.Unavailable):
                strict.health()

    def test_synthetic_fallback_when_artifact_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = sim.SimulatorApp(geopolitics_artifact=Path(tmp) / "absent.json")
            document = app.run({"scenario": "geopolitics", "country": "US"})
            self.assertEqual(document["status"], "RESEARCH_ONLY")
            self.assertIn("синтетическая", document["provenance"]["identity"])
            self.assertTrue(document["country"]["available"])


class Page(unittest.TestCase):
    def test_html_page_is_self_contained(self):
        page = sim.SimulatorApp().page()
        self.assertGreater(len(page), 5000)
        self.assertNotIn("__VERSION__", page)
        for needle in ("Рассчитать", "не прогноз", "/api/run", "Скачать JSON", "RESEARCH_ONLY"):
            self.assertIn(needle, page)
        self.assertNotIn("<script src=", page)
        self.assertNotIn("cdn.", page)
        self.assertNotIn("googleapis", page)
        # the only external-looking string is the SVG namespace used to build charts inline
        self.assertEqual(page.count("http://"), page.count('http://www.w3.org/2000/svg'))

    def test_charts_are_inline_svg(self):
        page = sim.SimulatorApp().page()
        self.assertIn("createElementNS", page)
        self.assertIn('svgNode("svg"', page)
        self.assertIn("polyline", page)
        self.assertIn("barChart", page)
        self.assertIn("lineChart", page)


class HttpServer(unittest.TestCase):
    """Prove the same app is served over ``http.server`` with the right codes."""

    @classmethod
    def setUpClass(cls):
        cls.app = sim.SimulatorApp(geopolitics_artifact=ARTIFACT)
        cls.server = sim.create_server(cls.app, "127.0.0.1", 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      kwargs={"poll_interval": 0.05}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)

    def request(self, method: str, path: str, body: bytes | None = None, headers=None):
        host, port = self.server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=15)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response, response.read()
        finally:
            connection.close()

    def test_page_and_health_over_http(self):
        response, payload = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.getheader("Content-Type"))
        self.assertIn("Рассчитать", payload.decode("utf-8"))
        self.assertIn("frame-ancestors", response.getheader("Content-Security-Policy"))
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")

        response, payload = self.request("GET", "/api/health")
        self.assertEqual(response.status, 200)
        self.assertIn("application/json", response.getheader("Content-Type"))
        health = json.loads(payload)
        self.assertEqual(health["status"], "RESEARCH_ONLY")
        self.assertIs(health["admission"], False)
        self.assertEqual(response.getheader("Cache-Control"), "no-store")

    def test_run_over_http(self):
        body = json.dumps({**HORMUZ}).encode("utf-8")
        response, payload = self.request("POST", "/api/run", body=body,
                                         headers={"Content-Type": "application/json"})
        self.assertEqual(response.status, 200)
        document = json.loads(payload)
        self.assertEqual(document["status"], "RESEARCH_ONLY")
        self.assertEqual(len(document["cards"]), 6)
        again_response, again = self.request("POST", "/api/run", body=body,
                                             headers={"Content-Type": "application/json"})
        self.assertEqual(payload, again)
        self.assertEqual(response.status, again_response.status)

    def test_error_codes_over_http(self):
        response, payload = self.request("POST", "/api/run", body=b"{oops",
                                         headers={"Content-Type": "application/json"})
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(payload)["code"], "INVALID_JSON")

        response, payload = self.request("POST", "/api/run",
                                         body=json.dumps({"scenario": "nope"}).encode("utf-8"))
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(payload)["code"], "UNKNOWN_SCENARIO")
        self.assertIs(json.loads(payload)["admission"], False)

        response, payload = self.request("GET", "/api/run")
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(payload)["code"], "METHOD_NOT_ALLOWED")
        self.assertEqual(response.getheader("Allow"), "POST, OPTIONS")

        response, payload = self.request("GET", "/nope")
        self.assertEqual(response.status, 404)
        self.assertEqual(json.loads(payload)["code"], "NOT_FOUND")

        response, payload = self.request("PUT", "/api/run", body=b"{}")
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(payload)["code"], "METHOD_NOT_ALLOWED")

        response, payload = self.request("OPTIONS", "/api/run")
        self.assertEqual(response.status, 204)
        self.assertIn("POST", response.getheader("Allow"))

        response, payload = self.request("HEAD", "/api/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(payload, b"")

    def test_oversized_body_is_refused(self):
        body = json.dumps({"scenario": "hormuz_base", "pad": "x" * (sim.MAX_BODY_BYTES + 10)})
        response, payload = self.request("POST", "/api/run", body=body.encode("utf-8"))
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(payload)["code"], "BODY_TOO_LARGE")

    def test_scenarios_endpoint(self):
        response, payload = self.request("GET", "/api/scenarios")
        self.assertEqual(response.status, 200)
        document = json.loads(payload)
        self.assertEqual(document["kind"], "scenarios")
        self.assertIs(document["admission"], False)
        self.assertEqual(len(document["scenarios"]), 4)
        for item in document["scenarios"]:
            self.assertIn("params", item)
            self.assertIn("id", item)
            self.assertIn("family", item)


if __name__ == "__main__":
    unittest.main(verbosity=2)
