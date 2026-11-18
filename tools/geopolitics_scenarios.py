"""Geopolitics scenario library (docs/177 §4.2, M16, block 16): protocol -> MRIO scenario reports.

The tool turns the declarative library ``protocols/geopolitics_scenarios.json``
(schema ``AMFE-GEOPOL-SCENARIO/1.0.0``) into **country x industry scenario reports** on the existing
MRIO engine (:mod:`world_pipeline`, the same solver that produces the P2 0.2 / docs/131 reports):

    build/geopolitics/report.json               every scenario report + index (aggregate-only)
    build/geopolitics/report.md                 the same as a human-readable summary
    build/geopolitics/reports/<scenario>.json   one scenario report
    build/geopolitics/reports/<scenario>.md     one scenario report, readable

Design rules
------------
* **Protocol is data, not prose.**  Every scenario is a record with ``family``, ``actors``,
  ``sectors``, ``channel``, ``size``, ``duration``, ``decay``, ``direction``, ``source``,
  ``pit_date`` and ``assumptions``; the library is validated before anything is solved.
* **Reuse the engine.**  The baseline, table validation, ``A = Z/x``, the Leontief solve and the
  aggregate-only report layer are :mod:`world_pipeline` / :mod:`world_reports`; this module only
  builds the declared perturbation, solves it and aggregates.  No second MRIO engine.
* **P-008 asymmetry is explicit.**  Each report lists beneficiaries (positive gross-output cells)
  or records ``direction=damage`` with ``beneficiaries.status=none`` and the note "выгод нет".
  Beneficiaries are never invented: a barrier that only shrinks cross-border coefficients cannot
  produce positive output cells in this comparative statics.
* **Sign-only historical checks.**  Two anchors (Iran 2018, Russia 2022) are checked **only by the
  sign** of three target country x industry cells; the direction-check block never publishes a
  magnitude, and the check is not a causal estimate.
* **Aggregate-only.**  Reports carry per-country and per-industry marginals, top lists and a few
  country x industry cells; never ``Z``, ``x``, ``y``, ``va`` or per-sector vectors.
* **Deterministic.**  Fixed scenario order, fixed key order, no wall clock and no timers in the
  payloads: two runs on the same protocol and dataset produce byte-identical JSON/MD, and every
  report carries a stable SHA-256 digest.
* **Fail-closed.**  A missing protocol or dataset is ``BLOCKED_*`` (exit 2); an invalid record, an
  unknown actor/sector, a shock that breaks productiveness or a failed balance raises (exit 1).
  Nothing is written unless every scenario succeeded.
* **Research only.**  ``status=RESEARCH_ONLY``, ``gating=false``, ``causal_claim=false``: a scenario
  is not a forecast, not an admission decision and not a causal estimate of sanctions/tariffs.

CLI
---
    python tools/geopolitics_scenarios.py \\
        --protocol protocols/geopolitics_scenarios.json --out build/geopolitics

    python tools/geopolitics_scenarios.py --synthetic --only TAR-USA-CHN-2018
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "src", ROOT / "tools"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import world_pipeline as wp  # noqa: E402
import world_reports as wr  # noqa: E402

# --------------------------------------------------------------------------------------- constants
SCHEMA = "AMFE-GEOPOL-SCENARIO/1.0.0"
REPORT_SCHEMA = "amfe.geopolitics.report.v1"
SCENARIO_SCHEMA = "amfe.geopolitics.scenario_report.v1"
INDEX_SCHEMA = "amfe.geopolitics.index.v1"
TOOL_VERSION = "1.0.0"
STATUS = "RESEARCH_ONLY"
STATUS_NOTE = ("Исследовательский набор геополитических сценариев: не прогноз, не допуск и не "
               "причинная оценка; gating=false, causal_claim=false (docs/177, M16, блок 16)")
DEFAULT_PROTOCOL = ROOT / "protocols" / "geopolitics_scenarios.json"
DEFAULT_OUT = ROOT / "build" / "geopolitics"
DEFAULT_ROOT = ROOT / "data" / "mrio" / "figaro"
DEFAULT_DATASET = "figaro_2019"
SOURCE_REGISTRY_PATH = ROOT / "protocols" / "source_registry.json"
FAMILIES = ("sanctions", "tariffs", "conflict", "chokepoint", "export_restriction", "agreement",
            "industrial_policy")
CHANNELS = ("import_restriction", "export_restriction", "chokepoint", "demand_diversion",
            "demand_shock", "trade_facilitation")
#: Which channels a family may use (keeps the library semantically coherent).
FAMILY_CHANNELS = {
    "sanctions": ("import_restriction", "export_restriction", "demand_diversion"),
    "tariffs": ("import_restriction",),
    "conflict": ("chokepoint", "export_restriction"),
    "chokepoint": ("chokepoint",),
    "export_restriction": ("export_restriction",),
    "agreement": ("trade_facilitation",),
    "industrial_policy": ("demand_shock",),
}
BARRIER_CHANNELS = frozenset({"import_restriction", "export_restriction", "chokepoint"})
COEFFICIENT_CHANNELS = frozenset(BARRIER_CHANNELS | {"trade_facilitation"})
DEMAND_CHANNELS = frozenset({"demand_diversion", "demand_shock"})
DIRECTIONS = ("damage", "benefit", "both")
DECAY_KINDS = ("none", "step", "linear", "exponential")
COMPANION_CHANNELS = frozenset({"demand_diversion", "demand_shock"})
SIZE_MIN = 0.0
SIZE_MAX = 0.95
MAX_DURATION = 600
ISO3 = re.compile(r"^[A-Z]{3}$")
SCENARIO_ID = re.compile(r"^[A-Z][A-Z0-9-]{2,60}$")
SCENARIO_REQUIRED = ("scenario_id", "title_ru", "family", "actors", "sectors", "channel", "size",
                     "duration", "decay", "direction", "source", "pit_date", "assumptions")
SCENARIO_OPTIONAL = ("companions", "historical_anchor", "notes")
SCENARIO_ALLOWED = frozenset(SCENARIO_REQUIRED + SCENARIO_OPTIONAL)
COMPONENT_REQUIRED = ("channel", "actors", "sectors", "size")
TOP_N = wr.TOP_N
EXIT_OK = 0
EXIT_INVALID = 1
EXIT_BLOCKED = 2
LIMITATIONS = (
    "сравнительная статика x'=(I-A')^-1 y': нет цен, замещения, динамики, ожиданий и обратных "
    "связей; длительность и затухание объявлены, но в решении не разворачиваются — числа относятся "
    "к пику шока;",
    "все барьерные каналы (санкции, тарифы, проливы, экспортные ограничения) реализованы как "
    "сокращение межстрановых коэффициентов на объявленном направлении; физические мощности "
    "проливов, фрахт, запасы и очереди не моделируются (это блок 8);",
    "отрасли — агрегаты CPA/FIGARO; узкие товары (галлий, графит, отдельные компоненты) шире "
    "своего отраслевого кода, поэтому сценарий завышает охват отрасли;",
    "акторы, отсутствующие в таблице (Иран, Тайвань), заменены прокси/допущениями, помеченными "
    "в сценарии; такие сценарии не являются оценкой соответствующей страны;",
    "величины шоков — допущения (source.kind=assumption), не оценки по данным; вероятности "
    "событий не оцениваются (это слой ожиданий, блок 14);",
    "сценарий не является причинной оценкой эффекта санкций/тарифов, не гейтит допуск "
    "(gating=false) и не публикуется как прогноз.",
)


class ProtocolError(RuntimeError):
    """The protocol is invalid for this table/run (fail-closed, exit 1)."""


class BlockedInput(ProtocolError):
    """A required input (protocol, dataset) is missing (fail-closed, exit 2)."""


# ---------------------------------------------------------------------------------------- helpers
def _canonical(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and np.isfinite(value)


def _require(condition, message: str) -> None:
    if not condition:
        raise ProtocolError(message)


def parse_list(text) -> tuple:
    if text is None:
        return ()
    if isinstance(text, (list, tuple)):
        return tuple(str(item) for item in text)
    return tuple(part.strip() for part in str(text).split(",") if part.strip())


def _iso3_list(value, field: str) -> list[str]:
    _require(isinstance(value, list) and value, f"{field}: нужен непустой список ISO3")
    labels = []
    for item in value:
        _require(isinstance(item, str) and ISO3.match(item),
                 f"{field}: ожидается код ISO3 (три заглавные буквы), получено {item!r}")
        labels.append(item)
    _require(len(set(labels)) == len(labels), f"{field}: повторяющиеся коды")
    return labels


def _validate_decay(value, field: str = "decay") -> dict:
    if isinstance(value, str):
        _require(value in DECAY_KINDS, f"{field}: допустимо {DECAY_KINDS}, получено {value!r}")
        if value == "exponential":
            raise ProtocolError(f"{field}: exponential требует half_life_months "
                                "(число или объект {'kind': 'exponential', 'half_life_months': x})")
        return {"kind": value, "half_life_months": None}
    if _is_number(value):
        _require(float(value) > 0.0, f"{field}: период полураспада должен быть > 0")
        return {"kind": "exponential", "half_life_months": float(value)}
    if isinstance(value, dict):
        _require(set(value) == {"kind", "half_life_months"},
                 f"{field}: объект должен содержать ровно kind и half_life_months")
        _require(value["kind"] == "exponential", f"{field}: поддерживается только exponential")
        _require(_is_number(value["half_life_months"]) and float(value["half_life_months"]) > 0.0,
                 f"{field}: half_life_months должен быть > 0")
        return {"kind": "exponential", "half_life_months": float(value["half_life_months"])}
    raise ProtocolError(f"{field}: ожидается строка/число/объект, получено {type(value).__name__}")


def _validate_source(value, registry_ids: set[str] | None) -> dict:
    _require(isinstance(value, dict), "source: ожидается объект")
    allowed = {"kind", "id", "replace_with", "note"}
    _require(set(value) <= allowed, f"source: неизвестные поля {sorted(set(value) - allowed)}")
    kind = value.get("kind")
    _require(kind in ("assumption", "source_id"), "source.kind: assumption | source_id")
    if kind == "assumption":
        replace_with = value.get("replace_with")
        _require(isinstance(replace_with, str) and replace_with.strip(),
                 "source.replace_with: для assumption нужно указать, чем заменить допущение")
        return {"kind": kind, "id": value.get("id") or "assumption",
                "replace_with": replace_with.strip(), "note": value.get("note")}
    source_id = value.get("id")
    _require(isinstance(source_id, str) and source_id.strip(),
             "source.id: для source_id нужен идентификатор из реестра")
    if registry_ids is not None:
        _require(source_id in registry_ids,
                 f"source.id {source_id!r} отсутствует в protocols/source_registry.json")
    return {"kind": kind, "id": source_id.strip(), "replace_with": value.get("replace_with"),
            "note": value.get("note")}


def _validate_actors(value, field: str = "actors") -> dict:
    _require(isinstance(value, dict) and set(value) == {"from", "to"},
             f"{field}: ожидается объект с полями from и to")
    return {"from": _iso3_list(value["from"], f"{field}.from"),
            "to": _iso3_list(value["to"], f"{field}.to")}


def _validate_component(raw: dict, family_channel: str | None = None) -> dict:
    _require(isinstance(raw, dict), "component: ожидается объект")
    _require(set(raw) <= set(COMPONENT_REQUIRED) | {"note"},
             f"component: неизвестные поля {sorted(set(raw) - set(COMPONENT_REQUIRED) - {'note'})}")
    for key in COMPONENT_REQUIRED:
        _require(key in raw, f"component: отсутствует поле {key}")
    channel = raw["channel"]
    _require(channel in COMPANION_CHANNELS,
             f"component.channel: допустимо {sorted(COMPANION_CHANNELS)}, получено {channel!r}")
    if family_channel is not None:
        _require(channel != family_channel, "component не должен повторять канал сценария")
    actors = _validate_actors(raw["actors"], "component.actors")
    if channel == "demand_diversion":
        overlap = set(actors["from"]) & set(actors["to"])
        _require(not overlap,
                 f"component.actors: для demand_diversion from и to не пересекаются ({sorted(overlap)})")
    sectors = raw["sectors"]
    _require(isinstance(sectors, list) and sectors, "component.sectors: непустой список")
    for sector in sectors:
        _require(isinstance(sector, str) and sector.strip(), "component.sectors: строки")
    _require(len(set(sectors)) == len(sectors), "component.sectors: повторяющиеся отрасли")
    size = raw["size"]
    _require(_is_number(size) and SIZE_MIN < float(size) <= SIZE_MAX,
             f"component.size: должно быть в ({SIZE_MIN}, {SIZE_MAX}], получено {size!r}")
    return {"channel": channel, "actors": actors, "sectors": list(sectors),
            "size": round(float(size), 6), "note": raw.get("note")}


def validate_scenario(raw: dict, registry_ids: set[str] | None = None) -> dict:
    """Validate one scenario record and return its normalised form (fail-closed)."""
    _require(isinstance(raw, dict), "сценарий должен быть объектом")
    unknown = set(raw) - SCENARIO_ALLOWED
    _require(not unknown, f"сценарий: неизвестные поля {sorted(unknown)}")
    for key in SCENARIO_REQUIRED:
        _require(key in raw, f"сценарий: отсутствует обязательное поле {key}")
    scenario_id = raw["scenario_id"]
    _require(isinstance(scenario_id, str) and SCENARIO_ID.match(scenario_id),
             f"scenario_id: ожидается [A-Z][A-Z0-9-]+, получено {scenario_id!r}")
    title = raw["title_ru"]
    _require(isinstance(title, str) and title.strip(), "title_ru: непустая строка")
    family = raw["family"]
    _require(family in FAMILIES, f"family: допустимо {list(FAMILIES)}, получено {family!r}")
    channel = raw["channel"]
    _require(channel in CHANNELS, f"channel: допустимо {list(CHANNELS)}, получено {channel!r}")
    _require(channel in FAMILY_CHANNELS[family],
             f"channel {channel!r} несовместим с family {family!r} "
             f"(допустимо {list(FAMILY_CHANNELS[family])})")
    actors = _validate_actors(raw["actors"])
    if channel in BARRIER_CHANNELS or channel == "demand_diversion":
        overlap = set(actors["from"]) & set(actors["to"])
        _require(not overlap,
                 f"actors: для канала {channel} from и to не пересекаются ({sorted(overlap)})")
    sectors = raw["sectors"]
    _require(isinstance(sectors, list) and sectors, "sectors: непустой список")
    for sector in sectors:
        _require(isinstance(sector, str) and sector.strip(), "sectors: строки")
    _require(len(set(sectors)) == len(sectors), "sectors: повторяющиеся отрасли")
    size = raw["size"]
    _require(_is_number(size) and SIZE_MIN < float(size) <= SIZE_MAX,
             f"size: должно быть в ({SIZE_MIN}, {SIZE_MAX}], получено {size!r}")
    duration = raw["duration"]
    _require(isinstance(duration, int) and not isinstance(duration, bool)
             and 1 <= duration <= MAX_DURATION,
             f"duration: целое в [1, {MAX_DURATION}], получено {duration!r}")
    decay = _validate_decay(raw["decay"])
    direction = raw["direction"]
    _require(direction in DIRECTIONS, f"direction: допустимо {list(DIRECTIONS)}, "
                                      f"получено {direction!r}")
    source = _validate_source(raw["source"], registry_ids)
    pit_date = raw["pit_date"]
    _require(isinstance(pit_date, str), "pit_date: строка ISO 8601")
    try:
        date.fromisoformat(pit_date)
    except ValueError as exc:
        raise ProtocolError(f"pit_date: ожидается YYYY-MM-DD, получено {pit_date!r}") from exc
    assumptions = raw["assumptions"]
    _require(isinstance(assumptions, list) and assumptions, "assumptions: непустой список")
    for item in assumptions:
        _require(isinstance(item, str) and item.strip(), "assumptions: непустые строки")
    companions = [_validate_component(item, channel)
                  for item in (raw.get("companions") or [])]
    notes = raw.get("notes")
    _require(notes is None or isinstance(notes, str), "notes: строка")
    anchor = raw.get("historical_anchor")
    if anchor is not None:
        _require(isinstance(anchor, dict) and {"fact", "fact_source"} <= set(anchor),
                 "historical_anchor: нужны fact и fact_source")
    return {
        "scenario_id": scenario_id,
        "title_ru": title.strip(),
        "family": family,
        "actors": actors,
        "sectors": list(sectors),
        "channel": channel,
        "size": round(float(size), 6),
        "duration": int(duration),
        "decay": decay,
        "direction": direction,
        "source": source,
        "pit_date": pit_date,
        "assumptions": [item.strip() for item in assumptions],
        "companions": companions,
        "historical_anchor": anchor,
        "notes": notes,
    }


def _validate_direction_check(raw: dict, scenario_ids: set[str]) -> dict:
    _require(isinstance(raw, dict), "direction_check: ожидается объект")
    allowed = {"check_id", "title_ru", "scenario_id", "anchor_year", "scope", "fact",
               "fact_source", "targets", "min_matches", "notes"}
    _require(set(raw) <= allowed,
             f"direction_check: неизвестные поля {sorted(set(raw) - allowed)}")
    for key in ("check_id", "scenario_id", "scope", "fact", "targets", "min_matches"):
        _require(key in raw, f"direction_check: отсутствует поле {key}")
    _require(isinstance(raw["check_id"], str) and raw["check_id"].strip(),
             "direction_check.check_id: непустая строка")
    _require(raw["scenario_id"] in scenario_ids,
             f"direction_check {raw['check_id']}: неизвестный scenario_id "
             f"{raw['scenario_id']!r}")
    _require(raw["scope"] == "sign_only",
             "direction_check.scope: поддерживается только sign_only (магнитуда не оценивается)")
    _require(isinstance(raw["fact"], str) and raw["fact"].strip(),
             "direction_check.fact: непустая строка")
    fact_source = raw.get("fact_source")
    _require(isinstance(fact_source, dict) and fact_source.get("kind") == "assumption"
             and isinstance(fact_source.get("note"), str) and fact_source["note"].strip(),
             "direction_check.fact_source: нужен {'kind': 'assumption', 'note': ...}")
    targets = raw["targets"]
    _require(isinstance(targets, list) and targets, "direction_check.targets: непустой список")
    normalised = []
    for target in targets:
        _require(isinstance(target, dict) and {"actor", "industry", "expected_sign"} <= set(target),
                 "direction_check.target: нужны actor, industry, expected_sign")
        _require(isinstance(target["actor"], str) and ISO3.match(target["actor"]),
                 "direction_check.target.actor: ISO3")
        _require(isinstance(target["industry"], str) and target["industry"].strip(),
                 "direction_check.target.industry: строка")
        _require(target["expected_sign"] in (-1, 0, 1),
                 "direction_check.target.expected_sign: -1 | 0 | 1")
        normalised.append({"actor": target["actor"], "industry": target["industry"],
                           "expected_sign": int(target["expected_sign"])})
    min_matches = raw["min_matches"]
    _require(isinstance(min_matches, int) and not isinstance(min_matches, bool)
             and 1 <= min_matches <= len(normalised),
             "direction_check.min_matches: целое в [1, число целей]")
    anchor_year = raw.get("anchor_year")
    _require(anchor_year is None or (isinstance(anchor_year, int)
                                     and 1900 <= anchor_year <= 2100),
             "direction_check.anchor_year: год")
    notes = raw.get("notes") or []
    _require(isinstance(notes, list) and all(isinstance(item, str) for item in notes),
             "direction_check.notes: список строк")
    return {
        "check_id": raw["check_id"].strip(),
        "title_ru": raw.get("title_ru"),
        "scenario_id": raw["scenario_id"],
        "anchor_year": anchor_year,
        "scope": "sign_only",
        "fact": raw["fact"].strip(),
        "fact_source": fact_source,
        "targets": normalised,
        "min_matches": int(min_matches),
        "notes": list(notes),
    }


def validate_protocol(document: dict, registry_ids: set[str] | None = None) -> dict:
    """Validate the whole protocol; return a summary (fail-closed)."""
    _require(isinstance(document, dict), "протокол должен быть JSON-объектом")
    _require(document.get("schema") == SCHEMA,
             f"schema: ожидается {SCHEMA}, получено {document.get('schema')!r}")
    _require(document.get("status") == STATUS,
             f"status: ожидается {STATUS}, получено {document.get('status')!r}")
    _require(document.get("causal_claim") is False, "causal_claim должен быть false")
    _require(document.get("gating") is False, "gating должен быть false")
    created = document.get("created")
    if created is not None:
        _require(isinstance(created, str), "created: строка ISO 8601")
        try:
            date.fromisoformat(created)
        except ValueError as exc:
            raise ProtocolError(f"created: ожидается YYYY-MM-DD, получено {created!r}") from exc
    country_map = document.get("country_map") or {}
    _require(isinstance(country_map, dict), "country_map: объект ISO3 -> метка набора")
    for actor, label in country_map.items():
        _require(isinstance(actor, str) and ISO3.match(actor), f"country_map: ключ {actor!r} не ISO3")
        _require(isinstance(label, str) and label.strip(), f"country_map[{actor}]: непустая метка")
    proxies = document.get("proxies") or {}
    _require(isinstance(proxies, dict), "proxies: объект")
    for actor, proxy in proxies.items():
        _require(isinstance(actor, str) and ISO3.match(actor), f"proxies: ключ {actor!r} не ISO3")
        _require(isinstance(proxy, dict) and {"label", "kind", "note"} <= set(proxy),
                 f"proxies[{actor}]: нужны label, kind, note")
        _require(proxy["kind"] in ("aggregate_block", "neighbour"),
                 f"proxies[{actor}].kind: aggregate_block | neighbour")
        _require(isinstance(proxy["note"], str) and proxy["note"].strip(),
                 f"proxies[{actor}].note: непустая строка")
    _require(set(country_map) & set(proxies) == set(),
             "actor не может быть одновременно в country_map и proxies")

    scenarios_raw = document.get("scenarios")
    _require(isinstance(scenarios_raw, list) and scenarios_raw,
             "scenarios: непустой список сценариев")
    scenarios = []
    seen_ids = set()
    seen_signatures = set()
    for raw in scenarios_raw:
        scenario = validate_scenario(raw, registry_ids)
        scenario_id = scenario["scenario_id"]
        _require(scenario_id not in seen_ids, f"scenario_id {scenario_id!r} повторяется")
        seen_ids.add(scenario_id)
        signature = (scenario["family"], tuple(scenario["actors"]["from"]),
                     tuple(scenario["actors"]["to"]), scenario["channel"])
        _require(signature not in seen_signatures,
                 f"сценарий {scenario_id}: дубль family+actors+channel")
        seen_signatures.add(signature)
        scenarios.append(scenario)

    checks_raw = document.get("direction_checks") or []
    _require(isinstance(checks_raw, list), "direction_checks: список")
    checks = [_validate_direction_check(item, seen_ids) for item in checks_raw]
    _require(len({item["check_id"] for item in checks}) == len(checks),
             "direction_checks: повторяющиеся check_id")
    _require(len({item["scenario_id"] for item in checks}) == len(checks),
             "direction_checks: две проверки на один сценарий не поддерживаются")

    families = {}
    for scenario in scenarios:
        families[scenario["family"]] = families.get(scenario["family"], 0) + 1
    countries = sorted({actor for scenario in scenarios
                        for side in ("from", "to")
                        for actor in scenario["actors"][side]})
    industries = sorted({industry for scenario in scenarios for industry in scenario["sectors"]})
    return {
        "scenario_count": len(scenarios),
        "family_count": len(families),
        "families": dict(sorted(families.items())),
        "country_count": len(countries),
        "countries": countries,
        "industry_count": len(industries),
        "industries": industries,
        "direction_check_count": len(checks),
        "scenarios": scenarios,
        "direction_checks": checks,
    }


# ------------------------------------------------------------------------------------- resolution
def _document_country_maps(document: dict) -> tuple[dict, dict]:
    return dict(document.get("country_map") or {}), dict(document.get("proxies") or {})


def resolve_actor(actor: str, document: dict, table) -> dict:
    """ISO3 -> dataset label; an explicit proxy is allowed and recorded (fail-closed)."""
    country_map, proxies = _document_country_maps(document)
    if actor in country_map:
        label, via, proxy_note = country_map[actor], "country_map", None
    elif actor in proxies:
        label = proxies[actor]["label"]
        via, proxy_note = "proxy", proxies[actor]["note"]
    else:
        raise ProtocolError(f"актор {actor!r} не отображён в country_map/proxies протокола")
    if label not in table.countries:
        raise ProtocolError(f"метки {label!r} (актор {actor!r}) нет в таблице; "
                            f"доступны {list(table.countries)}")
    return {"actor": actor, "label": label, "via": via, "proxy_note": proxy_note}


def resolve_protocol(document: dict, table, summary: dict | None = None) -> dict:
    """Resolve every actor and every industry against the table before solving anything."""
    summary = summary or validate_protocol(document)
    industries = set(table.industries)
    actors = {}
    proxies_by_scenario: dict[str, list[dict]] = {}
    for scenario in summary["scenarios"]:
        proxy_actors = []
        for side in ("from", "to"):
            for actor in scenario["actors"][side]:
                actors.setdefault(actor, resolve_actor(actor, document, table))
        for component in scenario["companions"]:
            for side in ("from", "to"):
                for actor in component["actors"][side]:
                    actors.setdefault(actor, resolve_actor(actor, document, table))
        for actor in sorted({actor for side in ("from", "to")
                             for actor in scenario["actors"][side]}
                            | {actor for component in scenario["companions"]
                               for side in ("from", "to")
                               for actor in component["actors"][side]}):
            resolved = actors[actor]
            if resolved["via"] == "proxy":
                proxy_actors.append({"actor": actor, "label": resolved["label"],
                                     "note": resolved["proxy_note"]})
        if proxy_actors:
            proxies_by_scenario[scenario["scenario_id"]] = proxy_actors
        missing = sorted(set(scenario["sectors"]) - industries)
        if missing:
            raise ProtocolError(f"сценарий {scenario['scenario_id']}: отраслей нет в таблице: "
                                f"{missing}")
        for component in scenario["companions"]:
            missing = sorted(set(component["sectors"]) - industries)
            if missing:
                raise ProtocolError(f"сценарий {scenario['scenario_id']} (companion): отраслей нет "
                                    f"в таблице: {missing}")
    for check in summary["direction_checks"]:
        for target in check["targets"]:
            actors.setdefault(target["actor"], resolve_actor(target["actor"], document, table))
            if target["industry"] not in industries:
                raise ProtocolError(f"проверка {check['check_id']}: отрасли "
                                    f"{target['industry']!r} нет в таблице")
    return {"actors": dict(sorted(actors.items())), "industries": sorted(industries),
            "proxies_by_scenario": proxies_by_scenario}


# --------------------------------------------------------------------------------------- protocol
def load_protocol(path=DEFAULT_PROTOCOL) -> tuple[dict, str]:
    """Read the protocol fail-closed; returns ``(document, raw_sha256)``."""
    target = Path(path)
    if not target.is_file():
        raise BlockedInput(f"протокол не найден: {target} (ожидается "
                           "protocols/geopolitics_scenarios.json)")
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise BlockedInput(f"протокол не читается: {target}: {exc}") from exc
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise ProtocolError(f"протокол не является корректным JSON: {exc}") from exc
    return document, hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_source_registry(path=SOURCE_REGISTRY_PATH) -> set[str] | None:
    """Registered source ids, or ``None`` if the registry is absent (validation then skips)."""
    target = Path(path)
    if not target.is_file():
        return None
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sources = document.get("sources") if isinstance(document, dict) else None
    if not isinstance(sources, list):
        return None
    return {str(item.get("source_id")) for item in sources
            if isinstance(item, dict) and item.get("source_id")}


# ------------------------------------------------------------------------------------------ shock
def _country_index(table, label: str) -> int:
    try:
        return table.countries.index(label)
    except ValueError as exc:  # pragma: no cover - resolve_protocol already checked
        raise ProtocolError(f"страны {label!r} нет в таблице") from exc


def _sector_indices(table, sectors) -> np.ndarray:
    return np.asarray([table.industries.index(name) for name in sectors], dtype=np.int64)


def _entry_mask(table, matrix, row_countries, col_countries, sectors) -> np.ndarray:
    """Boolean mask over the non-zero entries of ``A``: row country, column country, column sector."""
    rows = np.repeat(np.arange(table.n), np.diff(matrix.indptr))
    cols = matrix.indices
    mask = np.ones(rows.size, dtype=bool)
    if row_countries is not None:
        mask &= np.isin(rows // table.n_industries, np.asarray(row_countries, dtype=np.int64))
    if col_countries is not None:
        mask &= np.isin(cols // table.n_industries, np.asarray(col_countries, dtype=np.int64))
    if sectors is not None:
        mask &= np.isin(cols % table.n_industries, np.asarray(sectors, dtype=np.int64))
    return mask


def _apply_coefficient_shock(table, matrix, component, resolution, factor_sign):
    """Scale one declared block of cross-country coefficients; return (A_new, detail)."""
    channel = component["channel"]
    size = float(component["size"])
    from_idx = [_country_index(table, resolution["actors"][actor]["label"])
                for actor in component["actors"]["from"]]
    to_idx = [_country_index(table, resolution["actors"][actor]["label"])
              for actor in component["actors"]["to"]]
    sectors = _sector_indices(table, component["sectors"])
    rows = np.repeat(np.arange(table.n), np.diff(matrix.indptr))
    cols = matrix.indices
    mask = _entry_mask(table, matrix, to_idx, from_idx, sectors)
    if not mask.any():
        raise ProtocolError(
            f"{channel}: на объявленном направлении нет межстрановых коэффициентов "
            f"(from={component['actors']['from']}, to={component['actors']['to']}, "
            f"sectors={component['sectors']})")
    multiplier = -size if factor_sign < 0 else size
    delta = matrix.multiply(sp.csr_matrix((multiplier * mask.astype(float), (rows, cols)),
                                          shape=matrix.shape))
    shocked = (matrix + delta).tocsr()
    shocked.sum_duplicates()
    shocked.sort_indices()
    detail = {
        "channel": channel,
        "actors": component["actors"],
        "sectors": list(component["sectors"]),
        "size": round(size, 6),
        "direction": "cut" if factor_sign < 0 else "raise",
        "coefficient_entries": int(mask.sum()),
        "entries_share_of_A": round(float(mask.sum()) / float(matrix.nnz), 9) if matrix.nnz else 0.0,
    }
    return shocked, detail


def _demand_delta(table, component, resolution) -> tuple[np.ndarray, dict]:
    """Declared final-demand change: zero-sum diversion or a net demand shock."""
    channel = component["channel"]
    size = float(component["size"])
    base = table.effective_final_demand
    delta = np.zeros(table.n, dtype=float)
    sectors = [table.industries.index(name) for name in component["sectors"]]
    from_labels = [resolution["actors"][actor]["label"] for actor in component["actors"]["from"]]
    to_labels = [resolution["actors"][actor]["label"] for actor in component["actors"]["to"]]
    if channel == "demand_diversion":
        # Trade diversion: the transferred volume is `size` of min(source final demand,
        # recipients' final demand) — it cannot exceed either side's market, so a large domestic
        # market does not produce an absurd transfer.  Recipients gain in proportion to their
        # market size; the source loses exactly the transferred volume (zero-sum).
        for sector in sectors:
            source_base = 0.0
            for label in from_labels:
                source_base += float(base[_country_index(table, label) * table.n_industries
                                          + sector])
            target_base = {}
            for label in to_labels:
                target_base[label] = float(base[_country_index(table, label) * table.n_industries
                                                + sector])
            targets_total = sum(target_base.values())
            pool = min(source_base, targets_total)
            if pool <= 0.0:
                continue
            transfer = size * pool
            for label, value in target_base.items():
                if targets_total <= 0.0:
                    continue
                index = _country_index(table, label) * table.n_industries + sector
                delta[index] += transfer * (value / targets_total)
            if source_base > 0.0:
                for label in from_labels:
                    index = _country_index(table, label) * table.n_industries + sector
                    share = float(base[index]) / source_base
                    delta[index] -= transfer * share
        detail = {"channel": channel, "actors": component["actors"],
                  "sectors": list(component["sectors"]), "size": round(size, 6),
                  "direction": "transfer", "zero_sum": True,
                  "basis": "min(конечный спрос источника, конечный спрос получателей)"}
    else:
        for label in from_labels:
            source = _country_index(table, label)
            for sector in sectors:
                index = source * table.n_industries + sector
                delta[index] += size * float(base[index])
        detail = {"channel": channel, "actors": component["actors"],
                  "sectors": list(component["sectors"]), "size": round(size, 6),
                  "direction": "increase", "zero_sum": False}
    if not np.any(delta):
        raise ProtocolError(f"{channel}: объявленный спрос нулевой на этой таблице, шок пуст")
    detail["delta_final_demand_total"] = round(float(delta.sum()), 6)
    detail["delta_final_demand_max_abs"] = round(float(np.abs(delta).max()), 6)
    if channel == "demand_diversion":
        detail["transfer_total"] = round(float(delta[delta > 0.0].sum()), 6)
    return delta, detail


def execution_plan(scenario: dict) -> list[dict]:
    """Scenario itself plus its declared companions, in a fixed order."""
    return [scenario] + list(scenario["companions"])


def execute_scenario(table, solver, scenario: dict, resolution: dict) -> dict:
    """Build the declared perturbation, solve it and return the aggregates + checks."""
    A = solver.A
    delta_y = np.zeros(table.n, dtype=float)
    details = []
    technology_changed = False
    for component in execution_plan(scenario):
        channel = component["channel"]
        if channel in COEFFICIENT_CHANNELS:
            sign = +1 if channel == "trade_facilitation" else -1
            A, detail = _apply_coefficient_shock(table, A, component, resolution, sign)
            technology_changed = True
            details.append(detail)
        elif channel in DEMAND_CHANNELS:
            delta, detail = _demand_delta(table, component, resolution)
            delta_y += delta
            details.append(detail)
        else:  # pragma: no cover - validated earlier
            raise ProtocolError(f"неизвестный канал {channel!r}")
    if not technology_changed:
        A = solver.A
    y_used = table.effective_final_demand + delta_y
    tolerance = float(table.tolerance)
    if float(y_used.min()) < -tolerance:
        raise ProtocolError(f"сценарий {scenario['scenario_id']}: конечный спрос стал отрицательным")
    spectral_radius = solver.spectral_radius
    if technology_changed:
        if A.nnz and float(A.data.min()) < -tolerance:
            raise ProtocolError(f"сценарий {scenario['scenario_id']}: отрицательный "
                                "технический коэффициент")
        spectral_radius = wp.perron_root(A)
        if not spectral_radius < 1.0 - tolerance:
            raise ProtocolError(f"сценарий {scenario['scenario_id']}: ломает продуктивность, "
                                f"rho(A') = {spectral_radius:.12f} >= 1")
        try:
            factor = spla.splu(sp.identity(table.n, format="csc") - A.tocsc())
        except RuntimeError as exc:
            raise ProtocolError(f"сценарий {scenario['scenario_id']}: (I - A') вырождена: "
                                f"{exc}") from exc
        scenario_x = np.asarray(factor.solve(y_used), dtype=float)
    else:
        scenario_x = solver.solve(y_used, label=scenario["scenario_id"])
    scenario_x = np.asarray(scenario_x, dtype=float)
    if not np.all(np.isfinite(scenario_x)):
        raise ProtocolError(f"сценарий {scenario['scenario_id']}: решение не конечно")
    scale = max(1.0, float(np.max(np.abs(y_used))))
    if float(scenario_x.min()) < -tolerance * scale:
        raise ProtocolError(f"сценарий {scenario['scenario_id']}: недопустимое решение "
                            "(отрицательный выпуск)")
    scenario_x = np.maximum(scenario_x, 0.0)
    residual = A @ scenario_x + y_used - scenario_x
    max_residual = float(np.max(np.abs(residual))) if residual.size else 0.0
    if max_residual > tolerance * max(1.0, float(np.max(scenario_x))):
        raise ProtocolError(f"сценарий {scenario['scenario_id']}: баланс не сошёлся, "
                            f"остаток {max_residual:.6e}")
    impact = scenario_x - solver.baseline_x
    return {
        "gross_output": scenario_x,
        "delta_output": impact,
        "spectral_radius": float(spectral_radius),
        "technology_changed": bool(technology_changed),
        "material_residual": max_residual,
        "delta_final_demand_total": round(float(delta_y.sum()), 6),
        "components": details,
        "feasible": True,
    }


# -------------------------------------------------------------------------------------- aggregate
def _aggregate(table, values) -> tuple[dict, dict]:
    array = np.asarray(values, dtype=float).ravel()
    if array.size != table.n:
        raise ProtocolError(f"вектор должен быть длины {table.n}, получено {array.size}")
    matrix = array.reshape(table.n_countries, table.n_industries)
    by_country = matrix.sum(axis=1)
    by_industry = matrix.sum(axis=0)
    return ({name: round(float(value), 6) for name, value in zip(table.countries, by_country)},
            {name: round(float(value), 6) for name, value in zip(table.industries, by_industry)})


def _positive_cells(table, delta, threshold: float, limit: int = 5) -> list[dict]:
    matrix = np.asarray(delta, dtype=float).reshape(table.n_countries, table.n_industries)
    cells = [(table.countries[ci], table.industries[ii], float(matrix[ci, ii]))
             for ci in range(table.n_countries) for ii in range(table.n_industries)
             if float(matrix[ci, ii]) > threshold]
    cells.sort(key=lambda item: (-item[2], item[0], item[1]))
    return [{"country": country, "industry": industry, "delta_output": round(value, 3)}
            for country, industry, value in cells[:max(0, int(limit))]]


def beneficiaries(table, delta, threshold: float) -> dict:
    """P-008: explicit beneficiaries, or an explicit "no gain" record."""
    by_country, by_industry = _aggregate(table, delta)
    countries = [{"name": name, "delta_output": value}
                 for name, value in sorted(by_country.items(), key=lambda item: (-item[1], item[0]))
                 if value > threshold]
    industries = [{"name": name, "delta_output": value}
                  for name, value in sorted(by_industry.items(), key=lambda item: (-item[1], item[0]))
                  if value > threshold]
    cells = _positive_cells(table, delta, threshold)
    if not countries and not industries and not cells:
        return {
            "status": "none",
            "unit": "USD_mn",
            "threshold": round(float(threshold), 6),
            "counts": {"countries": 0, "industries": 0, "cells": 0},
            "countries": [],
            "industries": [],
            "cells": [],
            "note": ("выгод нет: ни одна страна или отрасль не получает положительного Δвыпуска "
                     "выше порога; в этой сравнительной статике барьер только сокращает "
                     "межстрановые коэффициенты (P-008: асимметрия не выдумывается)"),
        }
    gain_total = float(sum(item["delta_output"] for item in countries))
    return {
        "status": "found",
        "unit": "USD_mn",
        "threshold": round(float(threshold), 6),
        "counts": {"countries": len(countries), "industries": len(industries), "cells": len(cells)},
        "countries": countries[:TOP_N],
        "industries": industries[:TOP_N],
        "cells": cells,
        "note": (f"выгодоприобретатели: {len(countries)} стран(ы) и {len(industries)} отраслей(и) "
                 f"с положительным Δвыпуска выше порога; сумма положительных страновых Δ — "
                 f"{round(gain_total, 3)} USD млн (P-008)"),
    }


def asymmetry(table, delta, declared: str, threshold: float) -> dict:
    array = np.asarray(delta, dtype=float)
    gain_total = float(array[array > threshold].sum()) if array.size else 0.0
    loss_total = float(-array[array < -threshold].sum()) if array.size else 0.0
    has_gain, has_loss = gain_total > threshold, loss_total > threshold
    observed = "both" if has_gain and has_loss else ("benefit" if has_gain
                                                     else ("damage" if has_loss else "none"))
    consistent = observed == declared or declared == "both" and observed == "both"
    return {
        "declared_direction": declared,
        "observed_direction": observed,
        "consistent": bool(consistent),
        "gain_total": round(gain_total, 3),
        "loss_total": round(loss_total, 3),
        "gain_share_of_abs": (round(gain_total / (gain_total + loss_total), 6)
                              if gain_total + loss_total else 0.0),
        "gain_loss_ratio": (round(gain_total / loss_total, 6) if loss_total else None),
        "threshold": round(float(threshold), 6),
        "note": ("наблюдаемое направление — знак агрегированного Δвыпуска, а не оценка "
                 "благосостояния; P-008: выгоды и ущерб считаются отдельно"),
    }


# ------------------------------------------------------------------------------------------ decay
def decay_profile(scenario: dict) -> dict:
    """Declared decay of the shock size over the declared duration (no dynamics is solved)."""
    decay = scenario["decay"]
    kind = decay["kind"]
    duration = int(scenario["duration"])
    half_life = decay["half_life_months"]
    profile = []
    for month in range(duration + 1):
        if kind in ("none", "step"):
            factor = 1.0 if (kind == "none" or month < duration) else 0.0
        elif kind == "linear":
            factor = 1.0 - month / float(duration)
        else:
            factor = 0.5 ** (month / float(half_life))
        profile.append({"month": month, "factor": round(max(0.0, float(factor)), 6)})
    return {
        "kind": kind,
        "half_life_months": half_life,
        "duration_months": duration,
        "profile": profile,
        "effective_size_at_horizon": round(float(scenario["size"]) * profile[-1]["factor"], 6),
        "solved_at": "peak",
        "note": ("статическая модель не имеет времени: числа отчёта относятся к пику шока "
                 "(factor=1); профиль — объявленное затухание, не динамика и не путь"),
    }


# ----------------------------------------------------------------------------------------- report
def _synthetic_meta(table) -> dict:
    return {
        "dataset_id": "synthetic-labelled",
        "sha256": "-",
        "bytes": 0,
        "schema": "amfe.geopolitics.synthetic.v1",
        "countries": table.n_countries,
        "industries": table.n_industries,
        "sectors": table.n,
        "stored_Z": int(table.Z.nnz),
        "stored_share": round(float(table.Z.nnz) / float(table.n * table.n), 6),
        "years": [2020],
        "units": table.units,
        "total_output": round(float(table.x.sum()), 3),
        "total_final_demand": round(float(table.y.sum()), 3),
        "total_value_added": round(float(table.va.sum()), 3),
        "source": "synthetic labelled table (not an observation)",
        "provenance_note": "deterministic constructed table with FIGARO labels; tests/demo only",
    }


def build_scenario_report(scenario: dict, execution: dict, table, dataset_meta: dict,
                          baseline: dict, validation: dict, proxies: list[dict] | None = None) -> dict:
    """Assemble one aggregate-only scenario report."""
    proxies = list(proxies or [])
    proxy_warning = None
    if proxies:
        proxy_warning = (
            "Сценарий использует прокси вместо отсутствующей страны: "
            + "; ".join(f"{item['actor']} -> {item['label']}" for item in proxies)
            + ". Числа относятся к прокси-блоку и НЕ являются оценкой этой страны "
              "(см. proxies в протоколе).")
    delta = execution["delta_output"]
    by_country, by_industry = _aggregate(table, delta)
    scale = max(1.0, float(np.max(np.abs(delta)))) if delta.size else 1.0
    threshold = max(1e-6, 1e-9 * scale)
    total_delta = float(np.sum(delta))
    baseline_total = float(baseline["total_output"])
    tolerance = float(table.tolerance)
    max_abs_output = float(np.max(np.abs(execution["gross_output"]))) if delta.size else 0.0
    balances_pass = bool(validation["pass"] and execution["feasible"]
                         and execution["material_residual"] <= tolerance * max(1.0, max_abs_output)
                         and execution["spectral_radius"] < 1.0 - tolerance)
    report = {
        "schema": SCENARIO_SCHEMA,
        "status": STATUS,
        "status_note": STATUS_NOTE,
        "gating": False,
        "causal_claim": False,
        "proxy": proxies,
        "proxy_warning": proxy_warning,
        "scenario": {
            "scenario_id": scenario["scenario_id"],
            "title_ru": scenario["title_ru"],
            "family": scenario["family"],
            "actors": scenario["actors"],
            "sectors": scenario["sectors"],
            "channel": scenario["channel"],
            "size": scenario["size"],
            "duration": scenario["duration"],
            "decay": scenario["decay"],
            "direction": scenario["direction"],
            "source": scenario["source"],
            "pit_date": scenario["pit_date"],
            "assumptions": scenario["assumptions"],
            "companions": scenario["companions"],
            "historical_anchor": scenario.get("historical_anchor"),
            "notes": scenario.get("notes"),
        },
        "dataset": dataset_meta,
        "baseline": baseline,
        "result": {
            "total_output": round(float(np.sum(execution["gross_output"])), 3),
            "total_delta_output": round(total_delta, 3),
            "relative_delta_output": (round(total_delta / baseline_total, 9)
                                      if baseline_total else None),
            "max_abs_delta_output": round(float(np.max(np.abs(delta))) if delta.size else 0.0, 3),
            "delta_final_demand_total": execution["delta_final_demand_total"],
            "units": table.units,
        },
        "impacts": {
            "by_country": by_country,
            "by_industry": by_industry,
            "countries": wr.top_list(by_country),
            "countries_named": wr.top_list(by_country, exclude=wr.REST_OF_WORLD_LABELS),
            "industries": wr.top_list(by_industry),
        },
        "perturbation": execution["components"],
        "decay": decay_profile(scenario),
        "beneficiaries": beneficiaries(table, delta, threshold),
        "asymmetry": asymmetry(table, delta, scenario["direction"], threshold),
        "checks": {
            "validation_pass": bool(validation["pass"]),
            "max_abs_row_residual": validation["max_abs_row_residual"],
            "max_abs_value_added_residual": validation["max_abs_value_added_residual"],
            "baseline_productive": baseline["productive"],
            "baseline_spectral_radius": baseline["spectral_radius"],
            "scenario_spectral_radius": round(float(execution["spectral_radius"]), 12),
            "scenario_productive": bool(execution["spectral_radius"] < 1.0 - tolerance),
            "scenario_material_residual": round(float(execution["material_residual"]), 12),
            "balance_tolerance": tolerance,
            "balances_pass": balances_pass,
            "feasible": bool(execution["feasible"]),
            "gating": False,
            "causal_claim": False,
        },
        "method": {
            "model": "статическая линейная модель Леонтьева на MRIO (сравнительная статика)",
            "equation": "x = (I - A)^-1 y; сценарий: x' = (I - A')^-1 (y + dy)",
            "channels": {
                "import_restriction": "сокращение A на направлении from -> to в объявленных отраслях",
                "export_restriction": "то же сокращение на объявленном направлении (сторона-инициатор "
                                      "описывается в actors)",
                "chokepoint": "то же сокращение поставок через пролив; физическая мощность не "
                              "моделируется",
                "demand_diversion": "перенос конечного спроса from -> to, сумма dy = 0",
                "demand_shock": "прирост конечного спроса получателя поддержки",
                "trade_facilitation": "рост A на объявленном направлении (соглашение)",
            },
            "aggregation": "Δx суммируется по странам и отраслям; топ-10 по |Δвыпуск|",
            "direction_check": "только знак трёх целевых ячеек; магнитуда не оценивается",
            "top_n": TOP_N,
        },
        "limitations": list(LIMITATIONS),
    }
    report["digest"] = _sha256(_canonical({key: value for key, value in report.items()
                                           if key != "digest"}))
    return report


def run_direction_checks(summary: dict, reports_by_id: dict, deltas: dict, table,
                         resolution: dict) -> list[dict]:
    """Sign-only historical anchors: compare the sign of three cells with the recorded fact."""
    results = []
    for check in summary["direction_checks"]:
        scenario_id = check["scenario_id"]
        if scenario_id not in deltas:
            results.append({"check_id": check["check_id"], "scenario_id": scenario_id,
                            "scope": "sign_only", "status": "skipped",
                            "note": "сценарий не прогонялся в этом запуске (--only)"})
            continue
        delta = np.asarray(deltas[scenario_id], dtype=float)
        scale = max(1.0, float(np.max(np.abs(delta)))) if delta.size else 1.0
        threshold = max(1e-6, 1e-9 * scale)
        targets = []
        proxy_notes = []
        for target in check["targets"]:
            resolved = resolution["actors"][target["actor"]]
            index = (_country_index(table, resolved["label"]) * table.n_industries
                     + table.industries.index(target["industry"]))
            value = float(delta[index])
            sign = 1 if value > threshold else (-1 if value < -threshold else 0)
            targets.append({
                "actor": target["actor"],
                "table_label": resolved["label"],
                "resolved_via": resolved["via"],
                "industry": target["industry"],
                "expected_sign": int(target["expected_sign"]),
                "sign": int(sign),
                "match": bool(sign == target["expected_sign"]),
            })
            if resolved["via"] == "proxy" and resolved["proxy_note"]:
                proxy_notes.append(f"{target['actor']} -> {resolved['label']}: "
                                   f"{resolved['proxy_note']}")
        matched = sum(1 for target in targets if target["match"])
        results.append({
            "check_id": check["check_id"],
            "title_ru": check.get("title_ru"),
            "scenario_id": scenario_id,
            "anchor_year": check.get("anchor_year"),
            "scope": "sign_only",
            "status": "run",
            "fact": check["fact"],
            "fact_source": check["fact_source"],
            "targets": targets,
            "matched": int(matched),
            "targets_total": len(targets),
            "min_matches": int(check["min_matches"]),
            "pass": bool(matched >= check["min_matches"]),
            "magnitude_reported": False,
            "proxy": proxy_notes,
            "notes": list(check["notes"]) + [
                "блок проверки не содержит чисел Δвыпуска: проверяется и публикуется только знак",
            ],
        })
    return results


# ------------------------------------------------------------------------------------------- index
def build_index(reports: list[dict], direction_checks: list[dict], dataset_meta: dict,
                protocol_meta: dict, validation: dict, core: dict) -> dict:
    entries = []
    for report in reports:
        scenario = report["scenario"]
        entries.append({
            "id": scenario["scenario_id"],
            "title_ru": scenario["title_ru"],
            "family": scenario["family"],
            "channel": scenario["channel"],
            "size": scenario["size"],
            "duration": scenario["duration"],
            "pit_date": scenario["pit_date"],
            "direction": scenario["direction"],
            "observed_direction": report["asymmetry"]["observed_direction"],
            "beneficiaries_status": report["beneficiaries"]["status"],
            "proxy": bool(report["proxy"]),
            "file_json": f"reports/{scenario['scenario_id']}.json",
            "file_md": f"reports/{scenario['scenario_id']}.md",
            "digest": report["digest"],
            "headline": {
                "total_delta_output": report["result"]["total_delta_output"],
                "relative_delta_output": report["result"]["relative_delta_output"],
                "largest_country": report["impacts"]["countries_named"]["largest"],
                "largest_industry": report["impacts"]["industries"]["largest"],
                "max_abs_material_residual": report["checks"]["scenario_material_residual"],
            },
        })
    gain_scenarios = sum(1 for report in reports if report["beneficiaries"]["status"] == "found")
    none_scenarios = sum(1 for report in reports if report["beneficiaries"]["status"] == "none")
    index = {
        "schema": INDEX_SCHEMA,
        "status": STATUS,
        "status_note": STATUS_NOTE,
        "gating": False,
        "causal_claim": False,
        "tool": {"name": "tools/geopolitics_scenarios.py", "version": TOOL_VERSION},
        "protocol": protocol_meta,
        "dataset": dataset_meta,
        "baseline": {
            "total_output": round(float(reports[0]["baseline"]["total_output"]), 3)
            if reports else None,
            "spectral_radius": (reports[0]["baseline"]["spectral_radius"] if reports else None),
        },
        "checks": {
            "scenario_count": len(entries),
            "family_count": len({entry["family"] for entry in entries}),
            "validation_pass": bool(validation["pass"]),
            "all_balances_pass": all(report["checks"]["balances_pass"] for report in reports),
            "all_feasible": all(report["checks"]["feasible"] for report in reports),
            "all_technology_productive": all(report["checks"]["scenario_productive"]
                                             for report in reports),
            "max_abs_material_residual": max(
                (report["checks"]["scenario_material_residual"] for report in reports),
                default=0.0),
            "hawkins_simon_pass": bool(core.get("hawkins_simon_pass")),
            "country_block_spectral_radius": core.get("spectral_radius"),
            "directions_consistent": all(report["asymmetry"]["consistent"] for report in reports),
            "beneficiaries_found": gain_scenarios,
            "beneficiaries_none": none_scenarios,
            "direction_checks_run": sum(1 for check in direction_checks
                                        if check.get("status") == "run"),
            "direction_checks_passed": sum(1 for check in direction_checks if check.get("pass")),
        },
        "scenarios": entries,
        "direction_checks": direction_checks,
        "limitations": list(LIMITATIONS),
    }
    index["digest"] = _sha256(_canonical({key: value for key, value in index.items()
                                          if key != "digest"}))
    return index


# ------------------------------------------------------------------------------------------ render
def _format_value(value, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{float(value):,.{digits}f}".replace(",", " ")


def render_scenario_markdown(report: dict) -> str:
    scenario = report["scenario"]
    dataset = report["dataset"]
    result = report["result"]
    impacts = report["impacts"]
    checks = report["checks"]
    lines = [
        f"# Сценарий `{scenario['scenario_id']}`: {scenario['title_ru']}",
        "",
        f"**Статус: {STATUS}** — {STATUS_NOTE}.",
        "",
        "## Данные",
        "",
        f"- датасет: `{dataset['dataset_id']}` (SHA-256 `{str(dataset['sha256'])[:16]}…`), "
        f"{dataset['countries']} стран × {dataset['industries']} отраслей = {dataset['sectors']} "
        f"секторов, единицы {dataset['units']}",
        f"- baseline: выпуск {_format_value(report['baseline']['total_output'])} "
        f"{dataset['units']}, конечный спрос "
        f"{_format_value(report['baseline']['total_final_demand'])}; ρ(A) = "
        f"{checks['baseline_spectral_radius']:.9f}",
        "",
        "## Сценарий",
        "",
        f"- семейство: `{scenario['family']}`; канал: `{scenario['channel']}`; "
        f"направление (P-008): `{scenario['direction']}`",
        f"- акторы: {', '.join(scenario['actors']['from'])} → "
        f"{', '.join(scenario['actors']['to'])}",
        f"- отрасли: {', '.join(scenario['sectors'])}",
        f"- величина: {scenario['size']}; длительность: {scenario['duration']} мес.; "
        f"затухание: {scenario['decay']['kind']}"
        + (f" (полураспад {scenario['decay']['half_life_months']} мес.)"
           if scenario["decay"]["half_life_months"] else ""),
        f"- PIT-дата: {scenario['pit_date']}; источник: `{scenario['source']['kind']}` "
        f"`{scenario['source']['id']}`",
    ]
    if scenario["source"].get("replace_with"):
        lines.append(f"- чем заменить допущение: {scenario['source']['replace_with']}")
    if scenario.get("notes"):
        lines.append(f"- примечание: {scenario['notes']}")
    if report.get("proxy_warning"):
        lines += ["", f"> **Прокси.** {report['proxy_warning']}", ""]
    lines += ["", "Допущения:", ""]
    lines += [f"- {item}" for item in scenario["assumptions"]]
    if scenario["companions"]:
        lines += ["", "Дополнительные шоки (асимметрия):", ""]
        for component in scenario["companions"]:
            lines.append(f"- `{component['channel']}`: {', '.join(component['actors']['from'])} → "
                         f"{', '.join(component['actors']['to'])}, отрасли "
                         f"{', '.join(component['sectors'])}, величина {component['size']}")
    lines += [
        "",
        "## Результат (пик шока)",
        "",
        f"- Δвыпуск: **{_format_value(result['total_delta_output'])}** {dataset['units']} "
        f"({(result['relative_delta_output'] or 0) * 100:+.4f}% от baseline)",
        f"- выпуск после шока: {_format_value(result['total_output'])} {dataset['units']}",
        f"- Δконечного спроса (сумма): {_format_value(result['delta_final_demand_total'], 3)}",
        f"- максимальное |Δ| по сектору: {_format_value(result['max_abs_delta_output'], 3)}",
        "",
        f"## Топ-10 стран по |Δвыпуска| (всего {impacts['countries_named']['entries']} стран)",
        "",
        "| # | страна | Δвыпуск | доля в сумме |Δ| |",
        "|---|---|---:|---:|",
    ]
    for position, entry in enumerate(impacts["countries_named"]["top"], 1):
        lines.append(f"| {position} | {entry['name']} | {_format_value(entry['delta_output'], 3)} | "
                     f"{entry['share_of_abs'] * 100:.2f}% |")
    rest_of_world = next((name for name in wr.REST_OF_WORLD_LABELS
                          if name in impacts["by_country"]), None)
    if rest_of_world is not None:
        lines += ["",
                  f"Агрегированный остаток мира `{rest_of_world}` (не одна страна) в топ не "
                  f"входит: Δвыпуск {_format_value(impacts['by_country'][rest_of_world], 3)}; "
                  f"в полном ранжировании он "
                  f"{'первый' if impacts['countries']['largest'] == rest_of_world else 'не первый'}."]
    lines += [
        "",
        f"## Топ-10 отраслей по |Δвыпуска| (всего {impacts['industries']['entries']} отраслей)",
        "",
        "| # | отрасль | Δвыпуск | доля в сумме |Δ| |",
        "|---|---|---:|---:|",
    ]
    for position, entry in enumerate(impacts["industries"]["top"], 1):
        lines.append(f"| {position} | {entry['name']} | {_format_value(entry['delta_output'], 3)} | "
                     f"{entry['share_of_abs'] * 100:.2f}% |")
    lines += ["", "## Асимметрия (P-008)", ""]
    asym = report["asymmetry"]
    lines.append(f"- объявленное направление: `{asym['declared_direction']}`; наблюдаемое: "
                 f"`{asym['observed_direction']}`; согласовано: "
                 f"{'да' if asym['consistent'] else 'НЕТ'}")
    lines.append(f"- сумма выгод {_format_value(asym['gain_total'], 3)}; сумма ущерба "
                 f"{_format_value(asym['loss_total'], 3)}; доля выгод "
                 f"{asym['gain_share_of_abs'] * 100:.1f}%")
    lines += ["", f"### Выгодоприобретатели: `{report['beneficiaries']['status']}`", ""]
    if report["beneficiaries"]["status"] == "none":
        lines.append(f"- {report['beneficiaries']['note']}")
    else:
        lines.append(f"- стран: {report['beneficiaries']['counts']['countries']}, отраслей: "
                     f"{report['beneficiaries']['counts']['industries']}; "
                     f"{report['beneficiaries']['note']}")
        for entry in report["beneficiaries"]["countries"][:5]:
            lines.append(f"- страна {entry['name']}: {_format_value(entry['delta_output'], 3)}")
        for entry in report["beneficiaries"]["cells"][:5]:
            lines.append(f"- ячейка {entry['country']}|{entry['industry']}: "
                         f"{_format_value(entry['delta_output'], 3)}")
    if report.get("direction_check"):
        check = report["direction_check"]
        lines += ["", f"## Проверка направления `{check['check_id']}` (только знак)", "",
                  f"- сценарий: `{check['scenario_id']}`; совпало {check['matched']} из "
                  f"{check['targets_total']} (порог {check['min_matches']}); "
                  f"итог: {'PASS' if check['pass'] else 'FAIL'}",
                  f"- факт: {check['fact']}",
                  "- магнитуда не оценивается и не публикуется (docs/177 B4).", "",
                  "| актор | метка | отрасль | ожидаемый знак | знак модели | совпало |",
                  "|---|---|---|---:|---:|---|"]
        for target in check["targets"]:
            lines.append(f"| {target['actor']} | {target['table_label']} | {target['industry']} | "
                         f"{target['expected_sign']:+d} | {target['sign']:+d} | "
                         f"{'да' if target['match'] else 'нет'} |")
        for note in check["proxy"]:
            lines.append(f"- прокси: {note}")
    lines += ["", "## Проверки", "",
              f"- валидация таблицы: {'PASS' if checks['validation_pass'] else 'FAIL'}; "
              f"невязки продукта {checks['max_abs_row_residual']:.3e} и ДС "
              f"{checks['max_abs_value_added_residual']:.3e}",
              f"- балансы сценария: {'PASS' if checks['balances_pass'] else 'FAIL'}; материальный "
              f"остаток {checks['scenario_material_residual']:.3e} при допуске "
              f"{checks['balance_tolerance']:.1e}",
              f"- технология после шока: ρ(A') = {checks['scenario_spectral_radius']:.9f}, "
              f"продуктивна {'да' if checks['scenario_productive'] else 'нет'}",
              f"- gating=false, causal_claim=false; статус {STATUS}",
              "", "## Затухание (объявлено, не решено)", "",
              f"- вид: `{report['decay']['kind']}`; эффективный размер к концу срока: "
              f"{report['decay']['effective_size_at_horizon']}",
              f"- {report['decay']['note']}",
              "", "## Ограничения", ""]
    lines += [f"- {item}" for item in report["limitations"]]
    lines += ["", "## Воспроизведение", "", "```powershell",
              "python tools/geopolitics_scenarios.py --protocol "
              f"protocols/geopolitics_scenarios.json --only {scenario['scenario_id']} "
              "--out build/geopolitics", "```", "",
              f"digest: `{report['digest']}`", ""]
    return "\n".join(lines)


def render_index_markdown(index: dict) -> str:
    dataset = index["dataset"]
    protocol = index["protocol"]
    lines = [
        "# Геополитические сценарии (M16, блок 16)",
        "",
        f"**Статус: {STATUS}** — {STATUS_NOTE}.",
        "",
        f"Протокол: `{protocol['path']}` (schema `{protocol['schema']}`, "
        f"sha256 `{str(protocol['sha256'])[:16]}…`), сценариев {protocol['scenario_count']}, "
        f"семейств {protocol['family_count']}, проверок направления "
        f"{protocol['direction_check_count']}.",
        f"Данные: `{dataset['dataset_id']}` (sha256 `{str(dataset['sha256'])[:16]}…`), "
        f"{dataset['countries']} стран × {dataset['industries']} отраслей, единицы "
        f"{dataset['units']}; baseline выпуск {_format_value(dataset['total_output'])}.",
        "",
        "## Сценарии",
        "",
        "| сценарий | семейство | канал | величина | мес. | Δвыпуск | % baseline | направление | выгоды | отчёт |",
        "|---|---|---|---:|---:|---:|---:|---|---|---|",
    ]
    baseline = float(dataset["total_output"] or 0.0)
    for entry in index["scenarios"]:
        headline = entry["headline"]
        delta = float(headline["total_delta_output"] or 0.0)
        marker = " ⚠ прокси" if entry.get("proxy") else ""
        lines.append(
            f"| `{entry['id']}` — {entry['title_ru']}{marker} | {entry['family']} | "
            f"{entry['channel']} | {entry['size']} | {entry['duration']} | "
            f"{_format_value(delta)} | "
            f"{(delta / baseline * 100.0) if baseline else 0.0:+.4f}% | "
            f"{entry['direction']}/{entry['observed_direction']} | "
            f"{entry['beneficiaries_status']} | [MD]({entry['file_md']}) / "
            f"[JSON]({entry['file_json']}) |")
    lines += [
        "",
        "## Проверки",
        "",
        f"- сценариев: {index['checks']['scenario_count']}, семейств: "
        f"{index['checks']['family_count']}; валидация таблицы: "
        f"{'PASS' if index['checks']['validation_pass'] else 'FAIL'}; балансы всех сценариев: "
        f"{'PASS' if index['checks']['all_balances_pass'] else 'FAIL'}; все допустимы: "
        f"{'да' if index['checks']['all_feasible'] else 'нет'};",
        f"- продуктивность после шоков: {'да' if index['checks']['all_technology_productive'] else 'нет'}; "
        f"максимальный материальный остаток: {index['checks']['max_abs_material_residual']:.3e}; "
        f"Hawkins–Simon: {'PASS' if index['checks']['hawkins_simon_pass'] else 'FAIL'};",
        f"- направления согласованы: {'да' if index['checks']['directions_consistent'] else 'НЕТ'}; "
        f"выгодоприобретатели найдены в {index['checks']['beneficiaries_found']} сценариях, "
        f"явно «выгод нет» — в {index['checks']['beneficiaries_none']};",
        f"- проверки направления: {index['checks']['direction_checks_passed']} из "
        f"{index['checks']['direction_checks_run']} PASS (только знак).",
        "",
        "## Проверки направления (только знак)",
        "",
        "| проверка | сценарий | совпало | порог | итог |",
        "|---|---|---:|---:|---|",
    ]
    for check in index["direction_checks"]:
        if check.get("status") != "run":
            lines.append(f"| {check['check_id']} | {check['scenario_id']} | — | — | skipped |")
            continue
        lines.append(f"| {check['check_id']} | `{check['scenario_id']}` | {check['matched']} из "
                     f"{check['targets_total']} | {check['min_matches']} | "
                     f"{'PASS' if check['pass'] else 'FAIL'} |")
    lines += ["", "## Ограничения", ""]
    lines += [f"- {item}" for item in index["limitations"]]
    lines += ["", "## Воспроизведение", "", "```powershell",
              "python tools/geopolitics_scenarios.py --protocol "
              "protocols/geopolitics_scenarios.json --out build/geopolitics", "```", "",
              f"digest: `{index['digest']}`", ""]
    return "\n".join(lines)


def render_report_json(index: dict, reports: list[dict]) -> str:
    payload = {
        "schema": REPORT_SCHEMA,
        "status": STATUS,
        "status_note": STATUS_NOTE,
        "gating": False,
        "causal_claim": False,
        "index": index,
        "scenarios": reports,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def write_outputs(index: dict, reports: list[dict], out_dir) -> dict:
    """Write every artefact deterministically (UTF-8, LF); no timers, no wall clock."""
    target = Path(out_dir)
    reports_dir = target / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    paths = {"reports": {}}
    for report in reports:
        scenario_id = report["scenario"]["scenario_id"]
        json_path = reports_dir / f"{scenario_id}.json"
        md_path = reports_dir / f"{scenario_id}.md"
        json_path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                             encoding="utf-8", newline="\n")
        md_path.write_text(render_scenario_markdown(report), encoding="utf-8", newline="\n")
        paths["reports"][scenario_id] = {"json": str(json_path), "md": str(md_path)}
    report_json = target / "report.json"
    report_md = target / "report.md"
    report_json.write_text(render_report_json(index, reports), encoding="utf-8", newline="\n")
    report_md.write_text(render_index_markdown(index), encoding="utf-8", newline="\n")
    paths["report"] = {"json": str(report_json), "md": str(report_md)}
    return paths


# -------------------------------------------------------------------------------------------- table
def labelled_table(countries, industries, units: str = wp.UNITS,
                   own_share: float = 0.10, cross_share: float = 0.001):
    """Deterministic constructed table with the protocol's labels (demo/tests; not an observation).

    ``A[c1|i, c2|j] = own_share`` for ``c1 == c2 and i == j`` and ``cross_share`` for every
    ``c1 != c2``; final demand is constant, gross output is solved, ``Z = A diag(x)`` and
    ``va = x - 1'Z``.  Both IO identities hold by construction; the table is a fixture, not data.
    """
    country_labels = tuple(sorted({str(name) for name in countries}))
    industry_labels = tuple(sorted({str(name) for name in industries}))
    if not country_labels or not industry_labels:
        raise ProtocolError("labelled_table: нужны непустые списки стран и отраслей")
    nc, ni = len(country_labels), len(industry_labels)
    n = nc * ni
    rows, cols, data = [], [], []
    for row in range(n):
        c1, i1 = divmod(row, ni)
        for col in range(n):
            c2, i2 = divmod(col, ni)
            if c1 == c2 and i1 == i2:
                rows.append(row)
                cols.append(col)
                data.append(float(own_share))
            elif c1 != c2:
                rows.append(row)
                cols.append(col)
                data.append(float(cross_share))
    A = sp.csr_matrix((data, (rows, cols)), shape=(n, n), dtype=float)
    A.sum_duplicates()
    A.sort_indices()
    column_sums = np.asarray(A.sum(axis=0)).ravel()
    if float(column_sums.max()) >= 1.0:
        raise ProtocolError("labelled_table: сумма коэффициентов по столбцу >= 1")
    y = np.full(n, 100.0, dtype=float)
    try:
        factor = spla.splu(sp.identity(n, format="csc") - A.tocsc())
    except RuntimeError as exc:  # pragma: no cover - defensive
        raise ProtocolError(f"labelled_table: (I - A) вырождена: {exc}") from exc
    x = np.asarray(factor.solve(y), dtype=float)
    if not np.all(np.isfinite(x)) or float(x.min()) <= 0.0:
        raise ProtocolError("labelled_table: решение должно быть положительным")
    Z = (A @ sp.diags(x)).tocsr()
    Z.sum_duplicates()
    Z.sort_indices()
    va = x - np.asarray(Z.sum(axis=0)).ravel()
    table = wp.WorldTable(countries=country_labels, industries=industry_labels, Z=Z, y=y, x=x,
                          va=va, units=units, tolerance=1e-6,
                          provenance={"name": "synthetic-labelled", "synthetic": True,
                                      "years": [2020],
                                      "note": "constructed labels + fixed coefficients; fixture"})
    wp.validate_table(table)  # fail-closed: the fixture must balance as a real table would
    return table


def synthetic_table_for_protocol(document: dict, summary: dict | None = None):
    """Build the demo table from the protocol labels (all actors/sectors/check targets)."""
    summary = summary or validate_protocol(document)
    country_labels, industry_labels = set(), set()
    country_map, proxies = _document_country_maps(document)
    for scenario in summary["scenarios"]:
        for side in ("from", "to"):
            for actor in scenario["actors"][side]:
                label = proxies[actor]["label"] if actor in proxies else country_map.get(actor)
                if label is None:
                    raise ProtocolError(f"актор {actor!r} не отображён в country_map/proxies")
                country_labels.add(label)
        industry_labels.update(scenario["sectors"])
        for component in scenario["companions"]:
            for side in ("from", "to"):
                for actor in component["actors"][side]:
                    label = proxies[actor]["label"] if actor in proxies else country_map.get(actor)
                    if label is None:
                        raise ProtocolError(f"актор {actor!r} не отображён в country_map/proxies")
                    country_labels.add(label)
            industry_labels.update(component["sectors"])
    for check in summary["direction_checks"]:
        for target in check["targets"]:
            actor = target["actor"]
            label = proxies[actor]["label"] if actor in proxies else country_map.get(actor)
            if label is None:
                raise ProtocolError(f"актор {actor!r} не отображён в country_map/proxies")
            country_labels.add(label)
            industry_labels.add(target["industry"])
    return labelled_table(country_labels, industry_labels)


# --------------------------------------------------------------------------------------------- run
def build_reports(table, document: dict, dataset_meta: dict, protocol_sha: str,
                  only: tuple = (), summary: dict | None = None) -> dict:
    """Validate, solve, aggregate and check; fail-closed at the first refusal."""
    summary = summary or validate_protocol(document, load_source_registry())
    resolution = resolve_protocol(document, table, summary)
    validation = wp.validate_table(table)
    core = wp.country_core(table)
    solver = wp.WorldSolver(table)
    baseline = {
        "total_output": round(float(solver.baseline_x.sum()), 3),
        "total_final_demand": round(float(table.effective_final_demand.sum()), 3),
        "total_value_added": round(float(table.va.sum()), 3),
        "spectral_radius": round(float(solver.spectral_radius), 12),
        "productive": bool(solver.spectral_radius < 1.0 - table.tolerance),
        "max_abs_material_residual": round(float(solver.material_residual()), 12),
        "units": table.units,
    }
    selected = [scenario for scenario in summary["scenarios"]
                if not only or scenario["scenario_id"] in only]
    if only:
        missing = sorted(set(only) - {scenario["scenario_id"] for scenario in summary["scenarios"]})
        if missing:
            raise ProtocolError(f"--only: неизвестные сценарии {missing}")
    if not selected:
        raise ProtocolError("--only: не выбран ни один сценарий")
    reports, deltas, reports_by_id = [], {}, {}
    for scenario in selected:
        execution = execute_scenario(table, solver, scenario, resolution)
        report = build_scenario_report(
            scenario, execution, table, dataset_meta, baseline, validation,
            proxies=resolution["proxies_by_scenario"].get(scenario["scenario_id"]))
        reports.append(report)
        deltas[scenario["scenario_id"]] = execution["delta_output"]
        reports_by_id[scenario["scenario_id"]] = report
    direction_checks = run_direction_checks(summary, reports_by_id, deltas, table, resolution)
    checks_by_id = {check["scenario_id"]: check for check in direction_checks
                    if check.get("status") == "run"}
    for scenario_id, check in checks_by_id.items():
        reports_by_id[scenario_id]["direction_check"] = check
        # the sign-only block is part of the report digest
        report = reports_by_id[scenario_id]
        report["digest"] = _sha256(_canonical({key: value for key, value in report.items()
                                               if key != "digest"}))
    protocol_meta = {
        "schema": SCHEMA,
        "path": str(Path(document.get("_path", DEFAULT_PROTOCOL)).as_posix())
        if document.get("_path") else str(DEFAULT_PROTOCOL.as_posix()),
        "sha256": protocol_sha,
        "scenario_count": summary["scenario_count"],
        "family_count": summary["family_count"],
        "families": summary["families"],
        "country_count": summary["country_count"],
        "industry_count": summary["industry_count"],
        "direction_check_count": summary["direction_check_count"],
        "created": document.get("created"),
        "tool_version": TOOL_VERSION,
    }
    index = build_index(reports, direction_checks, dataset_meta, protocol_meta, validation, core)
    return {"index": index, "reports": reports, "context": {
        "validation": validation, "core": core, "baseline": baseline,
        "resolution": resolution, "deltas": deltas}}


def resolve_table(args):
    """Load the MRIO table fail-closed (``--synthetic`` builds the labelled fixture instead)."""
    if getattr(args, "synthetic", False):
        document, _sha = load_protocol(getattr(args, "protocol", DEFAULT_PROTOCOL))
        summary = validate_protocol(document, load_source_registry())
        table = synthetic_table_for_protocol(document, summary)
        return table, _synthetic_meta(table), "synthetic-labelled"
    root = getattr(args, "root", None) or DEFAULT_ROOT
    dataset_id = getattr(args, "dataset", None) or DEFAULT_DATASET
    try:
        table, meta = wr.load_table(root, str(dataset_id))
    except (wr.BlockedData, wr.ReportError, wp.BlockedData, wp.PipelineError) as exc:
        raise BlockedInput(f"{dataset_id}: {exc}") from exc
    except Exception as exc:  # pragma: no cover - loader-specific failures stay fail-closed
        raise BlockedInput(f"{dataset_id}: загрузка не удалась: {exc}") from exc
    return table, meta, str(dataset_id)


def run(args) -> dict:
    """Full CLI action: protocol -> table -> scenarios -> reports -> files."""
    protocol_path = Path(getattr(args, "protocol", DEFAULT_PROTOCOL))
    document, protocol_sha = load_protocol(protocol_path)
    try:
        protocol_label = protocol_path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:  # protocol outside the repository
        protocol_label = protocol_path.as_posix()
    document["_path"] = protocol_label
    summary = validate_protocol(document, load_source_registry())
    table, dataset_meta, dataset_id = resolve_table(args)
    result = build_reports(table, document, dataset_meta, protocol_sha,
                           only=parse_list(getattr(args, "only", None)), summary=summary)
    paths = write_outputs(result["index"], result["reports"],
                          getattr(args, "out", DEFAULT_OUT))
    result["paths"] = paths
    result["dataset_id"] = dataset_id
    return result


def _print_summary(result: dict) -> None:
    index = result["index"]
    dataset = index["dataset"]
    print(f"Геополитические сценарии — статус {index['status']} (исследовательский, не допуск)")
    print(f"  данные: {dataset['dataset_id']}  {dataset['countries']}x{dataset['industries']} "
          f"= {dataset['sectors']} секторов; baseline={dataset['total_output']:,.1f} "
          f"{dataset['units']}")
    for entry in index["scenarios"]:
        headline = entry["headline"]
        print(f"  {entry['id']:<26} {entry['family']:<20} дельта {headline['total_delta_output']:>15,.3f} "
              f"({(headline['relative_delta_output'] or 0) * 100:+.4f}%)  "
              f"выгоды: {entry['beneficiaries_status']}")
    checks = index["checks"]
    print(f"  проверки: валидация {'PASS' if checks['validation_pass'] else 'FAIL'}, балансы "
          f"{'PASS' if checks['all_balances_pass'] else 'FAIL'}, max остаток "
          f"{checks['max_abs_material_residual']:.3e}, направления "
          f"{'согласованы' if checks['directions_consistent'] else 'НЕ согласованы'}")
    for check in index["direction_checks"]:
        if check.get("status") == "run":
            print(f"  направление {check['check_id']}: {check['matched']} из "
                  f"{check['targets_total']} — {'PASS' if check['pass'] else 'FAIL'} (только знак)")
    print(f"  отчёт: {result['paths']['report']['json']}")
    print(f"  сводка: {result['paths']['report']['md']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Геополитические сценарии страна×отрасль на MRIO (docs/177 M16, блок 16)")
    parser.add_argument("--protocol", default=str(DEFAULT_PROTOCOL),
                        help="путь к protocols/geopolitics_scenarios.json")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="корень MRIO-данных")
    parser.add_argument("--dataset", default=DEFAULT_DATASET, help="id канонического датасета")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="каталог отчётов (build/geopolitics)")
    parser.add_argument("--only", default=None, help="прогнать только эти сценарии (id через запятую)")
    parser.add_argument("--synthetic", action="store_true",
                        help="построить детерминированную таблицу из меток протокола (тесты/демо)")
    parser.add_argument("--json", action="store_true", help="напечатать индекс как JSON")
    return parser


def _configure_output() -> None:
    """Windows consoles default to cp1251: make UTF-8 output non-fatal (never crash on Δ)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - non-reconfigurable stream
            pass


def main(argv=None) -> int:
    _configure_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except BlockedInput as exc:
        print(f"BLOCKED_INPUT: {exc}", file=sys.stderr)
        return EXIT_BLOCKED
    except (ProtocolError, wp.PipelineError) as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return EXIT_INVALID
    if args.json:
        print(json.dumps(result["index"], ensure_ascii=True, sort_keys=True, indent=2))
    else:
        _print_summary(result)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
