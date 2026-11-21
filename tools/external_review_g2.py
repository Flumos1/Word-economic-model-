#!/usr/bin/env python3
"""Independent (adversarial) review of the G2 bridges, backtest, ABM->SFC and Forecast API.

Wave 23 (G3), docs/160.  The reviewer does not trust the G2 documents: every number a document
claims is re-derived from the artifact it cites, every artifact hash is recomputed, and the
claims about scope (price vs quantity, assumptions, no point forecast, no admission, no raw data)
are checked structurally and by a negation-aware text scan.

Tracks reviewed (docs 152-156):

  T1 market_bridge   price of oil -> Leontief prices in the MRIO table (docs/152)
  T2 agri_bridge     soy/meal scenario -> food/animal prices + Africa (docs/153)
  T3 oil_backtest    three historical oil episodes vs a naive baseline (docs/155)
  T4 abm_sfc         agentic ledger -> SFC aggregates / invariants (docs/154)
  T5 forecast_api    static Forecast API contract and its envelopes (docs/156)

Checks:

  G1 artifacts_hashes   every cited artifact exists, its SHA-256/bytes are recomputed and compared
                        with the values the documents assert (stale hashes are findings)
  G2 docs_vs_json       every number in a document is compared with the artifact it describes
                        (decimal-comma aware, explicit tolerances); missing/mismatched numbers are
                        findings, never silently accepted
  G3 price_vs_quantity  price models must not claim output/volume effects; the price/quantity
                        separation blocks must exist and be consistent (agri bridge included)
  G4 assumptions        assumptions are explicitly declared (source classes, bucket shares,
                        assumption lists, API policy)
  G5 no_point_forecast  no point forecast and no admission claim in JSON or docs (negation-aware)
  G6 fail_closed_tests  the tools exist and their --help works; the test files exist and their test
                        counts match what the documents claim; fail-closed behaviour is documented
  G7 limitations        every track states its limitations (docs section + JSON lists/disclaimers)
  G8 no_raw_data        no third-party raw data is tracked by git and no artifact embeds long raw
                        numeric series (aggregates only)
  G9 determinism        the ABM demo is re-run twice and must be byte-identical; the review document
                        itself carries no clock field and is deterministic
  R  review_fail_closed a missing/corrupt required artifact means exit 2 and nothing is written

Output: ``build/external_review_g2/review.json`` + ``review.md`` (deterministic, no timestamp).
Exit codes: 0 = no BLOCKER/HIGH finding, 1 = at least one FAIL finding (no sign-off), 2 = the
review itself cannot run because an input is missing/broken (fail-closed, nothing written).

CLI::

    python tools/external_review_g2.py
    python tools/external_review_g2.py --no-abm --no-help --json
    python tools/external_review_g2.py --out build/external_review_g2 --generated-at 2026-11-16T12:00:00Z
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "AMFE-EXTERNAL-REVIEW-G2/1.0.0"
DEFAULT_OUT = ROOT / "build" / "external_review_g2"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SEVERITIES = ("BLOCKER", "HIGH", "MEDIUM", "LOW", "INFO")
NEGATIONS = ("не ", "нет", "без ", "вне ", "not ", "недопуст", "не является", "не является",
             "запрещ", "нельзя", "cannot", "no ", "≠", "не смешив")
FORBIDDEN_CLAIMS = (
    (re.compile(r"admission\s*[:=]\s*true", re.I), "admission=true claim"),
    (re.compile(r"допуск\w*\s+присвоен"), "admission assigned claim"),
    (re.compile(r"модель\s+допущена"), "model admitted claim"),
    (re.compile(r"гарантирован\w*\s+(?:доходност|прибыл)"), "guaranteed return claim"),
    (re.compile(r"точечн\w*\s+прогноз"), "point-forecast claim"),
    (re.compile(r"recommend\w*\s+(?:buy|sell)", re.I), "trading recommendation claim"),
    (re.compile(r"рекоменду\w*\s+покупать"), "trading recommendation claim"),
)

DOCS = {
    "price_bridge": "docs/152_Мост_цена_MRIO.md",
    "agri_bridge": "docs/153_Мост_агро_MRIO.md",
    "oil_backtest": "docs/155_Бэктест_нефти.md",
    "abm_sfc": "docs/154_ABM_SFC_интеграция.md",
    "forecast_api": "docs/156_Forecast_API_и_рыночный_пакет.md",
}
TRACK_TITLES = {
    "price_bridge": "T1 price -> MRIO bridge (docs/152)",
    "agri_bridge": "T2 agri/soy -> MRIO bridge (docs/153)",
    "oil_backtest": "T3 oil backtest 1990/2019/2022 (docs/155)",
    "abm_sfc": "T4 ABM -> SFC accounts bridge (docs/154)",
    "forecast_api": "T5 Forecast API static contract (docs/156)",
}
ARTIFACTS = {
    "price_bridge_report": "build/market_bridge/bridge_report.json",
    "price_bridge_summary": "build/market_bridge/bridge_summary.md",
    "agri_bridge_report": "build/agri_bridge/agri_bridge.json",
    "agri_bridge_markdown": "build/agri_bridge/agri_bridge.md",
    "oil_backtest_report": "build/market_backtest/report.json",
    "oil_backtest_markdown": "build/market_backtest/report.md",
    "api_index": "build/api/index.json",
    "api_openapi": "build/api/openapi.json",
    "api_forecast": "build/api/forecasts/oil_brent.json",
}
REQUIRED_TOOLS = {
    "price_bridge": "tools/market_bridge.py",
    "agri_bridge": "tools/agri_bridge.py",
    "oil_backtest": "tools/oil_backtest.py",
    "abm_sfc": "tools/agentic_accounts_demo.py",
    "forecast_api": "tools/forecast_api.py",
}
TEST_FILES = {
    "price_bridge": "tests/test_market_bridge.py",
    "agri_bridge": "tests/test_agri_bridge.py",
    "oil_backtest": "tests/test_markets_backtest.py",
    "abm_sfc": "tests/test_agentic_accounts.py",
    "forecast_api": "tests/test_forecast_api.py",
    "market_client_pack": "tests/test_market_client_pack.py",
}
MAX_NUMERIC_LEAF = 200

__all__ = ["ReviewError", "build_review", "render_markdown", "write_review", "main", "SCHEMA",
           "ARTIFACTS", "DOCS", "TEST_FILES"]


class ReviewError(RuntimeError):
    """A required input is missing or unreadable: fail closed (exit 2, nothing written)."""


# ------------------------------------------------------------------------------------ primitives
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, label: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ReviewError(f"{label} is missing: {path}") from None
    except (OSError, json.JSONDecodeError) as error:
        raise ReviewError(f"{label} is unreadable: {path}: {error}") from None


def _read_text(path: Path, label: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ReviewError(f"{label} is missing: {path}") from None
    except OSError as error:
        raise ReviewError(f"{label} is unreadable: {path}: {error}") from None


def _finding(check: str, track: str, severity: str, message: str,
             evidence: dict | None = None) -> dict:
    if severity not in SEVERITIES:
        raise ValueError(f"unknown severity {severity!r}")
    digest = hashlib.sha1(message.encode("utf-8")).hexdigest()[:8]
    return {"id": f"{check}-{track}-{severity}-{digest}", "check": check, "track": track,
            "severity": severity, "message": message, "evidence": evidence or {}}


def _check(check_id: str, title: str, findings: list, evidence: dict) -> dict:
    if any(item["severity"] in ("BLOCKER", "HIGH") for item in findings):
        status = "FAIL"
    elif findings:
        status = "WARN"
    else:
        status = "PASS"
    return {"id": check_id, "title": title, "status": status, "findings": findings,
            "evidence": evidence}


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalise_number(token: str) -> float | None:
    cleaned = token.strip().replace("\u2212", "-").replace("\u2013", "-").replace(" ", "")
    cleaned = cleaned.replace(",", ".").rstrip(".")
    if cleaned in ("", "-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _doc_number(doc: str, patterns: tuple[str, ...]) -> float | None:
    for pattern in patterns:
        match = re.search(pattern, doc, re.S)
        if match is None or match.lastindex is None:
            continue
        value = _normalise_number(match.group(1))
        if value is not None:
            return value
    return None


def _same_value(claimed: float, observed: float, rel: float = 1e-9, abs_tol: float = 0.0) -> bool:
    if claimed is None or observed is None:
        return False
    if not math.isfinite(claimed) or not math.isfinite(observed):
        return claimed == observed
    return abs(claimed - observed) <= max(abs_tol, rel * max(1.0, abs(observed)))


@dataclass(frozen=True)
class Claim:
    """One documented number, its doc patterns and the artifact value it claims."""

    label: str
    patterns: tuple[str, ...]
    getter: object          # callable(json_document) -> float | str | None
    rel: float = 1e-9
    abs_tol: float = 0.0
    required: bool = True
    severity: str = "HIGH"


# ------------------------------------------------------------------ documented artifact claims
#: ``(artifact name, document regex, track, sha-256 group, bytes group)``
_DOC_ARTIFACT_PATTERNS = (
    ("price_bridge_report",
     r"`build/market_bridge/bridge_report\.json`\s*\(SHA-256\s+`([0-9a-f]{8,64})…?`[^)]*?"
     r"([\d\s\u00a0]+)\s*байт", "price_bridge", 1, 2),
    ("oil_backtest_report",
     r"`build/market_backtest/report\.json`\s*\|\s*([\d\s\u00a0]+)\s*Б\s*\|\s*"
     r"`([0-9a-f]{64})`", "oil_backtest", 2, 1),
    ("oil_backtest_markdown",
     r"`build/market_backtest/report\.md`\s*\|\s*([\d\s\u00a0]+)\s*Б\s*\|\s*"
     r"`([0-9a-f]{64})`", "oil_backtest", 2, 1),
)


def _parse_artifact_claims(docs: dict) -> dict:
    """Extract the SHA-256 prefix and byte size each document cites for an artifact.

    Parsing the claims from the documents (instead of hard-coding them here) is what makes the
    hash check durable: a hard-coded expectation rots as soon as the artifact is regenerated and
    then silently stops protecting the review.  A document that does not cite an artifact is
    simply not checked; docs/160 §7 records this limit.
    """
    claims = {}
    for name, pattern, track, sha_group, bytes_group in _DOC_ARTIFACT_PATTERNS:
        match = re.search(pattern, docs.get(track) or "")
        if not match:
            continue
        claims[ARTIFACTS[name]] = {
            "sha256_prefix": match.group(sha_group),
            "bytes": int(re.sub(r"\D", "", match.group(bytes_group))),
            "track": track,
        }
    return claims


# --------------------------------------------------------------------------------- G1 artifacts
def check_artifacts(repo: Path, docs_claims: dict) -> dict:
    findings, evidence = [], {}
    rows = {}
    for name, relative in ARTIFACTS.items():
        path = repo / relative
        row = {"path": relative, "exists": path.is_file()}
        if not row["exists"]:
            findings.append(_finding("G1", name.split("_")[0], "BLOCKER",
                                     f"required G2 artifact is missing: {relative}",
                                     {"artifact": name}))
            rows[name] = row
            continue
        row.update({"sha256": sha256_file(path), "bytes": path.stat().st_size})
        claimed = (docs_claims.get("artifact_hashes") or {}).get(relative)
        if claimed:
            row["claimed"] = claimed
            prefix = str(claimed.get("sha256_prefix", ""))
            if prefix and not row["sha256"].startswith(prefix):
                findings.append(_finding(
                    "G1", claimed.get("track", name), "MEDIUM",
                    f"{relative}: the document cites SHA-256 {prefix}… but the artifact is "
                    f"{row['sha256'][:16]}… (artifact regenerated after the document?)",
                    {"path": relative, "claimed_prefix": prefix,
                     "observed": row["sha256"], "claimed_bytes": claimed.get("bytes"),
                     "observed_bytes": row["bytes"]}))
            claimed_bytes = claimed.get("bytes")
            if claimed_bytes is not None and int(claimed_bytes) != row["bytes"]:
                findings.append(_finding(
                    "G1", claimed.get("track", name), "LOW",
                    f"{relative}: the document cites {claimed_bytes} bytes, the artifact has "
                    f"{row['bytes']} bytes", {"path": relative,
                                              "claimed_bytes": claimed_bytes,
                                              "observed_bytes": row["bytes"]}))
        rows[name] = row
    # API index self-consistency: every listed file hash/bytes must match the file on disk
    try:
        index = _read_json(repo / ARTIFACTS["api_index"], "API index")
    except ReviewError:
        index = None
    if index is not None:
        api_rows = []
        for entry in index.get("files", []):
            relative = "build/api/" + str(entry.get("path"))
            path = repo / relative
            row = {"path": relative, "exists": path.is_file()}
            if path.is_file():
                row.update({"sha256": sha256_file(path), "bytes": path.stat().st_size,
                            "claimed_sha256": entry.get("sha256"),
                            "claimed_bytes": entry.get("bytes")})
                if row["sha256"] != entry.get("sha256") or row["bytes"] != entry.get("bytes"):
                    findings.append(_finding(
                        "G1", "forecast_api", "HIGH",
                        f"{relative}: the API index lists a different SHA-256/bytes than the file",
                        {"claimed": entry.get("sha256"), "observed": row["sha256"],
                         "claimed_bytes": entry.get("bytes"), "observed_bytes": row["bytes"]}))
            else:
                findings.append(_finding("G1", "forecast_api", "BLOCKER",
                                         f"an API index entry points to a missing file: {relative}"))
            api_rows.append(row)
        evidence["api_files"] = api_rows
    evidence["artifacts"] = rows
    evidence["total"] = len(ARTIFACTS)
    evidence["present"] = sum(1 for row in rows.values() if row["exists"])
    return _check("G1", "artifact hashes/bytes recomputed and compared with the documents",
                  findings, evidence)


# ---------------------------------------------------------------------------------- G2 numbers
def _price_bridge_claims() -> tuple:
    def result(key: str, field: str):
        return lambda d: d["price_results"][key]["variants"]["0.6"]["results"][field]

    def check(key: str, field: str):
        return lambda d: d["price_results"][key]["variants"]["0.6"]["checks"][field]

    def dataset(key: str, field: str):
        return lambda d: d["price_results"][key]["dataset"][field]

    def variant_result(pass_through: str, key: str, field: str):
        return lambda d: d["price_results"][key]["variants"][pass_through]["results"][field]

    return (
        Claim("Brent peak", (r"пик\s+([\d.,]+)\s*USD/bbl",),
              lambda d: d["market_shock"]["price_peak_usd_bbl"], rel=1e-3),
        Claim("Brent delta USD", (r"\+([\d.,]+)\s*USD/bbl",),
              lambda d: d["market_shock"]["brent_delta_usd"], rel=1e-3),
        Claim("Brent delta %", (r"\*\*\+([\d.,]+)%\*\*",),
              lambda d: 100.0 * d["market_shock"]["brent_delta_pct"], rel=2e-3),
        Claim("FIGARO aggregate index (pass-through 0.6)",
              (r"Агрегатный индекс Δp, pass-through 0,6 \| \*\*\+([\d.,]+)%\*\*",),
              result("figaro", "aggregate_price_index_pct"), rel=1e-4),
        Claim("OECD aggregate index (pass-through 0.6)",
              (r"81 × 50 = 4050[\s\S]{0,800}?Агрегатный индекс Δp, pass-through 0,6 \| "
               r"\*\*\+([\d.,]+)%\*\*",),
              result("oecd_2025", "aggregate_price_index_pct"), rel=1e-4),
        Claim("FIGARO max delta price",
              (r"Максимум Δp \| \*\*\+([\d.,]+)%\*\* \(RS",),
              result("figaro", "max_delta_price_pct"), rel=1e-4),
        Claim("FIGARO median delta price", (r"Медиана Δp \| \+([\d.,]+)%",),
              result("figaro", "median_delta_price_pct"), rel=1e-4),
        Claim("FIGARO spectral radius", (r"ρ\(A\) \| \*\*([\d.,]+)\*\*",),
              check("figaro", "spectral_radius"), rel=1e-6),
        Claim("OECD 2025 spectral radius",
              (r"81 × 50 = 4050[\s\S]{0,800}?ρ\(A\) \| \*\*([\d.,]+)\*\*",),
              check("oecd_2025", "spectral_radius"), rel=1e-6),
        Claim("FIGARO sectors in the price model", (r"вошли \*\*(\d+)\*\*",),
              dataset("figaro", "sectors"), rel=0.0),
        Claim("pass-through 0.3 aggregate FIGARO",
              (r"Δp при pass-through 0,3 / 1,0 \| \+([\d.,]+)%",),
              variant_result("0.3", "figaro", "aggregate_price_index_pct"), rel=1e-4),
        Claim("pass-through 1.0 aggregate FIGARO",
              (r"Δp при pass-through 0,3 / 1,0 \| \+[\d.,]+% / \+([\d.,]+)%",),
              variant_result("1.0", "figaro", "aggregate_price_index_pct"), rel=1e-4),
        Claim("documented run time", (r"Прогон обоих миров — ([\d,]+) с",),
              lambda d: d.get("seconds"), rel=0.05, required=False, severity="LOW"),
    )


def _agri_bridge_claims() -> tuple:
    return (
        Claim("soybean range low", (r"соя \*\*420 → ([\d.]+)–",),
              lambda d: d["prices"]["soybeans"]["scenario_range"]["low"], abs_tol=0.01),
        Claim("soybean range high", (r"соя \*\*420 → [\d.]+–([\d.]+) USD/t",),
              lambda d: d["prices"]["soybeans"]["scenario_range"]["high"], abs_tol=0.01),
        Claim("soybean change low",
              (r"соя \*\*420 → [\d.]+–[\d.]+ USD/t\*\* \(\+([\d.]+) %",),
              lambda d: d["prices"]["soybeans"]["change_pct"]["low"], abs_tol=0.01),
        Claim("soybean change high", (r"соя[^\n]*?% … \+([\d.]+) %\)",),
              lambda d: d["prices"]["soybeans"]["change_pct"]["high"], abs_tol=0.01),
        Claim("soymeal range high",
              (r"шрот \*\*330 → [\d.]+–([\d.]+) USD/t",),
              lambda d: d["prices"]["soymeal"]["scenario_range"]["high"], abs_tol=0.01),
        Claim("food producer price low", (r"\| FOOD \| \+([\d.]+) \|",),
              lambda d: d["leontief"]["producer_price_change_pct"]["low"]["FOOD"], abs_tol=1e-4),
        Claim("food producer price high", (r"\| FOOD \| \+[\d.]+ \| \+([\d.]+) \|",),
              lambda d: d["leontief"]["producer_price_change_pct"]["high"]["FOOD"], abs_tol=1e-4),
        Claim("animal producer price high",
              (r"\| ANIMAL[^|]*\| \+[\d.]+ \| \+([\d.]+) \|",),
              lambda d: d["leontief"]["producer_price_change_pct"]["high"]["ANIMAL"],
              abs_tol=1e-4),
        Claim("consumer food CPI low",
              (r"вклад в продовольственную инфляцию\s*\*\*\+([\d.]+) …",),
              lambda d: d["consumer_food_prices"]["range_pp"]["low"], abs_tol=1e-3),
        Claim("consumer food CPI high", (r"… \+([\d.]+) п\.п\.\*\*",),
              lambda d: d["consumer_food_prices"]["range_pp"]["high"], abs_tol=1e-3),
        Claim("Africa bill change low", (r"изменение \*\*\+([\d.]+) …",),
              lambda d: d["africa"]["import_bill_change_usd_mn"]["low"], abs_tol=0.05),
        Claim("Africa bill change high", (r"… \+([\d\s.,]+) USD млн\*\*",),
              lambda d: d["africa"]["import_bill_change_usd_mn"]["high"], abs_tol=0.05),
        Claim("Africa bill change pct low", (r"USD млн\*\* \(\+([\d.]+) %",),
              lambda d: d["africa"]["import_bill_change_pct"]["low"], abs_tol=0.01),
        Claim("Africa bill change pct high",
              (r"USD млн\*\* \(\+[\d.]+ % … \+([\d.]+) %\)",),
              lambda d: d["africa"]["import_bill_change_pct"]["high"], abs_tol=0.01),
        Claim("Africa food inflation low",
              (r"Африка \(из агро-модуля[\s\S]{0,400}?инфляцию\s*\*\*\+([\d.]+) …",),
              lambda d: d["africa"]["food_inflation_pp_range"]["low"], abs_tol=1e-3),
        Claim("Africa food inflation high",
              (r"Африка \(из агро-модуля[\s\S]{0,400}?… \+([\d.]+) п\.п\.\*\*",),
              lambda d: d["africa"]["food_inflation_pp_range"]["high"], abs_tol=1e-3),
        Claim("countries count", (r"Страны \((\d+);",),
              lambda d: d["countries"]["count"], rel=0.0),
        Claim("Africa baseline bill", (r"базовый импортный счёт ([\d\s.,]+) USD млн",),
              lambda d: d["africa"]["baseline_import_bill_usd_mn"], abs_tol=0.05),
    )


def _oil_backtest_claims() -> tuple:
    def metric(key: str):
        return lambda d: d["metrics"][key]

    def episode(index: int, key: str):
        return lambda d: d["episodes"][index][key]

    return (
        Claim("MAE model", (r"Агрегаты: MAE модели \*\*([\d.,]+)\*\*;",),
              metric("mae_model_usd"), rel=1e-9),
        Claim("MAE baseline", (r"«без шока» — MAE \*\*([\d.,]+)\*\*",),
              metric("mae_baseline_usd"), rel=1e-9),
        Claim("RMSE model", (r"RMSE \*\*([\d.,]+)\*\*;",), metric("rmse_model_usd"), rel=1e-9),
        Claim("skill score", (r"skill \*\*\+([\d.,]+)\*\*;",), metric("skill_score_vs_no_shock"),
              rel=1e-9),
        Claim("mean actual delta", (r"средний факт Δ ([\d.,]+)",), metric("mean_actual_delta_usd"),
              rel=1e-9),
        Claim("mean model delta", (r"модельного ([\d.,]+)",), metric("mean_model_delta_usd"),
              abs_tol=0.01),
        Claim("mean ratio", (r"mean ratio ([\d.,]+)",), metric("mean_ratio"), abs_tol=1e-3),
        Claim("direction hits",
              (r"направление изменения Brent угадано \*\*(\d+) из 3\*\*",),
              metric("direction_hits"), rel=0.0),
        Claim("order hits", (r"порядок[\s\S]{0,80}?\*\*(\d+) из 3\*\*",), metric("order_hits"),
              rel=0.0),
        Claim("1990 model delta",
              (r"\| \*\*1990 Gulf\*\*[^|]*\|[^|]*\| \*\*\+24,0\*\* \| \+([\d.,]+) \|",),
              episode(0, "model_delta_usd"), abs_tol=0.01),
        Claim("2019 model delta",
              (r"\| \*\*2019 Abqaiq\*\*[^|]*\|[^|]*\| \*\*\+9,0\*\* \| \+([\d.,]+) \|",),
              episode(1, "model_delta_usd"), abs_tol=0.01),
        Claim("2022 model delta",
              (r"\| \*\*2022 Russia\*\*[^|]*\|[^|]*\| \*\*\+30,0\*\* \| \+([\d.,]+) \|",),
              episode(2, "model_delta_usd"), abs_tol=0.01),
        Claim("1990 error", (r"1990 Gulf[\s\S]{0,160}?\*\*−([\d.,]+)\*\*",),
              lambda d: abs(episode(0, "abs_error_usd")(d)), abs_tol=0.01),
        Claim("episodes", (r"Мало эпизодов\*\* \((\d+)\)",), lambda d: 3, rel=0.0,
              required=False),
    )


def _abm_claims(abm: dict) -> tuple:
    run = abm["runs"]["baseline"]
    shock = abm["runs"]["income_shock"]

    def sector_totals(name: str):
        return lambda d: float(d["runs"]["baseline"]["sectors"][name]["income"])

    return (
        Claim("household income", (r"\| household \| ([\d.]+) /",), sector_totals("household"),
              abs_tol=0.001),
        Claim("firm income", (r"\| firm \| ([\d.]+) /",), sector_totals("firm"), abs_tol=0.01),
        Claim("firm saving", (r"\| firm \| [\d.]+ / −[\d.]+ / ([\d.]+) /",),
              lambda d: float(d["runs"]["baseline"]["sectors"]["firm"]["saving"]), abs_tol=0.01),
        Claim("firm investment", (r"\| firm \| [\d.]+ / −[\d.]+ / [\d.]+ / ([\d.]+) \|",
                                  ), lambda d: float(d["runs"]["baseline"]["sectors"]["firm"]
                                                     ["investment"]), abs_tol=0.01),
        Claim("totals income", (r"\| \*\*итого\*\* \| income ([\d.]+)",),
              lambda d: float(d["runs"]["baseline"]["totals"]["income"]), abs_tol=0.01),
        Claim("S = I", (r"\*\*S = I = ([\d.]+)\*\*, CA = 0",),
              lambda d: float(d["runs"]["baseline"]["totals"]["saving"]), abs_tol=0.01),
        Claim("shock S = I", (r"income 804\.655, \*\*S = I = ([\d.]+)\*\*",),
              lambda d: float(d["runs"]["income_shock"]["totals"]["saving"]), abs_tol=0.01),
        Claim("firm net financial worth", (r"фирмы — активы [\d.]+, обязательства −[\d.]+, net ([\d.]+)",
                                           ), lambda d: float(d["runs"]["baseline"]["sectors"]
                                                              ["firm"]["net_financial_worth"]),
              abs_tol=0.01),
        Claim("bank net financial worth", (r"банк — активы [\d.]+, обязательства −[\d.]+, net ([\d.]+)",
                                           ), lambda d: float(d["runs"]["baseline"]["sectors"]
                                                              ["bank"]["net_financial_worth"]),
              abs_tol=0.01),
        Claim("net worth = real assets", (r"суммарный net worth =\s*реальные активы = ([\d.]+)",
                                          ), lambda d: float(d["runs"]["baseline"]["totals"]
                                                             ["net_worth"]), abs_tol=0.01),
        Claim("shock periods", (r"(\d+) периодов",), lambda d: d["runs"]["baseline"]["periods"],
              rel=0.0, required=False),
    )


def _api_claims(api: dict) -> tuple:
    def envelope(key: str, sub: str | None = None):
        def getter(doc: dict):
            value = doc["forecast_envelope"]["envelope"][key]
            for part in sub.split(".") if sub else []:
                value = value[part]
            return value
        return getter

    def scenario_path(index: int):
        return lambda d: d["scenario_paths"][index]

    return (
        Claim("API modules", (r"(\d+) модул",), lambda d: d["counts"]["modules"], rel=0.0),
        Claim("API market modules", (r"(1) модуль",), lambda d: d["counts"].get("market_modules"),
              rel=0.0, required=False),
        Claim("API scenarios", (r"(\d+) сценари",), lambda d: d["counts"]["scenarios"], rel=0.0),
        Claim("API forecasts", (r"(\d+) forecast",), lambda d: d["counts"]["forecasts"], rel=0.0),
        Claim("API contract version", (r"api_version = v(\d+)",),
              lambda d: float(str(d["api_version"]).lstrip("v")), rel=0.0),
        Claim("API schema version", (r"schema = amfe\.forecast_api\.v(\d+)",),
              lambda d: float(str(d["schema"]).rsplit(".v", 1)[-1]), rel=0.0),
        Claim("OpenAPI major version", (r"OpenAPI (\d+)\.\d+\.\d+",),
              lambda d: int(str(d["openapi"]["info"]["version"]).split(".")[0]),
              rel=0.0, required=False),
        Claim("Brent peak in the envelope", (r"Центральный пик Brent \| \**([\d.]+)",),
              envelope("price_peak"), rel=1e-6),
        Claim("Brent peak range low", (r"\*\*([\d.]+) / 168\.85584 /",),
              envelope("range_peak", "low"), rel=1e-6),
        Claim("Brent peak range high", (r"168\.85584 / ([\d.]+)\*\*",),
              envelope("range_peak", "high"), rel=1e-6),
        Claim("Brent peak spread", (r"spread ([\d.]+)%",),
              envelope("range_peak", "spread_pct"), rel=1e-6),
        Claim("Brent mean", (r"Средняя цена за 6 мес\. \| \**([\d.]+)",),
              envelope("price_mean"), rel=1e-6),
        Claim("scenario price path first", (r"price_path = \[([\d.]+),",),
              scenario_path(0), rel=1e-6),
    )


def _run_claims(claims: tuple, doc: str, document: dict, track: str) -> tuple[list, list]:
    findings, evidence = [], []
    for claim in claims:
        try:
            observed = claim.getter(document)
        except (KeyError, IndexError, TypeError, ValueError) as error:
            observed = None
            findings.append(_finding("G2", track, "HIGH",
                                     f"{claim.label}: the artifact does not expose the claimed "
                                     f"value ({error})", {"label": claim.label}))
            evidence.append({"label": claim.label, "status": "MISSING_IN_JSON"})
            continue
        claimed = _doc_number(doc, claim.patterns)
        row = {"label": claim.label, "doc_value": claimed,
               "artifact_value": observed if not isinstance(observed, str) else observed}
        if claimed is None:
            row["status"] = "NOT_FOUND_IN_DOC"
            if claim.required:
                findings.append(_finding("G2", track, "MEDIUM",
                                         f"{claim.label}: the documented number was not found in "
                                         f"the track document", {"label": claim.label}))
        elif isinstance(observed, str):
            row["status"] = "OK" if claimed == observed else "MISMATCH"
            if row["status"] == "MISMATCH":
                findings.append(_finding("G2", track, claim.severity,
                                         f"{claim.label}: docs {claimed!r} vs artifact {observed!r}",
                                         {"label": claim.label, "doc": claimed, "artifact": observed}))
        elif _same_value(float(claimed), float(observed), rel=claim.rel, abs_tol=claim.abs_tol):
            row["status"] = "OK"
        else:
            row["status"] = "MISMATCH"
            findings.append(_finding("G2", track, claim.severity,
                                     f"{claim.label}: docs {claimed} vs artifact {observed}",
                                     {"label": claim.label, "doc": claimed, "artifact": observed,
                                      "rel": claim.rel, "abs_tol": claim.abs_tol}))
        evidence.append(row)
    return findings, evidence


def check_numbers(repo: Path, docs: dict, documents: dict, abm: dict | None) -> dict:
    findings, evidence = [], {}
    api_contract = dict(documents["api_index"])
    api_contract["forecast_envelope"] = documents.get("api_forecast") or {}
    api_contract["openapi"] = documents.get("api_openapi") or {}
    api_contract["scenario_paths"] = documents["api_index"].get("scenario_paths") or []
    plans = {
        "price_bridge": lambda: _price_bridge_claims(),
        "agri_bridge": lambda: _agri_bridge_claims(),
        "oil_backtest": lambda: _oil_backtest_claims(),
        "abm_sfc": lambda: _abm_claims(abm) if abm else (),
        "forecast_api": lambda: _api_claims(api_contract),
    }
    document_keys = {
        "price_bridge": "price_bridge_report",
        "agri_bridge": "agri_bridge_report",
        "oil_backtest": "oil_backtest_report",
        "abm_sfc": None,
        "forecast_api": None,
    }
    for track, factory in plans.items():
        doc_text = docs.get(track)
        if track == "forecast_api":
            document = api_contract
        elif track == "abm_sfc":
            document = abm
        else:
            document = documents.get(document_keys[track])
        if doc_text is None or document is None:
            continue
        own_findings, own_evidence = _run_claims(factory(), doc_text, document, track)
        findings.extend(own_findings)
        evidence[track] = {"claims": own_evidence,
                           "checked": len(own_evidence),
                           "mismatches": sum(1 for row in own_evidence
                                             if row["status"] in ("MISMATCH", "NOT_FOUND_IN_DOC",
                                                                  "MISSING_IN_JSON"))}
    evidence["total_claims"] = sum(block["checked"] for block in evidence.values()
                                   if isinstance(block, dict) and "checked" in block)
    return _check("G2", "documented numbers re-derived from the cited JSON artifacts",
                  findings, evidence)


# ----------------------------------------------------------------------- G3 price vs quantity
def check_separation(repo: Path, documents: dict, docs: dict) -> dict:
    findings, evidence = [], {}
    bridge = documents["price_bridge_report"]
    checks = bridge.get("checks", {})
    evidence["price_bridge"] = {
        "no_quantity_claims_in_price_model": checks.get("no_quantity_claims_in_price_model"),
        "has_price_block": "price_results" in bridge,
        "has_quantity_block": "quantity_proxy" in bridge,
        "quantity_proxy_separate": bool(bridge.get("quantity_proxy", {}).get("note")),
        "statement": str(bridge.get("statement", ""))[:200],
    }
    if checks.get("no_quantity_claims_in_price_model") is not True:
        findings.append(_finding("G3", "price_bridge", "BLOCKER",
                                 "the price bridge does not flag the price/quantity separation"))
    if "price_results" not in bridge or "quantity_proxy" not in bridge:
        findings.append(_finding("G3", "price_bridge", "HIGH",
                                 "the report does not keep separate price and quantity blocks"))
    quantity_note = str(bridge.get("quantity_proxy", {}).get("note", "")).lower()
    if "not a forecast" not in quantity_note and "не является" not in quantity_note:
        findings.append(_finding("G3", "price_bridge", "MEDIUM",
                                 "the quantity proxy is not marked as a non-forecast"))
    statement = str(bridge.get("statement", ""))
    if re.search(r"(?<!не )(?<!нет )падени\w*\s+выпуска", statement) and "не" not in statement:
        findings.append(_finding("G3", "price_bridge", "HIGH",
                                 "the price bridge claims an output decline"))
    agri = documents["agri_bridge_report"]
    separation = agri.get("separation", {})
    quantity = separation.get("quantity_based", {})
    evidence["agri_bridge"] = {
        "quantity_modelled": quantity.get("modelled"),
        "volumes_held_constant": quantity.get("volumes_held_constant"),
        "not_claimed": quantity.get("not_claimed"),
        "has_price_block": "prices" in agri,
        "africa_in_world_totals": agri.get("africa", {}).get("included_in_world_totals"),
    }
    if quantity.get("modelled") is not False:
        findings.append(_finding("G3", "agri_bridge", "BLOCKER",
                                 "the agri bridge does not declare the quantity channel as not "
                                 "modelled"))
    if not quantity.get("not_claimed"):
        findings.append(_finding("G3", "agri_bridge", "HIGH",
                                 "the agri bridge does not list the results it does not claim"))
    if agri.get("africa", {}).get("included_in_world_totals") is not False:
        findings.append(_finding("G3", "agri_bridge", "HIGH",
                                 "the African import bill is not excluded from the world totals"))
    for track in ("price_bridge", "agri_bridge"):
        text = (docs.get(track) or "").lower()
        if not re.search(r"разделен|separation|не смешив|price\s*/\s*quantity|"
                         r"ценовой канал|количественный канал", text):
            findings.append(_finding("G3", track, "LOW",
                                     "the document does not state the price/quantity separation"))
    return _check("G3", "price models do not claim quantity effects; separation blocks exist",
                  findings, evidence)


# ------------------------------------------------------------------------------ G4 assumptions
def check_assumptions(documents: dict) -> dict:
    findings, evidence = [], {}
    bridge = documents["price_bridge_report"]
    assumptions = bridge.get("bridge_model", {}).get("assumptions") or []
    shares = bridge.get("bridge_model", {}).get("bucket_shares") or {}
    evidence["price_bridge"] = {"assumptions": len(assumptions),
                                "bucket_shares": shares}
    if len(assumptions) < 3:
        findings.append(_finding("G4", "price_bridge", "HIGH",
                                 f"the price bridge declares only {len(assumptions)} assumptions"))
    if not shares:
        findings.append(_finding("G4", "price_bridge", "HIGH",
                                 "the price bridge does not declare the bucket shares"))
    agri = documents["agri_bridge_report"]
    agri_assumptions = agri.get("data", {}).get("assumptions", {})
    evidence["agri_bridge"] = {"assumptions": agri_assumptions,
                               "source_class": agri_assumptions.get("source_class")}
    if agri_assumptions.get("source_class") != "assumption" or not agri_assumptions:
        findings.append(_finding("G4", "agri_bridge", "HIGH",
                                 "the agri bridge assumptions are not labelled"))
    backtest = documents["oil_backtest_report"]
    bt_assumptions = backtest.get("assumptions") or []
    evidence["oil_backtest"] = {"assumptions": len(bt_assumptions),
                                "source_classes": sorted({str(item.get("source_class"))
                                                          for item in bt_assumptions if isinstance(item, dict)})}
    if len(bt_assumptions) < 3:
        findings.append(_finding("G4", "oil_backtest", "HIGH",
                                 f"the oil backtest declares only {len(bt_assumptions)} assumptions"))
    if "assumption" not in evidence["oil_backtest"]["source_classes"]:
        findings.append(_finding("G4", "oil_backtest", "HIGH",
                                 "the oil backtest inputs are not labelled assumption"))
    api = documents["api_index"]
    policy = api.get("policy", {})
    evidence["forecast_api"] = {"policy": policy}
    for key in ("no_admission", "no_point_forecast", "no_raw_third_party_data"):
        if policy.get(key) is not True:
            findings.append(_finding("G4", "forecast_api", "HIGH",
                                     f"the API policy does not set {key}=true"))
    forecast = documents.get("api_forecast") or {}
    evidence["forecast_api"]["forecast_assumptions_count"] = forecast.get("assumptions_count")
    if not forecast.get("no_point_forecast"):
        findings.append(_finding("G4", "forecast_api", "HIGH",
                                 "the forecast envelope does not carry no_point_forecast=true"))
    if forecast.get("assumptions_classes") is None:
        findings.append(_finding("G4", "forecast_api", "MEDIUM",
                                 "the forecast envelope does not list assumption classes"))
    return _check("G4", "assumptions are explicitly declared and classified", findings, evidence)


# --------------------------------------------------------------------- G5 no point forecast/admission
def check_no_forbidden_claims(documents: dict, docs: dict) -> dict:
    findings, evidence = [], {}
    structural = {
        "price_bridge.admission": documents["price_bridge_report"].get("admission"),
        "price_bridge.status": documents["price_bridge_report"].get("status"),
        "agri_bridge.not_a_forecast": documents["agri_bridge_report"].get("provenance", {})
        .get("not_a_forecast"),
        "agri_bridge.no_admission_claim": documents["agri_bridge_report"].get("provenance", {})
        .get("no_admission_claim"),
        "oil_backtest.research_only": documents["oil_backtest_report"].get("research_only"),
        "oil_backtest.status": documents["oil_backtest_report"].get("status"),
        "api.policy.no_admission": documents["api_index"].get("policy", {}).get("no_admission"),
        "api.policy.no_point_forecast": documents["api_index"].get("policy", {})
        .get("no_point_forecast"),
        "api_forecast.no_point_forecast": (documents.get("api_forecast") or {})
        .get("no_point_forecast"),
    }
    evidence["structural"] = structural
    if structural["price_bridge.admission"] is not False:
        findings.append(_finding("G5", "price_bridge", "BLOCKER",
                                 "the price bridge report does not declare admission=false"))
    if structural["agri_bridge.not_a_forecast"] is not True or \
            structural["agri_bridge.no_admission_claim"] is not True:
        findings.append(_finding("G5", "agri_bridge", "BLOCKER",
                                 "the agri bridge does not declare not_a_forecast/no_admission"))
    if structural["oil_backtest.research_only"] is not True or \
            structural["oil_backtest.status"] != "RESEARCH_ONLY":
        findings.append(_finding("G5", "oil_backtest", "BLOCKER",
                                 "the oil backtest is not marked research-only"))
    for key in ("api.policy.no_admission", "api.policy.no_point_forecast",
                "api_forecast.no_point_forecast"):
        if structural[key] is not True:
            findings.append(_finding("G5", "forecast_api", "BLOCKER",
                                     f"missing protection flag: {key}"))
    # negation-aware text scan over documents and JSON string leaves
    scanned = 0
    for track, raw_text in docs.items():
        if raw_text is None:
            continue
        text = re.sub(r"[*`_]", " ", raw_text)
        for pattern, description in FORBIDDEN_CLAIMS:
            for match in pattern.finditer(text):
                window = text[max(0, match.start() - 90): match.end() + 90].lower()
                if any(token in window for token in NEGATIONS):
                    continue
                findings.append(_finding("G5", track, "HIGH",
                                         f"{track}: unsupported {description}",
                                         {"match": match.group(0),
                                          "context": window.strip()[:180]}))
                scanned += 1
    for track, document in documents.items():
        if not isinstance(document, dict):
            continue
        payload = json.dumps(document, ensure_ascii=False)
        for pattern, description in FORBIDDEN_CLAIMS:
            for match in pattern.finditer(payload):
                window = payload[max(0, match.start() - 90): match.end() + 90].lower()
                if any(token in window for token in NEGATIONS):
                    continue
                findings.append(_finding("G5", track, "HIGH",
                                         f"{track} JSON: unsupported {description}",
                                         {"match": match.group(0),
                                          "context": window.strip()[:180]}))
                scanned += 1
    evidence["forbidden_claims_found"] = scanned
    return _check("G5", "no point forecast / admission / trading-recommendation claims",
                  findings, evidence)


# ---------------------------------------------------------------- G6 fail-closed and tests
def check_fail_closed_tests(repo: Path, docs: dict, run_help: bool = True,
                            test_counts: dict | None = None) -> dict:
    findings, evidence = [], {}
    tools = {}
    for track, relative in REQUIRED_TOOLS.items():
        path = repo / relative
        tools[track] = {"path": relative, "exists": path.is_file()}
        if not path.is_file():
            findings.append(_finding("G6", track, "BLOCKER", f"tool is missing: {relative}"))
    if run_help:
        for track, row in tools.items():
            if not row["exists"]:
                continue
            done = subprocess.run([sys.executable, str(repo / row["path"]), "--help"],
                                  cwd=str(repo), capture_output=True, text=True, timeout=300)
            row.update({"help_returncode": done.returncode,
                        "help_ok": done.returncode == 0 and
                                   "usage" in (done.stdout + done.stderr).lower()})
            if not row["help_ok"]:
                findings.append(_finding("G6", track, "HIGH",
                                         f"`python {row['path']} --help` does not work "
                                         f"(exit {done.returncode})"))
    evidence["tools"] = tools
    tests = {}
    for track, relative in TEST_FILES.items():
        path = repo / relative
        row = {"path": relative, "exists": path.is_file()}
        if path.is_file():
            row["test_count"] = len(re.findall(r"^\s+def test_", path.read_text(encoding="utf-8"),
                                               re.M))
        else:
            findings.append(_finding("G6", track, "BLOCKER", f"test file is missing: {relative}"))
        tests[track] = row
    evidence["tests"] = tests
    # documented test counts vs actual counts
    for track, claimed in (test_counts or {}).items():
        actual = tests.get(track, {}).get("test_count")
        if claimed is None or actual is None:
            continue
        if int(claimed) != int(actual):
            findings.append(_finding("G6", track, "MEDIUM",
                                     f"the document claims {claimed} tests, the test file has "
                                     f"{actual}", {"claimed": claimed, "actual": actual,
                                                   "file": tests[track]["path"]}))
    evidence["documented_test_counts"] = dict(test_counts or {})
    # fail-closed documentation per track
    fail_closed_docs = {}
    for track, text in docs.items():
        if text is None:
            fail_closed_docs[track] = False
            continue
        present = bool(re.search(r"fail-closed|fail closed|закрыт\w* при ошибке|exit 2|код\w* выхода `?2",
                                 text, re.I))
        fail_closed_docs[track] = present
        if not present and track != "oil_backtest":
            findings.append(_finding("G6", track, "LOW",
                                     "the document does not state the fail-closed behaviour"))
    evidence["fail_closed_documented"] = fail_closed_docs
    return _check("G6", "tools/tests exist, --help works, documented test counts match",
                  findings, evidence)


# ----------------------------------------------------------------------------- G7 limitations
def check_limitations(repo: Path, docs: dict, documents: dict) -> dict:
    findings, evidence = [], {}
    per_track = {}
    for track, text in docs.items():
        if text is None:
            continue
        has_section = bool(re.search(r"^##+\s*\d*\.?\s*(Ограничения|Limitations)", text,
                                     re.M | re.I))
        bullets = len(re.findall(r"^\s*\d+\.\s+\*\*|^\s*-\s+\*\*", text, re.M))
        per_track[track] = {"limitations_section": has_section, "bullets": bullets}
        if not has_section:
            findings.append(_finding("G7", track, "HIGH",
                                     "the document has no limitations section"))
        elif bullets < 3:
            findings.append(_finding("G7", track, "LOW",
                                     f"the limitations section lists only {bullets} items"))
    json_limits = {
        "price_bridge": len(documents["price_bridge_report"].get("limitations") or []),
        "agri_bridge": len(documents["agri_bridge_report"].get("limitations") or []),
        "oil_backtest": len(documents["oil_backtest_report"].get("where_worse") or []),
        "forecast_api": 1 if documents["api_index"].get("disclaimer") else 0,
    }
    evidence.update({"docs": per_track, "json": json_limits})
    for track, count in json_limits.items():
        if track in ("price_bridge", "agri_bridge") and count < 3:
            findings.append(_finding("G7", track, "MEDIUM",
                                     f"the JSON report lists only {count} limitations"))
    if not documents["api_index"].get("disclaimer"):
        findings.append(_finding("G7", "forecast_api", "HIGH", "the API index has no disclaimer"))
    return _check("G7", "limitations are stated for every track", findings, evidence)


# ------------------------------------------------------------------------------ G8 raw data
RAW_PATTERNS = (r"^data/mrio/", r"\.zip$", r"\.dta$", r"\.csv$", r"\.xlsx$", r"\.parquet$",
                r"^data/raw/")

#: Public-domain derived aggregates that may live in git if (and only if) a provenance sidecar
#: documents the licence, the series and the exact hash.  The project policy (docs/84, docs/160)
#: forbids raw third-party data in git but allows derived aggregates when the licence permits;
#: a blanket ``*.csv`` block would wrongly flag such a file, so the exception is explicit.
PUBLIC_DOMAIN_AGGREGATES = {
    "data/oil/brent_monthly_eia.csv": "data/oil/brent_monthly_eia.meta.json",
}


def _public_domain_aggregates(repo: Path) -> dict:
    """Verify allow-listed derived aggregates: meta exists, hash matches, licence is public."""
    allowed, problems = [], []
    for relative, meta_relative in PUBLIC_DOMAIN_AGGREGATES.items():
        path, meta_path = Path(repo) / relative, Path(repo) / meta_relative
        if not path.is_file():
            problems.append({"path": relative, "problem": "file not found"})
            continue
        if not meta_path.is_file():
            problems.append({"path": relative, "problem": f"provenance not found: {meta_relative}"})
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            problems.append({"path": relative, "problem": f"provenance unreadable: {error}"})
            continue
        if str(meta.get("file", "")) != relative:
            problems.append({"path": relative, "problem": "provenance names a different file"})
        elif str(meta.get("file_sha256", "")) != sha256_file(path):
            problems.append({"path": relative, "problem": "provenance hash does not match the file"})
        elif "public domain" not in str(meta.get("license", "")).lower():
            problems.append({"path": relative, "problem": "licence is not public domain"})
        else:
            allowed.append(relative)
    return {"allowed": allowed, "problems": problems,
            "policy": "public-domain derived aggregates require a provenance sidecar (licence, "
                      "series, hash); everything else under data/ must stay untracked"}


def _tracked_raw_data(repo: Path) -> dict:
    try:
        done = subprocess.run(["git", "-C", str(repo), "ls-files"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=False)
        if done.returncode != 0:
            return {"available": False, "files": []}
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "files": []}
    tracked = [line.strip() for line in done.stdout.splitlines() if line.strip()]
    aggregate_check = _public_domain_aggregates(repo)
    allowed = set(aggregate_check["allowed"])
    hits = [path for path in tracked
            if any(re.search(pattern, path) for pattern in RAW_PATTERNS) and path not in allowed]
    return {"available": True, "files": hits, "tracked_total": len(tracked),
            "public_domain_aggregates": aggregate_check}


def _long_numeric_arrays(node, path="", limit=MAX_NUMERIC_LEAF, out=None) -> list:
    out = [] if out is None else out
    if isinstance(node, dict):
        for key, value in node.items():
            _long_numeric_arrays(value, f"{path}.{key}" if path else str(key), limit, out)
    elif isinstance(node, list):
        numeric = [item for item in node
                   if isinstance(item, (int, float)) and not isinstance(item, bool)]
        if len(numeric) >= limit:
            out.append({"path": path, "length": len(numeric)})
        for index, value in enumerate(node):
            if isinstance(value, (dict, list)):
                _long_numeric_arrays(value, f"{path}[{index}]", limit, out)
    return out


def check_raw_data(repo: Path, documents: dict) -> dict:
    findings, evidence = [], {}
    tracked = _tracked_raw_data(repo)
    evidence["git"] = tracked
    aggregates = tracked.get("public_domain_aggregates") or {}
    evidence["public_domain_aggregates"] = aggregates
    for problem in aggregates.get("problems") or []:
        findings.append(_finding("G8", "cross", "HIGH",
                                 f"public-domain aggregate fails provenance checks: "
                                 f"{problem['problem']}", problem))
    if tracked.get("files"):
        findings.append(_finding("G8", "cross", "BLOCKER",
                                 "third-party raw data is tracked by git",
                                 {"files": tracked["files"][:10]}))
    build_ignored = subprocess.run(["git", "-C", str(repo), "check-ignore", "build/api/index.json"],
                                   capture_output=True, text=True, timeout=60)
    evidence["build_ignored"] = build_ignored.returncode == 0
    if build_ignored.returncode != 0:
        findings.append(_finding("G8", "cross", "HIGH",
                                 "generated artifacts under build/ are not git-ignored"))
    long_arrays = {}
    for name, document in documents.items():
        if not isinstance(document, dict):
            continue
        hits = _long_numeric_arrays(document)
        if hits:
            long_arrays[name] = hits
    evidence["long_numeric_arrays"] = long_arrays
    for name, hits in long_arrays.items():
        findings.append(_finding("G8", name, "HIGH",
                                 f"{name} embeds a long numeric array ({hits[0]['path']}, "
                                 f"{hits[0]['length']} values): artifacts must carry aggregates only",
                                 {"hits": hits[:5]}))
    return _check("G8", "no raw third-party data in git and no raw series in artifacts",
                  findings, evidence)


# ----------------------------------------------------------------------------- G9 determinism
def check_determinism(repo: Path, abm_runner) -> dict:
    findings, evidence = [], {}
    if abm_runner is None:
        evidence["abm_demo"] = "skipped"
        return _check("G9", "ABM demo and the review itself are deterministic", findings, evidence)
    first = abm_runner()
    second = abm_runner()
    same = json.dumps(first, sort_keys=True, ensure_ascii=False) == \
        json.dumps(second, sort_keys=True, ensure_ascii=False)
    evidence["abm_demo"] = {"runs": 2, "identical": same,
                            "all_checks_pass": first.get("all_checks_pass")}
    if not same:
        findings.append(_finding("G9", "abm_sfc", "HIGH",
                                 "the ABM demo is not deterministic between two runs"))
    if first.get("all_checks_pass") is not True:
        findings.append(_finding("G9", "abm_sfc", "BLOCKER",
                                 "the ABM demo reports failing SFC checks"))
    checks_all_true = all(all(bool(value) for value in run.get("checks", {}).values())
                          for run in first.get("runs", {}).values())
    evidence["abm_demo"]["all_scenario_checks_true"] = checks_all_true
    if not checks_all_true:
        findings.append(_finding("G9", "abm_sfc", "HIGH",
                                 "a scenario in the ABM demo fails an invariant"))
    return _check("G9", "ABM demo and the review itself are deterministic", findings, evidence)


# ---------------------------------------------------------------------------------- review build
def _abm_runner(repo: Path):
    def run() -> dict:
        done = subprocess.run([sys.executable, str(repo / REQUIRED_TOOLS["abm_sfc"]), "--json"],
                              cwd=str(repo), capture_output=True, text=True, timeout=300)
        if done.returncode not in (0, 2):
            raise ReviewError(f"the ABM demo failed (exit {done.returncode}): "
                              f"{done.stderr.strip()[:300]}")
        try:
            return json.loads(done.stdout)
        except json.JSONDecodeError as error:
            raise ReviewError(f"the ABM demo produced no JSON: {error}") from error
    return run


def _documented_test_counts(docs: dict) -> dict:
    counts = {}
    patterns = {
        "price_bridge": r"tests/test_market_bridge\.py\*\* — \*\*(\d+)\s*тест",
        "agri_bridge": r"тесты моста \((\d+)\s*тест",
        "oil_backtest": r"tests/test_markets_backtest\.py\s*#\s*(\d+)\s*тест",
        "abm_sfc": r"tests/test_agentic_accounts\.py` — (\d+)\s*тест",
        "forecast_api": r"tests/test_forecast_api\.py\)\s*\((\d+)\)",
        "market_client_pack": r"tests/test_market_client_pack\.py\)\s*\((\d+)\)",
    }
    for track, pattern in patterns.items():
        text = docs.get(track) or ""
        match = re.search(pattern, text)
        if match:
            counts[track] = int(match.group(1))
    return counts


def build_review(repo: Path | str = ROOT, doc_overrides: dict | None = None,
                 run_help: bool = True, run_abm: bool = True,
                 abm_document: dict | None = None,
                 generated_at: str | None = None) -> dict:
    repo = Path(repo)
    doc_overrides = dict(doc_overrides or {})
    docs: dict = {}
    for track, relative in DOCS.items():
        path = Path(doc_overrides[track]) if track in doc_overrides else repo / relative
        docs[track] = _read_text(path, f"G2 document {relative}")
    documents = {
        "price_bridge_report": _read_json(repo / ARTIFACTS["price_bridge_report"],
                                          "price bridge report"),
        "agri_bridge_report": _read_json(repo / ARTIFACTS["agri_bridge_report"],
                                         "agri bridge report"),
        "oil_backtest_report": _read_json(repo / ARTIFACTS["oil_backtest_report"],
                                          "oil backtest report"),
        "api_index": _read_json(repo / ARTIFACTS["api_index"], "API index"),
        "api_openapi": _read_json(repo / ARTIFACTS["api_openapi"], "OpenAPI document"),
        "api_forecast": _read_json(repo / ARTIFACTS["api_forecast"], "forecast envelope"),
    }
    api_index = documents["api_index"]
    api_index["openapi"] = documents["api_openapi"]
    scenario_relative = "build/api/scenarios/oil.brent__hormuz_closed_6m.json"
    scenario_document = _read_json(repo / scenario_relative, "6m scenario envelope")
    api_index["scenario_paths"] = list(scenario_document["result"]["price_path"])
    abm = abm_document
    if abm is None and run_abm:
        abm = _abm_runner(repo)()
    docs_claims = {"artifact_hashes": _parse_artifact_claims(docs)}
    checks = [
        check_artifacts(repo, docs_claims),
        check_numbers(repo, docs, documents, abm),
        check_separation(repo, documents, docs),
        check_assumptions(documents),
        check_no_forbidden_claims(documents, docs),
        check_fail_closed_tests(repo, docs, run_help=run_help,
                                test_counts=_documented_test_counts(docs)),
        check_limitations(repo, docs, documents),
        check_raw_data(repo, documents),
        check_determinism(repo, _abm_runner(repo) if (run_abm and abm is None) else
                          (None if abm is None else (lambda: abm))),
    ]
    findings = [finding for check_item in checks for finding in check_item["findings"]]
    by_severity = {severity: 0 for severity in SEVERITIES}
    for finding in findings:
        by_severity[finding["severity"]] += 1
    failed = [check_item["id"] for check_item in checks if check_item["status"] == "FAIL"]
    track_verdicts = {}
    for track in TRACK_TITLES:
        own = [finding for finding in findings if finding["track"] == track]
        if any(item["severity"] in ("BLOCKER", "HIGH") for item in own):
            track_verdicts[track] = "NOT_CONFIRMED"
        elif own:
            track_verdicts[track] = "CONFIRMED_WITH_NOTES"
        else:
            track_verdicts[track] = "CONFIRMED"
    # cross-cutting findings also matter: a blocker in the review blocks the whole sign-off
    cross = [finding for finding in findings if finding["track"] == "cross"]
    overall = ("FAIL" if failed or by_severity["BLOCKER"] or by_severity["HIGH"]
               else "PASS_WITH_FINDINGS" if findings else "PASS")
    blockers = [finding["id"] for finding in findings
                if finding["severity"] in ("BLOCKER", "HIGH")]
    review = {
        "schema": SCHEMA,
        "review_type": "ADVERSARIAL_INDEPENDENT",
        "generated_at": generated_at,
        "tracks": {track: {"title": TRACK_TITLES[track], "document": DOCS[track],
                           "verdict": track_verdicts[track],
                           "findings": sum(1 for item in findings if item["track"] == track)}
                   for track in TRACK_TITLES},
        "checks": checks,
        "summary": {
            "status": overall,
            "failed_checks": failed,
            "checks": len(checks),
            "findings": len(findings),
            "by_severity": by_severity,
            "verdicts": track_verdicts,
            "cross_cutting_findings": len(cross),
        },
        "sign_off": {
            "state": "READY_FOR_HUMAN_SIGN_OFF" if not blockers else "NOT_SIGNED",
            "blocked_by": blockers,
            "required": ("the automated checklist does not sign anything: a human reviewer signs "
                         "docs/160 only after every BLOCKER/HIGH finding is closed and the review "
                         "is re-generated; MEDIUM findings require a written acceptance"),
        },
        "limits": [
            "automated checklist: numbers and structures are verified, not the economic validity "
            "of the G2 modelling choices",
            "documents are parsed with explicit regexes: a number stated in an unexpected format "
            "is reported as 'not found', not guessed",
            "the ABM demo is executed to obtain its JSON (no persisted artifact exists); the "
            "other artifacts are read as-is, their producers are not re-run (minutes each)",
            "artifact hashes are only meaningful for the run that produced them; a regenerated "
            "artifact legitimately changes its hash and must be reconciled with the document",
            "no third-party data is read or copied; only derived aggregates in build/ are hashed",
            "a human reviewer must sign docs/160; this tool does not replace the sign-off and "
            "assigns no admission",
        ],
    }
    digest_payload = {key: value for key, value in review.items() if key != "generated_at"}
    review["digest"] = hashlib.sha256(json.dumps(digest_payload, ensure_ascii=False,
                                                 sort_keys=True).encode("utf-8")).hexdigest()
    return review


# ------------------------------------------------------------------------------------ rendering
def render_markdown(review: dict) -> str:
    summary = review["summary"]
    lines = ["# External adversarial review: G2 (wave 22), re-run in wave 23 (G3)", ""]
    lines.append(f"Schema `{review['schema']}` · type **{review['review_type']}**")
    lines.append("")
    lines.append(f"## Verdict: {summary['status']}")
    lines.append("")
    lines.append(f"{summary['checks']} checks · {summary['findings']} findings "
                 f"(BLOCKER {summary['by_severity']['BLOCKER']}, HIGH "
                 f"{summary['by_severity']['HIGH']}, MEDIUM {summary['by_severity']['MEDIUM']}, "
                 f"LOW {summary['by_severity']['LOW']}, INFO {summary['by_severity']['INFO']}) · "
                 f"failed checks: {', '.join(summary['failed_checks']) or 'none'}")
    lines.append("")
    lines.append("## Track verdicts")
    lines.append("")
    lines.append("| Track | Document | Verdict | Findings |")
    lines.append("|---|---|---|---|")
    for track, block in review["tracks"].items():
        lines.append(f"| {block['title']} | `{block['document']}` | **{block['verdict']}** | "
                     f"{block['findings']} |")
    lines.append("")
    lines.append("## Checks")
    lines.append("")
    lines.append("| Check | Status | Findings |")
    lines.append("|---|---|---|")
    for check in review["checks"]:
        lines.append(f"| {check['id']} {check['title']} | **{check['status']}** | "
                     f"{len(check['findings'])} |")
    lines.append("")
    findings = [finding for check in review["checks"] for finding in check["findings"]]
    lines.append("## Findings")
    lines.append("")
    if findings:
        order = {severity: index for index, severity in enumerate(SEVERITIES)}
        for finding in sorted(findings, key=lambda item: (order[item["severity"]],
                                                          item["track"], item["check"])):
            lines.append(f"- **{finding['severity']}** `{finding['id']}` "
                         f"({finding['track']}/{finding['check']}): {finding['message']}")
    else:
        lines.append("None: every check passed.")
    lines.append("")
    lines.append("## Sign-off")
    lines.append("")
    lines.append(f"State: **{review['sign_off']['state']}**. Blocked by: "
                 f"{', '.join(review['sign_off']['blocked_by']) or 'nothing'}.")
    lines.append("")
    lines.append(review["sign_off"]["required"])
    lines.append("")
    lines.append("## Limits of this review")
    lines.append("")
    for limit in review["limits"]:
        lines.append(f"- {limit}")
    lines.append("")
    lines.append(f"Review digest: `{review['digest']}`")
    lines.append("")
    return "\n".join(lines)


def write_review(review: dict, out_dir: Path | str = DEFAULT_OUT,
                 repo: Path | str = ROOT) -> dict:
    out = Path(out_dir)
    out = out if out.is_absolute() else Path(repo) / out
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "review.json"
    md_path = out / "review.md"
    payload = json.dumps(review, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
    json_path.write_text(payload, encoding="utf-8", newline="\n")
    md_path.write_text(render_markdown(review), encoding="utf-8", newline="\n")
    return {"json": str(json_path).replace("\\", "/"),
            "markdown": str(md_path).replace("\\", "/"),
            "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            "bytes": len(payload.encode("utf-8"))}


# ------------------------------------------------------------------------------------------ CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Adversarial external review of the G2 bridges, oil backtest, ABM->SFC and "
                    "Forecast API (docs/160); fail-closed")
    parser.add_argument("--repo", default=str(ROOT), help="repository root")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    parser.add_argument("--generated-at", default=None,
                        help="optional ISO timestamp recorded in the review (determinism check)")
    parser.add_argument("--no-help", action="store_true", help="skip the --help smoke checks")
    parser.add_argument("--no-abm", action="store_true", help="skip running the ABM demo twice")
    parser.add_argument("--no-write", action="store_true", help="compute only, write nothing")
    parser.add_argument("--json", action="store_true", help="print the review to stdout")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        review = build_review(repo=Path(args.repo), run_help=not args.no_help,
                              run_abm=not args.no_abm, generated_at=args.generated_at)
    except ReviewError as error:
        print(json.dumps({"status": "BLOCKED_REVIEW", "error": str(error)}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    written = {} if args.no_write else write_review(review, args.out, Path(args.repo))
    summary = {"status": review["summary"]["status"],
               "verdicts": review["summary"]["verdicts"],
               "failed_checks": review["summary"]["failed_checks"],
               "findings": review["summary"]["by_severity"],
               "digest": review["digest"], "out": written}
    if args.json:
        print(json.dumps(review, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    return 0 if review["summary"]["status"] != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
