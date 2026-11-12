#!/usr/bin/env python3
"""AMFE product pack: model card, machine-readable feed and a static read-only dashboard.

The tool reads **only committed project artefacts** (no network, no raw third-party data):

  * ``registry/forecasts.jsonl`` - the append-only forward registry (own forecasts only);
  * ``protocols/first_real_evaluation_report_v5.json`` - the v5 evaluation of the pair;
  * ``protocols/evaluation_protocol_v5.json`` and its pin - the frozen protocol;
  * the frozen R3 specification, the NF-SIM threshold table and the comparator composition
    (metadata and pins only, referenced from the report).

It writes five deterministic files to ``build/product/``:

  * ``model_card.md``     - purpose, frozen versions, what is confirmed / not confirmed, limits;
  * ``forecast_latest.json`` - machine-readable feed (schema below);
  * ``dashboard.html``    - static read-only panel, no JavaScript, no frameworks, no external URLs;
  * ``dashboard.md``      - the same panel in Markdown;
  * ``README.md``         - what the pack is, how to rebuild it, why it is not an admission.

Everything except ``generated_at`` is a pure function of the inputs: the same repository and the same
``--generated-at`` produce byte-identical files.  The tool never writes to the journal, the protocols,
``src/`` or any existing tool; it is fail-closed on a broken input or a pin mismatch, and it reports
derived statistics only, never raw third-party observations.

Feed schema (``forecast_latest.json``), version 1.0.0:

    schema_version, generated_at, status, status_label, conditional_data_class, data_class,
    report_id, protocol {file, version, sha256}, candidate, target, horizon, origin,
    point, quantiles_5_to_95_step_5 (19 strictly increasing numbers), controls
    {RW|EQUAL_WEIGHT_POINT|EQUAL_WEIGHT_DENSITY: {point}},
    c4_status {pass, rmse_relative_loss, crps_relative_loss, ..., episodes[]},
    c6_status {n_resolved, min_forecasts, min_distinct_target_periods, pass, status},
    limitations[], source_hashes {protocol, report, journal}

Usage:
    python tools/product_pack.py
    python tools/product_pack.py --out-dir build/product --generated-at 2026-11-12T12:00:00Z
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCHEMA_VERSION = "1.0.0"
FEED_NAME = "forecast_latest.json"
MODEL_CARD_NAME = "model_card.md"
DASHBOARD_HTML_NAME = "dashboard.html"
DASHBOARD_MD_NAME = "dashboard.md"
README_NAME = "README.md"

PROTOCOL_FILE = "protocols/evaluation_protocol_v5.json"
PROTOCOL_PIN = "protocols/evaluation_protocol_v5.sha256"
REPORT_FILE = "protocols/first_real_evaluation_report_v5.json"
JOURNAL_FILE = "registry/forecasts.jsonl"
NF_SIM_FILE = "protocols/nf_sim_thresholds.json"
NF_SIM_PIN = "protocols/nf_sim_thresholds.sha256"
R3_FILE = "protocols/r3_specification.json"

CONTROL_ARMS = ("RW", "EQUAL_WEIGHT_POINT", "EQUAL_WEIGHT_DENSITY")
DOC_LINKS = ("docs/107", "docs/108", "docs/110", "docs/111")

STATUS_LABELS = {
    "CANDIDATE": "CANDIDATE",
    "SHADOW_TEST": "SHADOW_TEST",
    "LIMITED_DEPLOYMENT": "LIMITED_DEPLOYMENT",
    "ACTIVE": "ACTIVE",
    "SUSPENDED": "SUSPENDED",
}


class ProductPackError(RuntimeError):
    """A required input is missing, broken or inconsistent with its pin."""


# --------------------------------------------------------------------------------------- generic
def repo_path(root: Path, path: Path) -> str:
    try:
        return str(Path(path).relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def lf_sha256(path: Path) -> str:
    """SHA-256 of the LF-normalised bytes (the form the project's pins use)."""
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def read_pin(path: Path) -> str | None:
    """The hash recorded in a ``*.sha256`` pin file (first token)."""
    if not Path(path).is_file():
        return None
    parts = Path(path).read_text(encoding="utf-8").split()
    return parts[0] if parts else None


def load_json(path: Path) -> dict:
    path = Path(path)
    if not path.is_file():
        raise ProductPackError(f"нет обязательного файла: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProductPackError(f"не удалось прочитать {path}: {exc!r}") from exc
    if not isinstance(document, dict):
        raise ProductPackError(f"{path}: ожидался JSON-объект")
    return document


def read_journal(path: Path) -> list[dict]:
    path = Path(path)
    if not path.is_file():
        raise ProductPackError(f"нет журнала прогнозов: {path}")
    rows: list[dict] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise ProductPackError(f"{path}: строка {number} не JSON: {exc!r}") from exc
        if not isinstance(row, dict):
            raise ProductPackError(f"{path}: строка {number} не JSON-объект")
        rows.append(row)
    if not rows:
        raise ProductPackError(f"{path}: журнал пуст")
    return rows


def _number(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProductPackError(f"{label}: ожидалось число")
    return float(value)


def _verify(path: Path, expected: str | None, label: str) -> str:
    """Fail closed when a frozen artefact does not match its recorded SHA-256."""
    if not Path(path).is_file():
        raise ProductPackError(f"{label}: нет файла {path}")
    actual = lf_sha256(path)
    if expected and actual != expected:
        raise ProductPackError(
            f"{label}: SHA-256 не совпадает с пином ({actual} != {expected})")
    return actual


def _resolve(root: Path, recorded: str) -> Path:
    """Resolve a file path recorded in a report (relative to the root or to protocols/)."""
    recorded = str(recorded).replace("\\", "/")
    candidate = root / recorded
    if candidate.is_file():
        return candidate
    candidate = root / "protocols" / Path(recorded).name
    if candidate.is_file():
        return candidate
    raise ProductPackError(f"нет файла, на который ссылается отчёт: {recorded}")


# ----------------------------------------------------------------------------------------- inputs
def load_inputs(root: Path = ROOT) -> dict:
    """Load and cross-check every input; raise ProductPackError when something is inconsistent."""
    root = Path(root)
    protocol_path = root / PROTOCOL_FILE
    report_path = root / REPORT_FILE
    journal_path = root / JOURNAL_FILE

    protocol = load_json(protocol_path)
    report = load_json(report_path)
    rows = read_journal(journal_path)

    if protocol.get("status") != "FROZEN":
        raise ProductPackError(f"{PROTOCOL_FILE}: статус протокола {protocol.get('status')!r}, ожидался FROZEN")
    pin = read_pin(root / PROTOCOL_PIN)
    if pin is None:
        raise ProductPackError(f"нет пина {PROTOCOL_PIN}")
    protocol_sha = _verify(protocol_path, pin, "протокол v5")

    report_protocol = report.get("protocol") or {}
    if report_protocol.get("version") != protocol.get("version"):
        raise ProductPackError(
            "версия протокола в отчёте не совпадает с замороженным файлом: "
            f"{report_protocol.get('version')!r} != {protocol.get('version')!r}")

    r3 = report.get("r3_specification") or {}
    r3_path = _resolve(root, r3.get("file", R3_FILE))
    r3_sha = _verify(r3_path, r3.get("sha256"), "R3-спецификация")

    nf_sim = load_json(root / NF_SIM_FILE)
    nf_sim_sha = _verify(root / NF_SIM_FILE, read_pin(root / NF_SIM_PIN), "NF-SIM")

    composition = report.get("composition") or {}
    composition_path = _resolve(root, composition.get("file", ""))
    composition_sha = _verify(composition_path, composition.get("sha256"), "состав компаратора")
    composition_document = load_json(composition_path)

    versions = {
        "protocol": {"file": repo_path(root, protocol_path), "version": protocol.get("version"),
                     "status": protocol.get("status"), "sha256": protocol_sha},
        "r3": {"file": repo_path(root, r3_path), "version": r3.get("version"), "sha256": r3_sha},
        "nf_sim": {"file": repo_path(root, root / NF_SIM_FILE),
                   "study_id": nf_sim.get("study_id"), "entries": len(nf_sim.get("entries") or []),
                   "sha256": nf_sim_sha},
        "composition": {"file": repo_path(root, composition_path),
                        "document_id": composition_document.get("document_id"),
                        "evaluation_date": composition_document.get("evaluation_date"),
                        "sha256": composition_sha},
        "report": {"file": repo_path(root, report_path),
                   "report_id": report.get("report_id"),
                   "sha256": lf_sha256(report_path)},
        "journal": {"file": repo_path(root, journal_path), "rows": len(rows),
                    "first_registered_at": next((row.get("registered_at") for row in rows
                                                  if row.get("registered_at")), None),
                    "sha256": lf_sha256(journal_path)},
    }
    versions["nf_sim"]["file"] = repo_path(root, root / NF_SIM_FILE)
    return {"root": root, "protocol": protocol, "report": report, "rows": rows,
            "versions": versions, "hashes": {"protocol": protocol_sha,
                                             "report": versions["report"]["sha256"],
                                             "journal": versions["journal"]["sha256"]}}


# ------------------------------------------------------------------------------------------- feed
def _latest_row(rows: list[dict], candidate_id: str, target: str, horizon: int) -> dict:
    matches = [row for row in rows
               if row.get("candidate_id") == candidate_id
               and row.get("target") == target and row.get("horizon") == horizon]
    if not matches:
        raise ProductPackError(
            f"в журнале нет строки кандидата {candidate_id} для {target} h={horizon}")
    return max(matches, key=lambda row: (str(row.get("origin")), str(row.get("registered_at") or "")))


def _control(rows: list[dict], arm: str, target: str, horizon: int, origin: str) -> dict:
    matches = [row for row in rows
               if row.get("candidate_id") == arm and row.get("target") == target
               and row.get("horizon") == horizon and row.get("origin") == origin]
    if not matches:
        raise ProductPackError(f"в журнале нет контрольного плеча {arm} на origin {origin}")
    return max(matches, key=lambda row: str(row.get("registered_at") or ""))


def _quantiles(row: dict) -> list[float]:
    values = row.get("quantiles_5_to_95_step_5")
    if not isinstance(values, list) or len(values) != 19:
        raise ProductPackError("квантили строки журнала: ожидался список из 19 чисел")
    numbers = [_number(value, "квантиль") for value in values]
    if any(b <= a for a, b in zip(numbers, numbers[1:])):
        raise ProductPackError("квантили строки журнала не возрастают строго")
    if any(value == 0 for value in numbers):
        raise ProductPackError("квантили строки журнала содержат ровно ноль")
    return numbers


def _c4_status(report: dict, protocol: dict) -> dict:
    criteria = ((report.get("admission") or {}).get("criteria") or {})
    c4 = criteria.get("C4") or {}
    per_window = c4.get("per_window") or {}
    w1 = per_window.get("W1") or {}
    w2 = per_window.get("W2") or {}
    rmse = w1.get("RMSE") or {}
    crps = w1.get("CRPS") or {}
    w2_rmse = w2.get("RMSE") or {}
    episodes_source = (((report.get("c4_episodes") or {}).get("W1_EXT")) or {})
    episodes = []
    for key in sorted(episodes_source):
        episode = episodes_source[key] or {}
        episodes.append({
            "episode": key.replace("_", "-"),
            "origins": (episode.get("RMSE") or {}).get("origins"),
            "rmse_relative_loss": (episode.get("RMSE") or {}).get("relative_loss_episode"),
            "rmse_leave_one_out": (episode.get("RMSE") or {}).get("relative_loss_leave_one_out"),
            "crps_relative_loss": (episode.get("CRPS") or {}).get("relative_loss_episode"),
        })
    minimum = c4.get("min_recession_origins")
    if minimum is None:
        minimum = (protocol.get("admission") or {}).get("c4_window_and_minimum", {}).get(
            "min_recession_months", 12)
    return {
        "pass": bool(c4.get("pass")),
        "rmse_pass": bool(rmse.get("pass")),
        "rmse_relative_loss": _number(rmse.get("relative_loss_recession"), "C4 RMSE"),
        "rmse_relative_loss_met": bool(rmse.get("relative_loss_met")),
        "conditional_gw_p": rmse.get("conditional_gw_p"),
        "favours_comparator_in_recession": bool(rmse.get("favours_comparator_in_recession")),
        "crps_pass": bool(crps.get("pass")),
        "crps_relative_loss": _number(crps.get("relative_loss_recession"), "C4 CRPS"),
        "recession_origins": rmse.get("recession_origins"),
        "min_recession_origins": minimum,
        "w2_rmse_relative_loss": _number(w2_rmse.get("relative_loss_recession"), "C4 W2 RMSE"),
        "w2_pass": bool(w2_rmse.get("pass")),
        "episodes": episodes,
    }


def _c6_status(report: dict, protocol: dict) -> dict:
    criteria = ((report.get("admission") or {}).get("criteria") or {})
    c6 = criteria.get("C6") or {}
    c6_text = ""
    for criterion in (protocol.get("admission") or {}).get("criteria") or []:
        if criterion.get("id") == "C6":
            c6_text = criterion.get("text") or ""
    min_forecasts = 12
    marker = "at least "
    if marker in c6_text:
        tail = c6_text.split(marker, 1)[1]
        digits = ""
        for char in tail:
            if char.isdigit():
                digits += char
            else:
                break
        if digits:
            min_forecasts = int(digits)
    min_distinct = ((protocol.get("forward_registry") or {})
                    .get("c6_min_distinct_target_periods", 4))
    resolved = c6.get("n_resolved")
    if resolved is None:
        resolved = 0 if c6.get("status") == "NO_RESOLVED_FORECASTS" else 0
    return {
        "n_resolved": int(resolved),
        "min_forecasts": int(min_forecasts),
        "min_distinct_target_periods": int(min_distinct),
        "pass": bool(c6.get("pass")),
        "status": c6.get("status") or "NO_RESOLVED_FORECASTS",
    }


def _limitations(feed: dict) -> list[str]:
    c4 = feed["c4_status"]
    c6 = feed["c6_status"]
    limitations = [
        "Статус CANDIDATE: SHADOW_TEST, LIMITED_DEPLOYMENT и ACTIVE не достигнуты; пакет не является допуском.",
        (f"C4 FAIL по RMSE: относительная потеря {c4['rmse_relative_loss']:.4f} в рецессионных месяцах "
         f"W1_EXT (порог 1.00), оба эпизода хуже компаратора: 1990-91 "
         f"{(c4['episodes'][0]['rmse_relative_loss'] if c4['episodes'] else float('nan')):.3f}, "
         f"2001 {(c4['episodes'][1]['rmse_relative_loss'] if len(c4['episodes']) > 1 else float('nan')):.3f}; "
         f"CRPS PASS ({c4['crps_relative_loss']:.4f}), но C4 требует прохождения по каждому loss."),
        (f"C6 не пройден: {c6['n_resolved']} из {c6['min_forecasts']} разрешённых прогнозов, "
         f"нужно не менее {c6['min_distinct_target_periods']} различных целевых периодов."),
        ("conditional_data_class=true: C4 опирается на реконструированные данные 1990-1996 "
         "(RECONSTRUCTED_PIT / REAL_TIME_RECONSTRUCTED_MARKET), а не на REAL_TIME; REAL_TIME-only срез W1 "
         "публикуется как отчётный и негейтящий."),
        ("Запись журнала для origin 2026-11 сделана до конца месяца (pre-origin); информационный набор — "
         "подмножество набора конца месяца, look-ahead нет."),
        "C3 (Model Confidence Set) не реализован — негейтящий критерий harness.",
        "T2 и T3 в этот продуктовый пакет не входят; здесь только T1_CPI_YOY h=1.",
        ("Сырые сторонние данные не публикуются: в пакете только собственные прогнозы и производные "
         "метрики (licence_policy, docs/84)."),
    ]
    return limitations


def build_feed(inputs: dict, generated_at: str) -> dict:
    """Build the machine-readable feed from the journal, the report and the frozen protocol."""
    report = inputs["report"]
    protocol = inputs["protocol"]
    versions = inputs["versions"]
    rows = inputs["rows"]

    candidate = report.get("candidate")
    target = report.get("target")
    horizon = report.get("horizon")
    if not candidate or not target or not isinstance(horizon, int):
        raise ProductPackError("отчёт не содержит candidate/target/horizon")
    admission = report.get("admission") or {}
    status = admission.get("status") or "CANDIDATE"
    if status not in STATUS_LABELS:
        raise ProductPackError(f"неизвестный статус в отчёте: {status!r}")

    row = _latest_row(rows, candidate, target, horizon)
    origin = row.get("origin")
    if not isinstance(origin, str):
        raise ProductPackError("строка журнала не содержит origin")
    quantiles = _quantiles(row)
    point = _number(row.get("point"), "point")
    controls = {}
    for arm in CONTROL_ARMS:
        controls[arm] = {"point": _number(_control(rows, arm, target, horizon, origin).get("point"),
                                          f"{arm}.point")}

    feed = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": status,
        "status_label": admission.get("status_label") or STATUS_LABELS[status],
        "conditional_data_class": bool(admission.get("conditional_data_class", False)),
        "data_class": admission.get("data_class") or report.get("data_class"),
        "report_id": report.get("report_id"),
        "protocol": {"file": versions["protocol"]["file"],
                     "version": versions["protocol"]["version"],
                     "sha256": versions["protocol"]["sha256"]},
        "candidate": candidate,
        "target": target,
        "horizon": horizon,
        "origin": origin,
        "point": point,
        "quantiles_5_to_95_step_5": quantiles,
        "controls": controls,
        "c4_status": _c4_status(report, protocol),
        "c6_status": _c6_status(report, protocol),
        "source_hashes": dict(inputs["hashes"]),
    }
    feed["limitations"] = _limitations(feed)
    return feed


# ------------------------------------------------------------------------------------------ texts
def _fmt(value: float, digits: int = 4) -> str:
    return f"{value:.{digits}f}"


def _short(value: str | None, size: int = 12) -> str:
    return f"{value[:size]}…" if value else "—"


def _interval(feed: dict) -> tuple[float, float]:
    return feed["quantiles_5_to_95_step_5"][0], feed["quantiles_5_to_95_step_5"][-1]


def build_model_card(inputs: dict, feed: dict) -> str:
    versions = inputs["versions"]
    c4 = feed["c4_status"]
    c6 = feed["c6_status"]
    q05, q95 = _interval(feed)
    episodes = {episode["episode"]: episode for episode in c4["episodes"]}
    episode_1990 = episodes.get("1990-91", {}).get("rmse_relative_loss")
    episode_2001 = episodes.get("2001", {}).get("rmse_relative_loss")
    date = feed["generated_at"][:10]

    lines = [
        "# Модельная карточка AMFE — T1 CPI г/г, горизонт h=1",
        "",
        f"Дата снимка: **{date}**. Источник: `{versions['report']['file']}`, "
        f"`{versions['protocol']['file']}`, `{versions['journal']['file']}`.",
        "",
        "> Это модельная карточка **исследовательского** продукта, а не допущенной модели и не "
        "инвестиционной рекомендации. Статус: **CANDIDATE**, `conditional_data_class = true`.",
        "",
        "## 1. Назначение",
        "",
        "Прогноз годовой инфляции CPI США (цель `T1_CPI_YOY`) на один месяц вперёд (горизонт `h=1`) "
        "в виде точечного прогноза и набора квантилей 5%…95% с шагом 5% (19 чисел). Кандидат — "
        f"`{feed['candidate']}`; продукт показывает текущее состояние forward-контура, не заменяет "
        "оценку допуска и не содержит сырых сторонних данных.",
        "",
        "Пакет отвечает на три вопроса: что именно заморожено и под какими хешами, что подтверждено "
        "и что не подтверждено на первой реальной оценке, каков последний зарегистрированный прогноз.",
        "",
        "## 2. Версии и хеши (FROZEN, LF-нормализованный SHA-256)",
        "",
        "| Артефакт | Версия / статус | SHA-256 |",
        "|---|---|---|",
        f"| Протокол оценки `{versions['protocol']['file']}` | {versions['protocol']['version']}, "
        f"**{versions['protocol']['status']}** | `{versions['protocol']['sha256']}` |",
        f"| R3-спецификация `{versions['r3']['file']}` | {versions['r3']['version']}, FROZEN | "
        f"`{versions['r3']['sha256']}` |",
        f"| NF-SIM `{versions['nf_sim']['file']}` | {versions['nf_sim']['study_id']}, "
        f"{versions['nf_sim']['entries']} порогов | `{versions['nf_sim']['sha256']}` |",
        f"| Состав компаратора `{versions['composition']['file']}` | "
        f"{versions['composition']['evaluation_date'] or '2026-11-11'} | "
        f"`{versions['composition']['sha256']}` |",
        f"| Отчёт оценки `{versions['report']['file']}` | {versions['report']['report_id']} | "
        f"`{versions['report']['sha256']}` |",
        f"| Журнал прогнозов `{versions['journal']['file']}` | {versions['journal']['rows']} строк(и) | "
        f"`{versions['journal']['sha256']}` |",
        "",
        "Замороженная цепочка: протокол v5 → R3 1.0.1 → NF-SIM → состав компаратора 2026-11-11. "
        "Хеш протокола сверен с пином `protocols/evaluation_protocol_v5.sha256`; при расхождении "
        "инструмент падает (fail-closed), а не публикует пакет.",
        "",
        "## 3. Что подтверждено",
        "",
        f"Кандидат `{feed['candidate']}`, пара `{feed['target']}` h={feed['horizon']}; окна W1 "
        "(REAL_TIME, 121 начало) и W2 (REAL_TIME, 156 начал).",
        "",
        "| Критерий | Результат | Комментарий |",
        "|---|---|---|",
        "| C1 (относительная потеря) | **PASS** | RMSE/CRPS: 0.947/0.968 на W1 и 0.948/0.954 на W2 "
        "при пороге 0.97 |",
        "| C2 (Romano–Wolf) | **PASS** | скорректированные p 0.0479/0.0935 (W1) и 0.0533/0.0084 (W2), "
        "FWER 0.10 |",
        "| C5 (вторичные факты) | **PASS** | кандидат не хуже компаратора значимо |",
        "",
        "Именно эти три критерия подтверждены на первой реальной оценке T1 h=1. Они не являются "
        "допуском: гейтинговыми остаются C4 и C6.",
        "",
        "## 4. Что НЕ подтверждено",
        "",
        "**C4 — FAIL.** В рецессионных месяцах W1_EXT относительная потеря по RMSE равна "
        f"**{_fmt(c4['rmse_relative_loss'])}** (порог ≤ 1.00), условный Giacomini–White отвергает в "
        "пользу компаратора "
        f"(p ≈ {c4['conditional_gw_p']:.2g}). Оба эпизода хуже: 1990-91 — "
        f"{_fmt(episode_1990, 3) if episode_1990 is not None else '—'}, 2001 — "
        f"{_fmt(episode_2001, 3) if episode_2001 is not None else '—'}. По CRPS C4 **PASS** "
        f"({_fmt(c4['crps_relative_loss'])}), по W2 RMSE PASS ({_fmt(c4['w2_rmse_relative_loss'])}), "
        "но C4 требует прохождения по каждому применимому loss; RMSE не пройден, поэтому C4 общий — "
        "**FAIL**.",
        "",
        f"**C6 — нет.** Разрешённых прогнозов в forward-журнале: **{c6['n_resolved']}** из "
        f"{c6['min_forecasts']}; требуется не менее {c6['min_distinct_target_periods']} различных "
        f"целевых периодов. Статус: `{c6['status']}`. C6 физически не может быть пройден до "
        "накопления журнала.",
        "",
        "**Статус и класс данных.** Итоговый статус — **CANDIDATE** "
        f"(`status_label = {feed['status_label']}`); "
        f"`conditional_data_class = {str(feed['conditional_data_class']).lower()}`. "
        "Метка условного класса означает, что C4 опирается на реконструированные данные 1990–1996 "
        "(классы `RECONSTRUCTED_PIT` и `REAL_TIME_RECONSTRUCTED_MARKET`), а не на REAL_TIME. "
        "REAL_TIME-only срез W1 публикуется как отчётный и не влияет на решение. SHADOW_TEST, "
        "LIMITED_DEPLOYMENT и ACTIVE не достигнуты.",
        "",
        "## 5. Последний прогноз",
        "",
        f"- origin: **{feed['origin']}** (месячный якорь; запись сделана до конца месяца, pre-origin);",
        f"- точка: **{_fmt(feed['point'])}**;",
        f"- 90% интервал: **[{_fmt(q05)}; {_fmt(q95)}]** (q05…q95, 19 квантилей с шагом 5%);",
        f"- медиана (q50): {_fmt(feed['quantiles_5_to_95_step_5'][9])};",
        "- контрольные плечи (точка): "
        + ", ".join(f"{arm} {_fmt(value['point'])}" for arm, value in feed["controls"].items())
        + ".",
        "",
        "Полный набор квантилей и контрольных точек — в `forecast_latest.json`; панель — "
        "`dashboard.html` / `dashboard.md`.",
        "",
        "## 6. Ограничения",
        "",
    ]
    lines.extend(f"- {item}" for item in feed["limitations"])
    lines.extend([
        "",
        "## 7. Ссылки",
        "",
        "- [docs/107](../docs/107_Протокол_v5_данные_1990-1996_и_C4.md) — протокол v5, расширенный C4, "
        "результаты прогона (включая отрицательный C4);",
        "- [docs/108](../docs/108_Реконструкция_PIT_1990-1996.md) — реконструкция PIT 1990–1996 и "
        "классы данных;",
        "- [docs/110](../docs/110_Forward_контур_и_первая_запись.md) — forward-контур, первая запись "
        "журнала и семантика pre-origin;",
        "- [docs/111](../docs/111_Независимая_проверка_v5_и_досье.md) — независимая проверка v5 и "
        "ограничения проверки;",
        "- [docs/96](../docs/96_Релиз_1.0_подготовка.md) — формат релизных артефактов и известные "
        "ограничения релиза 1.0;",
        "- `protocols/evaluation_protocol_v5.json`, `protocols/first_real_evaluation_report_v5.json`, "
        "`registry/forecasts.jsonl` — первоисточники этого пакета.",
        "",
        "## 8. Что публиковать нельзя",
        "",
        "Пакет содержит только собственные прогнозы AMFE и производные метрики. Сырые сторонние "
        "наборы данных (FRED/ALFRED и любые другие) в репозиторий и в пакет не попадают; "
        "`licence_policy` разрешает публиковать только производные статистики. Пересборка пакета "
        "не требует сети и не читает `data/raw/`.",
        "",
        "---",
        "",
        f"Сгенерировано `tools/product_pack.py` (схема `{SCHEMA_VERSION}`). "
        "Повторный запуск с теми же входами и той же датой даёт байт-идентичные файлы.",
        "",
    ])
    return "\n".join(lines)


def _status_rows(feed: dict) -> list[tuple[str, str]]:
    return [
        ("Статус", f"**{feed['status']}**"),
        ("status_label", f"`{feed['status_label']}`"),
        ("conditional_data_class", f"`{str(feed['conditional_data_class']).lower()}`"),
        ("Класс данных", f"`{feed['data_class']}`"),
        ("Протокол", f"{feed['protocol']['version']} (FROZEN), SHA `{_short(feed['protocol']['sha256'])}`"),
        ("Кандидат", f"`{feed['candidate']}`"),
        ("Цель / горизонт", f"`{feed['target']}` / h={feed['horizon']}"),
    ]


def build_dashboard_md(inputs: dict, feed: dict) -> str:
    c4 = feed["c4_status"]
    c6 = feed["c6_status"]
    q05, q95 = _interval(feed)
    q = feed["quantiles_5_to_95_step_5"]
    date = feed["generated_at"][:10]
    lines = [
        "# AMFE — статусная панель (read-only)",
        "",
        f"Снимок: **{feed['generated_at']}** · дата: {date} · сгенерировано `tools/product_pack.py`.",
        "",
        "> Статическая панель без JavaScript и внешних ресурсов. Это **не допуск** и не "
        "инвестиционная рекомендация; только собственные прогнозы и производные метрики, без сырых "
        "сторонних данных.",
        "",
        "## 1. Статус",
        "",
        "| Поле | Значение |",
        "|---|---|",
    ]
    lines.extend(f"| {name} | {value} |" for name, value in _status_rows(feed))
    lines.extend([
        "",
        "## 2. Последний прогноз и интервалы",
        "",
        f"origin **{feed['origin']}**, `{feed['target']}` h={feed['horizon']}, кандидат "
        f"`{feed['candidate']}`.",
        "",
        "| Показатель | Значение |",
        "|---|---|",
        f"| Точка | **{_fmt(feed['point'])}** |",
        f"| q05 | {_fmt(q[0])} |",
        f"| q25 | {_fmt(q[4])} |",
        f"| q50 (медиана) | {_fmt(q[9])} |",
        f"| q75 | {_fmt(q[14])} |",
        f"| q95 | {_fmt(q[18])} |",
        f"| 90% интервал | **[{_fmt(q05)}; {_fmt(q95)}]**, ширина {_fmt(q95 - q05)} |",
        "",
        "**Контрольные плечи (точка):**",
        "",
        "| Плечо | Точка |",
        "|---|---|",
    ])
    lines.extend(f"| `{arm}` | {_fmt(value['point'])} |" for arm, value in feed["controls"].items())
    lines.extend([
        "",
        "## 3. Что не подтверждено",
        "",
        f"- **C4 — FAIL по RMSE:** относительная потеря {_fmt(c4['rmse_relative_loss'])} в 16 "
        f"рецессионных месяцах W1_EXT (порог ≤ 1.00); эпизоды 1990-91 и 2001 хуже компаратора; "
        f"CRPS PASS ({_fmt(c4['crps_relative_loss'])}), W2 RMSE PASS — но C4 требует все loss.",
        f"- **C6 — не пройден:** {c6['n_resolved']} из {c6['min_forecasts']} разрешённых прогнозов.",
        "- **SHADOW_TEST / LIMITED_DEPLOYMENT / ACTIVE** не достигнуты; статус `CANDIDATE`.",
        "- **conditional_data_class = true:** C4 опирается на реконструированные данные 1990–1996.",
        "- C3 (Model Confidence Set) не реализован — негейтящий критерий.",
        "",
        "## 4. C6-счётчик",
        "",
        "| Показатель | Значение |",
        "|---|---|",
        f"| Разрешённых прогнозов | **{c6['n_resolved']} / {c6['min_forecasts']}** |",
        f"| Различных целевых периодов | **0 / {c6['min_distinct_target_periods']}** (разрешённых) |",
        f"| Статус C6 | `{c6['status']}` |",
        f"| Пройден | `{str(c6['pass']).lower()}` |",
        "",
        "## 5. Источники (SHA-256, LF-нормализованные)",
        "",
        "| Источник | SHA-256 |",
        "|---|---|",
        f"| Протокол {feed['protocol']['version']} | `{feed['protocol']['sha256']}` |",
        f"| Отчёт оценки | `{feed['source_hashes']['report']}` |",
        f"| Журнал прогнозов | `{feed['source_hashes']['journal']}` |",
        "",
        "---",
        "",
        "Полные ограничения — в `model_card.md`; машиночитаемые данные — в `forecast_latest.json`.",
        "",
    ])
    return "\n".join(lines)


def build_dashboard_html(inputs: dict, feed: dict) -> str:
    c4 = feed["c4_status"]
    c6 = feed["c6_status"]
    q = feed["quantiles_5_to_95_step_5"]
    q05, q95 = _interval(feed)
    esc = html.escape

    def status_rows() -> str:
        return "\n".join(
            f"        <tr><th>{esc(name)}</th><td>{esc(value.replace('`', ''))}</td></tr>"
            for name, value in _status_rows(feed))

    def control_rows() -> str:
        return "\n".join(
            f"        <tr><td>{esc(arm)}</td><td class=\"num\">{_fmt(value['point'])}</td></tr>"
            for arm, value in feed["controls"].items())

    def quantile_rows() -> str:
        labels = ["q05", "q10", "q15", "q20", "q25", "q30", "q35", "q40", "q45", "q50",
                  "q55", "q60", "q65", "q70", "q75", "q80", "q85", "q90", "q95"]
        return "\n".join(f"        <tr><td>{label}</td><td class=\"num\">{_fmt(value)}</td></tr>"
                         for label, value in zip(labels, q))

    def limitation_items() -> str:
        return "\n".join(f"      <li>{esc(item)}</li>" for item in feed["limitations"])

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AMFE — статусная панель (read-only)</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif; line-height: 1.45;
         margin: 0 auto; max-width: 60rem; padding: 1.5rem; }}
  h1 {{ font-size: 1.6rem; margin-bottom: 0.25rem; }}
  h2 {{ font-size: 1.2rem; margin-top: 2rem; border-bottom: 1px solid #8884; padding-bottom: 0.25rem; }}
  .meta {{ color: #666; font-size: 0.9rem; }}
  .banner {{ border-left: 4px solid #b45309; background: #b4530915; padding: 0.75rem 1rem;
             margin: 1rem 0; }}
  table {{ border-collapse: collapse; width: 100%; margin: 0.75rem 0; }}
  th, td {{ border: 1px solid #8884; padding: 0.4rem 0.6rem; text-align: left; vertical-align: top; }}
  th {{ background: #8881; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  code {{ font-size: 0.9em; }}
  footer {{ margin-top: 2.5rem; border-top: 1px solid #8884; padding-top: 0.75rem;
            color: #666; font-size: 0.85rem; }}
  .fail {{ color: #b91c1c; font-weight: 600; }}
  .pass {{ color: #15803d; font-weight: 600; }}
</style>
</head>
<body>
  <h1>AMFE — статусная панель (read-only)</h1>
  <p class="meta">Снимок: <strong>{esc(feed['generated_at'])}</strong> ·
     сгенерировано <code>tools/product_pack.py</code> · схема <code>{SCHEMA_VERSION}</code></p>

  <div class="banner">
    Статическая панель без JavaScript и без внешних ресурсов. Это <strong>не допуск</strong> и не
    инвестиционная рекомендация: только собственные прогнозы проекта и производные метрики, без
    сырых сторонних данных.
  </div>

  <h2>1. Статус</h2>
  <table>
    <tbody>
{status_rows()}
    </tbody>
  </table>

  <h2>2. Последний прогноз и интервалы</h2>
  <p>origin <strong>{esc(feed['origin'])}</strong>, <code>{esc(feed['target'])}</code>
     h={feed['horizon']}, кандидат <code>{esc(feed['candidate'])}</code>.</p>
  <table>
    <thead><tr><th>Показатель</th><th>Значение</th></tr></thead>
    <tbody>
      <tr><th>Точка</th><td class="num"><strong>{_fmt(feed['point'])}</strong></td></tr>
      <tr><th>90% интервал</th><td class="num">[{_fmt(q05)}; {_fmt(q95)}]</td></tr>
      <tr><th>Ширина интервала</th><td class="num">{_fmt(q95 - q05)}</td></tr>
      <tr><th>Медиана (q50)</th><td class="num">{_fmt(q[9])}</td></tr>
    </tbody>
  </table>

  <table>
    <thead><tr><th>Квантиль</th><th>Значение</th></tr></thead>
    <tbody>
{quantile_rows()}
    </tbody>
  </table>

  <table>
    <thead><tr><th>Контрольное плечо</th><th>Точка</th></tr></thead>
    <tbody>
{control_rows()}
    </tbody>
  </table>

  <h2>3. Что не подтверждено</h2>
  <ul>
    <li><span class="fail">C4 — FAIL по RMSE:</span> относительная потеря
        {_fmt(c4['rmse_relative_loss'])} в рецессионных месяцах W1_EXT (порог ≤ 1.00);
        эпизоды 1990-91 и 2001 хуже компаратора;
        <span class="pass">CRPS PASS</span> ({_fmt(c4['crps_relative_loss'])}),
        W2 RMSE PASS ({_fmt(c4['w2_rmse_relative_loss'])}) — но C4 требует прохождения по каждому loss.
    </li>
    <li><span class="fail">C6 — не пройден:</span> {c6['n_resolved']} из {c6['min_forecasts']}
        разрешённых прогнозов; статус <code>{esc(c6['status'])}</code>.</li>
    <li><strong>SHADOW_TEST / LIMITED_DEPLOYMENT / ACTIVE</strong> не достигнуты; статус
        <code>{esc(feed['status'])}</code>.</li>
    <li><strong>conditional_data_class = {str(feed['conditional_data_class']).lower()}</strong>:
        C4 опирается на реконструированные данные 1990–1996.</li>
    <li>C3 (Model Confidence Set) не реализован — негейтящий критерий.</li>
  </ul>

  <h2>4. C6-счётчик</h2>
  <table>
    <thead><tr><th>Показатель</th><th>Значение</th></tr></thead>
    <tbody>
      <tr><th>Разрешённых прогнозов</th>
          <td class="num"><strong>{c6['n_resolved']} / {c6['min_forecasts']}</strong></td></tr>
      <tr><th>Различных целевых периодов</th>
          <td class="num"><strong>0 / {c6['min_distinct_target_periods']}</strong> (разрешённых)</td></tr>
      <tr><th>Статус C6</th><td><code>{esc(c6['status'])}</code></td></tr>
      <tr><th>Пройден</th><td><code>{str(c6['pass']).lower()}</code></td></tr>
    </tbody>
  </table>

  <h2>5. Ограничения</h2>
  <ul>
{limitation_items()}
  </ul>

  <h2>6. Источники (SHA-256, LF-нормализованные)</h2>
  <table>
    <thead><tr><th>Источник</th><th>SHA-256</th></tr></thead>
    <tbody>
      <tr><td>Протокол {esc(str(feed['protocol']['version']))}</td>
          <td><code>{esc(feed['protocol']['sha256'])}</code></td></tr>
      <tr><td>Отчёт оценки</td><td><code>{esc(feed['source_hashes']['report'])}</code></td></tr>
      <tr><td>Журнал прогнозов</td><td><code>{esc(feed['source_hashes']['journal'])}</code></td></tr>
    </tbody>
  </table>

  <footer>
    Полные ограничения и назначение — в <code>model_card.md</code>; машиночитаемые данные —
    в <code>forecast_latest.json</code>. Файл статический: обновляется только пересборкой
    <code>python tools/product_pack.py</code> и не является допуском модели.
  </footer>
</body>
</html>
"""


def build_readme(inputs: dict, feed: dict) -> str:
    versions = inputs["versions"]
    return "\n".join([
        "# Продуктовый пакет AMFE (`build/product/`)",
        "",
        f"Снимок от **{feed['generated_at']}**. Собран `tools/product_pack.py` из замороженных "
        "артефактов репозитория. Только чтение входов, без сети.",
        "",
        "## Файлы",
        "",
        "| Файл | Назначение |",
        "|---|---|",
        "| `model_card.md` | модельная карточка: назначение, версии, подтверждённое/неподтверждённое, "
        "ограничения, что публиковать нельзя |",
        "| `forecast_latest.json` | машиночитаемый фид: статус, последний прогноз, квантили, "
        "контрольные плечи, C4/C6, хеши источников |",
        "| `dashboard.html` | статическая read-only панель без JavaScript и внешних ресурсов |",
        "| `dashboard.md` | та же панель в Markdown |",
        "| `README.md` | этот файл |",
        "",
        "## Как пересобрать",
        "",
        "```powershell",
        "python tools/product_pack.py",
        "# или с фиксированной датой (детерминизм):",
        "python tools/product_pack.py --generated-at 2026-11-12T00:00:00Z --out-dir build/product",
        "```",
        "",
        "Входы (только чтение):",
        f"- `{versions['journal']['file']}` — forward-журнал, append-only;",
        f"- `{versions['report']['file']}` — первая реальная оценка v5;",
        f"- `{versions['protocol']['file']}` и `{PROTOCOL_PIN}` — замороженный протокол и пин;",
        f"- `{versions['r3']['file']}`, `{versions['nf_sim']['file']}`, "
        f"`{versions['composition']['file']}` — замороженная цепочка (хеши сверяются).",
        "",
        "Инструмент **не меняет** журнал, протоколы, `src/**` и существующие `tools/**`; при "
        "несовпадении пина или битом входе он падает (fail-closed).",
        "",
        "## Это не допуск",
        "",
        f"- Статус: **{feed['status']}**; SHADOW_TEST, LIMITED_DEPLOYMENT и ACTIVE не достигнуты.",
        f"- `conditional_data_class = {str(feed['conditional_data_class']).lower()}`: C4 опирается на "
        "реконструированные данные 1990–1996, а не на REAL_TIME.",
        f"- C4 — **FAIL** по RMSE (относительная потеря {_fmt(feed['c4_status']['rmse_relative_loss'])} "
        "в рецессионных месяцах W1_EXT); CRPS PASS не компенсирует RMSE.",
        f"- C6 — нет: {feed['c6_status']['n_resolved']} из {feed['c6_status']['min_forecasts']} "
        f"разрешённых прогнозов, нужно {feed['c6_status']['min_distinct_target_periods']} различных "
        "целевых периодов.",
        "- Пакет не заменяет отчёт оценки и не меняет статус ни одного кандидата.",
        "",
        "## Данные и лицензии",
        "",
        "Пакет содержит только собственные прогнозы AMFE и производные метрики. Сырые сторонние "
        "данные не публикуются и не читаются (`licence_policy`, docs/84). Файлы пакета — "
        "сгенерированные; в git их добавляет только отдельное решение Lead.",
        "",
        "## Документация",
        "",
        "- [docs/113](../../docs/113_Продуктовый_пакет.md) — описание пакета и схемы JSON;",
        "- [docs/107](../../docs/107_Протокол_v5_данные_1990-1996_и_C4.md) — протокол v5 и результаты;",
        "- [docs/110](../../docs/110_Forward_контур_и_первая_запись.md) — forward-контур и журнал;",
        "- [docs/111](../../docs/111_Независимая_проверка_v5_и_досье.md) — независимая проверка v5;",
        "- [docs/96](../../docs/96_Релиз_1.0_подготовка.md) — формат релизных артефактов.",
        "",
    ])


# ------------------------------------------------------------------------------------------ write
def write_pack(inputs: dict, feed: dict, out_dir: Path) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    documents = {
        MODEL_CARD_NAME: build_model_card(inputs, feed),
        DASHBOARD_HTML_NAME: build_dashboard_html(inputs, feed),
        DASHBOARD_MD_NAME: build_dashboard_md(inputs, feed),
        README_NAME: build_readme(inputs, feed),
    }
    documents[FEED_NAME] = json.dumps(feed, ensure_ascii=False, indent=2) + "\n"
    written: list[Path] = []
    for name in (MODEL_CARD_NAME, FEED_NAME, DASHBOARD_HTML_NAME, DASHBOARD_MD_NAME, README_NAME):
        path = out_dir / name
        path.write_text(documents[name], encoding="utf-8", newline="\n")
        written.append(path)
    return written


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_timestamp(value: str) -> str:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProductPackError(f"--generated-at: ожидался ISO 8601, получено {value!r}") from exc
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Собрать продуктовый пакет AMFE в build/product/")
    parser.add_argument("--root", default=str(ROOT), help="корень репозитория")
    parser.add_argument("--out-dir", default=None, help="каталог вывода (по умолчанию build/product)")
    parser.add_argument("--generated-at", default=None,
                        help="ISO 8601 дата-время снимка; по умолчанию текущее UTC (для детерминизма)")
    args = parser.parse_args(argv)
    root = Path(args.root)
    out_dir = Path(args.out_dir) if args.out_dir else root / "build" / "product"
    try:
        generated_at = _validate_timestamp(args.generated_at) if args.generated_at else _utc_now()
        inputs = load_inputs(root)
        feed = build_feed(inputs, generated_at)
        written = write_pack(inputs, feed, out_dir)
    except ProductPackError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({
        "ok": True,
        "out_dir": str(out_dir).replace("\\", "/"),
        "files": [path.name for path in written],
        "status": feed["status"],
        "conditional_data_class": feed["conditional_data_class"],
        "candidate": feed["candidate"],
        "origin": feed["origin"],
        "journal_rows": inputs["versions"]["journal"]["rows"],
        "generated_at": feed["generated_at"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
