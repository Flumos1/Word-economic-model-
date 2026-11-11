#!/usr/bin/env python3
"""NF-SIM: the noise-floor study of the frozen evaluation protocol v4.

Protocol v4, admission.threshold_rule.noise_floor_study: simulate pairs of statistically equivalent
forecasting methods that differ only in estimation noise, on the effective REAL_TIME lengths of the
evaluation windows, and for every (target, horizon, loss, window) compute q05, the 5th percentile of
the relative loss candidate/comparator. The C1 threshold is min(0.97, q05). The combination variant
of the primary comparator is repeated and the stricter threshold is used.

Deterministic (fixed seed), no network. Writes protocols/nf_sim_thresholds.json and its .sha256.
Calibration parameters come from protocols/nf_sim_config.json (produced by tools/nf_sim_calibrate.py
from the local retrieved data; only metadata is committed).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "protocols" / "nf_sim_config.json"
OUT = ROOT / "protocols" / "nf_sim_thresholds.json"
PIN = ROOT / "protocols" / "nf_sim_thresholds.sha256"

TAUS = np.round(np.arange(0.05, 0.96, 0.05), 2)
from statistics import NormalDist

ZTAUS = np.array([NormalDist().inv_cdf(float(t)) for t in TAUS])

WINDOWS = {
    "T1_CPI_YOY": {"W1": (1990, 1, 2006, 12), "W2": (2007, 1, 2019, 12), "freq": "M",
                   "horizons": [1, 3, 6, 12], "losses": ["RMSE", "CRPS"], "windows": [156, 180]},
    "T2_GDP_GROWTH": {"W1": (1990, 1, 2006, 4), "W2": (2007, 1, 2019, 4), "freq": "Q",
                      "horizons": [0, 1, 2, 4], "losses": ["RMSE", "CRPS"], "windows": [52, 60]},
    "T3_RECESSION": {"W1": (1996, 12, 2006, 12), "W2": (2007, 1, 2019, 12), "freq": "M",
                     "horizons": [3, 6, 12], "losses": ["Brier", "log_score"], "windows": [180, 240]},
}


def periods(start_year: int, start_month: int, end_year: int, end_month: int, freq: str) -> int:
    if freq == "M":
        return (end_year - start_year) * 12 + (end_month - start_month) + 1
    return (end_year - start_year) * 4 + (end_month - start_month) + 1


def effective_length(n_periods: int, h: int) -> int:
    hh = max(1, h)
    return max(12, n_periods - hh - (hh - 1))


def sim_ar1(reps: int, n: int, phi: float, const: float, sigma: float, rng) -> np.ndarray:
    eps = rng.normal(0.0, sigma, size=(reps, n))
    y = np.empty((reps, n))
    y[:, 0] = const / max(1e-9, 1.0 - phi) + eps[:, 0]
    for t in range(1, n):
        y[:, t] = const + phi * y[:, t - 1] + eps[:, t]
    return y


def rolling_ar1(y: np.ndarray, origins: np.ndarray, window: int, h: int):
    """Vectorised rolling OLS AR(1) forecasts for every replication and origin."""
    reps, n = y.shape
    x = y[:, :-1]
    z = y[:, 1:]

    def cum(a):
        return np.concatenate([np.zeros((a.shape[0], 1)), np.cumsum(a, axis=1)], axis=1)

    Cx, Cz = cum(x), cum(z)
    Cxx, Czz, Cxz = cum(x * x), cum(z * z), cum(x * z)
    t = origins
    a = np.maximum(0, t - window)
    bw = np.maximum(10, t - 1 - a)
    Sx = Cx[:, t - 1] - Cx[:, a]
    Sz = Cz[:, t - 1] - Cz[:, a]
    Sxx = Cxx[:, t - 1] - Cxx[:, a]
    Szz = Czz[:, t - 1] - Czz[:, a]
    Sxz = Cxz[:, t - 1] - Cxz[:, a]
    den = Sxx - Sx * Sx / bw
    phi = np.where(np.abs(den) < 1e-12, 0.0, (Sxz - Sx * Sz / bw) / np.where(np.abs(den) < 1e-12, 1.0, den))
    phi = np.clip(phi, -0.98, 0.98)
    c = (Sz - phi * Sx) / bw
    ssr = (Szz - 2 * phi * Sxz + phi * phi * Sxx - 2 * c * Sz + 2 * phi * c * Sx + bw * c * c)
    var = np.maximum(1e-12, ssr / np.maximum(1, bw - 2))
    level = y[:, t]
    hh = max(1, h)
    power = phi.copy()
    mean = c.copy()
    for _ in range(1, hh):
        mean = mean + power * c
        power = power * phi
    mu = mean + power * level
    with np.errstate(invalid="ignore"):
        vh = np.where(np.abs(phi) < 1 - 1e-9,
                      var * (1 - phi ** (2 * hh)) / (1 - phi * phi + 1e-12), var * hh)
    sigma = np.sqrt(np.maximum(1e-12, vh))
    return mu, sigma


def crps_grid(target: np.ndarray, mu: np.ndarray, sigma: np.ndarray) -> np.ndarray:
    """Protocol CRPS per replication: twice the mean pinball loss over the 19 quantiles."""
    total = np.zeros(target.shape[0])
    for tau, z in zip(TAUS, ZTAUS):
        q = mu + sigma * z
        diff = target - q
        total += np.mean(np.where(diff < 0, (tau - 1.0) * diff, tau * diff), axis=1)
    return 2.0 * total / len(TAUS)


def simulate_point(cfg: dict, h: int, n_eff: int, reps: int, windows: list, rng) -> dict:
    phi, const, sigma = cfg["phi"], cfg["const"], cfg["sigma"]
    hh = max(1, h)
    burn = max(windows) + hh + 5
    n = burn + n_eff + hh + 2
    y = sim_ar1(reps, n, phi, const, sigma, rng)
    origins = np.arange(burn, burn + n_eff)
    target = y[:, origins + hh]
    mu, sig = {}, {}
    for w in windows:
        mu[w], sig[w] = rolling_ar1(y, origins, w, hh)

    def per_rep(loss, m, s):
        if loss == "RMSE":
            return np.sqrt(np.mean((target - m) ** 2, axis=1))
        return crps_grid(target, m, s)

    out = {}
    for loss in ("RMSE", "CRPS"):
        a = per_rep(loss, mu[windows[0]], sig[windows[0]])
        b = per_rep(loss, mu[windows[1]], sig[windows[1]])
        combo = 0.5 * (mu[windows[0]] + mu[windows[1]])
        combo_sig = np.sqrt(0.5 * (sig[windows[0]] ** 2 + sig[windows[1]] ** 2))
        c = per_rep(loss, combo, combo_sig)
        # exchangeable directions: A/B and B/A, each against the combination
        single = np.concatenate([a / b, b / a])
        combination = np.concatenate([a / c, b / c])
        out.setdefault("single", {})[loss] = single
        out.setdefault("combination", {})[loss] = combination
    return out


def simulate_event(cfg: dict, h: int, n_eff: int, reps: int, windows: list, rng) -> dict:
    p_er, p_re = cfg["p_ER"], cfg["p_RE"]
    burn = max(windows) + h + 5
    n = burn + n_eff + h + 2
    states = np.zeros((reps, n), dtype=np.int8)
    states[:, 0] = (rng.random(reps) < p_er / (p_er + p_re)).astype(np.int8)
    for t in range(1, n):
        u = rng.random(reps)
        stay_e = states[:, t - 1] == 0
        states[:, t] = np.where(stay_e, (u < p_er).astype(np.int8), (u >= p_re).astype(np.int8))
    origins = np.arange(burn, burn + n_eff)
    C = np.concatenate([np.zeros((reps, 1), dtype=np.int32),
                        np.cumsum(states, axis=1, dtype=np.int32)], axis=1)
    events = (C[:, origins + 1 + h] - C[:, origins + 1] > 0).astype(float)
    trans_er = (states[:, :-1] == 0) & (states[:, 1:] == 1)
    exp_any = states[:, :-1] == 0
    rec_any = states[:, :-1] == 1

    def cum(a):
        return np.concatenate([np.zeros((a.shape[0], 1)), np.cumsum(a, axis=1)], axis=1)

    C_er, C_exp, C_rec = cum(trans_er), cum(exp_any), cum(rec_any)

    def est(window):
        a = np.maximum(0, origins - window)
        e0 = (C_er[:, origins - 1] - C_er[:, a]).astype(float)
        n0 = (C_exp[:, origins - 1] - C_exp[:, a]).astype(float)
        e1 = (C_rec[:, origins - 1] - C_rec[:, a]).astype(float)
        n1 = (C_rec[:, origins - 1] - C_rec[:, a]).astype(float)
        t_er = (e0 + 1.0) / (n0 + 2.0)
        t_re = (e1 + 1.0) / (n1 + 2.0)
        s = states[:, origins]
        p_no = np.where(s == 1, t_re ** h, (1.0 - t_er) ** h)
        return np.clip(1.0 - p_no, 0.01, 0.99)

    probs = {w: est(w) for w in windows}

    def per_rep(kind, p):
        if kind == "Brier":
            return np.mean((p - events) ** 2, axis=1)
        return -np.mean(events * np.log(p) + (1 - events) * np.log(1 - p), axis=1)

    out = {}
    combo = 0.5 * (probs[windows[0]] + probs[windows[1]])
    for kind in ("Brier", "log_score"):
        a = per_rep(kind, probs[windows[0]])
        b = per_rep(kind, probs[windows[1]])
        c = per_rep(kind, combo)
        out.setdefault("single", {})[kind] = np.concatenate([a / b, b / a])
        out.setdefault("combination", {})[kind] = np.concatenate([a / c, b / c])
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="NF-SIM noise-floor study")
    ap.add_argument("--config", default=str(CONFIG))
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--replications", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=20261111)
    args = ap.parse_args(argv)

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    config_hash = hashlib.sha256(Path(args.config).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    rng = np.random.default_rng(args.seed)
    table = {
        "study_id": "NF-SIM",
        "protocol": "protocols/evaluation_protocol_v4.json",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "replications": args.replications, "seed": args.seed,
        "config": str(args.config).replace("\\", "/"), "config_sha256": config_hash,
        "threshold_rule": "min(0.97, q05_single, q05_combination)",
        "estimators": "statistically equivalent estimators differing only in the estimation window",
        "entries": [],
    }
    for target, spec in WINDOWS.items():
        for window_name in ("W1", "W2"):
            n_periods = periods(*spec[window_name], spec["freq"])
            for h in spec["horizons"]:
                n_eff = effective_length(n_periods, h)
                if target == "T3_RECESSION":
                    sim = simulate_event(config[target], h, n_eff, args.replications, spec["windows"], rng)
                else:
                    sim = simulate_point(config[target], h, n_eff, args.replications, spec["windows"], rng)
                for loss in spec["losses"]:
                    q_single = float(np.percentile(sim["single"][loss], 5))
                    q_combo = float(np.percentile(sim["combination"][loss], 5))
                    entry = {
                        "target": target, "horizon": h, "loss": loss, "window": window_name,
                        "n_periods": n_periods, "effective_length": n_eff,
                        "mean_relative_loss_single": round(float(np.mean(sim["single"][loss])), 4),
                        "q05_single": round(q_single, 4),
                        "mean_relative_loss_combination": round(float(np.mean(sim["combination"][loss])), 4),
                        "q05_combination": round(q_combo, 4),
                        "threshold": round(min(0.97, q_single, q_combo), 4),
                    }
                    table["entries"].append(entry)
                    print(json.dumps(entry, ensure_ascii=False), flush=True)
    out = Path(args.out)
    out.write_text(json.dumps(table, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    digest = hashlib.sha256(out.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    PIN.write_text(f"{digest}  nf_sim_thresholds.json (sha256 of the LF-normalised file)\n",
                   encoding="utf-8", newline="\n")
    thresholds = [e["threshold"] for e in table["entries"]]
    print(json.dumps({"entries": len(table["entries"]), "min_threshold": min(thresholds),
                      "max_threshold": max(thresholds), "digest": digest}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
