"""Live Forecast API gateway (tools/forecast_api_server.py): auth, quotas, codes, fail-closed.

The tests exercise the gateway logic directly (fast, no sockets) and then prove that the same
gateway is served over ``http.server``.  Every number is compared with the published static
contract (``build/api``) or the product feed -- the gateway must not invent values.
"""
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import forecast_api_server as gateway  # noqa: E402

NOW = 1_700_000_000.0
FREE_KEY = "test_free_key_0001"
PRO_KEY = "test_pro_key_0001"
INTERNAL_KEY = "test_internal_key_0001"
KEYS_SPEC = f"{FREE_KEY}:free,{PRO_KEY}:pro,{INTERNAL_KEY}:internal"
ENV = {"AMFE_API_KEYS": KEYS_SPEC}
HEADERS = {"X-API-Key": INTERNAL_KEY}
PRODUCT_PATH = ROOT / "build" / "product" / "forecast_latest.json"
FALLBACK_PRODUCT = {
    "schema_version": "1.0.0",
    "generated_at": "2026-11-14T02:09:51Z",
    "status": "CANDIDATE",
    "status_label": "CANDIDATE",
    "conditional_data_class": True,
    "data_class": "REAL_TIME_RECONSTRUCTED",
    "report_id": "AMFE-FIRST-REAL-EVALUATION",
    "protocol": {"file": "protocols/evaluation_protocol_v5.json", "version": "5.0.1",
                 "sha256": "0" * 64},
    "candidate": "ENSEMBLE_INVERSE_MSE",
    "target": "T1_CPI_YOY",
    "horizon": 1,
    "origin": "2026-11",
    "point": 3.6257264569782937,
    "quantiles_5_to_95_step_5": [2.4 + 0.1 * index for index in range(19)],
    "controls": {"RW": {"point": 3.7}},
    "c4_status": {"pass": False, "rmse_pass": False},
    "c6_status": {"pass": False, "status": "NO_RESOLVED_FORECASTS"},
    "source_hashes": {"protocol": "0" * 64},
    "limitations": ["test limitation: not an admission"],
}


# ---------------------------------------------------------------------------------------- helpers
def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def body_of(response: gateway.Response) -> str:
    """Byte-comparable serialisation of a response body (same options as the server)."""
    return json.dumps(response.payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def header(response: gateway.Response, name: str) -> str | None:
    return response.headers.get(name) or response.headers.get(name.lower())


def ensure_contract_dir() -> Path:
    """Return the published API directory, rebuilding it in a temp dir if it is absent."""
    api = ROOT / "build" / "api"
    if (api / "index.json").is_file():
        return api
    out = Path(tempfile.mkdtemp(prefix="amfe-api-contract-")) / "api"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "forecast_api.py"), "--out", str(out),
         "--generated-at", "2026-11-16T10:00:00Z"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if completed.returncode != 0:
        raise RuntimeError("cannot build the API contract for the gateway tests: "
                           + (completed.stderr or "")[-600:])
    return out


def ensure_product_feed() -> Path | None:
    return PRODUCT_PATH if PRODUCT_PATH.is_file() else None


def mutate_json(path: Path, mutator) -> None:
    document = read_json(path)
    mutator(document)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8")


class GatewayCase(unittest.TestCase):
    """Shared fixture: an immutable copy of the published contract plus the gateway."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name)
        cls.api_dir = cls.base / "api"
        shutil.copytree(ensure_contract_dir(), cls.api_dir)
        cls.product_path = cls.base / "forecast_latest.json"
        source = ensure_product_feed()
        if source is not None:
            shutil.copy2(source, cls.product_path)
        else:  # pragma: no cover - this workspace ships the feed
            cls.product_path.write_text(json.dumps(FALLBACK_PRODUCT), encoding="utf-8")
        cls.upstream_index = read_json(cls.api_dir / "index.json")
        modules_path = cls.api_dir / "modules.json"
        cls.upstream_modules = (read_json(modules_path) if modules_path.is_file()
                                else {"modules": list(cls.upstream_index.get("modules") or [])})
        cls.gateway = gateway.build_gateway(cls.api_dir, cls.product_path, environ=ENV,
                                           clock=lambda: NOW)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def get(self, path: str, headers=HEADERS, method: str = "GET") -> gateway.Response:
        return self.gateway.handle(method, path, headers)


class StatusDocument(GatewayCase):
    def test_status_is_public_and_versioned(self):
        response = self.get("/v1/status", headers={})
        self.assertEqual(response.status, 200)
        payload = response.payload
        self.assertEqual(payload["schema"], gateway.SCHEMA)
        self.assertEqual(payload["kind"], "status")
        self.assertEqual(payload["api_version"], gateway.DEFAULT_API_VERSION)
        self.assertEqual(payload["gateway_version"], gateway.GATEWAY_VERSION)
        self.assertIs(payload["admission"], False)
        self.assertEqual(payload["checks"]["api_index"], "OK")
        self.assertEqual(payload["checks"]["api_keys"], "OK")
        self.assertEqual(payload["counts"]["modules"], len(self.upstream_modules["modules"]))
        self.assertEqual(payload["counts"]["scenarios"],
                         len(self.upstream_index["scenarios"]))
        self.assertEqual(payload["counts"]["keys_configured"], 3)
        self.assertEqual(set(payload["limits"]), {"free", "pro", "internal"})
        self.assertEqual(header(response, "Access-Control-Allow-Origin"), "*")
        self.assertEqual(header(response, "Cache-Control"), "no-store")

    def test_status_lists_targets_and_limits(self):
        payload = self.get("/v1/status").payload
        targets = {item["target"]: item for item in payload["targets"]}
        self.assertIn("oil_brent", targets)
        self.assertIn(gateway.PRODUCT_TARGET, targets)
        self.assertEqual(targets["oil_brent"]["horizon_months"], 6)
        self.assertEqual(targets["oil_brent"]["status"], "RESEARCH_ONLY")
        self.assertEqual(targets[gateway.PRODUCT_TARGET]["status"], "CANDIDATE")
        # The aggregate status is the weakest published status, never an admission.
        self.assertEqual(payload["status"], "CANDIDATE")
        self.assertIs(payload["admission"], False)
        paths = {item["path"] for item in payload["endpoints"]}
        self.assertIn("/v1/forecast?target=&horizon=", paths)
        self.assertIn("/v1/modules", paths)

    def test_status_reports_feed_provenance(self):
        payload = self.get("/v1/status").payload
        index_feed = payload["feeds"]["api_index"]
        self.assertEqual(index_feed["schema"], self.upstream_index["schema"])
        self.assertEqual(index_feed["generated_at"], self.upstream_index["generated_at"])
        self.assertEqual(len(index_feed["sha256"]), 64)
        product_feed = payload["feeds"]["product_feed"]
        self.assertEqual(product_feed["state"], "OK")
        self.assertEqual(product_feed["status"], "CANDIDATE")
        self.assertEqual(len(product_feed["sha256"]), 64)


class Authentication(GatewayCase):
    def test_modules_requires_api_key(self):
        missing = self.get("/v1/modules", headers={})
        self.assertEqual(missing.status, 401)
        self.assertEqual(missing.payload["code"], "MISSING_API_KEY")
        self.assertEqual(header(missing, "WWW-Authenticate"), 'ApiKey realm="amfe"')
        wrong = self.get("/v1/modules", headers={"X-API-Key": "not_a_configured_key"})
        self.assertEqual(wrong.status, 401)
        self.assertEqual(wrong.payload["code"], "INVALID_API_KEY")
        scheme = self.get("/v1/modules", headers={"Authorization": "Basic abc"})
        self.assertEqual(scheme.status, 401)
        self.assertEqual(scheme.payload["code"], "INVALID_AUTH_SCHEME")
        bearer = self.get("/v1/modules",
                          headers={"Authorization": f"Bearer {INTERNAL_KEY}"})
        self.assertEqual(bearer.status, 200)
        self.assertIs(bearer.payload["admission"], False)

    def test_public_endpoints_need_no_key(self):
        for path in ("/openapi.json", "/v1/status"):
            response = self.get(path, headers={})
            self.assertEqual(response.status, 200, path)
        self.assertIs(self.get("/v1/status", headers={}).payload["admission"], False)
        self.assertIs(self.get("/openapi.json", headers={}).payload["x-admission"], False)
        # A key is still accepted on public paths and counts against that key's quota.
        self.assertEqual(self.get("/v1/status", headers=HEADERS).status, 200)

    def test_keys_are_not_retained_or_echoed(self):
        client = self.gateway.keys.authenticate(INTERNAL_KEY)
        self.assertIsNotNone(client)
        self.assertEqual(client.client_id, client.key_sha256[:8])
        self.assertNotIn(INTERNAL_KEY, repr(client))
        self.assertNotIn(INTERNAL_KEY, json.dumps(self.gateway.diagnostics()))
        for path in ("/v1/status", "/v1/modules", "/v1/scenarios",
                     "/v1/forecast?target=oil_brent"):
            self.assertNotIn(INTERNAL_KEY, body_of(self.get(path)), path)
        # The store holds digests only, never the plaintext keys.
        self.assertNotIn(INTERNAL_KEY, json.dumps(sorted(self.gateway.keys._clients)))


class ModulesAndScenarios(GatewayCase):
    def test_modules_passports_are_research_only(self):
        response = self.get("/v1/modules")
        self.assertEqual(response.status, 200)
        payload = response.payload
        self.assertEqual(payload["kind"], "modules")
        modules = payload["modules"]
        self.assertEqual(len(modules), len(self.upstream_modules["modules"]))
        self.assertEqual(payload["counts"]["modules"], len(modules))
        for passport in modules:
            self.assertIn(passport["status"], gateway.SERVED_STATUSES)
            self.assertIs(passport["admission"], False)
            self.assertEqual(len(passport["code_sha256"]), 64)
            self.assertTrue(passport["limitations"], passport["module_id"])
        oil = next(item for item in modules if item["module_id"] == "oil.brent")
        upstream_oil = next(item for item in self.upstream_modules["modules"]
                            if item["module_id"] == "oil.brent")
        self.assertEqual(oil["version"], upstream_oil["version"])
        self.assertEqual(oil["code_sha256"], upstream_oil["code_sha256"])
        self.assertEqual(oil["kind"], "market")
        self.assertEqual(payload["status"], "RESEARCH_ONLY")
        self.assertIs(payload["admission"], False)

    def test_scenarios_list_and_detail_match_contract(self):
        listing = self.get("/v1/scenarios")
        self.assertEqual(listing.status, 200)
        self.assertEqual(listing.payload["kind"], "scenarios")
        listed = listing.payload["scenarios"]
        self.assertEqual(len(listed), len(self.upstream_index["scenarios"]))
        self.assertEqual({item["id"] for item in listed},
                         {item["id"] for item in self.upstream_index["scenarios"]})
        for item in listed:
            self.assertIn(item["status"], gateway.SERVED_STATUSES)
            self.assertTrue(item["links"]["self"].startswith("/v1/scenarios/"))
        detail = self.get("/v1/scenarios/oil.brent/hormuz_closed_6m")
        self.assertEqual(detail.status, 200)
        upstream = read_json(self.api_dir / "scenarios" / "oil.brent__hormuz_closed_6m.json")
        self.assertEqual(detail.payload["id"], upstream["id"])
        self.assertEqual(detail.payload["data"], upstream)
        self.assertIs(detail.payload["admission"], False)
        # The path-free form used by the static contract is accepted too.
        alias = self.get("/v1/scenarios/oil.brent__hormuz_closed_6m")
        self.assertEqual(alias.status, 200)
        self.assertEqual(alias.payload["id"], upstream["id"])
        self.assertEqual(self.get("/v1/scenarios/nope/nope").status, 404)


class ForecastEndpoint(GatewayCase):
    def test_oil_envelope_matches_published_contract(self):
        response = self.get("/v1/forecast?target=oil_brent&horizon=6")
        self.assertEqual(response.status, 200)
        payload = response.payload
        upstream = read_json(self.api_dir / "forecasts" / "oil_brent.json")
        self.assertEqual(payload["query"], {"target": "oil_brent", "horizon": 6})
        self.assertEqual(payload["data"], upstream)
        self.assertEqual(payload["data"]["envelope"]["price_path"],
                         upstream["envelope"]["price_path"])
        self.assertEqual(payload["data"]["module_ref"]["code_sha256"],
                         upstream["module_ref"]["code_sha256"])
        self.assertEqual(payload["status"], "RESEARCH_ONLY")
        self.assertIs(payload["admission"], False)
        self.assertIs(payload["policy"]["no_point_forecast"], True)
        self.assertEqual(payload["source"]["api_index"]["sha256"],
                         self.gateway.feed.index_sha256)

    def test_aliases_resolve_to_the_same_target(self):
        for alias in ("oil.brent", "brent", "Brent", "oil_brent"):
            response = self.get(f"/v1/forecast?target={alias}")
            self.assertEqual(response.status, 200, alias)
            self.assertEqual(response.payload["query"]["target"], "oil_brent", alias)
            self.assertEqual(response.payload["query"]["horizon"], 6, alias)

    def test_horizon_and_parameter_validation(self):
        wrong_horizon = self.get("/v1/forecast?target=oil_brent&horizon=3")
        self.assertEqual(wrong_horizon.status, 400)
        self.assertEqual(wrong_horizon.payload["code"], "UNSUPPORTED_HORIZON")
        self.assertEqual(wrong_horizon.payload["details"]["allowed_horizons"], [6])
        bad_horizon = self.get("/v1/forecast?target=oil_brent&horizon=abc")
        self.assertEqual(bad_horizon.status, 400)
        self.assertEqual(bad_horizon.payload["code"], "INVALID_HORIZON")
        missing = self.get("/v1/forecast")
        self.assertEqual(missing.status, 400)
        self.assertEqual(missing.payload["code"], "MISSING_PARAMETER")
        self.assertIn("oil_brent", missing.payload["details"]["available_targets"])
        unknown = self.get("/v1/forecast?target=gas_ttf")
        self.assertEqual(unknown.status, 404)
        self.assertEqual(unknown.payload["code"], "UNKNOWN_TARGET")
        self.assertIn("oil_brent", unknown.payload["details"]["available_targets"])
        extra = self.get("/v1/forecast?target=oil_brent&offset=2")
        self.assertEqual(extra.status, 400)
        self.assertEqual(extra.payload["code"], "UNKNOWN_PARAMETER")
        duplicate = self.get("/v1/forecast?target=oil_brent&target=brent")
        self.assertEqual(duplicate.status, 400)
        self.assertEqual(duplicate.payload["code"], "DUPLICATE_PARAMETER")
        for path in ("/v1/nope", "/v2/forecast", "/"):
            self.assertEqual(self.get(path).status, 404, path)

    def test_product_target_withholds_the_point_by_default(self):
        response = self.get(f"/v1/forecast?target={gateway.PRODUCT_TARGET}&horizon=1")
        self.assertEqual(response.status, 200)
        payload = response.payload
        data = payload["data"]
        upstream = read_json(self.product_path)
        self.assertEqual(payload["query"], {"target": gateway.PRODUCT_TARGET, "horizon": 1})
        self.assertEqual(payload["status"], "CANDIDATE")
        self.assertIs(payload["admission"], False)
        self.assertIs(data["point_withheld"], True)
        self.assertNotIn("point", data)
        self.assertEqual(data["interval"]["low"], upstream["quantiles_5_to_95_step_5"][0])
        self.assertEqual(data["interval"]["high"], upstream["quantiles_5_to_95_step_5"][-1])
        self.assertEqual(data["candidate"], upstream["candidate"])
        self.assertEqual(data["limitations"], upstream["limitations"])
        self.assertEqual(payload["policy"]["no_point_forecast"], True)
        self.assertEqual(self.get("/v1/forecast?target=cpi_yoy&horizon=3").status, 400)

    def test_product_target_can_serve_the_point_when_enabled(self):
        enabled = gateway.build_gateway(self.api_dir, self.product_path, environ=ENV,
                                        allow_point=True, clock=lambda: NOW)
        response = enabled.handle(
            "GET", f"/v1/forecast?target={gateway.PRODUCT_TARGET}", HEADERS)
        self.assertEqual(response.status, 200)
        upstream = read_json(self.product_path)
        self.assertEqual(response.payload["data"]["point"], upstream["point"])
        self.assertIs(response.payload["data"]["point_withheld"], False)
        self.assertIs(response.payload["policy"]["no_point_forecast"], False)
        self.assertIs(response.payload["admission"], False)


class ErrorsAndHeaders(GatewayCase):
    def test_error_codes_table(self):
        cases = [
            ("GET", "/v1/modules", {}, 401, "MISSING_API_KEY"),
            ("GET", "/v1/forecast?target=oil_brent", {}, 401, "MISSING_API_KEY"),
            ("GET", "/v1/modules", {"X-API-Key": "bad_key_value"}, 401, "INVALID_API_KEY"),
            ("GET", "/v1/nope", HEADERS, 404, "NOT_FOUND"),
            ("GET", "/v1/scenarios/missing", HEADERS, 404, "UNKNOWN_SCENARIO"),
            ("POST", "/v1/modules", HEADERS, 400, "METHOD_NOT_ALLOWED"),
            ("PUT", "/v1/modules", HEADERS, 400, "METHOD_NOT_ALLOWED"),
            ("GET", "/v1/forecast?target=", HEADERS, 400, "MISSING_PARAMETER"),
        ]
        for method, path, headers, expected_status, expected_code in cases:
            response = self.get(path, headers=headers, method=method)
            self.assertEqual(response.status, expected_status, (method, path))
            self.assertEqual(response.payload["code"], expected_code, (method, path))
            self.assertIs(response.payload["admission"], False, (method, path))
        method_not_allowed = self.get("/v1/modules", method="POST")
        self.assertEqual(header(method_not_allowed, "Allow"), "GET, OPTIONS")

    def test_cors_preflight(self):
        preflight = self.get("/v1/forecast?target=oil_brent", headers={}, method="OPTIONS")
        self.assertEqual(preflight.status, 200)
        self.assertEqual(header(preflight, "Access-Control-Allow-Origin"), "*")
        self.assertEqual(header(preflight, "Access-Control-Allow-Methods"), "GET, OPTIONS")
        self.assertIn("X-API-Key", header(preflight, "Access-Control-Allow-Headers"))
        self.assertIn("Retry-After", header(preflight, "Access-Control-Expose-Headers"))
        self.assertEqual(preflight.payload["kind"], "preflight")

    def test_cors_origin_can_be_restricted(self):
        restricted = gateway.build_gateway(self.api_dir, self.product_path, environ=ENV,
                                           cors_origin="https://client.example", clock=lambda: NOW)
        response = restricted.handle("GET", "/v1/status", {})
        self.assertEqual(header(response, "Access-Control-Allow-Origin"),
                         "https://client.example")
        self.assertEqual(header(response, "Vary"), "Origin")


class OpenApi(GatewayCase):
    def test_openapi_is_public_and_describes_the_gateway(self):
        response = self.get("/openapi.json", headers={})
        self.assertEqual(response.status, 200)
        document = response.payload
        self.assertEqual(document["openapi"], "3.0.3")
        self.assertEqual(document["info"]["version"], gateway.GATEWAY_VERSION)
        self.assertIn("/v1/status", document["paths"])
        self.assertIn("/v1/modules", document["paths"])
        self.assertIn("/v1/scenarios", document["paths"])
        self.assertIn("/v1/scenarios/{scenario_id}", document["paths"])
        self.assertIn("/v1/forecast", document["paths"])
        self.assertEqual(document["components"]["securitySchemes"]["ApiKeyAuth"]["name"],
                         "X-API-Key")
        self.assertIs(document["x-admission"], False)
        forecast_errors = document["paths"]["/v1/forecast"]["get"]["responses"]
        for code in ("200", "400", "401", "404", "429", "503"):
            self.assertIn(code, forecast_errors)
        self.assertEqual(document["paths"]["/v1/status"]["get"]["security"], [])
        self.assertEqual(document["x-served-statuses"], list(gateway.SERVED_STATUSES))


class Determinism(GatewayCase):
    def test_responses_are_byte_stable(self):
        for path in ("/v1/status", "/v1/modules", "/v1/scenarios",
                     "/v1/scenarios/oil.brent/hormuz_closed_6m",
                     "/v1/forecast?target=oil_brent&horizon=6",
                     "/v1/forecast?target=cpi_yoy", "/openapi.json"):
            first = body_of(self.get(path, headers=HEADERS))
            second = body_of(self.get(path, headers=HEADERS))
            self.assertEqual(first, second, path)

    def test_feed_timestamp_does_not_drift(self):
        payload = self.get("/v1/status").payload
        self.assertEqual(payload["feeds"]["api_index"]["generated_at"],
                         self.upstream_index["generated_at"])
        upstream = read_json(self.api_dir / "forecasts" / "oil_brent.json")
        forecast = self.get("/v1/forecast?target=oil_brent").payload
        self.assertEqual(forecast["data"]["generated_at"], upstream["generated_at"])


class NoRawData(GatewayCase):
    def test_no_raw_values_paths_or_urls_leak(self):
        paths = ("/v1/status", "/v1/modules", "/v1/scenarios",
                 "/v1/scenarios/oil.brent/hormuz_closed_3m",
                 "/v1/forecast?target=oil_brent", "/v1/forecast?target=cpi_yoy",
                 "/openapi.json")
        for path in paths:
            text = body_of(self.get(path, headers=HEADERS))
            self.assertNotIn("G:/", text, path)
            self.assertNotIn("C:\\", text, path)
            self.assertNotIn("http://", text, path)
            self.assertNotIn("https://", text, path)
            self.assertNotIn('"ACTIVE"', text, path)
            self.assertNotIn('"LIMITED"', text, path)
            lowered = text.lower()
            for forbidden in ('"raw', 'raw_values', '"observations"', '"series"'):
                self.assertNotIn(forbidden, lowered, f"{path} leaks {forbidden}")
        # No admission anywhere and every 200 is research-only or candidate.
        for path in paths:
            payload = self.get(path, headers=HEADERS).payload
            if "admission" in payload:
                self.assertIs(payload["admission"], False, path)
            if "status" in payload:
                self.assertIn(payload["status"], gateway.SERVED_STATUSES + ("OK",), path)

    def test_disclaimer_is_served_on_every_document(self):
        for path in ("/v1/status", "/v1/modules", "/v1/scenarios",
                     "/v1/forecast?target=oil_brent"):
            payload = self.get(path, headers=HEADERS).payload
            self.assertEqual(payload["disclaimer"], gateway.DISCLAIMER, path)
            self.assertIn("not an admission", payload["disclaimer"], path)


class Quotas(GatewayCase):
    def test_minute_rate_limit_and_headers(self):
        plans = {"free": gateway.Plan("free", 3, 100), "pro": gateway.Plan("pro", 60, 5000),
                 "internal": gateway.Plan("internal", 600, 200_000)}
        limited = gateway.build_gateway(self.api_dir, self.product_path,
                                        environ={"AMFE_API_KEYS": f"{FREE_KEY}:free,"
                                                                  f"{PRO_KEY}:pro"},
                                        plans=plans, clock=lambda: NOW)
        for index in range(3):
            response = limited.handle("GET", "/v1/modules", {"X-API-Key": FREE_KEY})
            self.assertEqual(response.status, 200, index)
            self.assertEqual(header(response, "X-RateLimit-Limit"), "3")
            self.assertEqual(header(response, "X-RateLimit-Remaining"), str(2 - index))
            self.assertEqual(header(response, "X-Plan"), "free")
        blocked = limited.handle("GET", "/v1/modules", {"X-API-Key": FREE_KEY})
        self.assertEqual(blocked.status, 429)
        self.assertEqual(blocked.payload["code"], "RATE_LIMITED")
        self.assertEqual(header(blocked, "Retry-After"), str(int(60 - NOW % 60)))
        self.assertEqual(header(blocked, "X-RateLimit-Remaining"), "0")
        self.assertEqual(blocked.payload["details"]["requests_per_minute"], 3)
        # A different key has its own counter and is not blocked.
        other = limited.handle("GET", "/v1/modules", {"X-API-Key": PRO_KEY})
        self.assertEqual(other.status, 200)
        self.assertEqual(header(other, "X-Plan"), "pro")

    def test_daily_quota_is_enforced(self):
        plans = {"free": gateway.Plan("free", 10, 2), "pro": gateway.Plan("pro", 60, 5000)}
        limited = gateway.build_gateway(self.api_dir, self.product_path,
                                        environ={"AMFE_API_KEYS": f"{FREE_KEY}:free"},
                                        plans=plans, clock=lambda: NOW)
        for index in range(2):
            self.assertEqual(
                limited.handle("GET", "/v1/modules", {"X-API-Key": FREE_KEY}).status, 200,
                index)
        blocked = limited.handle("GET", "/v1/modules", {"X-API-Key": FREE_KEY})
        self.assertEqual(blocked.status, 429)
        self.assertEqual(blocked.payload["code"], "QUOTA_EXCEEDED")
        retry_after = int(header(blocked, "Retry-After"))
        self.assertTrue(1 <= retry_after <= 86400, retry_after)
        self.assertEqual(header(blocked, "X-RateLimit-Day-Limit"), "2")

    def test_different_keys_have_independent_daily_counters(self):
        plans = {"free": gateway.Plan("free", 10, 1), "pro": gateway.Plan("pro", 10, 1)}
        limited = gateway.build_gateway(self.api_dir, self.product_path,
                                        environ={"AMFE_API_KEYS": f"{FREE_KEY}:free,"
                                                                  f"{PRO_KEY}:pro"},
                                        plans=plans, clock=lambda: NOW)
        self.assertEqual(limited.handle("GET", "/v1/status", {}).status, 200)
        self.assertEqual(
            limited.handle("GET", "/v1/modules", {"X-API-Key": FREE_KEY}).status, 200)
        self.assertEqual(
            limited.handle("GET", "/v1/modules", {"X-API-Key": FREE_KEY}).status, 429)
        self.assertEqual(
            limited.handle("GET", "/v1/modules", {"X-API-Key": PRO_KEY}).status, 200)


class FailClosed(unittest.TestCase):
    """Every failure mode must produce 503 (or refuse to start), never open access."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _copy_contract(self) -> Path:
        api = self.base / f"api-{uuid.uuid4().hex[:8]}"
        shutil.copytree(ensure_contract_dir(), api)
        return api

    def _product(self) -> Path:
        path = self.base / "forecast_latest.json"
        source = ensure_product_feed()
        if source is None:  # pragma: no cover
            path.write_text(json.dumps(FALLBACK_PRODUCT), encoding="utf-8")
        else:
            shutil.copy2(source, path)
        return path

    def _gateway(self, api_dir: Path, product: Path, env=ENV) -> gateway.ForecastGateway:
        return gateway.build_gateway(api_dir, product, environ=env, clock=lambda: NOW)

    def test_missing_api_index_returns_503(self):
        g = self._gateway(self.base / "nothing", self._product())
        self.assertEqual(g.diagnostics()["status"], "BLOCKED")
        for path in ("/v1/status", "/v1/modules", "/v1/scenarios",
                     "/v1/forecast?target=oil_brent"):
            response = g.handle("GET", path, HEADERS)
            self.assertEqual(response.status, 503, path)
            self.assertEqual(response.payload["code"], "FEED_UNAVAILABLE", path)
            self.assertIs(response.payload["admission"], False, path)
        # The description of the gateway itself stays available.
        self.assertEqual(g.handle("GET", "/openapi.json", {}).status, 200)

    def test_missing_scenario_file_returns_503(self):
        api = self._copy_contract()
        (api / "scenarios" / "oil.brent__hormuz_closed_3m.json").unlink()
        g = self._gateway(api, self._product())
        self.assertEqual(g.handle("GET", "/v1/scenarios", HEADERS).status, 503)
        self.assertEqual(g.handle("GET", "/v1/scenarios", HEADERS).payload["code"],
                         "FEED_UNAVAILABLE")

    def test_no_api_keys_is_503_not_open_access(self):
        g = self._gateway(self._copy_contract(), self._product(), env={})
        self.assertEqual(g.diagnostics()["status"], "BLOCKED")
        for path in ("/v1/status", "/v1/modules", "/v1/forecast?target=oil_brent"):
            response = g.handle("GET", path, {})
            self.assertEqual(response.status, 503, path)
            self.assertEqual(response.payload["code"], "AUTH_NOT_CONFIGURED", path)
        # Even a made-up key cannot help: no key store means fail closed.
        self.assertEqual(g.handle("GET", "/v1/modules", {"X-API-Key": "guess"}).status, 503)
        self.assertEqual(g.handle("GET", "/openapi.json", {}).status, 200)

    def test_missing_product_feed_degrades_market_targets(self):
        g = self._gateway(self._copy_contract(), self.base / "no_product.json")
        status = g.handle("GET", "/v1/status", {})
        self.assertEqual(status.status, 200)
        self.assertEqual(status.payload["feeds"]["product_feed"]["state"], "MISSING")
        self.assertEqual(status.payload["checks"]["product_feed"], "MISSING")
        self.assertEqual(g.handle("GET", "/v1/forecast?target=oil_brent", HEADERS).status, 200)
        blocked = g.handle("GET", "/v1/forecast?target=cpi_yoy", HEADERS)
        self.assertEqual(blocked.status, 503)
        self.assertEqual(blocked.payload["code"], "PRODUCT_FEED_UNAVAILABLE")
        self.assertEqual(g.diagnostics()["status"], "DEGRADED")

    def test_admission_claim_in_the_feed_is_refused(self):
        api = self._copy_contract()

        def drop_no_admission(document):
            document["policy"]["no_admission"] = False

        mutate_json(api / "index.json", drop_no_admission)
        g = self._gateway(api, self._product())
        response = g.handle("GET", "/v1/modules", HEADERS)
        self.assertEqual(response.status, 503)
        self.assertEqual(response.payload["code"], "POLICY_VIOLATION")

    def test_raw_data_key_in_the_feed_is_refused(self):
        api = self._copy_contract()
        mutate_json(api / "index.json",
                    lambda document: document.__setitem__("raw_values", [1.0, 2.0]))
        g = self._gateway(api, self._product())
        response = g.handle("GET", "/v1/forecast?target=oil_brent", HEADERS)
        self.assertEqual(response.status, 503)
        self.assertEqual(response.payload["code"], "POLICY_VIOLATION")
        self.assertIn("raw", response.payload["error"])

    def test_absolute_path_in_the_feed_is_refused(self):
        api = self._copy_contract()
        mutate_json(api / "index.json", lambda document: document.__setitem__(
            "provenance_note", "C:\\Users\\analyst\\private\\series.csv"))
        g = self._gateway(api, self._product())
        response = g.handle("GET", "/v1/status", {})
        self.assertEqual(response.status, 503)
        self.assertEqual(response.payload["code"], "POLICY_VIOLATION")

    def test_external_url_in_the_feed_is_refused(self):
        api = self._copy_contract()
        mutate_json(api / "forecasts" / "oil_brent.json",
                    lambda document: document.__setitem__("source_url", "https://example.com/x"))
        g = self._gateway(api, self._product())
        response = g.handle("GET", "/v1/forecast?target=oil_brent", HEADERS)
        self.assertEqual(response.status, 503)
        self.assertEqual(response.payload["code"], "POLICY_VIOLATION")

    def test_unsupported_status_is_refused(self):
        api = self._copy_contract()

        def promote(document):
            document["modules"][0]["status"] = "ACTIVE"

        mutate_json(api / "modules.json", promote)
        g = self._gateway(api, self._product())
        response = g.handle("GET", "/v1/modules", HEADERS)
        self.assertEqual(response.status, 503)
        self.assertEqual(response.payload["code"], "UNSUPPORTED_STATUS")

    def test_invalid_key_configuration_refuses_to_start(self):
        for spec in ("short:free", f"{FREE_KEY}:platinum", f"{FREE_KEY}:free,{FREE_KEY}:pro"):
            with self.assertRaises(gateway.ApiConfigError, msg=spec):
                gateway.parse_keys(spec)
        self.assertEqual(gateway.parse_keys(""), {})
        clients = gateway.parse_keys(KEYS_SPEC)
        self.assertEqual(len(clients), 3)
        self.assertTrue(all(len(digest) == 64 for digest in clients))
        self.assertEqual({client.plan.name for client in clients.values()},
                         {"free", "pro", "internal"})


class HttpServer(unittest.TestCase):
    """Prove the same gateway is actually served over ``http.server``."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        api = Path(cls.tmp.name) / "api"
        shutil.copytree(ensure_contract_dir(), api)
        product = Path(cls.tmp.name) / "forecast_latest.json"
        source = ensure_product_feed()
        if source is None:  # pragma: no cover
            product.write_text(json.dumps(FALLBACK_PRODUCT), encoding="utf-8")
        else:
            shutil.copy2(source, product)
        cls.gateway = gateway.build_gateway(api, product, environ=ENV, clock=lambda: NOW)
        cls.server = gateway.create_server(cls.gateway, "127.0.0.1", 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      kwargs={"poll_interval": 0.05}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)
        cls.tmp.cleanup()

    def connection(self) -> http.client.HTTPConnection:
        host, port = self.server.server_address[:2]
        return http.client.HTTPConnection(host, port, timeout=10)

    def request(self, method: str, path: str, headers=None, body=None):
        connection = self.connection()
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            payload = response.read()
            return response, payload
        finally:
            connection.close()

    def test_end_to_end_over_http(self):
        response, payload = self.request("GET", "/v1/status")
        self.assertEqual(response.status, 200)
        self.assertIn("application/json", response.getheader("Content-Type"))
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "*")
        self.assertTrue(response.getheader("X-Request-Id"))
        document = json.loads(payload.decode("utf-8"))
        self.assertEqual(document["schema"], gateway.SCHEMA)
        self.assertIs(document["admission"], False)

        response, _ = self.request("GET", "/v1/modules")
        self.assertEqual(response.status, 401)

        response, payload = self.request("GET", "/v1/modules",
                                         headers={"X-API-Key": INTERNAL_KEY})
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(payload)["kind"], "modules")
        self.assertEqual(response.getheader("X-Plan"), "internal")

        response, payload = self.request(
            "GET", "/v1/forecast?target=oil_brent&horizon=6",
            headers={"Authorization": f"Bearer {INTERNAL_KEY}"})
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(payload)["query"]["horizon"], 6)

        response, payload = self.request("GET", "/v1/forecast?target=nope",
                                         headers={"X-API-Key": INTERNAL_KEY})
        self.assertEqual(response.status, 404)
        self.assertEqual(json.loads(payload)["code"], "UNKNOWN_TARGET")

        response, payload = self.request("OPTIONS", "/v1/forecast")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Access-Control-Allow-Methods"), "GET, OPTIONS")

        response, payload = self.request("POST", "/v1/modules", body=b"{}",
                                         headers={"Content-Type": "application/json",
                                                  "X-API-Key": INTERNAL_KEY})
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(payload)["code"], "METHOD_NOT_ALLOWED")

        response, payload = self.request("HEAD", "/v1/status")
        self.assertEqual(response.status, 200)
        self.assertEqual(payload, b"")

        response, payload = self.request("GET", "/openapi.json")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(payload)["openapi"], "3.0.3")


class CommandLine(unittest.TestCase):
    def _run(self, args, env) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(ROOT / "tools" / "forecast_api_server.py")]
                              + args, cwd=str(ROOT), capture_output=True, text=True,
                              encoding="utf-8", timeout=90, env=env)

    def test_check_reports_configuration_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = Path(tmp) / "api"
            shutil.copytree(ensure_contract_dir(), api)
            product = Path(tmp) / "forecast_latest.json"
            source = ensure_product_feed()
            if source is None:  # pragma: no cover
                product.write_text(json.dumps(FALLBACK_PRODUCT), encoding="utf-8")
            else:
                shutil.copy2(source, product)
            base_env = {**os.environ, "PYTHONIOENCODING": "utf-8",
                        "AMFE_API_KEYS": KEYS_SPEC}
            completed = self._run(["--check", "--api-dir", str(api),
                                   "--product-feed", str(product)], base_env)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["status"], "OK")
            self.assertEqual(summary["keys_configured"], 3)
            self.assertEqual(summary["feeds"]["product_feed"]["state"], "OK")
            self.assertEqual(set(summary["plans"]), {"free", "pro", "internal"})

            blocked_env = {key: value for key, value in base_env.items()
                           if key != "AMFE_API_KEYS"}
            blocked = self._run(["--check", "--api-dir", str(api),
                                 "--product-feed", str(product)], blocked_env)
            self.assertEqual(blocked.returncode, 2)
            self.assertEqual(json.loads(blocked.stdout)["status"], "BLOCKED")

            degraded = self._run(["--check", "--api-dir", str(api),
                                  "--product-feed", str(Path(tmp) / "absent.json")], base_env)
            self.assertEqual(degraded.returncode, 0, degraded.stderr)
            self.assertEqual(json.loads(degraded.stdout)["status"], "DEGRADED")

    def test_version_and_help(self):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        version = self._run(["--version"], env)
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertIn(gateway.GATEWAY_VERSION, version.stdout)
        help_run = self._run(["--help"], env)
        self.assertEqual(help_run.returncode, 0)
        for flag in ("--api-dir", "--product-feed", "--check", "--cors-origin"):
            self.assertIn(flag, help_run.stdout)

    def test_invalid_key_specification_exits_2(self):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "AMFE_API_KEYS": "short:free"}
        completed = self._run(["--check"], env)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("BLOCKED", completed.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
