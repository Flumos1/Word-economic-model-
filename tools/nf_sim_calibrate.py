#!/usr/bin/env python3
"""Calibrate the NF-SIM data-generating parameters from the local retrieved data.

Reads data/raw/latest.csv (ignored by git) and writes protocols/nf_sim_config.json (metadata:
AR(1) coefficients for inflation and GDP growth, two-state transition probabilities for the
recession indicator, and the calibration windows). Run locally after the data-refresh workflow.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
OUT = ROOT / "protocols" / "nf_sim_config.json"


def read_series(name: str) -> list:
    path = RAW / name / "latest.csv"
    rows = []
    with path.open(encoding="utf-8") as fh:
        reader = csv.reader(fh)
        next(reader)
        for row in reader:
            if len(row) >= 2 and row[1] not in ("", "."):
                rows.append((row[0][:10], float(row[1])))
    return rows


def month_index(date: str) -> int:
    parts = date.split("-")
    y, m = parts[0], parts[1]
    return int(y) * 12 + int(m) - 1


def fit_ar1(y: list) -> dict:
    x, z = y[:-1], y[1:]
    n = len(x)
    mx, mz = sum(x) / n, sum(z) / n
    sxx = sum((v - mx) ** 2 for v in x)
    sxy = sum((x[i] - mx) * (z[i] - mz) for i in range(n))
    phi = sxy / sxx if sxx else 0.0
    phi = max(-0.98, min(0.95, phi))
    c = mz - phi * mx
    var = sum((z[i] - c - phi * x[i]) ** 2 for i in range(n)) / max(1, n - 2)
    return {"phi": round(phi, 6), "const": round(c, 6), "sigma": round(var ** 0.5, 6), "n": n}


def main() -> int:
    # T1: monthly CPI y/y, 1960-01 .. 2019-12
    cpi = read_series("CPIAUCSL")
    levels = {month_index(d): v for d, v in cpi}
    yy = []
    for m in range(month_index("1960-01"), month_index("2019-12") + 1):
        if m in levels and (m - 12) in levels:
            yy.append(100.0 * (levels[m] / levels[m - 12] - 1.0))
    t1 = fit_ar1(yy)

    # T2: quarterly real GDP growth, 1960Q1 .. 2019Q4
    gdp = read_series("GDPC1")
    growth = []
    for i in range(4, len(gdp)):
        y, m, _ = gdp[i][0].split("-")
        if not ("1960-01-01" <= f"{y}-{m}-01" <= "2019-12-31"):
            continue
        growth.append(400.0 * (gdp[i][1] / gdp[i - 1][1] - 1.0) if False else 0.0)
    # recompute with log differences over the filtered range
    growth = []
    for i in range(1, len(gdp)):
        prev, cur = gdp[i - 1][0], gdp[i][0]
        if not ("1960-01-01" <= prev <= "2019-12-31"):
            continue
        growth.append(400.0 * __import__("math").log(gdp[i][1] / gdp[i - 1][1]))
    t2 = fit_ar1(growth)

    # T3: two-state transition probabilities of USREC, 1960-01 .. 2019-12
    usrec = [v for d, v in read_series("USREC") if "1960-01-01" <= d <= "2019-12-31"]
    states = [1 if v >= 0.5 else 0 for v in usrec]
    n0 = sum(1 for s in states if s == 0)
    n1 = len(states) - n0
    e0 = sum(1 for i in range(len(states) - 1) if states[i] == 0 and states[i + 1] == 1)
    e1 = sum(1 for i in range(len(states) - 1) if states[i] == 1 and states[i + 1] == 0)
    config = {
        "config_id": "NF-SIM-CONFIG",
        "calibration": "data/raw latest files (CPIAUCSL, GDPC1, USREC); windows 1960-01..2019-12",
        "T1_CPI_YOY": t1,
        "T2_GDP_GROWTH": t2,
        "T3_RECESSION": {"p_ER": round(e0 / n0, 6), "p_RE": round(e1 / n1, 6),
                         "n_expansion": n0, "n_recession": n1},
        "note": ("AR(1) approximations calibrated on the actual series; the simulation is a noise-floor "
                 "study, not a model of the economy. The raw series are not committed; see protocols/data_log.json."),
    }
    OUT.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(config, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
