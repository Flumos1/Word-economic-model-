#!/usr/bin/env python3
"""Tests for the API SLA/health/metrics endpoints (M1 product-ready demo)."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

from test_forecast_api_server import GatewayCase, body_of  # noqa: E402


class Observability(GatewayCase):
    def test_health_is_public_and_honest(self):
        response = self.get("/v1/health", headers={})
        self.assertEqual(response.status, 200)
        payload = response.payload
        self.assertEqual(payload["kind"], "health")
        self.assertIs(payload["admission"], False)
        self.assertIn(payload["status"], ("OK", "DEGRADED", "BLOCKED"))
        self.assertIn("checks", payload)
        self.assertGreaterEqual(payload["uptime_seconds"], 0)

    def test_health_does_not_require_key(self):
        self.assertEqual(self.get("/v1/health", headers={}).status, 200)

    def test_metrics_are_public_and_count(self):
        before = self.get("/v1/metrics", headers={}).payload["requests_total"]
        self.get("/v1/modules", headers={"X-API-Key": "test_internal_key_0001"})
        payload = self.get("/v1/metrics", headers={}).payload
        self.assertEqual(payload["kind"], "metrics")
        self.assertGreater(payload["requests_total"], before)
        self.assertIn("latency_ms", payload)
        self.assertIn("p50", payload["latency_ms"])
        self.assertIn("p95", payload["latency_ms"])
        self.assertIn("by_path", payload)
        self.assertIn("status_counts", payload)

    def test_metrics_record_errors(self):
        self.get("/v1/does-not-exist", headers={"X-API-Key": "test_internal_key_0001"})
        payload = self.get("/v1/metrics", headers={}).payload
        self.assertGreaterEqual(payload["errors_total"], 1)
        self.assertIn("404", payload["status_counts"])

    def test_sla_is_declared_not_contractual(self):
        payload = self.get("/v1/sla", headers={}).payload
        self.assertEqual(payload["kind"], "sla")
        self.assertIs(payload["declared"]["contractual"], False)
        self.assertIn("availability", payload["declared"])
        self.assertIn("latency_p95_ms", payload["declared"])
        self.assertIn("observed", payload)
        self.assertIn("договор", payload["note"].lower())

    def test_sla_no_secrets(self):
        text = body_of(self.get("/v1/sla", headers={}))
        self.assertNotIn("test_internal_key", text)
        self.assertNotIn("test_free_key", text)

    def test_status_lists_observability_endpoints(self):
        payload = self.get("/v1/status").payload
        paths = {item["path"] for item in payload["endpoints"]}
        for path in ("/v1/health", "/v1/metrics", "/v1/sla"):
            self.assertIn(path, paths)

    def test_openapi_lists_observability(self):
        document = self.get("/openapi.json", headers={}).payload
        for path in ("/v1/health", "/v1/metrics", "/v1/sla"):
            self.assertIn(path, document["paths"])

    def test_metrics_no_raw_data_or_paths(self):
        text = body_of(self.get("/v1/metrics", headers={}))
        for token in ("C:\\\\", "data/raw", "stored_Z", "cells_Z"):
            self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()
