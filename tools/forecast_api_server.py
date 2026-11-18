#!/usr/bin/env python3
"""AMFE live Forecast API gateway (stdlib only: ``http.server``, no Flask).

The static contract built by :mod:`forecast_api` (``build/api/*.json``) is served as a live,
read-only HTTP service with API keys, per-key quotas and explicit upstream versions.  It is a
**gateway**, not a model: every number comes from the shipped JSON documents, nothing is recomputed
here.

Endpoints (all ``GET`` unless noted):

* ``GET /openapi.json``          -- OpenAPI 3.0.3 description (public, no key);
* ``GET /v1/status``             -- health/version/feed/limits document (public, no key);
* ``GET /v1/modules``            -- module passports from ``build/api/modules.json`` (key required);
* ``GET /v1/scenarios``          -- scenario index (key required);
* ``GET /v1/scenarios/{id}``     -- one scenario document (key required);
* ``GET /v1/forecast?target=&horizon=`` -- scenario envelope for a target (key required).

Security and policy:

* keys are read from ``AMFE_API_KEYS`` (``key[:plan]`` items, comma/space separated); only
  SHA-256 digests are kept in memory and compared with :func:`hmac.compare_digest`;
* a key is sent as ``X-API-Key: <key>`` or ``Authorization: Bearer <key>``; keys in the query
  string are deliberately **not** accepted (they leak into access logs and proxies);
* per-key quotas: requests per minute and per day, fixed windows, in-memory; exceeding them
  returns ``429`` with ``Retry-After`` and ``X-RateLimit-*`` headers;
* **fail-closed**: no keys configured -> ``503`` (never open access); missing/invalid
  ``build/api/index.json`` -> ``503``; a feed that claims admission, an unsupported status or
  contains raw/absolute-path/external-URL values -> ``503 POLICY_VIOLATION``;
* no raw third-party observations, no absolute local paths and no external URLs are served; the
  responses carry ``status`` in ``{RESEARCH_ONLY, CANDIDATE}`` and ``admission: false``;
* the product feed (``build/product/forecast_latest.json``) is exposed as target ``cpi_yoy`` with
  its point estimate withheld by default (``AMFE_API_ALLOW_POINT=1`` turns the point back on).

Exit codes: ``0`` -- served / ``--check`` OK; ``2`` -- invalid configuration or ``--check`` blocked.

Usage::

    $env:AMFE_API_KEYS = "demo_free_key_01:free,demo_pro_key_01:pro"
    python tools/forecast_api_server.py --host 127.0.0.1 --port 8080
    python tools/forecast_api_server.py --check          # config/feed diagnostics, no server
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, unquote

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_API_DIR = ROOT / "build" / "api"
DEFAULT_PRODUCT_FEED = ROOT / "build" / "product" / "forecast_latest.json"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_API_VERSION = "v1"
SCHEMA = "amfe.forecast_api.live.v1"
GATEWAY_VERSION = "1.0.0"
OPENAPI_VERSION = "3.0.3"
SERVED_STATUSES = ("RESEARCH_ONLY", "CANDIDATE")
STATUS_ORDER = {name: rank for rank, name in enumerate(SERVED_STATUSES)}
MAX_REQUEST_TARGET = 2048
MAX_QUERY_PARAMS = 8
DISCLAIMER = (
    "Research-only scenario arithmetic served by a read-only gateway: not a forecast, not an "
    "admission decision and not investment advice. Every number is copied from the published "
    "static contract or the product feed; the gateway recomputes nothing. The reported range is a "
    "range over sensitivity variants, not a confidence interval. No third-party observations are "
    "redistributed and no local paths are exposed."
)
PRODUCT_TARGET = "cpi_yoy"
PRODUCT_ALIASES = ("cpi_yoy", "t1_cpi_yoy", "T1_CPI_YOY", "cpi.yoy", "cpi")
LOGGER = logging.getLogger("amfe.forecast_api_server")

RAW_KEYS = frozenset({
    "raw", "raw_data", "raw_values", "observations", "observed_values", "source_rows",
    "source_series", "series", "series_values", "third_party_data", "data_points",
})
DRIVE_PATH_RE = re.compile(r"(?:^|[^A-Za-z0-9_.\-])[A-Za-z]:[\\/]")
POSIX_PATH_RE = re.compile(r"(?:^|[^A-Za-z0-9_.\-])/(?:mnt|home|Users|var|etc|opt|tmp|root)/")
EXTERNAL_URL_RE = re.compile(r"\b(?:https?|ftp|file)://", re.IGNORECASE)


# ------------------------------------------------------------------------------------------- errors
class GatewayError(Exception):
    """A request cannot be served; carries the HTTP status, a stable code and safe details."""

    def __init__(self, status: int, code: str, message: str,
                 details: Mapping[str, Any] | None = None,
                 headers: Mapping[str, str] | None = None) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.headers = dict(headers or {})


class BadRequest(GatewayError):
    def __init__(self, code: str, message: str, details=None) -> None:
        headers = {"Allow": "GET, OPTIONS"} if code == "METHOD_NOT_ALLOWED" else None
        super().__init__(400, code, message, details, headers=headers)


class Unauthorized(GatewayError):
    def __init__(self, code: str, message: str, details=None) -> None:
        super().__init__(401, code, message, details,
                         headers={"WWW-Authenticate": 'ApiKey realm="amfe"'})


class NotFound(GatewayError):
    def __init__(self, code: str, message: str, details=None) -> None:
        super().__init__(404, code, message, details)


class RateLimited(GatewayError):
    def __init__(self, code: str, message: str, retry_after: int, details=None,
                 headers: Mapping[str, str] | None = None) -> None:
        details = dict(details or {})
        details.setdefault("retry_after_seconds", int(retry_after))
        merged = {"Retry-After": str(int(retry_after))}
        merged.update(dict(headers or {}))
        super().__init__(429, code, message, details, headers=merged)


class Unavailable(GatewayError):
    def __init__(self, code: str, message: str, details=None) -> None:
        super().__init__(503, code, message, details)


class PolicyViolation(Unavailable):
    """The upstream document cannot be served (admission, raw data, path/URL leak, status)."""


class ApiConfigError(RuntimeError):
    """The gateway configuration is invalid and the process refuses to start (fail-closed)."""


# ------------------------------------------------------------------------------------------- plans
@dataclass(frozen=True)
class Plan:
    name: str
    requests_per_minute: int
    requests_per_day: int

    def as_dict(self) -> dict:
        return {"requests_per_minute": self.requests_per_minute,
                "requests_per_day": self.requests_per_day}


PLANS: dict[str, Plan] = {
    "free": Plan("free", 10, 200),
    "pro": Plan("pro", 60, 5000),
    "internal": Plan("internal", 600, 200_000),
}


@dataclass(frozen=True)
class Client:
    """An authenticated caller; the plaintext key is never retained."""

    key_sha256: str
    client_id: str
    plan: Plan


def parse_keys(spec: str, plans: Mapping[str, Plan] | None = None) -> dict[str, Client]:
    """Parse ``AMFE_API_KEYS`` (``key[:plan]`` items) into ``{sha256(key): Client}``.

    The plaintext key is hashed immediately: quotas, logs and diagnostics only ever see a short
    digest prefix.  Bad specs raise :class:`ApiConfigError` (fail-closed, no silent default key).
    """
    plans = dict(plans or PLANS)
    clients: dict[str, Client] = {}
    for item in re.split(r"[\s,;]+", spec.strip()):
        if not item:
            continue
        key, _, plan_name = item.partition(":")
        key = key.strip()
        plan_name = plan_name.strip().lower() or "free"
        if len(key) < 8:
            raise ApiConfigError(
                "AMFE_API_KEYS: every key must be at least 8 characters long "
                "(short keys are rejected to avoid trivially guessable tokens)")
        if plan_name not in plans:
            raise ApiConfigError(
                f"AMFE_API_KEYS: unknown plan {plan_name!r} for a key; "
                f"available plans: {', '.join(sorted(plans))}")
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        if digest in clients:
            raise ApiConfigError("AMFE_API_KEYS: the same key is listed more than once")
        clients[digest] = Client(key_sha256=digest, client_id=digest[:8], plan=plans[plan_name])
    return clients


class KeyStore:
    """Digest-only key store with constant-time comparison."""

    def __init__(self, clients: Mapping[str, Client] | None = None) -> None:
        self._clients = dict(clients or {})

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None,
                 plans: Mapping[str, Plan] | None = None) -> "KeyStore":
        environ = os.environ if environ is None else environ
        return cls(parse_keys(environ.get("AMFE_API_KEYS", ""), plans))

    def __len__(self) -> int:
        return len(self._clients)

    def __bool__(self) -> bool:
        return bool(self._clients)

    def authenticate(self, token: str) -> Client | None:
        if not token:
            return None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        found = None
        for known, client in self._clients.items():
            if hmac.compare_digest(digest, known):
                found = client
        return found


# ------------------------------------------------------------------------------------------ quotas
@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    code: str = ""
    retry_after: int = 0
    minute_limit: int = 0
    minute_remaining: int = 0
    minute_reset: int = 0
    day_limit: int = 0
    day_remaining: int = 0


class QuotaTracker:
    """In-memory fixed-window quotas per key (single process; documented limitation)."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._minute: dict[tuple[str, int], int] = {}
        self._day: dict[tuple[str, int], int] = {}

    def consume(self, client: Client) -> QuotaDecision:
        now = float(self._clock())
        minute_bucket = int(now // 60)
        day_bucket = int(now // 86400)
        minute_limit = client.plan.requests_per_minute
        day_limit = client.plan.requests_per_day
        minute_reset = max(1, int(60 - (now % 60)))
        day_reset = max(1, int(86400 - (now % 86400)))
        with self._lock:
            self._prune(minute_bucket, day_bucket)
            minute_key = (client.client_id, minute_bucket)
            day_key = (client.client_id, day_bucket)
            minutes_used = self._minute.get(minute_key, 0)
            days_used = self._day.get(day_key, 0)
            if minutes_used >= minute_limit:
                return QuotaDecision(False, "RATE_LIMITED", minute_reset, minute_limit, 0,
                                     minute_reset, day_limit,
                                     max(0, day_limit - days_used))
            if days_used >= day_limit:
                return QuotaDecision(False, "QUOTA_EXCEEDED", day_reset, minute_limit,
                                     max(0, minute_limit - minutes_used), minute_reset,
                                     day_limit, 0)
            self._minute[minute_key] = minutes_used + 1
            self._day[day_key] = days_used + 1
            return QuotaDecision(True, "", 0, minute_limit, minute_limit - minutes_used - 1,
                                 minute_reset, day_limit, day_limit - days_used - 1)

    def _prune(self, minute_bucket: int, day_bucket: int) -> None:
        if len(self._minute) > 4096:
            self._minute = {key: value for key, value in self._minute.items()
                            if key[1] >= minute_bucket}
        if len(self._day) > 4096:
            self._day = {key: value for key, value in self._day.items()
                         if key[1] >= day_bucket}


# -------------------------------------------------------------------------------------------- scrub
def _scrub(value: Any, where: str, depth: int = 0) -> Any:
    """Copy ``value``, refusing raw-data keys, admissions, local paths and external URLs."""
    if depth > 24:
        raise PolicyViolation("POLICY_VIOLATION", f"{where}: document nesting is too deep")
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            lowered = key.lower()
            if lowered in RAW_KEYS or lowered.endswith("_raw") or lowered.startswith("raw_"):
                raise PolicyViolation(
                    "POLICY_VIOLATION",
                    f"{where}: the document contains raw third-party data key {key!r}")
            if lowered == "admission" and raw_value is True:
                raise PolicyViolation(
                    "POLICY_VIOLATION",
                    f"{where}: the document claims admission; the gateway refuses to serve it")
            cleaned[key] = _scrub(raw_value, f"{where}.{key}", depth + 1)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_scrub(item, f"{where}[{index}]", depth + 1) for index, item in enumerate(value)]
    if isinstance(value, str):
        if EXTERNAL_URL_RE.search(value):
            raise PolicyViolation("POLICY_VIOLATION",
                                  f"{where}: the document contains an external URL")
        if DRIVE_PATH_RE.search(value) or POSIX_PATH_RE.search(value):
            raise PolicyViolation("POLICY_VIOLATION",
                                  f"{where}: the document contains an absolute local path")
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise PolicyViolation("POLICY_VIOLATION",
                          f"{where}: the document contains a non-JSON value of type "
                          f"{type(value).__name__}")


def _require_status(document: Mapping[str, Any], where: str) -> str:
    status = document.get("status")
    if status not in STATUS_ORDER:
        raise PolicyViolation(
            "UNSUPPORTED_STATUS",
            f"{where}: status {status!r} is not served by this gateway "
            f"(only {', '.join(SERVED_STATUSES)}); admission is never claimed")
    return str(status)


def _worst_status(statuses: list[str]) -> str:
    if not statuses:
        return SERVED_STATUSES[0]
    return max(statuses, key=lambda name: STATUS_ORDER.get(name, 0))


PRODUCT_TARGET_ALIASES_LOWER = frozenset(alias.lower() for alias in PRODUCT_ALIASES) | {
    PRODUCT_TARGET}


def _is_product_alias(name: str) -> bool:
    """True when ``name`` denotes the product-feed target (even if its feed is missing)."""
    return str(name).strip().lower() in PRODUCT_TARGET_ALIASES_LOWER


# --------------------------------------------------------------------------------------------- feed
@dataclass(frozen=True)
class TargetSpec:
    target: str
    canvas: str  # "api" | "product"
    horizon: int
    allowed_horizons: tuple[int, ...]
    status: str
    commodity: str | None = None
    units: str | None = None
    module_id: str | None = None


@dataclass
class FeedSnapshot:
    """An immutable-at-startup view of the published documents (fail-closed on load errors)."""

    api_dir: Path = DEFAULT_API_DIR
    product_feed_path: Path = DEFAULT_PRODUCT_FEED
    index: dict | None = None
    index_sha256: str | None = None
    index_status: str | None = None
    modules: list = field(default_factory=list)
    scenarios: list = field(default_factory=list)
    scenario_documents: dict = field(default_factory=dict)
    forecasts: dict = field(default_factory=dict)
    targets: dict = field(default_factory=dict)
    product: dict | None = None
    product_document: dict | None = None
    product_sha256: str | None = None
    product_status: str | None = None
    product_state: str = "MISSING"
    error: tuple[str, str] | None = None
    product_error: tuple[str, str] | None = None

    @property
    def canonical_targets(self) -> list[dict]:
        seen: dict[str, TargetSpec] = {}
        for spec in self.targets.values():
            seen.setdefault(spec.target, spec)
        return [seen[name].__dict__ for name in sorted(seen)]


def _read_json(path: Path) -> tuple[Any, str]:
    payload = Path(path).read_bytes()
    return json.loads(payload.decode("utf-8")), hashlib.sha256(payload).hexdigest()


def _load_document(path: Path) -> tuple[Any, str]:
    """Read one published document; a missing/unreadable file fails the whole feed closed."""
    path = Path(path)
    if not path.is_file():
        raise Unavailable("FEED_UNAVAILABLE",
                          "a published contract document is missing; the gateway fails closed")
    try:
        return _read_json(path)
    except (OSError, ValueError) as error:
        raise Unavailable("FEED_INVALID",
                          "a published contract document cannot be read or parsed") from error


def load_feed(api_dir: Path = DEFAULT_API_DIR,
              product_feed: Path = DEFAULT_PRODUCT_FEED,
              *, allow_point: bool = False) -> FeedSnapshot:
    """Load the published documents once; any policy problem disables serving (fail-closed)."""
    snapshot = FeedSnapshot(api_dir=Path(api_dir), product_feed_path=Path(product_feed))
    try:
        _load_api(snapshot)
    except GatewayError as error:
        snapshot.error = (error.code, error.message)
        LOGGER.error("feed rejected: %s: %s", error.code, error.message)
    except Exception as error:  # noqa: BLE001 - fail-closed, details stay in the log
        snapshot.error = ("FEED_INVALID", "the published API documents cannot be loaded")
        LOGGER.exception("feed loading failed: %r", error)
    try:
        _load_product(snapshot, allow_point=allow_point)
    except GatewayError as error:
        snapshot.product_error = (error.code, error.message)
        snapshot.product_state = "INVALID"
        LOGGER.error("product feed rejected: %s: %s", error.code, error.message)
    except Exception as error:  # noqa: BLE001
        snapshot.product_error = ("FEED_INVALID", "the product feed cannot be loaded")
        snapshot.product_state = "INVALID"
        LOGGER.exception("product feed loading failed: %r", error)
    return snapshot


def _load_api(snapshot: FeedSnapshot) -> None:
    index_path = snapshot.api_dir / "index.json"
    index, index_sha = _load_document(index_path)
    if not isinstance(index, dict):
        raise PolicyViolation("FEED_INVALID", "the API index is not a JSON object")
    schema = str(index.get("schema") or "")
    if not schema.startswith("amfe.forecast_api"):
        raise PolicyViolation("FEED_INVALID",
                              "the API index does not declare an amfe.forecast_api schema")
    policy = index.get("policy")
    if not isinstance(policy, dict) or policy.get("no_admission") is not True:
        raise PolicyViolation("POLICY_VIOLATION",
                              "the API index does not declare no_admission=true")
    snapshot.index_status = _require_status(index, "api index")
    public_index = dict(index)
    registry = public_index.get("registry")
    if isinstance(registry, dict):
        registry = dict(registry)
        if registry.get("path"):
            # The registry path is local: publish the file name only, never the workspace path.
            registry["path"] = Path(str(registry["path"])).name
        public_index["registry"] = registry
    snapshot.index = _scrub(public_index, "api index")
    snapshot.index_sha256 = index_sha

    modules_path = snapshot.api_dir / "modules.json"
    if modules_path.is_file():
        modules_document, _ = _load_document(modules_path)
        if not isinstance(modules_document, dict):
            raise PolicyViolation("FEED_INVALID", "the modules document is not a JSON object")
        _require_status(modules_document, "modules document")
        passports = modules_document.get("modules") or []
    else:
        passports = index.get("modules") or []
    cleaned_modules = []
    for passport in passports:
        if not isinstance(passport, dict) or not passport.get("module_id"):
            raise PolicyViolation("FEED_INVALID", "a module passport is malformed")
        status = _require_status(passport, f"module {passport.get('module_id')}")
        document = _scrub(passport, f"module {passport.get('module_id')}")
        document["status"] = status
        document["admission"] = False
        cleaned_modules.append(document)
    if not cleaned_modules:
        raise PolicyViolation("FEED_INVALID", "the module registry export has no passports")
    snapshot.modules = sorted(cleaned_modules, key=lambda item: str(item["module_id"]))

    scenarios: list[dict] = []
    for entry in index.get("scenarios") or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            raise PolicyViolation("FEED_INVALID", "a scenario index entry is malformed")
        scenario_id = str(entry["id"])
        module_id, _, scenario_name = scenario_id.partition("/")
        file_name = str(entry.get("file")
                        or f"scenarios/{module_id}__{scenario_name}.json")
        document, _ = _load_document(snapshot.api_dir / file_name)
        if not isinstance(document, dict):
            raise PolicyViolation("FEED_INVALID", f"scenario {scenario_id} is not a JSON object")
        status = _require_status(document, f"scenario {scenario_id}")
        cleaned = _scrub(document, f"scenario {scenario_id}")
        result = cleaned.get("result") or {}
        scenarios.append({
            "id": scenario_id,
            "module_id": str(cleaned.get("result", {}).get("module_id") or module_id),
            "scenario_id": str(result.get("scenario_id") or scenario_name),
            "description": str(entry.get("description") or ""),
            "horizon_months": int(entry.get("horizon_months")
                                  or result.get("horizon_months") or 0),
            "status": status,
            "units": result.get("units"),
            "digest": str(entry.get("digest") or result.get("digest") or ""),
            "module_version": entry.get("module_version"),
            "module_ref": entry.get("module_ref"),
            "summary": entry.get("summary"),
            "links": {"self": f"/v1/scenarios/{scenario_id}"},
        })
        snapshot.scenario_documents[scenario_id] = cleaned
        snapshot.scenario_documents[scenario_id.replace("/", "__")] = cleaned
    snapshot.scenarios = sorted(scenarios, key=lambda item: item["id"])

    targets: dict[str, TargetSpec] = {}
    for entry in index.get("forecasts") or []:
        if not isinstance(entry, dict) or not entry.get("target"):
            raise PolicyViolation("FEED_INVALID", "a forecast index entry is malformed")
        target = str(entry["target"])
        file_name = str(entry.get("file") or f"forecasts/{target}.json")
        document, _ = _load_document(snapshot.api_dir / file_name)
        if not isinstance(document, dict):
            raise PolicyViolation("FEED_INVALID", f"forecast {target} is not a JSON object")
        status = _require_status(document, f"forecast {target}")
        cleaned = _scrub(document, f"forecast {target}")
        definition = cleaned.get("target_definition") or {}
        horizon = int(definition.get("horizon_months") or 0)
        module_id = str(cleaned.get("module_id") or entry.get("module_id") or "")
        spec = TargetSpec(target=target, canvas="api", horizon=horizon,
                          allowed_horizons=(horizon,) if horizon else (),
                          status=status, commodity=definition.get("commodity"),
                          units=definition.get("units"), module_id=module_id or None)
        snapshot.forecasts[target] = cleaned
        aliases = {target}
        if module_id:
            aliases.add(module_id)
            aliases.add(module_id.replace(".", "_"))
        if module_id == "oil.brent":
            aliases.add("brent")
        for alias in aliases:
            targets[alias] = spec
            targets[alias.lower()] = spec
    if not snapshot.forecasts:
        raise PolicyViolation("FEED_INVALID", "the API index lists no forecast targets")
    snapshot.targets = targets


def _load_product(snapshot: FeedSnapshot, *, allow_point: bool) -> None:
    path = snapshot.product_feed_path
    if not path.is_file():
        snapshot.product_state = "MISSING"
        return
    document, digest = _load_document(path)
    if not isinstance(document, dict):
        raise PolicyViolation("FEED_INVALID", "the product feed is not a JSON object")
    status = _require_status(document, "product feed")
    target = str(document.get("target") or "")
    horizon = document.get("horizon")
    if not target or not isinstance(horizon, int):
        raise PolicyViolation("FEED_INVALID", "the product feed lacks target/horizon")
    projection: dict[str, Any] = {
        "schema": "amfe.forecast_api.live.v1.product",
        "kind": "product_forecast",
        "target": target,
        "horizon": horizon,
        "origin": document.get("origin"),
        "status": status,
        "generated_at": document.get("generated_at"),
        "candidate": document.get("candidate"),
        "data_class": document.get("data_class"),
        "conditional_data_class": bool(document.get("conditional_data_class")),
        "admission": False,
        "point_withheld": not allow_point,
        "no_point_forecast": not allow_point,
        "c4_status": document.get("c4_status"),
        "c6_status": document.get("c6_status"),
        "limitations": list(document.get("limitations") or []),
        "protocol": document.get("protocol"),
        "source_hashes": document.get("source_hashes"),
    }
    quantiles = document.get("quantiles_5_to_95_step_5")
    if isinstance(quantiles, list) and len(quantiles) >= 2:
        projection["interval"] = {
            "quantile_low": 5,
            "quantile_high": 95,
            "low": quantiles[0],
            "high": quantiles[-1],
            "source": "product feed quantiles_5_to_95_step_5",
        }
    if allow_point:
        projection["point"] = document.get("point")
    snapshot.product = _scrub(document, "product feed")
    snapshot.product_document = _scrub(projection, "product projection")
    snapshot.product_status = status
    snapshot.product_sha256 = digest
    snapshot.product_state = "OK"
    spec = TargetSpec(target=PRODUCT_TARGET, canvas="product", horizon=horizon,
                      allowed_horizons=(horizon,), status=status)
    aliases = set(PRODUCT_ALIASES) | {target, PRODUCT_TARGET, PRODUCT_TARGET.upper()}
    for alias in aliases:
        snapshot.targets[alias] = spec
        snapshot.targets[alias.lower()] = spec


# ------------------------------------------------------------------------------------------ gateway
@dataclass
class Response:
    status: int
    payload: dict
    headers: dict = field(default_factory=dict)


def _header(headers: Mapping[str, str] | None, name: str) -> str | None:
    if not headers:
        return None
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value)
    return None


def _normalise_path(path: str) -> str:
    try:
        decoded = unquote(path, errors="strict")
    except UnicodeDecodeError as error:  # pragma: no cover - defensive
        raise BadRequest("INVALID_PATH", "the request path is not valid UTF-8") from error
    if any(ord(char) < 32 for char in decoded) or "\x00" in decoded:
        raise BadRequest("INVALID_PATH", "the request path contains control characters")
    decoded = re.sub(r"/{2,}", "/", decoded)
    if len(decoded) > 1:
        decoded = decoded.rstrip("/") or "/"
    return decoded


def _parse_query(query: str) -> dict[str, str]:
    params = parse_qs(query, keep_blank_values=True, strict_parsing=False)
    if len(params) > MAX_QUERY_PARAMS:
        raise BadRequest("TOO_MANY_PARAMETERS",
                         f"at most {MAX_QUERY_PARAMS} query parameters are supported")
    for name, values in params.items():
        if len(values) > 1:
            raise BadRequest("DUPLICATE_PARAMETER",
                             f"query parameter {name!r} is repeated", {"parameter": name})
    return {name: values[0] for name, values in params.items()}


class ForecastGateway:
    """Pure request/response logic; the HTTP layer is a thin adapter around :meth:`handle`."""

    def __init__(self, feed: FeedSnapshot, keys: KeyStore, *,
                 plans: Mapping[str, Plan] | None = None,
                 quota: QuotaTracker | None = None,
                 allow_point: bool = False,
                 cors_origin: str = "*",
                 clock: Callable[[], float] = time.time) -> None:
        self.feed = feed
        self.keys = keys
        self.plans = dict(plans or PLANS)
        self.quota = quota or QuotaTracker(clock=clock)
        self.allow_point = bool(allow_point)
        self.cors_origin = cors_origin or "*"
        self.policy = {
            "read_only": True,
            "research_only": True,
            "no_admission": True,
            "no_point_forecast": not self.allow_point,
            "no_raw_third_party_data": True,
            "deterministic": True,
            "api_key_required": True,
        }

    # -- public API ------------------------------------------------------------------------
    def handle(self, method: str, request_target: str,
               headers: Mapping[str, str] | None = None) -> Response:
        request_id = uuid.uuid4().hex[:12]
        try:
            response = self._route(method, request_target, headers)
        except GatewayError as error:
            response = self._error_response(error)
            LOGGER.info("request %s %s -> %s %s", method, _redact(request_target),
                        error.status, error.code)
        except Exception:  # noqa: BLE001 - fail-closed, never leak a traceback to clients
            LOGGER.exception("unhandled gateway error for %s", _redact(request_target))
            response = self._error_response(
                Unavailable("INTERNAL_ERROR", "internal error; the gateway fails closed"))
        response.headers.setdefault("X-Request-Id", request_id)
        response.headers.setdefault("X-API-Version", DEFAULT_API_VERSION)
        response.headers.setdefault("X-Gateway-Version", GATEWAY_VERSION)
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        origin = self.cors_origin
        response.headers.setdefault("Access-Control-Allow-Origin", origin)
        response.headers.setdefault("Access-Control-Allow-Methods", "GET, OPTIONS")
        response.headers.setdefault(
            "Access-Control-Allow-Headers", "X-API-Key, Authorization, Content-Type")
        response.headers.setdefault("Access-Control-Expose-Headers",
                                    "X-Request-Id, Retry-After, X-RateLimit-Remaining")
        response.headers.setdefault("Access-Control-Max-Age", "600")
        if origin != "*":
            response.headers.setdefault("Vary", "Origin")
        return response

    def diagnostics(self) -> dict:
        """Configuration/feed summary for ``--check`` (no request handling, no secrets)."""
        if self.feed.error:
            state = "BLOCKED"
        elif not self.keys:
            state = "BLOCKED"
        elif self.feed.product_state != "OK":
            state = "DEGRADED"
        else:
            state = "OK"
        return {
            "schema": SCHEMA,
            "gateway_version": GATEWAY_VERSION,
            "status": state,
            "api_dir": self.feed.api_dir.name,
            "product_feed": self.feed.product_feed_path.name,
            "keys_configured": len(self.keys),
            "allow_point": self.allow_point,
            "cors_origin": self.cors_origin,
            "plans": {name: plan.as_dict() for name, plan in sorted(self.plans.items())},
            "feeds": {
                "api_index": {
                    "state": "OK" if not self.feed.error else "INVALID",
                    "schema": (self.feed.index or {}).get("schema"),
                    "api_version": (self.feed.index or {}).get("api_version"),
                    "generated_at": (self.feed.index or {}).get("generated_at"),
                    "sha256": self.feed.index_sha256,
                    "error": list(self.feed.error) if self.feed.error else None,
                },
                "product_feed": {
                    "state": self.feed.product_state,
                    "status": self.feed.product_status,
                    "sha256": self.feed.product_sha256,
                    "error": list(self.feed.product_error) if self.feed.product_error else None,
                },
            },
            "counts": {"modules": len(self.feed.modules),
                       "scenarios": len(self.feed.scenarios),
                       "targets": len({spec.target for spec in self.feed.targets.values()})},
        }

    # -- routing ---------------------------------------------------------------------------
    def _route(self, method: str, request_target: str,
               headers: Mapping[str, str] | None) -> Response:
        if not isinstance(request_target, str) or not request_target.startswith("/"):
            raise BadRequest("INVALID_PATH", "the request target must be an absolute path")
        if len(request_target) > MAX_REQUEST_TARGET:
            raise BadRequest("REQUEST_TOO_LARGE",
                             f"the request target exceeds {MAX_REQUEST_TARGET} characters")
        raw_path, _, query = request_target.partition("?")
        path = _normalise_path(raw_path)
        method = (method or "").upper()
        if method == "OPTIONS":
            return Response(200, {"schema": SCHEMA, "kind": "preflight", "status": "OK",
                                  "admission": False}, {})
        if method not in ("GET", "HEAD"):
            raise BadRequest("METHOD_NOT_ALLOWED", "only GET and OPTIONS are supported",
                             {"allow": ["GET", "OPTIONS"]})
        if path == "/openapi.json":
            return Response(200, self.openapi(), {})
        if path == "/v1/status":
            return Response(200, self._status(), {})
        if path != "/v1" and not path.startswith("/v1/"):
            raise NotFound("NOT_FOUND", "unknown path", {"paths": self._public_paths()})
        client = self._authenticate(headers)
        decision = self.quota.consume(client)
        quota_headers = {
            "X-RateLimit-Limit": str(decision.minute_limit),
            "X-RateLimit-Remaining": str(decision.minute_remaining),
            "X-RateLimit-Reset": str(decision.minute_reset),
            "X-RateLimit-Day-Limit": str(decision.day_limit),
            "X-RateLimit-Day-Remaining": str(decision.day_remaining),
            "X-Plan": client.plan.name,
        }
        if not decision.allowed:
            raise RateLimited(decision.code,
                              "the key exceeded its quota",
                              decision.retry_after,
                              {"plan": client.plan.name,
                               "requests_per_minute": decision.minute_limit,
                               "requests_per_day": decision.day_limit,
                               "retry_after_seconds": decision.retry_after,
                               "minute_remaining": decision.minute_remaining,
                               "day_remaining": decision.day_remaining},
                              quota_headers)
        if path == "/v1/modules":
            response = self._modules(client)
        elif path == "/v1/scenarios":
            response = self._scenarios(client)
        elif path.startswith("/v1/scenarios/"):
            scenario_id = path[len("/v1/scenarios/"):]
            response = self._scenario(scenario_id, client)
        elif path == "/v1/forecast":
            response = self._forecast(query, client)
        elif path == "/v1":
            raise NotFound("NOT_FOUND", "unknown path", {"paths": self._public_paths()})
        else:
            raise NotFound("NOT_FOUND", "unknown path", {"paths": self._public_paths()})
        response.headers.update(quota_headers)
        LOGGER.info("request %s %s -> %s plan=%s remaining=%s", method,
                    _redact(request_target), response.status, client.plan.name,
                    decision.minute_remaining)
        return response

    def _public_paths(self) -> list[str]:
        return ["/openapi.json", "/v1/status", "/v1/modules", "/v1/scenarios",
                "/v1/scenarios/{scenario_id}", "/v1/forecast?target=&horizon="]

    def _authenticate(self, headers: Mapping[str, str] | None) -> Client:
        if not self.keys:
            raise Unavailable("AUTH_NOT_CONFIGURED",
                              "no API keys are configured; the gateway fails closed")
        token = self._api_key(headers)
        if not token:
            raise Unauthorized("MISSING_API_KEY",
                               "provide the key in X-API-Key or Authorization: Bearer")
        client = self.keys.authenticate(token)
        if client is None:
            raise Unauthorized("INVALID_API_KEY", "the API key is not valid")
        return client

    @staticmethod
    def _api_key(headers: Mapping[str, str] | None) -> str | None:
        for name in ("X-API-Key", "Api-Key"):
            value = _header(headers, name)
            if value:
                return value.strip()
        authorization = _header(headers, "Authorization")
        if authorization:
            scheme, separator, token = authorization.partition(" ")
            if not separator or scheme.strip().lower() != "bearer" or not token.strip():
                raise Unauthorized("INVALID_AUTH_SCHEME",
                                   "the Authorization header must be 'Bearer <key>'")
            return token.strip()
        return None

    # -- documents -------------------------------------------------------------------------
    def _envelope(self, kind: str, status: str, **extra: Any) -> dict:
        document = {
            "schema": SCHEMA,
            "kind": kind,
            "api_version": DEFAULT_API_VERSION,
            "gateway_version": GATEWAY_VERSION,
            "status": status,
            "admission": False,
            "disclaimer": DISCLAIMER,
            "policy": dict(self.policy),
        }
        document.update(extra)
        return document

    def _source(self) -> dict:
        index = self.feed.index or {}
        return {
            "api_index": {
                "schema": index.get("schema"),
                "api_version": index.get("api_version"),
                "generated_at": index.get("generated_at"),
                "sha256": self.feed.index_sha256,
                "counts": index.get("counts"),
            }
        }

    def _status(self) -> dict:
        self._require_feed()
        if not self.keys:
            raise Unavailable("AUTH_NOT_CONFIGURED",
                              "no API keys are configured; the gateway fails closed")
        index = self.feed.index or {}
        statuses = [self.feed.index_status or SERVED_STATUSES[0]]
        statuses += [str(item.get("status")) for item in self.feed.modules]
        statuses += [str(item.get("status")) for item in self.feed.scenarios]
        statuses += [spec.status for spec in self.feed.targets.values()]
        if self.feed.product_status:
            statuses.append(self.feed.product_status)
        spec_by_target: dict[str, TargetSpec] = {}
        for spec in self.feed.targets.values():
            spec_by_target.setdefault(spec.target, spec)
        targets = []
        for name in sorted(spec_by_target):
            spec = spec_by_target[name]
            targets.append({
                "target": spec.target,
                "horizon_months": spec.horizon,
                "allowed_horizons": list(spec.allowed_horizons),
                "status": spec.status,
                "source": "product_feed" if spec.canvas == "product" else "api_contract",
                "module_id": spec.module_id,
                "units": spec.units,
            })
        return self._envelope(
            "status",
            _worst_status(statuses),
            feeds={
                "api_index": {
                    "state": "OK",
                    "schema": index.get("schema"),
                    "api_version": index.get("api_version"),
                    "generated_at": index.get("generated_at"),
                    "sha256": self.feed.index_sha256,
                    "counts": index.get("counts"),
                },
                "product_feed": {
                    "state": self.feed.product_state,
                    "status": self.feed.product_status,
                    "generated_at": (self.feed.product or {}).get("generated_at"),
                    "sha256": self.feed.product_sha256,
                    "target": (self.feed.product or {}).get("target"),
                    "error": list(self.feed.product_error) if self.feed.product_error else None,
                },
            },
            checks={
                "api_index": "OK",
                "modules": "OK" if self.feed.modules else "FAIL",
                "scenarios": "OK" if self.feed.scenarios else "FAIL",
                "product_feed": self.feed.product_state,
                "api_keys": "OK" if self.keys else "FAIL",
            },
            counts={"modules": len(self.feed.modules),
                     "scenarios": len(self.feed.scenarios),
                     "forecasts": len({spec.target for spec in self.feed.targets.values()
                                       if spec.canvas == "api"}),
                     "targets": len(targets),
                     "keys_configured": len(self.keys)},
            limits={name: plan.as_dict() for name, plan in sorted(self.plans.items())},
            targets=targets,
            endpoints=[
                {"method": "GET", "path": "/openapi.json", "auth": False,
                 "description": "OpenAPI description of this gateway"},
                {"method": "GET", "path": "/v1/status", "auth": False,
                 "description": "gateway status, feeds, versions and limits"},
                {"method": "GET", "path": "/v1/modules", "auth": True,
                 "description": "module passports (versions, statuses, limitations, hashes)"},
                {"method": "GET", "path": "/v1/scenarios", "auth": True,
                 "description": "scenario index"},
                {"method": "GET", "path": "/v1/scenarios/{scenario_id}", "auth": True,
                 "description": "one scenario document"},
                {"method": "GET", "path": "/v1/forecast?target=&horizon=", "auth": True,
                 "description": "scenario envelope or product-feed projection for a target"},
            ],
        )

    def _require_feed(self) -> None:
        if self.feed.error:
            raise Unavailable(*self.feed.error)

    def _modules(self, client: Client) -> Response:
        self._require_feed()
        modules = self.feed.modules
        document = self._envelope(
            "modules",
            _worst_status([str(item["status"]) for item in modules]),
            source=self._source(),
            counts={"modules": len(modules)},
            modules=modules,
            links={"self": "/v1/modules", "scenarios": "/v1/scenarios",
                   "openapi": "/openapi.json"},
        )
        return Response(200, document, {})

    def _scenarios(self, client: Client) -> Response:
        self._require_feed()
        document = self._envelope(
            "scenarios",
            _worst_status([str(item["status"]) for item in self.feed.scenarios]),
            source=self._source(),
            counts={"scenarios": len(self.feed.scenarios)},
            scenarios=self.feed.scenarios,
            links={"self": "/v1/scenarios", "modules": "/v1/modules",
                   "openapi": "/openapi.json"},
        )
        return Response(200, document, {})

    def _scenario(self, scenario_id: str, client: Client) -> Response:
        self._require_feed()
        scenario_id = scenario_id.strip()
        if not scenario_id:
            raise NotFound("UNKNOWN_SCENARIO", "no scenario id was provided",
                           {"available_scenarios": self._scenario_ids()})
        document = self.feed.scenario_documents.get(scenario_id)
        if document is None:
            document = self.feed.scenario_documents.get(scenario_id.replace("__", "/"))
        if document is None:
            raise NotFound("UNKNOWN_SCENARIO",
                           f"unknown scenario {scenario_id!r}",
                           {"available_scenarios": self._scenario_ids()})
        status = _require_status(document, f"scenario {scenario_id}")
        identifier = str(document.get("id") or scenario_id.replace("__", "/"))
        response = self._envelope(
            "scenario", status,
            id=identifier,
            source=self._source(),
            data=document,
            links={"self": f"/v1/scenarios/{identifier}", "index": "/v1/scenarios",
                   "modules": "/v1/modules"},
        )
        return Response(200, response, {})

    def _scenario_ids(self) -> list[str]:
        return [str(item["id"]) for item in self.feed.scenarios]

    def _forecast(self, query: str, client: Client) -> Response:
        self._require_feed()
        params = _parse_query(query)
        unknown = sorted(set(params) - {"target", "horizon"})
        if unknown:
            raise BadRequest("UNKNOWN_PARAMETER",
                             f"unknown query parameter(s): {', '.join(unknown)}",
                             {"supported": ["target", "horizon"]})
        raw_target = (params.get("target") or "").strip()
        if not raw_target:
            raise BadRequest("MISSING_PARAMETER", "query parameter 'target' is required",
                             {"required": ["target"],
                              "available_targets": self._target_names()})
        spec = self.feed.targets.get(raw_target) or self.feed.targets.get(raw_target.lower())
        if spec is None and _is_product_alias(raw_target):
            # The target name is known but its feed is missing/invalid: answer 503, not 404,
            # so a client can tell "not published" from "dependency down" (fail-closed).
            if self.feed.product_error:
                raise Unavailable(*self.feed.product_error)
            raise Unavailable("PRODUCT_FEED_UNAVAILABLE",
                              "the product feed is not available; this target fails closed")
        if spec is None:
            raise NotFound("UNKNOWN_TARGET", f"unknown forecast target {raw_target!r}",
                           {"available_targets": self._target_names()})
        horizon = spec.horizon
        raw_horizon = (params.get("horizon") or "").strip()
        if raw_horizon:
            if not re.fullmatch(r"[0-9]{1,4}", raw_horizon):
                raise BadRequest("INVALID_HORIZON",
                                 "horizon must be a non-negative integer (months)",
                                 {"allowed_horizons": list(spec.allowed_horizons)})
            horizon = int(raw_horizon)
        if spec.allowed_horizons and horizon not in spec.allowed_horizons:
            raise BadRequest("UNSUPPORTED_HORIZON",
                             f"horizon {horizon} is not published for target {spec.target!r}",
                             {"horizon": horizon,
                              "allowed_horizons": list(spec.allowed_horizons),
                              "scenarios_endpoint": "/v1/scenarios"})
        links = {
            "self": f"/v1/forecast?target={spec.target}&horizon={horizon}",
            "scenarios": "/v1/scenarios",
            "modules": "/v1/modules",
            "status": "/v1/status",
            "openapi": "/openapi.json",
        }
        query_document = {"target": spec.target, "horizon": horizon}
        if spec.canvas == "product":
            if self.feed.product_error:
                raise Unavailable(*self.feed.product_error)
            if self.feed.product_document is None:
                raise Unavailable("PRODUCT_FEED_UNAVAILABLE",
                                  "the product feed is not available; this target fails closed")
            document = self._envelope(
                "forecast", spec.status,
                query=query_document,
                data=self.feed.product_document,
                source={"product_feed": {
                    "schema_version": (self.feed.product or {}).get("schema_version"),
                    "status": self.feed.product_status,
                    "generated_at": (self.feed.product or {}).get("generated_at"),
                    "sha256": self.feed.product_sha256,
                    "candidate": (self.feed.product or {}).get("candidate"),
                }},
                links=links,
            )
            return Response(200, document, {})
        data = self.feed.forecasts[spec.target]
        document = self._envelope(
            "forecast", spec.status,
            query=query_document,
            data=data,
            source=self._source(),
            links=links,
        )
        return Response(200, document, {})

    def _target_names(self) -> list[str]:
        return sorted({spec.target for spec in self.feed.targets.values()})

    # -- errors ----------------------------------------------------------------------------
    def _error_response(self, error: GatewayError) -> Response:
        payload = {
            "schema": SCHEMA,
            "kind": "error",
            "api_version": DEFAULT_API_VERSION,
            "gateway_version": GATEWAY_VERSION,
            "status": "ERROR",
            "admission": False,
            "code": error.code,
            "error": error.message,
        }
        if error.details:
            payload["details"] = error.details
        return Response(error.status, payload, dict(error.headers))

    # -- OpenAPI ----------------------------------------------------------------------------
    def openapi(self) -> dict:
        """A static OpenAPI 3.0.3 description (no feed numbers, so it is always available)."""
        error_ref = {"$ref": "#/components/schemas/Error"}
        document = {
            "openapi": OPENAPI_VERSION,
            "info": {
                "title": "AMFE live Forecast API gateway (research-only)",
                "version": GATEWAY_VERSION,
                "description": (
                    "Read-only HTTP gateway over the published AMFE forecast contract. Research "
                    "use only: no admission, no raw third-party observations, no local paths. "
                    "Every response carries status in {RESEARCH_ONLY, CANDIDATE} and "
                    "admission=false. Quotas are per API key; keys travel in X-API-Key or "
                    "Authorization: Bearer."
                ),
                "license": {"name": "Research use only; see docs/08 licence and disclaimer"},
            },
            "servers": [{"url": "/", "description": "same-origin gateway"}],
            "security": [{"ApiKeyAuth": []}],
            "tags": [
                {"name": "meta", "description": "status and description (public)"},
                {"name": "modules", "description": "module passports"},
                {"name": "scenarios", "description": "published scenarios"},
                {"name": "forecast", "description": "forecast envelopes / product projection"},
            ],
            "paths": {
                "/v1/status": {
                    "get": {
                        "tags": ["meta"], "operationId": "getStatus",
                        "summary": "Gateway status, feeds, versions, targets and limits",
                        "security": [],
                        "responses": {
                            "200": {"description": "status document",
                                    "content": {"application/json": {"schema": {
                                        "$ref": "#/components/schemas/Status"}}}},
                            "503": {"description": "feed unavailable or keys not configured",
                                    "content": {"application/json": {"schema": error_ref}}},
                        },
                    }
                },
                "/v1/modules": {
                    "get": {
                        "tags": ["modules"], "operationId": "listModules",
                        "summary": "Module passports (registry metadata, not an admission)",
                        "responses": {
                            "200": {"description": "module passports",
                                    "content": {"application/json": {"schema": {
                                        "$ref": "#/components/schemas/Modules"}}}},
                            "401": {"description": "missing or invalid API key",
                                    "content": {"application/json": {"schema": error_ref}}},
                            "429": {"description": "quota exceeded",
                                    "content": {"application/json": {"schema": error_ref}}},
                            "503": {"description": "gateway fails closed",
                                    "content": {"application/json": {"schema": error_ref}}},
                        },
                    }
                },
                "/v1/scenarios": {
                    "get": {
                        "tags": ["scenarios"], "operationId": "listScenarios",
                        "summary": "Index of published scenarios",
                        "responses": {
                            "200": {"description": "scenario index",
                                    "content": {"application/json": {"schema": {
                                        "$ref": "#/components/schemas/Scenarios"}}}},
                            "401": {"description": "missing or invalid API key",
                                    "content": {"application/json": {"schema": error_ref}}},
                            "503": {"description": "gateway fails closed",
                                    "content": {"application/json": {"schema": error_ref}}},
                        },
                    }
                },
                "/v1/scenarios/{scenario_id}": {
                    "get": {
                        "tags": ["scenarios"], "operationId": "getScenario",
                        "summary": "One scenario document",
                        "parameters": [{
                            "name": "scenario_id", "in": "path", "required": True,
                            "description": "module_id/scenario_id (module_id__scenario_id also accepted)",
                            "schema": {"type": "string",
                                       "example": "oil.brent/hormuz_closed_6m"},
                        }],
                        "responses": {
                            "200": {"description": "scenario document",
                                    "content": {"application/json": {"schema": {
                                        "$ref": "#/components/schemas/Scenario"}}}},
                            "401": {"description": "missing or invalid API key",
                                    "content": {"application/json": {"schema": error_ref}}},
                            "404": {"description": "unknown scenario",
                                    "content": {"application/json": {"schema": error_ref}}},
                            "503": {"description": "gateway fails closed",
                                    "content": {"application/json": {"schema": error_ref}}},
                        },
                    }
                },
                "/v1/forecast": {
                    "get": {
                        "tags": ["forecast"], "operationId": "getForecast",
                        "summary": "Scenario envelope (or product-feed projection) for a target",
                        "parameters": [
                            {"name": "target", "in": "query", "required": True,
                             "description": "target id (oil_brent, cpi_yoy and documented aliases)",
                             "schema": {"type": "string", "example": "oil_brent"}},
                            {"name": "horizon", "in": "query", "required": False,
                             "description": "published horizon in months; must match the target",
                             "schema": {"type": "integer", "minimum": 0, "example": 6}},
                        ],
                        "responses": {
                            "200": {"description": "forecast envelope",
                                    "content": {"application/json": {"schema": {
                                        "$ref": "#/components/schemas/Forecast"}}}},
                            "400": {"description": "missing/invalid/unsupported parameter",
                                    "content": {"application/json": {"schema": error_ref}}},
                            "401": {"description": "missing or invalid API key",
                                    "content": {"application/json": {"schema": error_ref}}},
                            "404": {"description": "unknown target",
                                    "content": {"application/json": {"schema": error_ref}}},
                            "429": {"description": "quota exceeded",
                                    "content": {"application/json": {"schema": error_ref}}},
                            "503": {"description": "feed unavailable or policy violation",
                                    "content": {"application/json": {"schema": error_ref}}},
                        },
                    }
                },
            },
            "components": {
                "securitySchemes": {
                    "ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key",
                                   "description": "Authorization: Bearer <key> is also accepted"}
                },
                "schemas": {
                    "Envelope": {
                        "type": "object",
                        "required": ["schema", "kind", "api_version", "status", "admission"],
                        "properties": {
                            "schema": {"type": "string", "enum": [SCHEMA]},
                            "kind": {"type": "string"},
                            "api_version": {"type": "string", "enum": [DEFAULT_API_VERSION]},
                            "gateway_version": {"type": "string"},
                            "status": {"type": "string", "enum": list(SERVED_STATUSES)},
                            "admission": {"type": "boolean", "enum": [False]},
                            "disclaimer": {"type": "string"},
                            "policy": {"type": "object",
                                       "additionalProperties": {"type": "boolean"}},
                        },
                    },
                    "Status": {"allOf": [{"$ref": "#/components/schemas/Envelope"},
                                         {"type": "object",
                                          "properties": {"feeds": {"type": "object"},
                                                         "checks": {"type": "object"},
                                                         "counts": {"type": "object"},
                                                         "limits": {"type": "object"},
                                                         "targets": {"type": "array",
                                                                     "items": {
                                                                         "type": "object"}},
                                                         "endpoints": {"type": "array",
                                                                       "items": {
                                                                           "type": "object"}}},
                                          "required": ["feeds", "checks", "counts", "limits"]}]},
                    "Modules": {"allOf": [{"$ref": "#/components/schemas/Envelope"},
                                          {"type": "object",
                                           "properties": {
                                               "modules": {"type": "array", "items": {
                                                   "$ref": "#/components/schemas/Module"}},
                                               "counts": {"type": "object"},
                                               "source": {"type": "object"}},
                                           "required": ["modules", "counts"]}]},
                    "Module": {"type": "object",
                               "required": ["module_id", "version", "status", "admission"],
                               "properties": {"module_id": {"type": "string"},
                                              "version": {"type": "string"},
                                              "status": {"type": "string",
                                                         "enum": list(SERVED_STATUSES)},
                                              "admission": {"type": "boolean", "enum": [False]},
                                              "kind": {"type": "string"},
                                              "limitations": {"type": "array",
                                                              "items": {"type": "string"}},
                                              "code_sha256": {"type": "string"},
                                              "data_status": {"type": "string"}}},
                    "Scenarios": {"allOf": [{"$ref": "#/components/schemas/Envelope"},
                                            {"type": "object",
                                             "properties": {
                                                 "scenarios": {"type": "array", "items": {
                                                     "$ref": "#/components/schemas/Scenario"}},
                                                 "counts": {"type": "object"}},
                                             "required": ["scenarios", "counts"]}]},
                    "Scenario": {"type": "object",
                                 "required": ["id", "status", "admission"],
                                 "properties": {"id": {"type": "string"},
                                                "module_id": {"type": "string"},
                                                "scenario_id": {"type": "string"},
                                                "horizon_months": {"type": "integer"},
                                                "status": {"type": "string",
                                                           "enum": list(SERVED_STATUSES)},
                                                "admission": {"type": "boolean", "enum": [False]},
                                                "digest": {"type": "string"},
                                                "data": {"type": "object"}}},
                    "Forecast": {"allOf": [{"$ref": "#/components/schemas/Envelope"},
                                           {"type": "object",
                                            "properties": {"query": {"type": "object"},
                                                           "data": {"type": "object"},
                                                           "source": {"type": "object"},
                                                           "links": {"type": "object"}},
                                            "required": ["query", "data"]}]},
                    "Error": {"type": "object",
                              "required": ["schema", "kind", "status", "code", "error",
                                           "admission"],
                              "properties": {"schema": {"type": "string"},
                                             "kind": {"type": "string", "enum": ["error"]},
                                             "status": {"type": "string", "enum": ["ERROR"]},
                                             "code": {"type": "string"},
                                             "error": {"type": "string"},
                                             "admission": {"type": "boolean", "enum": [False]},
                                             "details": {"type": "object"}}},
                },
            },
            "x-schema": SCHEMA,
            "x-api-version": DEFAULT_API_VERSION,
            "x-served-statuses": list(SERVED_STATUSES),
            "x-admission": False,
            "x-auth": {"header": "X-API-Key", "alternative": "Authorization: Bearer <key>"},
            "x-quota-headers": ["X-RateLimit-Limit", "X-RateLimit-Remaining",
                                "X-RateLimit-Reset", "X-RateLimit-Day-Limit",
                                "X-RateLimit-Day-Remaining", "Retry-After"],
        }
        return _scrub(document, "openapi")


def _redact(request_target: str) -> str:
    """Never log a key: keep the path, drop the query string."""
    return str(request_target).split("?", 1)[0][:200]


# -------------------------------------------------------------------------------------------- HTTP
class GatewayRequestHandler(BaseHTTPRequestHandler):
    """Thin HTTP adapter: JSON in, JSON out, CORS, no HTML error pages."""

    server_version = f"AMFE-Forecast-Gateway/{GATEWAY_VERSION}"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch("OPTIONS")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length > 0:
            self.rfile.read(min(length, 65536))
        gateway: ForecastGateway = self.server.gateway  # type: ignore[attr-defined]
        response = gateway.handle(method, self.path, dict(self.headers.items()))
        body = json.dumps(response.payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
        self.send_response(response.status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        LOGGER.debug("http %s - %s", self.address_string(), format % args)


class GatewayServer(ThreadingHTTPServer):
    """Threaded HTTP server carrying the gateway instance."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], gateway: ForecastGateway,
                 handler_class: type[BaseHTTPRequestHandler] = GatewayRequestHandler) -> None:
        self.gateway = gateway
        super().__init__(address, handler_class)

    def server_bind(self) -> None:
        super().server_bind()
        self.server_name = "amfe-forecast-gateway"


def create_server(gateway: ForecastGateway, host: str = DEFAULT_HOST,
                  port: int = DEFAULT_PORT) -> GatewayServer:
    """Bind the gateway to ``host:port`` (``port=0`` picks a free port)."""
    return GatewayServer((host, port), gateway)


# --------------------------------------------------------------------------------------------- config
def _env_flag(value: str | None, *, default: bool = False) -> bool:
    if value is None or not str(value).strip():
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def build_gateway(api_dir: Path = DEFAULT_API_DIR,
                  product_feed: Path = DEFAULT_PRODUCT_FEED,
                  *, environ: Mapping[str, str] | None = None,
                  plans: Mapping[str, Plan] | None = None,
                  quota: QuotaTracker | None = None,
                  allow_point: bool | None = None,
                  cors_origin: str | None = None,
                  clock: Callable[[], float] = time.time) -> ForecastGateway:
    """Build a gateway from the environment (``AMFE_API_KEYS`` etc.) and the published feeds."""
    environ = dict(os.environ if environ is None else environ)
    plans = dict(plans or PLANS)
    allow = (_env_flag(environ.get("AMFE_API_ALLOW_POINT")) if allow_point is None
             else bool(allow_point))
    origin = cors_origin if cors_origin is not None else (
        environ.get("AMFE_API_CORS_ORIGIN") or "*")
    keys = KeyStore.from_env(environ, plans=plans)
    feed = load_feed(Path(api_dir), Path(product_feed), allow_point=allow)
    return ForecastGateway(feed, keys, plans=plans, quota=quota, allow_point=allow,
                           cors_origin=origin, clock=clock)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AMFE live Forecast API gateway (stdlib http.server, read-only)")
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="TCP port (0 picks a free port)")
    parser.add_argument("--api-dir", default=str(DEFAULT_API_DIR),
                        help="directory of the static contract (default build/api)")
    parser.add_argument("--product-feed", default=str(DEFAULT_PRODUCT_FEED),
                        help="product feed JSON (default build/product/forecast_latest.json)")
    parser.add_argument("--cors-origin", default=None,
                        help="Access-Control-Allow-Origin (default '*', or AMFE_API_CORS_ORIGIN)")
    parser.add_argument("--allow-point", action="store_true", default=None,
                        help="serve the product-feed point estimate (default: withheld)")
    parser.add_argument("--check", action="store_true",
                        help="print diagnostics and exit without starting the server")
    parser.add_argument("--quiet", action="store_true", help="log warnings only")
    parser.add_argument("--version", action="store_true", help="print the gateway version")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"amfe.forecast_api_server {GATEWAY_VERSION} ({SCHEMA})")
        return 0
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        gateway = build_gateway(Path(args.api_dir), Path(args.product_feed),
                                cors_origin=args.cors_origin,
                                allow_point=args.allow_point)
    except ApiConfigError as error:
        print(json.dumps({"status": "BLOCKED", "error": str(error)}, ensure_ascii=True),
              file=sys.stderr)
        return 2
    if args.check:
        summary = gateway.diagnostics()
        print(json.dumps(summary, ensure_ascii=True, sort_keys=True, indent=2))
        return 0 if summary["status"] in ("OK", "DEGRADED") else 2
    server = create_server(gateway, args.host, args.port)
    host, port = server.server_address[0], server.server_address[1]
    print(json.dumps({"status": gateway.diagnostics()["status"], "url": f"http://{host}:{port}",
                      "schema": SCHEMA, "version": GATEWAY_VERSION}, ensure_ascii=True),
          flush=True)
    if not gateway.keys:
        LOGGER.warning("AMFE_API_KEYS is empty: every data endpoint answers 503 (fail-closed)")
    if gateway.feed.error:
        LOGGER.warning("the API feed is not servable: %s", gateway.feed.error[0])
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        LOGGER.info("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
