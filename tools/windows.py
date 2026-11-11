#!/usr/bin/env python3
"""Evaluation origins per target, horizon and window (protocol v4).

Windows are defined by the origin and the target date (protocol v4, windows.defined_by). The T3
window W1 starts 1996-12 (windows.target_overrides, decision D-131). Origins whose target falls in
the COVID window are excluded from W2 and reported separately. T2 horizon 0 uses every month end;
horizons 1, 2, 4 use the third month end of the quarter (origin_policy.t2_origins).
"""
from __future__ import annotations

import argparse
import calendar
import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "protocols" / "evaluation_protocol_v4.json"


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def add_months(d: date, months: int) -> date:
    total = d.year * 12 + (d.month - 1) + months
    return month_end(total // 12, total % 12 + 1)


def month_iter(start: str, end: str):
    y, m = map(int, start.split("-")[:2])
    ey, em = map(int, end.split("-")[:2])
    while (y, m) <= (ey, em):
        yield month_end(y, m)
        m += 1
        if m == 13:
            y, m = y + 1, 1


def quarter_of(d: date) -> tuple:
    return d.year, (d.month - 1) // 3 + 1


def quarter_third_month_end(year: int, quarter: int) -> date:
    return month_end(year, quarter * 3)


def in_covid(period: date, covid_start: str, covid_end: str) -> bool:
    return covid_start <= period.isoformat()[:7] <= covid_end


def origins_for(protocol: dict, target: str, horizon: int, window: str):
    w = protocol["windows"]
    covid = w["origin_exclusion"]["covid_targets"]
    covid_start, covid_end = covid.split(" to ")
    start = w[window]["start"]
    end = w[window]["end"]
    override = w.get("target_overrides", {}).get(target, {})
    if window == "W1" and override.get("W1_start"):
        start = override["W1_start"]
    out = []
    if target == "T2_GDP_GROWTH":
        step = 1 if horizon == 0 else 3  # every month for h=0, third month of quarter otherwise
        current = None
        for d in month_iter(start, end):
            q = quarter_of(d)
            if step == 3 and not (d.month == q[1] * 3):
                continue
            target_quarter = q[0] * 4 + (q[1] - 1) + horizon
            ty, tq = target_quarter // 4, target_quarter % 4 + 1
            target_date = quarter_third_month_end(ty, tq)
            if not (start <= target_date.isoformat()[:7] <= end):
                continue
            if in_covid(target_date, covid_start, covid_end):
                continue
            out.append({"origin": d.isoformat(), "target": target_date.isoformat()})
        return out
    if target == "T1_CPI_YOY":
        for d in month_iter(start, end):
            target_month = add_months(d, horizon - 1)  # horizon counted from the last published CPI month t-1
            if not (start <= target_month.isoformat()[:7] <= end):
                continue
            if in_covid(target_month, covid_start, covid_end):
                continue
            out.append({"origin": d.isoformat(), "target": target_month.isoformat()})
        return out
    if target == "T3_RECESSION":
        for d in month_iter(start, end):
            interval = [add_months(d, k) for k in range(1, horizon + 1)]
            target = interval[-1]
            if not (start <= target.isoformat()[:7] <= end):
                continue
            if any(in_covid(m, covid_start, covid_end) for m in interval):
                continue
            out.append({"origin": d.isoformat(), "target": target.isoformat()})
        return out
    raise ValueError(target)


def build(protocol: dict) -> dict:
    targets = {}
    for target in protocol["targets"]:
        tid = target["id"]
        horizons = target.get("horizons_months") or target.get("horizons_quarters")
        targets[tid] = {}
        for window in ("W1", "W2", "W3"):
            targets[tid][window] = {}
            for h in horizons:
                origins = origins_for(protocol, tid, h, window)
                targets[tid][window][str(h)] = {
                    "count": len(origins),
                    "first": origins[0] if origins else None,
                    "last": origins[-1] if origins else None,
                }
    return {
        "protocol": "protocols/evaluation_protocol_v4.json",
        "generated": "2026-11-11",
        "note": ("Windows are defined by origin and target dates; T3 W1 starts 1996-12 (D-131); origins whose "
                 "target falls in the COVID window are excluded from W2 and reported separately; W3 is stress only."),
        "targets": targets,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Evaluation origins per protocol v4")
    ap.add_argument("--out", default=str(ROOT / "protocols" / "evaluation_origins.json"))
    args = ap.parse_args(argv)
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    out = build(protocol)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out["targets"], ensure_ascii=False, indent=1)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
