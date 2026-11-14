#!/usr/bin/env python3
"""AMFE weekly bulletin: publication artefact for the research product (status CANDIDATE).

The bulletin is a **derived, read-only snapshot** of already registered results: it changes no status,
recomputes no criterion and is not an admission. It reads only repository artefacts (no network, no
``data/raw/``):

  * ``build/product/forecast_latest.json`` - the machine-readable product feed (built by
    ``tools/product_pack.py``; if it is missing, the bulletin asks product_pack to build it once);
  * ``registry/forecasts.jsonl`` - the append-only forward journal (own forecasts only);
  * ``protocols/first_real_evaluation_report_v5.json`` - C1/C2/C5 evidence of the first real evaluation;
  * ``protocols/evaluation_protocol_v5.json`` + pin, R3 1.0.1, NF-SIM and the comparator composition -
    the frozen chain (versions and SHA-256 are re-verified through product_pack's loader);
  * ``protocols/regime_candidate_evaluation_report.json`` - the ENSEMBLE_REGIME near-miss (context only).

It writes two deterministic files to ``build/bulletin/<YYYY-MM-DD>.md`` and ``.json``:

  * the Markdown bulletin: status and frozen chain, the latest forecast with 50/80/90% intervals and
    control arms, what is confirmed (C1/C2/C5 PASS on T1 h=1), what is not confirmed (C4 RMSE FAIL and
    C6 zero), the ENSEMBLE_REGIME near-miss as a separate line, the change against the previous journal
    record ("первый выпуск" when there is none), disclaimers and limitations;
  * the JSON twin (schema below) with ``schema_version, generated_at, status, status_label,
    conditional_data_class, forecast{}, changes[], confirmed[], not_confirmed[], hashes{}``.

Everything except ``generated_at`` is a pure function of the inputs: the same repository and the same
``--generated-at`` produce byte-identical files. The tool never writes to the journal, the protocols,
the product pack, ``src/`` or existing tools; a missing product pack (when ``--no-build`` is given), a
stale feed, a broken pin or an inconsistent journal fails closed with exit code ``2`` and no output.

JSON schema (``schema_version`` 1.0.0):

    schema_version, bulletin_id, generated_at, date, status, status_label, conditional_data_class,
    data_class, headline, forecast {candidate, target, horizon, origin, point, median,
    quantiles_5_to_95_step_5[19], intervals {"50"|"80"|"90": {level_percent, low, low_quantile,
    high, high_quantile}}, controls {RW|EQUAL_WEIGHT_POINT|EQUAL_WEIGHT_DENSITY: {point}}, pre_origin},
    changes[], changes_note, confirmed[], not_confirmed[], near_miss_candidate,
    versions{}, hashes{}, disclaimer, limitations[], sources[]

Usage:
    python tools/weekly_bulletin.py
    python tools/weekly_bulletin.py --date 2026-11-12 --generated-at 2026-11-12T12:00:00Z
    python tools/weekly_bulletin.py --no-build          # fail closed if build/product is absent
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from datetime import date as date_type
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import product_pack  # noqa: E402  (read-only reuse of the verified product feed loader)

SCHEMA_VERSION = "1.0.0"
BULLETIN_ID = "AMFE-WEEKLY-BULLETIN"
PRODUCT_DIR = "build/product"
BULLETIN_DIR = "build/bulletin"
FEED_NAME = "forecast_latest.json"
REGIME_REPORT = "protocols/regime_candidate_evaluation_report.json"

STATUSES = ("CANDIDATE", "SHADOW_TEST", "LIMITED_DEPLOYMENT", "ACTIVE", "SUSPENDED")
CONTROL_ARMS = tuple(product_pack.CONTROL_ARMS)
CONFIRMED_CRITERIA = ("C1", "C2", "C5")
# percentile -> (low index, high index) in the 19 quantiles q05..q95
INTERVALS = {"50": (4, 14), "80": (1, 17), "90": (0, 18)}
QUANTILE_LABELS = tuple(f"q{level:02d}" for level in range(5, 100, 5))

DISCLAIMER = (
    "Это не допущенная модель и не инвестиционная рекомендация. Пакет фиксирует статус CANDIDATE и "
    "уже зарегистрированные результаты; он не меняет статусы, не пересчитывает критерии и не является "
    "инвестиционным советом."
)

SOURCE_FILES = (
    "protocols/evaluation_protocol_v5.json",
    "protocols/evaluation_protocol_v5.sha256",
    "protocols/first_real_evaluation_report_v5.json",
    "protocols/r3_specification.json",
    "protocols/nf_sim_thresholds.json",
    "protocols/nf_sim_thresholds.sha256",
    "protocols/comparator_composition_2026-11-11.json",
    "registry/forecasts.jsonl",
    "build/product/forecast_latest.json",
)


class BulletinError(RuntimeError):
    """A required input is missing, stale, broken or inconsistent: the bulletin is not written."""


# --------------------------------------------------------------------------------------- generic
def lf_sha256(path: Path) -> str:
    """SHA-256 of the LF-normalised bytes (the form the project's pins use)."""
    return product_pack.lf_sha256(Path(path))


def read_json(path: Path, label: str) -> dict:
    path = Path(path)
    if not path.is_file():
        raise BulletinError(f"{label}: нет файла {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BulletinError(f"{label}: не удалось прочитать {path}: {exc!r}") from exc
    if not isinstance(document, dict):
        raise BulletinError(f"{label}: ожидался JSON-объект")
    return document


def _fmt(value, digits: int = 4) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _short(value: str | None, size: int = 12) -> str:
    return f"{value[:size]}…" if value else "—"


def _number(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BulletinError(f"{label}: ожидалось число")
    return float(value)


def _validate_timestamp(value: str) -> str:
    try:
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise BulletinError(f"--generated-at: ожидался ISO 8601, получено {value!r}") from exc
    return str(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------------------------- inputs
def ensure_feed(root: Path, allow_build: bool = True) -> Path:
    """Return the product feed path; build it once through product_pack when it is missing."""
    root = Path(root)
    feed_path = root / PRODUCT_DIR / FEED_NAME
    if feed_path.is_file():
        return feed_path
    if not allow_build:
        raise BulletinError(
            f"нет {PRODUCT_DIR}/{FEED_NAME}; соберите продуктовый пакет "
            "(python tools/product_pack.py) или уберите --no-build")
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        code = product_pack.main(["--root", str(root), "--out-dir", str(root / PRODUCT_DIR)])
    if code != 0 or not feed_path.is_file():
        raise BulletinError(
            f"не удалось собрать {PRODUCT_DIR}/{FEED_NAME} через tools/product_pack.py "
            f"(код {code}); пакет и бюллетень не пишутся")
    return feed_path


def _latest_row(rows: list[dict], candidate: str, target: str, horizon: int) -> dict:
    matches = [row for row in rows
               if row.get("candidate_id") == candidate
               and row.get("target") == target and row.get("horizon") == horizon]
    if not matches:
        raise BulletinError(f"в журнале нет строки кандидата {candidate} для {target} h={horizon}")
    return max(matches,
               key=lambda row: (str(row.get("origin")), str(row.get("registered_at") or "")))


def _validate_quantiles(values, label: str) -> list[float]:
    if not isinstance(values, list) or len(values) != 19:
        raise BulletinError(f"{label}: ожидался список из 19 квантилей")
    numbers = [_number(value, label) for value in values]
    if any(b <= a for a, b in zip(numbers, numbers[1:])):
        raise BulletinError(f"{label}: квантили не возрастают строго")
    return numbers


def load_context(root: Path, allow_build: bool = True) -> dict:
    """Load and cross-check the feed, the frozen chain and the forward journal (fail-closed)."""
    root = Path(root)
    feed_path = ensure_feed(root, allow_build)
    feed = read_json(feed_path, "фид build/product/forecast_latest.json")
    required = ("schema_version", "status", "status_label", "conditional_data_class", "candidate",
                "target", "horizon", "origin", "point", "quantiles_5_to_95_step_5", "controls",
                "c4_status", "c6_status", "protocol", "source_hashes")
    missing = [key for key in required if key not in feed]
    if missing:
        raise BulletinError(f"фид: нет обязательных полей {missing}")
    if feed["status"] not in STATUSES:
        raise BulletinError(f"фид: неизвестный статус {feed['status']!r}")
    if not isinstance(feed["horizon"], int) or isinstance(feed["horizon"], bool):
        raise BulletinError("фид: horizon должен быть целым числом")
    quantiles = _validate_quantiles(feed["quantiles_5_to_95_step_5"], "фид.квантили")
    point = _number(feed["point"], "фид.point")
    if set((feed.get("controls") or {}).keys()) != set(CONTROL_ARMS):
        raise BulletinError(f"фид.controls: ожидались плечи {list(CONTROL_ARMS)}")
    for arm in CONTROL_ARMS:
        _number((feed["controls"][arm] or {}).get("point"), f"фид.controls.{arm}.point")

    inputs = product_pack.load_inputs(root)  # verifies the frozen chain and every pin
    report = inputs["report"]
    if feed["candidate"] != report.get("candidate") or feed["target"] != report.get("target") \
            or feed["horizon"] != report.get("horizon"):
        raise BulletinError("фид не соответствует отчёту первой реальной оценки (кандидат/цель/горизонт)")
    if feed["protocol"].get("sha256") != inputs["hashes"]["protocol"]:
        raise BulletinError("фид: SHA-256 протокола не совпадает с замороженным файлом")
    if feed["source_hashes"].get("journal") != inputs["hashes"]["journal"]:
        raise BulletinError("фид: SHA-256 журнала не совпадает с текущим registry/forecasts.jsonl")

    row = _latest_row(inputs["rows"], feed["candidate"], feed["target"], feed["horizon"])
    if row.get("origin") != feed["origin"]:
        raise BulletinError("фид устарел: origin не совпадает с последней строкой журнала")
    if _number(row.get("point"), "журнал.point") != point:
        raise BulletinError("фид устарел: точка не совпадает с последней строкой журнала")
    if _validate_quantiles(row.get("quantiles_5_to_95_step_5"), "журнал.квантили") != quantiles:
        raise BulletinError("фид устарел: квантили не совпадают с последней строкой журнала")

    regime = None
    regime_path = root / REGIME_REPORT
    if regime_path.is_file():
        regime = read_json(regime_path, "отчёт ENSEMBLE_REGIME")
    return {"root": root, "feed": feed, "inputs": inputs, "row": row, "regime": regime,
            "quantiles": quantiles, "point": point}


# ------------------------------------------------------------------------------------ document
def _criterion_value(block: dict, window: str, loss: str, field: str):
    return (((block.get("windows") or {}).get(window) or {}).get(loss) or {}).get(field)


def _criterion_evidence(block: dict) -> dict:
    evidence = {}
    for window in sorted((block.get("windows") or {}).keys()):
        losses = {}
        for loss in sorted((block["windows"][window] or {}).keys()):
            values = block["windows"][window][loss] or {}
            losses[loss] = {key: values.get(key)
                            for key in ("relative_loss", "adjusted_p_value", "p_value", "threshold")
                            if values.get(key) is not None}
        evidence[window] = losses
    return evidence


def _confirmed_summary(key: str, block: dict) -> str:
    if key == "C1":
        return (f"относительная потеря RMSE/CRPS: "
                f"{_fmt(_criterion_value(block, 'W1', 'RMSE', 'relative_loss'))}/"
                f"{_fmt(_criterion_value(block, 'W1', 'CRPS', 'relative_loss'))} на W1 и "
                f"{_fmt(_criterion_value(block, 'W2', 'RMSE', 'relative_loss'))}/"
                f"{_fmt(_criterion_value(block, 'W2', 'CRPS', 'relative_loss'))} на W2 при пороге "
                f"{_fmt(_criterion_value(block, 'W1', 'RMSE', 'threshold'), 2)}")
    if key == "C2":
        fwer = ((block.get("multiplicity") or {}).get("fwer"))
        return (f"Romano–Wolf, скорректированные p: "
                f"{_fmt(_criterion_value(block, 'W1', 'RMSE', 'adjusted_p_value'))}/"
                f"{_fmt(_criterion_value(block, 'W1', 'CRPS', 'adjusted_p_value'))} (W1) и "
                f"{_fmt(_criterion_value(block, 'W2', 'RMSE', 'adjusted_p_value'))}/"
                f"{_fmt(_criterion_value(block, 'W2', 'CRPS', 'adjusted_p_value'))} (W2), "
                f"FWER {_fmt(fwer, 2) if fwer is not None else '0.10'}")
    return "кандидат не хуже компаратора на вторичных фактах (p ≥ 0.10)"


def _confirmed(report: dict) -> list[dict]:
    criteria = ((report.get("admission") or {}).get("criteria") or {})
    items = []
    for key in CONFIRMED_CRITERIA:
        block = criteria.get(key) or {}
        if block.get("pass") is not True:
            continue
        items.append({"id": key, "status": "PASS", "gating": bool(block.get("gating")),
                      "summary": _confirmed_summary(key, block),
                      "evidence": _criterion_evidence(block)})
    return items


def _not_confirmed(feed: dict) -> list[dict]:
    c4 = feed["c4_status"]
    episodes = {episode.get("episode"): episode for episode in (c4.get("episodes") or [])}
    episode_1990 = (episodes.get("1990-91") or {}).get("rmse_relative_loss")
    episode_2001 = (episodes.get("2001") or {}).get("rmse_relative_loss")
    c4_summary = (
        f"относительная потеря {_fmt(c4.get('rmse_relative_loss'))} в рецессионных месяцах W1_EXT "
        f"(порог ≤ 1.00); эпизоды хуже компаратора: 1990-91 — {_fmt(episode_1990, 3)}, "
        f"2001 — {_fmt(episode_2001, 3)}; CRPS PASS ({_fmt(c4.get('crps_relative_loss'))}), "
        "но C4 требует прохождения по каждому применимому loss.")
    c6 = feed["c6_status"]
    resolved = int(c6.get("n_resolved") or 0)
    distinct = 0 if resolved == 0 else None
    c6_summary = (
        f"разрешённых прогнозов {resolved} из {c6.get('min_forecasts')} "
        f"({resolved}/{c6.get('min_forecasts')}), различных целевых периодов "
        f"{distinct} из {c6.get('min_distinct_target_periods')} "
        f"({distinct}/{c6.get('min_distinct_target_periods')}); "
        f"статус {c6.get('status')}.")
    return [
        {"id": "C4", "status": "FAIL", "gating": True,
         "rmse": {"relative_loss": c4.get("rmse_relative_loss"),
                  "pass": c4.get("rmse_pass"),
                  "episodes": {"1990-91": episode_1990, "2001": episode_2001}},
         "crps": {"relative_loss": c4.get("crps_relative_loss"), "pass": c4.get("crps_pass")},
         "summary": c4_summary},
        {"id": "C6", "status": "NOT_PASSED", "gating": True,
         "n_resolved": resolved,
         "min_forecasts": c6.get("min_forecasts"),
         "min_distinct_target_periods": c6.get("min_distinct_target_periods"),
         "reported_status": c6.get("status"),
         "summary": c6_summary},
    ]


def _near_miss(regime: dict | None) -> dict:
    if not regime:
        return {"id": "ENSEMBLE_REGIME", "status": "NOT_AVAILABLE",
                "summary": "отчёт `protocols/regime_candidate_evaluation_report.json` отсутствует."}
    criteria = ((regime.get("admission") or {}).get("criteria") or {})
    c4 = ((criteria.get("C4") or {}).get("per_window") or {})
    w1_rmse = ((c4.get("W1") or {}).get("RMSE") or {}).get("relative_loss_recession")
    w2_rmse = ((c4.get("W2") or {}).get("RMSE") or {}).get("relative_loss_recession")
    w1_crps = ((c4.get("W1") or {}).get("CRPS") or {}).get("relative_loss_recession")
    w2_crps = ((c4.get("W2") or {}).get("CRPS") or {}).get("relative_loss_recession")
    c1_w2 = _criterion_value(criteria.get("C1") or {}, "W2", "RMSE", "relative_loss")
    summary = (
        f"C4 RMSE {_fmt(w1_rmse)} (W1) / {_fmt(w2_rmse)} (W2) — лучше компаратора, "
        f"но C4 CRPS {_fmt(w1_crps)} (W1) / {_fmt(w2_crps)} (W2) хуже "
        f"(≈{_fmt(w1_crps, 3)}/{_fmt(w2_crps, 3)}); C1 на W2 {_fmt(c1_w2)} при пороге 0.97 "
        f"(FAIL). Итог: `{regime.get('status') or 'CANDIDATE'}` (near-miss), "
        "SHADOW_TEST не достигнут.")
    return {"id": "ENSEMBLE_REGIME", "status": "NEAR_MISS",
            "candidate_status": regime.get("status") or "CANDIDATE",
            "c4_rmse": {"W1": w1_rmse, "W2": w2_rmse},
            "c4_crps": {"W1": w1_crps, "W2": w2_crps},
            "c1_w2_rmse": c1_w2,
            "summary": summary}


def _changes(rows: list[dict], candidate: str, target: str, horizon: int, origin: str,
             point: float, quantiles: list[float]) -> tuple[list[dict], str, dict | None]:
    pair_rows = [row for row in rows
                 if row.get("candidate_id") == candidate and row.get("target") == target
                 and row.get("horizon") == horizon]
    distinct_origins = sorted({str(row.get("origin")) for row in pair_rows})
    earlier = [row for row in pair_rows if str(row.get("origin")) < str(origin)]
    previous = None
    if earlier:
        previous_origin = max(str(row["origin"]) for row in earlier)
        previous = max([row for row in earlier if str(row["origin"]) == previous_origin],
                       key=lambda row: str(row.get("registered_at") or ""))
    changes: list[dict] = []
    if previous is None:
        note = (f"Первый выпуск: предыдущей записи журнала по паре `{target}` h={horizon} нет; "
                f"в журнале {len(rows)} строк одного origin `{origin}` "
                f"(различных origin: {len(distinct_origins)}).")
        return changes, note, None
    previous_quantiles = _validate_quantiles(previous.get("quantiles_5_to_95_step_5"),
                                             "журнал.предыдущие квантили")
    previous_point = _number(previous.get("point"), "журнал.предыдущая точка")
    change = {
        "from_origin": previous.get("origin"),
        "to_origin": origin,
        "from_registered_at": previous.get("registered_at"),
        "point_delta": point - previous_point,
        "point_delta_percent": ((point - previous_point) / previous_point * 100.0)
        if previous_point else None,
        "median_delta": quantiles[9] - previous_quantiles[9],
        "interval_90_delta": [quantiles[0] - previous_quantiles[0],
                              quantiles[18] - previous_quantiles[18]],
    }
    changes.append(change)
    note = (f"изменение к предыдущей записи журнала: origin `{previous.get('origin')}` → "
            f"`{origin}` (зарегистрирована {previous.get('registered_at')}).")
    return changes, note, change


def _research_candidate(root: Path) -> dict | None:
    """Optional block: the research candidate currently on SHADOW_TEST (tools/shadow_track.py)."""
    path = Path(root) / "build" / "shadow" / "shadow_status.json"
    if not path.exists():
        return None
    document = read_json(path, "shadow status")
    c6 = document.get("c6") or {}
    return {
        "id": (document.get("candidate") or {}).get("id"),
        "status": (document.get("candidate") or {}).get("status"),
        "status_label": (document.get("candidate") or {}).get("status_label"),
        "conditional_data_class": bool((document.get("candidate") or {}).get("conditional_data_class")),
        "track_status": document.get("status"),
        "n_resolved": c6.get("n_resolved"),
        "min_forecasts": c6.get("min_forecasts"),
        "distinct_target_periods": c6.get("distinct_target_periods"),
        "min_distinct_target_periods": c6.get("min_distinct_target_periods"),
        "alerts": [{"code": a.get("code"), "severity": a.get("severity"),
                    "message": a.get("message")} for a in document.get("alerts") or []],
        "last_registered_at": (document.get("journal") or {}).get("last_registered_at"),
        "estimated_gate_date": (document.get("projection") or {}).get("estimated_gate_date"),
        "source": "build/shadow/shadow_status.json",
    }


def build_document(root: Path = ROOT, generated_at: str | None = None,
                   date: str | None = None, allow_build: bool = True) -> dict:
    """Build the bulletin document from the feed, the frozen chain and the journal."""
    context = load_context(Path(root), allow_build=allow_build)
    feed = context["feed"]
    inputs = context["inputs"]
    quantiles = context["quantiles"]
    point = context["point"]
    generated_at = generated_at or _utc_now()
    date = date or generated_at[:10]

    intervals = {}
    for level, (low, high) in INTERVALS.items():
        intervals[level] = {"level_percent": int(level),
                            "low": quantiles[low], "low_quantile": QUANTILE_LABELS[low],
                            "high": quantiles[high], "high_quantile": QUANTILE_LABELS[high]}
    row = context["row"]
    forecast = {
        "candidate": feed["candidate"],
        "target": feed["target"],
        "horizon": feed["horizon"],
        "origin": feed["origin"],
        "point": point,
        "median": quantiles[9],
        "quantiles_5_to_95_step_5": quantiles,
        "intervals": intervals,
        "controls": {arm: {"point": float(feed["controls"][arm]["point"])}
                     for arm in CONTROL_ARMS},
        "pre_origin": "pre_origin=true" in str(row.get("notes") or ""),
    }
    changes, changes_note, _change = _changes(inputs["rows"], feed["candidate"], feed["target"],
                                              feed["horizon"], feed["origin"], point, quantiles)
    versions = inputs["versions"]
    hashes = {"protocol": versions["protocol"]["sha256"],
              "r3": versions["r3"]["sha256"],
              "nf_sim": versions["nf_sim"]["sha256"],
              "composition": versions["composition"]["sha256"],
              "report": versions["report"]["sha256"],
              "journal": versions["journal"]["sha256"]}
    document = {
        "schema_version": SCHEMA_VERSION,
        "bulletin_id": BULLETIN_ID,
        "generated_at": generated_at,
        "date": date,
        "status": feed["status"],
        "status_label": feed["status_label"],
        "conditional_data_class": bool(feed["conditional_data_class"]),
        "data_class": feed.get("data_class"),
        "headline": (f"AMFE · {feed['target']} h={feed['horizon']} · `{feed['candidate']}` · "
                     f"origin {feed['origin']}"),
        "forecast": forecast,
        "changes": changes,
        "changes_note": changes_note,
        "confirmed": _confirmed(inputs["report"]),
        "not_confirmed": _not_confirmed(feed),
        "near_miss_candidate": _near_miss(context["regime"]),
        "research_candidate": _research_candidate(Path(root)),
        "versions": versions,
        "hashes": hashes,
        "disclaimer": DISCLAIMER,
        "limitations": list(feed.get("limitations") or []),
        "sources": list(SOURCE_FILES),
    }
    return document


# ------------------------------------------------------------------------------------- markdown
def render_markdown(document: dict) -> str:
    forecast = document["forecast"]
    versions = document["versions"]
    hashes = document["hashes"]
    intervals = forecast["intervals"]
    conditional = str(document["conditional_data_class"]).lower()
    lines = [
        f"# AMFE — недельный бюллетень ({document['date']})",
        "",
        f"Снимок: **{document['generated_at']}** · `{document['bulletin_id']}` · схема "
        f"`{document['schema_version']}` · сгенерировано `tools/weekly_bulletin.py`.",
        "",
        f"> **Статус: {document['status']}** (`status_label = {document['status_label']}`), "
        f"`conditional_data_class = {conditional}`. Это **не допущенная модель** и **не "
        "инвестиционная рекомендация**; SHADOW_TEST, LIMITED_DEPLOYMENT и ACTIVE не достигнуты.",
        "",
        "## 1. Статус и замороженная цепочка",
        "",
        "| Поле | Значение |",
        "|---|---|",
        f"| Статус | **{document['status']}** |",
        f"| status_label | `{document['status_label']}` |",
        f"| conditional_data_class | `{conditional}` |",
        f"| Класс данных | `{document.get('data_class') or '—'}` |",
        f"| Кандидат | `{forecast['candidate']}` |",
        f"| Цель / горизонт | `{forecast['target']}` / h={forecast['horizon']} |",
        f"| Origin | `{forecast['origin']}` (pre-origin: запись до конца месяца) |",
        f"| Протокол оценки | {versions['protocol']['version']}, "
        f"**{versions['protocol']['status']}**, SHA-256 `{_short(hashes['protocol'])}` |",
        f"| R3-спецификация | {versions['r3']['version']}, SHA-256 `{_short(hashes['r3'])}` |",
        f"| NF-SIM | {versions['nf_sim']['study_id']}, {versions['nf_sim']['entries']} порогов, "
        f"SHA-256 `{_short(hashes['nf_sim'])}` |",
        f"| Состав компаратора | {versions['composition']['evaluation_date']}, "
        f"SHA-256 `{_short(hashes['composition'])}` |",
        f"| Отчёт оценки | {versions['report']['report_id']}, "
        f"SHA-256 `{_short(hashes['report'])}` |",
        f"| Журнал прогнозов | {versions['journal']['rows']} строк(и), "
        f"SHA-256 `{_short(hashes['journal'])}` |",
        "",
        "Замороженная цепочка: протокол v5 → R3 1.0.1 → NF-SIM → состав компаратора 2026-11-11; "
        "SHA-256 сверяются с пинами, при расхождении инструмент падает (fail-closed).",
        "",
        "## 2. Последний прогноз",
        "",
        f"- кандидат: `{forecast['candidate']}`; цель `{forecast['target']}`, горизонт "
        f"h={forecast['horizon']}; origin **{forecast['origin']}** (pre-origin);",
        f"- точка: **{_fmt(forecast['point'])}**; медиана (q50): {_fmt(forecast['median'])};",
        f"- 50% интервал: **[{_fmt(intervals['50']['low'])}; {_fmt(intervals['50']['high'])}]** "
        f"({intervals['50']['low_quantile']}…{intervals['50']['high_quantile']});",
        f"- 80% интервал: **[{_fmt(intervals['80']['low'])}; {_fmt(intervals['80']['high'])}]** "
        f"({intervals['80']['low_quantile']}…{intervals['80']['high_quantile']});",
        f"- 90% интервал: **[{_fmt(intervals['90']['low'])}; {_fmt(intervals['90']['high'])}]** "
        f"({intervals['90']['low_quantile']}…{intervals['90']['high_quantile']}).",
        "",
        "Контрольные плечи (точка): "
        + ", ".join(f"`{arm}` {_fmt(value['point'])}"
                    for arm, value in forecast["controls"].items())
        + ".",
        "",
        "## 3. Что подтверждено",
        "",
        f"Первая реальная оценка v5 по паре `{forecast['target']}` h={forecast['horizon']} "
        f"(кандидат `{forecast['candidate']}`):",
        "",
    ]
    for index, item in enumerate(document["confirmed"]):
        separator = ";" if index < len(document["confirmed"]) - 1 else "."
        lines.append(f"- **{item['id']} PASS** — {item['summary']}{separator}")
    lines.extend([
        "",
        "Эти PASS не являются допуском: гейтинговыми остаются C4 и C6.",
        "",
        "## 4. Что не подтверждено",
        "",
    ])
    for item in document["not_confirmed"]:
        if item["id"] == "C4":
            lines.append(f"- **C4 — FAIL (RMSE):** {item['summary']}")
        elif item["id"] == "C6":
            lines.append(f"- **C6 — нет:** {item['summary']}")
    near_miss = document["near_miss_candidate"]
    lines.append(f"- Near-miss `{near_miss['id']}` (отдельная, заново зарегистрированная модель): "
                 f"{near_miss['summary']}")
    lines.extend([
        "",
        f"**Статус остаётся `{document['status']}`; `conditional_data_class={conditional}`.**",
        "",
        "## 5. Изменение к предыдущей записи журнала",
        "",
        f"- {document['changes_note']}",
    ])
    if document["changes"]:
        for change in document["changes"]:
            lines.append(
                f"- точка: {_fmt(change['point_delta'], 4)} п.п. "
                f"({_fmt(change['point_delta_percent'], 2)}%), медиана: "
                f"{_fmt(change['median_delta'], 4)}, 90% интервал: "
                f"[{_fmt(change['interval_90_delta'][0], 4)}; "
                f"{_fmt(change['interval_90_delta'][1], 4)}].")
    else:
        lines.append("- Изменений прогноза нет: это первый выпуск по паре.")
    lines.extend([
        "",
        "## 6. Дисклеймер и ограничения",
        "",
        f"**Это не допущенная модель и не инвестиционная рекомендация.** Статус "
        f"`{document['status']}` (`conditional_data_class = {conditional}`); допуск не присвоен, "
        "SHADOW_TEST / LIMITED_DEPLOYMENT / ACTIVE не достигнуты. Бюллетень — отчётный снимок уже "
        "зарегистрированных результатов: он не меняет статусы, не пересчитывает критерии и не "
        "является инвестиционным советом.",
        "",
        "Ограничения:",
        "",
    ])
    lines.extend(f"- {item}" for item in document["limitations"])
    research = document.get("research_candidate")
    lines.extend([
        "",
        "## 7. Research candidate: SHADOW_TEST",
        "",
    ])
    if research:
        lines += [
            f"- Кандидат: `{research['id']}`; статус **{research['status']}**"
            + (f" (`{research['status_label']}`)" if research.get("status_label") else "")
            + f"; `conditional_data_class = {str(research['conditional_data_class']).lower()}`.",
            f"- Теневой трек: **{research['track_status']}**; разрешённых прогнозов "
            f"{research['n_resolved']} из {research['min_forecasts']}, различных целевых периодов "
            f"{research['distinct_target_periods']} из {research['min_distinct_target_periods']}.",
            f"- Последняя строка кандидата: {research['last_registered_at'] or '—'}; "
            f"ориентировочная дата гейта C6: {research['estimated_gate_date'] or '—'}.",
            f"- Алерты: "
            + (", ".join(f"`{a['code']}` ({a['severity']})" for a in research["alerts"])
               or "нет")
            + ".",
            "- Это отдельный исследовательский кандидат; он не меняет статус публикуемой модели "
            "и не является допуском. Регламент: docs/119.",
        ]
    else:
        lines.append("- Теневой трек не запущен (нет `build/shadow/shadow_status.json`).")
    lines.extend([
        "",
        "## 8. Источники (полные SHA-256, LF-нормализованные)",
        "",
        f"- `{versions['protocol']['file']}` — {versions['protocol']['version']} "
        f"{versions['protocol']['status']}, `{hashes['protocol']}`;",
        f"- `{versions['r3']['file']}` — {versions['r3']['version']}, `{hashes['r3']}`;",
        f"- `{versions['nf_sim']['file']}` — {versions['nf_sim']['study_id']}, "
        f"`{hashes['nf_sim']}`;",
        f"- `{versions['composition']['file']}` — {versions['composition']['evaluation_date']}, "
        f"`{hashes['composition']}`;",
        f"- `{versions['report']['file']}` — {versions['report']['report_id']}, "
        f"`{hashes['report']}`;",
        f"- `{versions['journal']['file']}` — {versions['journal']['rows']} строк(и), "
        f"`{hashes['journal']}`.",
        "",
        "Сырые сторонние данные не публикуются и не читаются: в бюллетене только собственные "
        "прогнозы AMFE и производные метрики (`licence_policy`, docs/84).",
        "",
        "---",
        "",
        "Сгенерировано `tools/weekly_bulletin.py`; повторный запуск с теми же входами и той же "
        "датой даёт байт-идентичные файлы.",
        "",
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------- write
def write_bulletin(document: dict, out_dir: Path = None) -> list[Path]:
    out_dir = Path(out_dir) if out_dir is not None else ROOT / BULLETIN_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown = render_markdown(document)
    payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    markdown_path = out_dir / f"{document['date']}.md"
    json_path = out_dir / f"{document['date']}.json"
    markdown_path.write_text(markdown, encoding="utf-8", newline="\n")
    json_path.write_text(payload, encoding="utf-8", newline="\n")
    return [markdown_path, json_path]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Собрать недельный публикационный бюллетень AMFE в build/bulletin/")
    parser.add_argument("--root", default=str(ROOT), help="корень репозитория")
    parser.add_argument("--out-dir", default=None, help="каталог вывода (по умолчанию build/bulletin)")
    parser.add_argument("--date", default=None, help="дата выпуска YYYY-MM-DD (по умолчанию из generated_at)")
    parser.add_argument("--generated-at", default=None,
                        help="ISO 8601 дата-время снимка; по умолчанию текущее UTC (для детерминизма)")
    parser.add_argument("--no-build", action="store_true",
                        help="не запускать tools/product_pack.py при отсутствии build/product")
    args = parser.parse_args(argv)
    root = Path(args.root)
    out_dir = Path(args.out_dir) if args.out_dir else root / BULLETIN_DIR
    try:
        generated_at = _validate_timestamp(args.generated_at) if args.generated_at else _utc_now()
        date = args.date or generated_at[:10]
        try:
            date_type.fromisoformat(date)
        except ValueError as exc:
            raise BulletinError(f"--date: ожидался YYYY-MM-DD, получено {date!r}") from exc
        document = build_document(root, generated_at=generated_at, date=date,
                                  allow_build=not args.no_build)
        written = write_bulletin(document, out_dir)
    except BulletinError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({
        "ok": True,
        "out_dir": str(out_dir).replace("\\", "/"),
        "files": [path.name for path in written],
        "date": document["date"],
        "status": document["status"],
        "conditional_data_class": document["conditional_data_class"],
        "candidate": document["forecast"]["candidate"],
        "origin": document["forecast"]["origin"],
        "generated_at": document["generated_at"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
