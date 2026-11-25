"""Tests for the Forecast Gateway Prometheus exposition and the alert-rule file (M1).

The gateway is research-only (``RESEARCH_ONLY``, ``admission=false``): these tests pin the *format*
and the *honesty* of the metrics endpoint (no secrets, no raw paths, bounded cardinality), not a
SLA claim.  No network access is required: the gateway logic is exercised directly, plus one local
``http.server`` round-trip on 127.0.0.1.

Run: ``python -m unittest tests.test_api_prometheus -v``.
"""
from __future__ import annotations

import http.client
import json
import math
import re
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import forecast_api_server as gateway  # noqa: E402
from test_forecast_api_server import (  # noqa: E402
    ENV,
    FREE_KEY,
    HEADERS,
    INTERNAL_KEY,
    NOW,
    PRO_KEY,
    GatewayCase,
)

ALERTS_PATH = ROOT / "deploy" / "prometheus" / "amfe_alerts.yml"
PROM_PATH = "/v1/metrics/prometheus"
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

METRIC_TYPES = {
    "amfe_gateway_up": "gauge",
    "amfe_uptime_seconds": "gauge",
    "amfe_requests_total": "counter",
    "amfe_errors_total": "counter",
    "amfe_error_rate": "gauge",
    "amfe_request_duration_p50_ms": "gauge",
    "amfe_request_duration_p95_ms": "gauge",
    "amfe_http_status_total": "counter",
    "amfe_requests_by_path_total": "counter",
}

SAMPLE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{(?P<labels>.*)\})?\s+(?P<value>-?(?:\d+(?:\.\d+)?(?:e[+-]?\d+)?|NaN|[+-]Inf))$")


def parse_labels(text: str) -> dict:
    """Parse the ``{key="value",...}`` part of a sample line (values already unescaped enough)."""
    labels = {}
    if not text:
        return labels
    for part in re.finditer(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"', text):
        labels[part.group(1)] = part.group(2)
    return labels


def samples(text: str) -> list[tuple[str, dict, float]]:
    """Return ``[(metric, labels, value), ...]`` for every sample line (HELP/TYPE skipped)."""
    out = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = SAMPLE_RE.match(line)
        if match is None:
            raise AssertionError(f"not a valid Prometheus sample line: {line!r}")
        out.append((match.group("name"), parse_labels(match.group("labels")),
                    float(match.group("value"))))
    return out


def sample_value(text: str, name: str, labels: dict | None = None) -> float:
    wanted = labels or {}
    for metric, found, value in samples(text):
        if metric == name and all(found.get(key) == item for key, item in wanted.items()):
            return value
    raise AssertionError(f"sample {name}{wanted} not found in exposition")


class PrometheusCase(GatewayCase):
    """Shared fixture plus a fresh gateway per test (metrics are in-process and mutable)."""

    def fresh_gateway(self):
        return gateway.build_gateway(self.api_dir, self.product_path,
                                     environ=ENV, clock=lambda: NOW)

    def exposition(self, gw) -> tuple[gateway.Response, str]:
        response = gw.handle("GET", PROM_PATH, headers={})
        return response, response.encoded_body().decode("utf-8")


class ExpositionFormat(PrometheusCase):
    def test_endpoint_is_public_text_plain_and_lf_terminated(self):
        gw = self.fresh_gateway()
        response, text = self.exposition(gw)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, CONTENT_TYPE)
        self.assertEqual(response.headers.get("Content-Type"), CONTENT_TYPE)
        self.assertTrue(text.endswith("\n"))
        self.assertNotIn("\r", text)
        # public: no API key at all
        self.assertEqual(gw.handle("GET", PROM_PATH, headers=None).status, 200)

    def test_help_and_type_exist_for_every_metric(self):
        gw = self.fresh_gateway()
        _, text = self.exposition(gw)
        for name, kind in METRIC_TYPES.items():
            self.assertIn(f"# HELP {name} ", text, name)
            self.assertIn(f"# TYPE {name} {kind}", text, name)

    def test_every_sample_parses_and_uses_only_known_metrics(self):
        gw = self.fresh_gateway()
        gw.handle("GET", "/v1/health", headers={})
        _, text = self.exposition(gw)
        for metric, labels, value in samples(text):
            self.assertIn(metric, METRIC_TYPES)
            self.assertTrue(math.isfinite(value))
            self.assertTrue(set(labels) <= {"status", "path"}, labels)

    def test_empty_metrics_on_startup(self):
        gw = self.fresh_gateway()
        _, text = self.exposition(gw)
        self.assertEqual(sample_value(text, "amfe_gateway_up"), 1.0)
        self.assertEqual(sample_value(text, "amfe_requests_total"), 0.0)
        self.assertEqual(sample_value(text, "amfe_errors_total"), 0.0)
        self.assertEqual(sample_value(text, "amfe_error_rate"), 0.0)
        self.assertEqual(sample_value(text, "amfe_request_duration_p50_ms"), 0.0)
        self.assertEqual(sample_value(text, "amfe_request_duration_p95_ms"), 0.0)
        self.assertGreaterEqual(sample_value(text, "amfe_uptime_seconds"), 0.0)
        # no labelled samples before the first request
        self.assertNotIn("amfe_http_status_total{", text)
        self.assertNotIn("amfe_requests_by_path_total{", text)

    def test_counters_grow_and_error_rate_is_consistent(self):
        gw = self.fresh_gateway()
        gw.handle("GET", "/v1/health", headers={})
        gw.handle("GET", "/v1/modules", headers=HEADERS)
        gw.handle("GET", "/v1/no-such-path", headers=HEADERS)
        _, text = self.exposition(gw)
        self.assertEqual(sample_value(text, "amfe_requests_total"), 3.0)
        self.assertEqual(sample_value(text, "amfe_errors_total"), 1.0)
        self.assertAlmostEqual(sample_value(text, "amfe_error_rate"), 1.0 / 3.0, places=6)
        self.assertEqual(sample_value(text, "amfe_http_status_total", {"status": "200"}), 2.0)
        self.assertEqual(sample_value(text, "amfe_http_status_total", {"status": "404"}), 1.0)
        self.assertEqual(sample_value(text, "amfe_requests_by_path_total",
                                      {"path": "/v1/health"}), 1.0)
        self.assertEqual(sample_value(text, "amfe_requests_by_path_total",
                                      {"path": "/v1/modules"}), 1.0)
        self.assertEqual(sample_value(text, "amfe_requests_by_path_total",
                                      {"path": "other"}), 1.0)

    def test_consistency_with_the_json_metrics_document(self):
        gw = self.fresh_gateway()
        gw.handle("GET", "/v1/health", headers={})
        document = gw.handle("GET", "/v1/metrics", headers={}).payload
        _, text = self.exposition(gw)  # the JSON call itself is now counted, this one is not
        self.assertEqual(sample_value(text, "amfe_requests_total"),
                         float(document["requests_total"] + 1))
        self.assertEqual(sample_value(text, "amfe_errors_total"),
                         float(document["errors_total"]))
        self.assertAlmostEqual(sample_value(text, "amfe_error_rate"),
                               document["error_rate"], places=6)
        self.assertGreaterEqual(sample_value(text, "amfe_request_duration_p50_ms"), 0.0)
        self.assertGreaterEqual(sample_value(text, "amfe_request_duration_p95_ms"), 0.0)

    def test_exposition_is_deterministic_for_the_same_snapshot(self):
        snapshot = {"uptime_seconds": 12.5, "requests_total": 7, "errors_total": 1,
                    "error_rate": 1.0 / 7.0, "latency_ms": {"p50": 1.5, "p95": 9.25},
                    "by_path": {"/v1/health": 3, "/v1/nope": 4},
                    "status_counts": {"200": 6, "404": 1}}
        first = gateway.prometheus_exposition(snapshot)
        second = gateway.prometheus_exposition(snapshot)
        self.assertEqual(first, second)
        for token in ("generated_at", "timestamp", "2026-", "http://", "https://"):
            self.assertNotIn(token, first)

    def test_head_request_returns_the_same_headers(self):
        gw = self.fresh_gateway()
        response = gw.handle("HEAD", PROM_PATH, headers={})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, CONTENT_TYPE)
        self.assertIn("amfe_requests_total", response.encoded_body().decode("utf-8"))


class PathCardinality(PrometheusCase):
    def test_unknown_paths_collapse_to_other_and_ids_do_not_leak(self):
        gw = self.fresh_gateway()
        for index in range(20):
            gw.handle("GET", f"/v1/unknown-path-{index}", headers={})
        scenario_ids = [f"scenario-{index:04d}-{index * 7919}" for index in range(12)]
        for scenario_id in scenario_ids:
            gw.handle("GET", f"/v1/scenarios/{scenario_id}", headers=HEADERS)
        gw.handle("GET", "/v1/health", headers={})
        _, text = self.exposition(gw)
        labels = {labels_dict["path"] for name, labels_dict, _value in samples(text)
                  if name == "amfe_requests_by_path_total"}
        self.assertTrue(labels)
        self.assertTrue(labels <= set(gateway.PROMETHEUS_KNOWN_PATHS) | {"other"}, labels)
        self.assertEqual(len(labels), len({path for path in labels}))
        self.assertLessEqual(len(labels), len(gateway.PROMETHEUS_KNOWN_PATHS) + 1)
        self.assertEqual(sample_value(text, "amfe_requests_by_path_total",
                                      {"path": "other"}), 20.0)
        self.assertEqual(sample_value(text, "amfe_requests_by_path_total",
                                      {"path": gateway.PROMETHEUS_SCENARIO_PATH}), 12.0)
        for raw in ("unknown-path-", *scenario_ids):
            self.assertNotIn(raw, text)

    def test_path_label_escaping(self):
        evil = '/v1/health\\"\n'
        snapshot = {"by_path": {evil: 2}, "status_counts": {},
                    "requests_total": 2, "errors_total": 0, "error_rate": 0.0,
                    "latency_ms": {"p50": 0.0, "p95": 0.0}}
        text = gateway.prometheus_exposition(snapshot, known_paths=(evil,))
        self.assertIn('\\"', text)
        self.assertIn("\\\\", text)
        self.assertIn('\\n', text)
        sample_lines = [line for line in text.splitlines()
                        if line.startswith("amfe_requests_by_path_total{")]
        self.assertEqual(len(sample_lines), 1)
        escaped = sample_lines[0][len('amfe_requests_by_path_total{path="'):]
        self.assertFalse(escaped.startswith(evil), "the raw label value must not be emitted")
        self.assertNotIn("\n", sample_lines[0])

    def test_escape_helper_covers_backslash_newline_and_quote(self):
        self.assertEqual(gateway.prometheus_escape_label('a\\b'), "a\\\\b")
        self.assertEqual(gateway.prometheus_escape_label("a\nb"), "a\\nb")
        self.assertEqual(gateway.prometheus_escape_label('a"b'), 'a\\"b')
        self.assertEqual(gateway.prometheus_escape_label('a\\"\nb'), r'a\\\"\nb')

    def test_no_secrets_raw_paths_or_urls_in_the_exposition(self):
        gw = self.fresh_gateway()
        gw.handle("GET", "/v1/modules", headers={"X-API-Key": INTERNAL_KEY})
        gw.handle("GET", "/v1/scenarios", headers={"X-API-Key": PRO_KEY})
        gw.handle("GET", "/v1/no-such", headers={"X-API-Key": FREE_KEY})
        _, text = self.exposition(gw)
        for token in (INTERNAL_KEY, PRO_KEY, FREE_KEY, "test_", "data/raw", "sha256",
                      "protocols/", "X-API-Key", "Authorization"):
            self.assertNotIn(token, text)
        self.assertIsNone(re.search(r"(?:^|[^A-Za-z0-9_.\-])[A-Za-z]:[\\/]", text))
        self.assertIsNone(re.search(r"\b(?:https?|ftp|file)://", text))


class EndpointWiring(PrometheusCase):
    def test_openapi_documents_the_prometheus_endpoint(self):
        gw = self.fresh_gateway()
        document = gw.handle("GET", "/openapi.json", headers={}).payload
        self.assertIn(PROM_PATH, document["paths"])
        operation = document["paths"][PROM_PATH]["get"]
        self.assertEqual(operation["security"], [])
        self.assertIn("text/plain", operation["responses"]["200"]["content"])

    def test_status_document_lists_the_endpoint_as_public(self):
        gw = self.fresh_gateway()
        payload = gw.handle("GET", "/v1/status", headers={}).payload
        entry = next(item for item in payload["endpoints"] if item["path"] == PROM_PATH)
        self.assertFalse(entry["auth"])

    def test_unknown_path_advertises_the_prometheus_endpoint(self):
        gw = self.fresh_gateway()
        response = gw.handle("GET", "/v1/no-such-path", headers=HEADERS)
        self.assertEqual(response.status, 404)
        self.assertIn(PROM_PATH, response.payload["details"]["paths"])

    def test_live_http_content_type_and_body(self):
        gw = self.fresh_gateway()
        server = gateway.create_server(gw, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address[:2]
            connection = http.client.HTTPConnection(host, port, timeout=10)
            try:
                connection.request("GET", PROM_PATH)
                response = connection.getresponse()
                body = response.read().decode("utf-8")
            finally:
                connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), CONTENT_TYPE)
        self.assertIn("amfe_gateway_up 1", body)
        self.assertTrue(body.endswith("\n"))

    def test_options_preflight_still_works(self):
        gw = self.fresh_gateway()
        response = gw.handle("OPTIONS", PROM_PATH, headers={})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload.get("status"), "OK")


class AlertRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = ALERTS_PATH.read_text(encoding="utf-8")
        cls.rules = None
        try:  # optional: validate the YAML structurally when PyYAML is available
            import yaml  # noqa: PLC0415 - optional, stdlib-only runtime does not need it
            document = yaml.safe_load(cls.text)
            cls.rules = document["groups"][0]["rules"]
        except ImportError:  # pragma: no cover - minimal environments
            cls.rules = None

    def test_alert_file_exists_and_has_four_rules(self):
        self.assertTrue(ALERTS_PATH.is_file())
        names = ("AmfeGatewayDown", "AmfeHighErrorRate", "AmfeHighLatencyP95", "AmfeNoTraffic")
        for name in names:
            self.assertIn(f"alert: {name}", self.text)
        self.assertEqual(self.text.count("- alert:"), 4)
        self.assertIn("groups:", self.text)
        self.assertIn("name: amfe-forecast-gateway", self.text)

    def test_alert_expressions_and_timings(self):
        expected = {
            "AmfeGatewayDown": ("amfe_gateway_up == 0", "for: 2m", "critical"),
            "AmfeHighErrorRate": ("amfe_error_rate > 0.05", "for: 5m", "warning"),
            "AmfeHighLatencyP95": ("amfe_request_duration_p95_ms > 500", "for: 5m", "warning"),
            "AmfeNoTraffic": ("increase(amfe_requests_total[10m]) == 0", "for: 10m", "warning"),
        }
        for name, (expr, duration, severity) in expected.items():
            block = self.text.split(f"alert: {name}", 1)[1]
            block = block.split("- alert:", 1)[0]
            self.assertIn(expr, block, name)
            self.assertIn(duration, block, name)
            self.assertIn(f"severity: {severity}", block, name)
            self.assertIn("summary:", block, name)
            self.assertIn("description:", block, name)

    def test_alert_metrics_exist_in_the_exposition(self):
        # the metric names in the rules are the ones the endpoint actually serves
        snapshot = {"uptime_seconds": 1.0, "requests_total": 2, "errors_total": 0,
                    "error_rate": 0.0, "latency_ms": {"p50": 0.0, "p95": 0.0},
                    "by_path": {}, "status_counts": {}}
        text = gateway.prometheus_exposition(snapshot)
        for metric in ("amfe_gateway_up", "amfe_error_rate",
                       "amfe_request_duration_p95_ms", "amfe_requests_total"):
            self.assertIn(f"# TYPE {metric} ", text)

    def test_no_invented_urls_or_runbook_links(self):
        self.assertNotIn("http://", self.text)
        self.assertNotIn("https://", self.text)
        self.assertNotIn("runbook_url", self.text)

    def test_yaml_structure_when_pyyaml_is_available(self):
        if self.rules is None:
            self.skipTest("PyYAML is not installed; the text assertions cover the structure")
        self.assertEqual(len(self.rules), 4)
        names = {rule["alert"] for rule in self.rules}
        self.assertEqual(names, {"AmfeGatewayDown", "AmfeHighErrorRate",
                                 "AmfeHighLatencyP95", "AmfeNoTraffic"})
        for rule in self.rules:
            self.assertIn("expr", rule)
            self.assertIn("for", rule)
            self.assertIn("labels", rule)
            self.assertIn("severity", rule["labels"])
            self.assertIn("annotations", rule)


if __name__ == "__main__":
    unittest.main(verbosity=2)
