#!/usr/bin/env python3
"""Hormuz closure -> US pump prices, CPI and social/political pressure (use case UC-23/05/40).

This is a **mechanical translation** of the oil module's declared scenario into US consumer prices
and simple social-pressure indicators.  It is not a forecast, not an admission, and not a causal
claim: every pass-through is an explicit assumption (`source_class=assumption`), and the oil path
itself is a research prototype (docs/147, RESEARCH_ONLY).

Usage:
  python tools/hormuz_us_impact.py --scenario hormuz_closed_6m --out build/hormuz_us
  python tools/hormuz_us_impact.py --json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.markets import get_module  # noqa: E402

SCENARIOS = ("hormuz_closed_6m", "hormuz_closed_3m", "hormuz_closed_6m_no_spr")
DEFAULT_OUT = ROOT / "build" / "hormuz_us"

# All consumer/political parameters are assumptions, not estimates.  Replace them with client data.
ASSUMPTIONS = {
    "baseline_brent_usd": {"value": 75.0, "note": "oil module base price, docs/147"},
    "baseline_gasoline_usd_gal": {"value": 3.10, "note": "US regular retail baseline (assumption)"},
    "baseline_diesel_usd_gal": {"value": 3.80, "note": "US on-highway diesel baseline (assumption)"},
    "gasoline_pass_through_usd_gal_per_usd_bbl": {
        "value": 0.025, "note": "25 cents/gal per $10/bbl; historical rule of thumb, assumption"},
    "diesel_pass_through_usd_gal_per_usd_bbl": {
        "value": 0.030, "note": "30 cents/gal per $10/bbl; assumption"},
    "cpi_gasoline_weight": {"value": 0.036, "note": "CPI-U gasoline share, assumption"},
    "food_transport_weight": {"value": 0.030, "note": "CPI weight of diesel-sensitive food/transport, assumption"},
    "diesel_to_food_transport_pass_through": {"value": 0.35, "note": "share of diesel increase passed on, assumption"},
    "second_round_core_share": {"value": 0.15, "note": "expectations/wage second round relative to gasoline, assumption"},
    "household_gallons_per_month": {"value": 90.0, "note": "US household gasoline use, assumption"},
    "low_income_gas_budget_share": {"value": 0.08, "note": "gasoline share of low-income budget, assumption"},
    "median_gas_budget_share": {"value": 0.04, "note": "gasoline share of median household budget, assumption"},
    "approval_pp_per_usd_gal": {"value": 0.5, "note": "illustrative approval pressure per $1/gal, assumption"},
}

# Social-pressure thresholds are monitoring triggers, not predictions.
STRESS_ASSUMPTIONS = {
    "crude_quality_premium_usd_bbl": {
        "value": 12.0, "low": 5.0, "high": 20.0,
        "note": "US light-sweet slate vs heavy/sour refinery diet; distillate yield penalty, assumption"},
    "freight_insurance_usd_bbl": {
        "value": 10.0, "low": 5.0, "high": 20.0,
        "note": "war-risk insurance + tanker freight surcharge, assumption"},
    "voyage_multiplier": {
        "value": 2.5, "low": 1.5, "high": 3.0,
        "note": "rerouting/queue time multiplier; 3x is an extreme upper bound, assumption"},
    "venezuela_loss_mb_d": {
        "value": 0.6, "low": 0.3, "high": 1.0,
        "note": "lost heavy-sour supply (Venezuela/sanctions), assumption"},
    "diesel_crack_extra_usd_bbl": {
        "value": 15.0, "low": 8.0, "high": 25.0,
        "note": "additional diesel crack on top of Brent move, assumption"},
    "jet_crack_extra_usd_bbl": {
        "value": 12.0, "low": 8.0, "high": 20.0,
        "note": "additional jet crack, assumption"},
    "refinery_utilization_loss_pp": {
        "value": 5.0, "low": 0.0, "high": 10.0,
        "note": "US refinery utilization loss, assumption"},
    "baseline_jet_usd_gal": {"value": 2.50, "note": "US jet fuel baseline, assumption"},
}

THRESHOLDS = {
    "gasoline_elevated": 4.50,
    "gasoline_critical": 5.50,
    "gasoline_severe": 6.50,
    "diesel_critical": 6.00,
    "cpi_headline_critical": 5.0,
    "real_income_shock_critical": 5.0,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _price_range(result) -> tuple[float, float, float]:
    block = result.summary["brent_range_usd_per_bbl"]
    return float(block["low"]), float(block["central"]), float(block["high"])


def _consumer_block(price: float, peak_range: tuple[float, float, float], a: dict) -> dict:
    base = a["baseline_brent_usd"]["value"]
    delta = price - base
    gas = a["baseline_gasoline_usd_gal"]["value"] + delta * a["gasoline_pass_through_usd_gal_per_usd_bbl"]["value"]
    diesel = a["baseline_diesel_usd_gal"]["value"] + delta * a["diesel_pass_through_usd_gal_per_usd_bbl"]["value"]
    gas_pct = gas / a["baseline_gasoline_usd_gal"]["value"] - 1.0
    diesel_pct = diesel / a["baseline_diesel_usd_gal"]["value"] - 1.0

    direct = a["cpi_gasoline_weight"]["value"] * gas_pct
    food_transport = (a["food_transport_weight"]["value"]
                      * a["diesel_to_food_transport_pass_through"]["value"] * diesel_pct)
    second_round = a["second_round_core_share"]["value"] * direct
    core = second_round + food_transport
    headline = direct + core

    household = a["household_gallons_per_month"]["value"] * (gas - a["baseline_gasoline_usd_gal"]["value"])
    low_budget_shock = (household / (a["household_gallons_per_month"]["value"] * a["baseline_gasoline_usd_gal"]["value"]
                                     / a["low_income_gas_budget_share"]["value"]))
    median_budget_shock = (household / (a["household_gallons_per_month"]["value"] * a["baseline_gasoline_usd_gal"]["value"]
                                        / a["median_gas_budget_share"]["value"]))
    approval = a["approval_pp_per_usd_gal"]["value"] * (gas - a["baseline_gasoline_usd_gal"]["value"])

    # Range at peak from the oil module's declared sensitivity variants, same pass-through.
    peak_low, _, peak_high = peak_range
    gas_low = a["baseline_gasoline_usd_gal"]["value"] + (peak_low - base) * a["gasoline_pass_through_usd_gal_per_usd_bbl"]["value"]
    gas_high = a["baseline_gasoline_usd_gal"]["value"] + (peak_high - base) * a["gasoline_pass_through_usd_gal_per_usd_bbl"]["value"]

    return {
        "brent_usd_bbl": round(price, 2),
        "brent_delta_pct": round(100.0 * delta / base, 2),
        "gasoline_usd_gal": round(gas, 2),
        "gasoline_delta_pct": round(100.0 * gas_pct, 2),
        "diesel_usd_gal": round(diesel, 2),
        "diesel_delta_pct": round(100.0 * diesel_pct, 2),
        "cpi_direct_pp": round(100.0 * direct, 2),
        "cpi_core_round_pp": round(100.0 * core, 2),
        "cpi_headline_pp": round(100.0 * headline, 2),
        "household_monthly_cost_usd": round(household, 2),
        "household_annual_cost_usd": round(household * 12, 2),
        "low_income_budget_shock_pct": round(100.0 * low_budget_shock, 2),
        "median_budget_shock_pct": round(100.0 * median_budget_shock, 2),
        "approval_pressure_pp": round(-approval, 2),
        "gasoline_range_usd_gal": {"low": round(gas_low, 2), "high": round(gas_high, 2)},
    }


def _risk_block(peak: dict) -> dict:
    gas = peak["gasoline_usd_gal"]
    diesel = peak["diesel_usd_gal"]
    cpi = peak["cpi_headline_pp"]
    shock = peak["low_income_budget_shock_pct"]
    score = 0
    reasons = []
    if gas >= THRESHOLDS["gasoline_elevated"]:
        score += 25
        reasons.append("gasoline >= $4.50")
    if gas >= THRESHOLDS["gasoline_critical"]:
        score += 25
        reasons.append("gasoline >= $5.50")
    if gas >= THRESHOLDS["gasoline_severe"]:
        score += 15
        reasons.append("gasoline >= $6.50")
    if diesel >= THRESHOLDS["diesel_critical"]:
        score += 10
        reasons.append("diesel >= $6.00")
    if cpi >= THRESHOLDS["cpi_headline_critical"]:
        score += 15
        reasons.append("headline CPI +5 pp or more")
    if shock >= THRESHOLDS["real_income_shock_critical"]:
        score += 10
        reasons.append("low-income budget shock >= 5%")
    score = min(score, 100)
    band = "low" if score < 30 else "elevated" if score < 60 else "high" if score < 85 else "severe"
    return {"score_0_100": score, "band": band, "reasons": reasons,
            "note": "illustrative index on declared thresholds; not a probability of unrest"}


def _stress_block(report: dict) -> dict:
    """Add the user's stress factors: crude slate, Venezuela, freight/insurance, longer voyage."""
    a = STRESS_ASSUMPTIONS
    peak = report["peak"]
    brent = peak["brent_usd_bbl"]
    quality = a["crude_quality_premium_usd_bbl"]["value"]
    freight = a["freight_insurance_usd_bbl"]["value"]
    # The user's factors must actually move the number: Venezuela loss and refinery outages
    # tighten heavy-sour availability (quality premium), longer voyages multiply freight.
    quality_eff = (quality
                   + 10.0 * a["venezuela_loss_mb_d"]["value"]
                   + 1.5 * a["refinery_utilization_loss_pp"]["value"])
    freight_eff = freight * a["voyage_multiplier"]["value"] / 2.5
    delivered_delta = (brent - 75.0) + quality_eff + freight_eff
    gas = 3.10 + (brent - 75.0) * 0.025 + 0.5 * quality_eff * 0.025
    diesel = 3.80 + delivered_delta * 0.030 + a["diesel_crack_extra_usd_bbl"]["value"] / 42.0
    jet = a["baseline_jet_usd_gal"]["value"] + delivered_delta * 0.032 + a["jet_crack_extra_usd_bbl"]["value"] / 42.0
    gas_pct = gas / 3.10 - 1.0
    diesel_pct = diesel / 3.80 - 1.0
    jet_pct = jet / a["baseline_jet_usd_gal"]["value"] - 1.0
    direct = 0.036 * gas_pct
    freight_food = 0.04 * 0.40 * diesel_pct
    airfare = 0.007 * 0.60 * jet_pct
    headline = 100.0 * (direct + freight_food + airfare)
    risk_score = min(100, 45 + (20 if diesel >= 8.0 else 0) + (15 if jet >= 5.0 else 0) + (10 if headline >= 6.0 else 0))
    band = "low" if risk_score < 30 else "elevated" if risk_score < 60 else "high" if risk_score < 85 else "severe"
    return {
        "status": "STRESS_ADDONS",
        "assumptions": a,
        "effective_quality_premium_usd_bbl": round(quality_eff, 2),
        "effective_freight_usd_bbl": round(freight_eff, 2),
        "delivered_crude_delta_usd_bbl": round(delivered_delta, 2),
        "gasoline_usd_gal": round(gas, 2),
        "diesel_usd_gal": round(diesel, 2),
        "jet_usd_gal": round(jet, 2),
        "diesel_delta_pct": round(100.0 * diesel_pct, 2),
        "jet_delta_pct": round(100.0 * jet_pct, 2),
        "cpi_headline_pp": round(headline, 2),
        "social_political": {"score_0_100": risk_score, "band": band,
                             "note": "illustrative stress index; not a probability of unrest"},
        "checks": {
            "diesel_above_gasoline": diesel > gas,
            "jet_positive": jet > 0,
            "delivered_above_brent": delivered_delta > (brent - 75.0),
            "headline_positive": headline > 0,
        },
    }


def build_report(scenario: str, generated_at: str | None = None, stress: bool = False) -> dict:
    module = get_module("oil.brent")
    result = module.run(scenario)
    peak_low, peak_central, peak_high = _price_range(result)
    path = []
    for row in result.summary["monthly_path"]:
        path.append(_consumer_block(float(row["price"]), (peak_low, peak_central, peak_high), ASSUMPTIONS))
    peak = max(path, key=lambda row: row["gasoline_usd_gal"])
    return {
        "schema": "amfe.hormuz_us_impact.v1",
        "generated_at": generated_at,
        "scenario": scenario,
        "status": "RESEARCH_ONLY",
        "admission": False,
        "causal_claim": False,
        "oil_module": {
            "price_peak_usd_bbl": round(float(result.summary["price_peak"]), 2),
            "price_mean_usd_bbl": round(float(result.summary["price_mean"]), 2),
            "price_range_usd_bbl": {"low": round(peak_low, 2), "central": round(peak_central, 2), "high": round(peak_high, 2)},
            "note": "range is over declared sensitivity variants, not a confidence interval",
        },
        "assumptions": {key: value for key, value in ASSUMPTIONS.items()},
        "thresholds": THRESHOLDS,
        "monthly": path,
        "peak": peak,
        "social_political": _risk_block(peak),
        "policy_options": [
            "SPR release (already 150 mb in the central scenario)",
            "fuel waivers / blending flexibility",
            "diplomatic de-escalation and convoy protection",
            "targeted transfers to low-income households",
            "strategic demand measures (public transport, speed limits)",
        ],
        "monitoring": [
            "AAA US regular gasoline weekly",
            "EIA weekly stocks and refinery utilization",
            "CPI gasoline/energy and inflation expectations",
            "consumer sentiment and presidential approval",
            "freight/trucker actions and regional outages",
        ],
        "checks": {
            "path_months": len(path),
            "prices_positive": all(row["brent_usd_bbl"] > 0 for row in path),
            "gasoline_positive": all(row["gasoline_usd_gal"] > 0 for row in path),
            "cpi_sign_consistent": all(row["cpi_headline_pp"] >= 0 for row in path),
            "range_ordered": peak_low <= peak_central <= peak_high,
        },
        "limitations": [
            "oil module is RESEARCH_ONLY and not calibrated out-of-sample",
            "pass-through, CPI weights and approval sensitivity are assumptions, not estimates",
            "no substitution, inventories, refining constraints or policy reaction in the price path",
            "social risk is an illustrative index, not a probability of unrest or a causal claim",
            "one-month steps; the CPI peak may lag the oil peak by 1-3 months",
        ],
        "stress": _stress_block({"peak": peak}) if stress else None,
        "content_sha256": None,
    }


def render_markdown(report: dict) -> str:
    peak = report["peak"]
    oil = report["oil_module"]
    lines = [
        f"# Ормуз: цены в США и социально-политическое давление ({report['scenario']})",
        "",
        f"Статус: **{report['status']}**, admission=false, causal_claim=false. "
        "Механический пересчёт сценария нефтяного модуля в потребительские цены; не прогноз.",
        "",
        f"Brent: пик {oil['price_peak_usd_bbl']} USD/bbl, среднее {oil['price_mean_usd_bbl']}, "
        f"диапазон пика {oil['price_range_usd_bbl']['low']}–{oil['price_range_usd_bbl']['high']}.",
        "",
        "| Месяц | Brent | Бензин $/gal | Дизель $/gal | CPI headline, п.п. | Домохозяйство, $/мес |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for index, row in enumerate(report["monthly"], start=1):
        lines.append(f"| {index} | {row['brent_usd_bbl']} | {row['gasoline_usd_gal']} | "
                     f"{row['diesel_usd_gal']} | {row['cpi_headline_pp']} | {row['household_monthly_cost_usd']} |")
    lines += [
        "",
        f"Пик давления: бензин **${peak['gasoline_usd_gal']}/gal** "
        f"(диапазон ${peak['gasoline_range_usd_gal']['low']}–{peak['gasoline_range_usd_gal']['high']}), "
        f"дизель **${peak['diesel_usd_gal']}/gal**, CPI **+{peak['cpi_headline_pp']} п.п.**, "
        f"домохозяйство **+${peak['household_monthly_cost_usd']}/мес**.",
        "",
        f"Социальный риск: **{report['social_political']['band']}** "
        f"({report['social_political']['score_0_100']}/100); причины: "
        + (", ".join(report["social_political"]["reasons"]) or "ни один порог не пройден") + ".",
        "",
        "Ограничения: pass-through и веса CPI — допущения; CPI может отставать от нефти на 1–3 месяца; "
        "индекс риска иллюстративный, не вероятность беспорядков.",
        "",
    ]
    if report.get("stress"):
        s = report["stress"]
        lines += [
            "",
            "## Стресс-надбавка: сланцевый слэт, Венесуэла, фрахт/страховка, обход",
            "",
            f"Дополнительно к базовому сценарию (допущения): качество сырья +${s['assumptions']['crude_quality_premium_usd_bbl']['value']}/bbl, "
            f"фрахт/страховка +${s['assumptions']['freight_insurance_usd_bbl']['value']}/bbl, обход ×{s['assumptions']['voyage_multiplier']['value']}, "
            f"потеря Венесуэлы {s['assumptions']['venezuela_loss_mb_d']['value']} mb/d.",
            "",
            f"Дизель **${s['diesel_usd_gal']}/gal** (+{s['diesel_delta_pct']}%), бензин ${s['gasoline_usd_gal']}, "
            f"авиакеросин **${s['jet_usd_gal']}/gal** (+{s['jet_delta_pct']}%), CPI +{s['cpi_headline_pp']} п.п.",
            f"Социальный риск: **{s['social_political']['band']}** ({s['social_political']['score_0_100']}/100).",
            "",
        ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hormuz -> US pump prices/CPI/social pressure (RESEARCH_ONLY)")
    parser.add_argument("--scenario", choices=SCENARIOS, default="hormuz_closed_6m")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--stress", action="store_true", help="add crude-slate/Venezuela/freight/voyage stress factors")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = build_report(args.scenario, args.generated_at, stress=args.stress)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    digest = _sha(text)
    report["content_sha256"] = digest
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.json:
        print(text, end="")
        return 0
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_bytes(text.encode("utf-8"))
    (out / "report.md").write_bytes(render_markdown(report).encode("utf-8"))
    print(json.dumps({"status": report["status"], "scenario": report["scenario"],
                      "brent_peak": report["oil_module"]["price_peak_usd_bbl"],
                      "gasoline_peak": report["peak"]["gasoline_usd_gal"],
                      "cpi_peak_pp": report["peak"]["cpi_headline_pp"],
                      "risk": report["social_political"]["band"],
                      "stress": (report["stress"] or {}).get("social_political", {}).get("band") if report.get("stress") else None,
                      "sha256": digest, "out": str(out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
