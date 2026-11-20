#!/usr/bin/env python3
"""G6-prep decision packet for preregistration 0.3 (owner decision, no automatic activation).

The tool builds a deterministic **decision packet** about ``AMFE-PIT-PREREG-0.3`` (status
``PROPOSED``) and its evaluation package:

* reads the preregistration, its SHA-256 pin and its ``content_sha256``; both must recompute;
* reads the preregistration evaluation report (``amfe.pit_prereg_evaluation.v2``) and checks that it
  is ``PROVISIONAL``, ``admission=false``, ``pins_ok=true``, ``slices_complete=true`` and that its
  embedded preregistration pin matches the file on disk;
* reads ``protocols/signoff_registry.json`` and verifies the human gate signature: object/packet
  hashes recompute, the signature is human, status values are known, and an agent can never sign;
* reads the T1 product feed and the shadow-track status (T1 ``CANDIDATE``, C6 counters);
* emits three owner options (A: activate 0.3; B: keep ``PROPOSED`` and T1 ``CANDIDATE``; C: issue a
  new 0.4 with the V2 fixes), their consequences for G4/G5/G6, the blockers and a recommendation.

The tool **never changes a status and never writes into ``protocols/``**: it is a read-only decision
packet generator. It fails closed (exit ``2``) on any pin/hash/status inconsistency or if ``--out``
points inside ``protocols/``. Everything except ``generated_at`` is deterministic for fixed inputs.

Usage:
    python tools/prereg_decision.py
    python tools/prereg_decision.py --json
    python tools/prereg_decision.py --json --out build/prereg_decision.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PREREG = "protocols/pit_preregistration_0_3.json"
PREREG_PIN = "protocols/pit_preregistration_0_3.sha256.txt"
EVAL_REPORT = "protocols/pit_prereg_evaluation_report.json"
REGISTRY = "protocols/signoff_registry.json"
FEED = "build/product/forecast_latest.json"
SHADOW = "build/shadow/shadow_status.json"

DOC_REFS = {
    "docs/160": "docs/160_Внешняя_проверка_G2.md",
    "docs/164": "docs/164_План_и_статус_проекта.md",
    "docs/170": "docs/170_Оценка_по_предрегистрации_0_3.md",
    "docs/172": "docs/172_Независимая_V2_оценки_0_3.md",
    "docs/185": "docs/185_Подпись_docs_160.md",
}
SCHEMA = "AMFE-PREREG-DECISION/1.0.0"
SIGN_OFF_ID = "G4-SIGN-160"
SIGN_STATUSES = {"PENDING_HUMAN_SIGNATURE", "SIGNED_BY_HUMAN", "REJECTED_BY_HUMAN",
                 "SUPERSEDED"}
DECISIONS = {"APPROVE", "APPROVE_WITH_NOTES", "REJECT"}
REQUIRED_SIGNATURE_FIELDS = ("name", "role", "signer_type", "date", "decision", "scope")
STALE_LIMITATION_MARKER = "no extended-window evaluation has been run"


class DecisionError(RuntimeError):
    """The decision packet cannot be built or an input is inconsistent (fail-closed)."""


# ---------------------------------------------------------------------------------------- helpers
def lf_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path: Path, label: str) -> dict:
    path = Path(path)
    if not path.is_file():
        raise DecisionError(f"{label}: нет файла {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DecisionError(f"{label}: не удалось прочитать {path}: {exc!r}") from exc
    if not isinstance(document, dict):
        raise DecisionError(f"{label}: ожидался JSON-объект")
    return document


def read_text(path: Path, label: str) -> str:
    path = Path(path)
    if not path.is_file():
        raise DecisionError(f"{label}: нет файла {path}")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DecisionError(f"{label}: не удалось прочитать {path}: {exc!r}") from exc


def read_pin(path: Path, label: str) -> str:
    text = read_text(path, label).split()
    if not text or len(text[0]) != 64:
        raise DecisionError(f"{label}: ожидался SHA-256 в первой строке")
    return text[0].lower()


def content_sha256(document: dict) -> str:
    """sha256 of the canonical JSON document without its ``content_sha256`` field."""
    payload = {key: value for key, value in document.items() if key != "content_sha256"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DecisionError(message)


# ---------------------------------------------------------------------------------------- context
def load_context(root: Path = ROOT) -> dict:
    root = Path(root)
    prereg = read_json(root / PREREG, "preregistration 0.3")
    for key in ("preregistration_id", "version", "status", "content_sha256"):
        _require(key in prereg, f"preregistration: нет поля {key!r}")
    _require(prereg["status"] in ("PROPOSED", "ACTIVE", "SUPERSEDED", "WITHDRAWN"),
             f"preregistration: неизвестный статус {prereg['status']!r}")
    file_sha = lf_sha256(root / PREREG)
    pin = read_pin(root / PREREG_PIN, "preregistration pin")
    _require(file_sha == pin,
             f"preregistration pin mismatch: file={file_sha} pin={pin}")
    content_sha = content_sha256(prereg)
    _require(content_sha == prereg["content_sha256"],
             "preregistration content_sha256 mismatch: "
             f"computed={content_sha} stored={prereg['content_sha256']}")

    report = read_json(root / EVAL_REPORT, "preregistration evaluation report")
    _require(str(report.get("schema", "")).startswith("amfe.pit_prereg_evaluation"),
             f"отчёт: неожиданная схема {report.get('schema')!r}")
    _require(report.get("status") == "PROVISIONAL",
             f"отчёт: ожидался PROVISIONAL, получен {report.get('status')!r}")
    _require(report.get("admission") is False, "отчёт: admission должен быть false")
    _require(report.get("pins_ok") is True, "отчёт: pins_ok должен быть true")
    _require(report.get("slices_complete") is True, "отчёт: slices_complete должен быть true")
    prereg_ref = report.get("preregistration") or {}
    _require(prereg_ref.get("sha256") == file_sha,
             "отчёт ссылается на другой SHA-256 preregistration")
    _require(prereg_ref.get("content_sha256") == content_sha,
             "отчёт ссылается на другой content_sha256 preregistration")
    labels = {item.get("candidate"): item for item in report.get("labels") or []}
    _require("ENSEMBLE_INVERSE_MSE" in labels, "отчёт: нет метки ENSEMBLE_INVERSE_MSE")
    candidates = {item.get("candidate"): item for item in report.get("candidates") or []}
    _require("ENSEMBLE_INVERSE_MSE" in candidates, "отчёт: нет кандидата ENSEMBLE_INVERSE_MSE")
    candidate = candidates["ENSEMBLE_INVERSE_MSE"]
    criteria = candidate.get("criteria") or {}
    c4 = candidate.get("c4_w1_ext") or {}
    c4_w2 = candidate.get("c4_w2") or {}
    slices = candidate.get("slices") or {}

    registry = read_json(root / REGISTRY, "signoff registry")
    rules = registry.get("rules") or {}
    _require(rules.get("agent_may_sign") is False, "signoff registry: agent_may_sign должен быть false")
    _require(rules.get("fail_closed") is True, "signoff registry: fail_closed должен быть true")
    statuses = set(registry.get("status_values") or [])
    _require(SIGN_STATUSES.issubset(statuses), "signoff registry: не хватает статусов подписи")
    signoff = next((item for item in registry.get("signoffs") or []
                    if item.get("id") == SIGN_OFF_ID), None)
    _require(signoff is not None, f"signoff registry: нет записи {SIGN_OFF_ID}")
    _require(signoff.get("status") in SIGN_STATUSES,
             f"signoff: неизвестный статус {signoff.get('status')!r}")
    object_sha = None
    packet_sha = None
    for part, key in (("object", "object_sha"), ("packet", "packet_sha")):
        reference = signoff.get(part) or {}
        path = root / str(reference.get("path") or "")
        stored = reference.get("sha256")
        _require(path.is_file(), f"signoff: нет файла {reference.get('path')!r}")
        actual = lf_sha256(path)
        _require(actual == stored,
                 f"signoff {part} SHA-256 mismatch: file={actual} stored={stored}")
        if key == "object_sha":
            object_sha = actual
        else:
            packet_sha = actual
    signature = signoff.get("signature")
    if signoff["status"] == "SIGNED_BY_HUMAN":
        _require(isinstance(signature, dict), "signoff: SIGNED_BY_HUMAN без подписи")
        signature = dict(signature)
        signature.setdefault("date", signoff.get("date"))
        missing = [field for field in REQUIRED_SIGNATURE_FIELDS if not signature.get(field)]
        _require(not missing, f"signoff: в подписи нет полей {missing}")
        _require(signature.get("signer_type") == "human",
                 "signoff: подписант должен быть human (agent не может подписывать)")
        _require(signature.get("decision") in DECISIONS,
                 f"signoff: неизвестное решение {signature.get('decision')!r}")
    elif signoff["status"] == "PENDING_HUMAN_SIGNATURE":
        _require(signature is None, "signoff: PENDING не должен содержать подпись")
        _require(signoff.get("date") is None, "signoff: PENDING не должен содержать дату")

    feed = read_json(root / FEED, "T1 product feed")
    _require(feed.get("status") in ("CANDIDATE", "SHADOW_TEST", "LIMITED_DEPLOYMENT", "ACTIVE"),
             f"фид: неизвестный статус {feed.get('status')!r}")
    c6 = feed.get("c6_status") or {}
    shadow = read_json(root / SHADOW, "shadow track")
    shadow_c6 = shadow.get("c6") or {}
    shadow_candidate = shadow.get("candidate") or {}

    doc_tokens = {"docs/172": "NOT_CONFIRMED", "docs/164": "G4", "docs/170": "PROVISIONAL"}
    doc_text = {}
    for key, relative in DOC_REFS.items():
        doc_text[key] = read_text(root / relative, key)
    for key, token in doc_tokens.items():
        _require(token in doc_text[key], f"{key}: ожидался маркер {token!r}")

    limitations = prereg.get("limitations") or []
    stale_limitation = any(STALE_LIMITATION_MARKER in str(item) for item in limitations)
    return {"root": root, "prereg": prereg, "file_sha": file_sha, "pin": pin,
            "content_sha": content_sha, "report": report, "candidate": candidate,
            "labels": labels, "criteria": criteria, "c4": c4, "c4_w2": c4_w2,
            "slices": slices, "registry": registry, "signoff": signoff,
            "object_sha": object_sha, "packet_sha": packet_sha, "feed": feed,
            "c6": c6, "shadow": shadow, "shadow_c6": shadow_c6,
            "shadow_candidate": shadow_candidate, "doc_text": doc_text,
            "stale_limitation": stale_limitation}


# ---------------------------------------------------------------------------------------- packet
def build_packet(context: dict, generated_at: str | None = None) -> dict:
    generated_at = generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    prereg = context["prereg"]
    report = context["report"]
    candidate = context["candidate"]
    c4 = context["c4"]
    signature = context["signoff"].get("signature") or {}
    labels = context["labels"]
    label = labels["ENSEMBLE_INVERSE_MSE"]
    c6 = context["c6"]
    prereg_status = prereg["status"]
    signature_signed = context["signoff"]["status"] == "SIGNED_BY_HUMAN"
    reissue_independently_reviewed = False  # no post-reissue V2 document exists in this tree

    blockers = []
    if context["stale_limitation"]:
        blockers.append({
            "id": "prereg_limitations_stale",
            "severity": "HIGH",
            "detail": ("текст preregistration утверждает «no extended-window evaluation has been run», "
                       "но оценка docs/170 уже выполнена — активация без поправки создаёт "
                       "рассинхронизацию заявленного и фактического состояния"),
        })
    blockers.append({
        "id": "post_hoc_activation_risk",
        "severity": "HIGH",
        "detail": ("активация 0.3 после выполнения оценки (docs/170) — post-hoc: prereg-дисциплина "
                   "требует активации до прогона; допустима только как явно раскрытая поправка "
                   "владельца и не даёт права называть результат prereg-confirmed"),
    })
    if not reissue_independently_reviewed:
        blockers.append({
            "id": "no_independent_v2_of_reissue",
            "severity": "HIGH",
            "detail": ("независимая V2 (docs/172) — NOT_CONFIRMED для прежней редакции пакета; "
                       "перевыпуск docs/170 (schema v2) закрывает пункты 1–5 по заявлению авторов, "
                       "но независимого повторного V2-ревью перевыпуска в дереве нет"),
        })
    if signature_signed:
        blockers.append({
            "id": "signature_scope_note",
            "severity": "LOW",
            "detail": ("G4-SIGN-160 SIGNED_BY_HUMAN (decision="
                       f"{signature.get('decision')!r}, дата {signature.get('date')!r}); область — "
                       "только docs/160, это не допуск и не активация prereg 0.3; рукописное "
                       "замечание подписанта — «улучшить исследование значимости применения модели»"),
        })

    consequences_common_g5 = ("G5 не требует активации prereg: продукты остаются research-only; "
                              "изменений в G5 нет")
    consequences_common_g6 = ("G6 (beta 2.0) не получает prereg-confirmed доказательной базы; "
                              "ACTIVE/admission по-прежнему невозможны (C6 0/12)")
    variants = [
        {
            "id": "A",
            "title": "Активировать prereg 0.3 (PROPOSED → ACTIVE)",
            "action": ("владелец активирует protocols/pit_preregistration_0_3.json как есть, "
                       "фиксируя его пин; содержание файла не меняется"),
            "prereg_status_after": "ACTIVE",
            "consequences": {
                "G4": ("закрывает пункт «активация prereg», но оставляет high-блокер post-hoc "
                       "активации; G4 не может считаться пройденным без независимого V2-ревью "
                       "перевыпуска"),
                "G5": consequences_common_g5,
                "G6": consequences_common_g6 + "; риск: G6-аудит увидит активацию после результата",
            },
            "can_claim": [
                "оценка docs/170 перевыпущена и её C4 W1_EXT FAIL (1.0774564, 16 начал) остаётся "
                "провизорным свидетельством",
                "prereg 0.3 зафиксирован как документ (пин и content hash неизменны)",
            ],
            "cannot_claim": [
                "что C4 FAIL является prereg-confirmed результатом 0.3",
                "что G4 пройден",
                "что T1 изменил статус (остаётся CANDIDATE)",
                "что V2-пакет подтверждён (V2 NOT_CONFIRMED, перевыпуск не ре-ревьюился)",
            ],
            "blockers": ["post_hoc_activation_risk", "no_independent_v2_of_reissue",
                         "prereg_limitations_stale"],
            "status_changes": {"prereg": "PROPOSED → ACTIVE", "T1": "CANDIDATE (без изменений)",
                               "C6": "0/12 (без изменений)"},
        },
        {
            "id": "B",
            "title": "Оставить prereg 0.3 в PROPOSED, T1 — CANDIDATE",
            "action": "никаких изменений статусов; решение фиксируется как отложенное до V2/0.4",
            "prereg_status_after": "PROPOSED",
            "consequences": {
                "G4": ("остаётся открытым по пункту активации prereg; пункт подписи docs/160 уже "
                       "закрыт (SIGNED_BY_HUMAN 2026-11-20)"),
                "G5": consequences_common_g5 + "; Client Pack v2 и MVP-продукты продолжаются",
                "G6": ("откладывается: без активированного prereg и независимого V2 нет "
                       "prereg-confirmed доказательной базы для beta-аудита"),
            },
            "can_claim": [
                "C4 W1_EXT FAIL (1.0774564, 16 начал) — честное провизорное свидетельство docs/170",
                "T1 CANDIDATE, C6 0/12, ENSEMBLE_REGIME_V4 SHADOW_TEST — статусы без изменений",
                "подпись docs/160 существует и ограничена docs/160",
            ],
            "cannot_claim": [
                "что prereg 0.3 активирован",
                "что G4/G6 пройдены",
                "что C4-результат prereg-confirmed",
            ],
            "blockers": ["no_independent_v2_of_reissue"],
            "status_changes": {"prereg": "PROPOSED (без изменений)",
                               "T1": "CANDIDATE (без изменений)",
                               "C6": "0/12 (без изменений)"},
        },
        {
            "id": "C",
            "title": "Выпустить новую prereg 0.4 с исправлениями V2",
            "action": ("владелец готовит protocols/pit_preregistration_0_4.json (новый файл и пин): "
                       "процедурные исправления V2 без изменения порогов/окон/классов; активация "
                       "0.4 — до любого нового расширенного прогона"),
            "prereg_status_after": "0.4: PROPOSED (0.3 остаётся PROPOSED)",
            "consequences": {
                "G4": ("требует выпуска 0.4, независимого V2-ревью 0.4 и отдельного нового прогона; "
                       "только тогда пункт prereg закрывается, а docs/170 остаётся историческим "
                       "свидетельством"),
                "G5": consequences_common_g5 + "; 0.4 не блокирует продукты",
                "G6": ("единственный путь к prereg-confirmed базе для G6; требует времени и "
                       "независимой проверки до beta-аудита"),
            },
            "can_claim": [
                "план исправлений V2 (REAL_TIME-only срез, reconstructed-excluded/tape-free, "
                "публикация LOO/C4 W2, проверка пина prereg, coverage digest) — спецификация, "
                "а не результат",
                "старые результаты docs/170 сохраняются без перезаписи",
            ],
            "cannot_claim": [
                "что 0.4 уже существует/активирована (её нет в дереве)",
                "что новые пороги/классы изменены (запрещено менять после результата)",
                "что C4-результат станет prereg-confirmed автоматически",
            ],
            "blockers": ["requires_owner_new_file", "requires_independent_v2_of_0_4",
                         "requires_new_evaluation_run"],
            "status_changes": {"prereg": "0.3: PROPOSED (без изменений); 0.4 — новый файл",
                               "T1": "CANDIDATE (без изменений)",
                               "C6": "0/12 (без изменений)"},
        },
    ]

    status_table = {
        "T1_CPI_YOY": {"current": context["feed"].get("status"), "A": "CANDIDATE",
                       "B": "CANDIDATE", "C": "CANDIDATE"},
        "C6": {"current": f'{c6.get("n_resolved")}/{c6.get("min_forecasts")}',
               "A": "0/12", "B": "0/12", "C": "0/12"},
        "ENSEMBLE_REGIME_V4": {"current": context["shadow_candidate"].get("status"),
                               "A": "SHADOW_TEST", "B": "SHADOW_TEST", "C": "SHADOW_TEST"},
        "AMFE-PIT-PREREG-0.3": {"current": prereg_status, "A": "ACTIVE",
                                "B": "PROPOSED", "C": "PROPOSED"},
    }

    recommendation = {
        "primary": "B",
        "secondary": "C",
        "rejected": "A",
        "headline": ("Немедленно: B — не активировать 0.3, ничего не менять. "
                     "Для разблокировки G4/G6: C — выпустить 0.4 с процедурными исправлениями V2 "
                     "и активировать её до нового прогона. A не рекомендуется."),
        "rationale": [
            "A — post-hoc активация: оценка уже выполнена, а текст 0.3 утверждает, что прогонов не "
            "было; это нарушает prereg-дисциплину и обесценивает G6-аудит.",
            "V2 (docs/172) дала NOT_CONFIRMED прежнему пакету; перевыпуск docs/170 не имеет "
            "независимого повторного V2-ревью — активировать 0.3 сейчас означало бы закрепить "
            "непроверенный пакет.",
            "B безопасен и обратим: статусы не меняются, T1 остаётся CANDIDATE, C6 0/12; "
            "провизорное свидетельство docs/170 сохраняется.",
            "C — единственный путь к prereg-confirmed доказательной базе: процедурные исправления "
            "V2 без изменения порогов/окон/классов, новая активация до прогона, независимое "
            "ревью; это требует владельца и отдельной волны.",
        ],
        "owner_decisions_required": [
            "утвердить отказ от активации 0.3 (вариант B) сейчас",
            "санкционировать подготовку 0.4 (вариант C) с требованиями ниже",
            "назначить независимого ревьюера V2 для перевыпуска/0.4",
        ],
        "auto_activation": False,
        "auto_status_change": False,
    }

    requirements_0_4 = [
        "новый файл protocols/pit_preregistration_0_4.json + новый .sha256-пин; 0.3 не редактируется",
        "исправить только процедурные пункты V2: REAL_TIME-only срез как отдельный расчёт на "
        "121 начале (8 рецессионных → INCONCLUSIVE), алиасы reconstructed-excluded/tape-free, "
        "публикация C4 W2 и LOO, проверка пина prereg, пин coverage digest",
        "обновить устаревшее ограничение «no extended-window evaluation has been run» и явно "
        "зафиксировать историю: 0.3 никогда не активировалась; docs/170 — провизорное свидетельство",
        "не менять окна, пороги, классы, классификацию рядов и состав линз (post-hoc запрет)",
        "активация 0.4 — владельцем, до любого нового расширенного прогона; затем независимое V2",
        "новый прогон и новый отчёт со ссылкой на SHA-256 0.4; старые отчёты не перезаписываются",
    ]

    checks = {
        "prereg_pin_ok": context["file_sha"] == context["pin"],
        "prereg_content_sha_ok": context["content_sha"] == prereg["content_sha256"],
        "prereg_status_proposed": prereg_status == "PROPOSED",
        "eval_provisional": report.get("status") == "PROVISIONAL",
        "eval_admission_false": report.get("admission") is False,
        "eval_pins_ok": report.get("pins_ok") is True,
        "eval_slices_complete": report.get("slices_complete") is True,
        "registry_human_signed": signature_signed,
        "registry_agent_may_sign_false": context["registry"]["rules"].get("agent_may_sign") is False,
        "registry_hashes_match": bool(context["object_sha"] and context["packet_sha"]),
        "t1_candidate": context["feed"].get("status") == "CANDIDATE",
        "c6_zero": int(c6.get("n_resolved") or 0) == 0,
        "recommendation_no_auto_activation": recommendation["auto_activation"] is False,
    }
    _require(all(checks.values()), f"decision checks failed: "
                                   f"{[key for key, value in checks.items() if not value]}")
    return {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "decision": "OWNER_DECISION_REQUIRED",
        "auto_activation": False,
        "auto_status_change": False,
        "subject": {
            "preregistration_id": prereg["preregistration_id"],
            "version": prereg["version"],
            "status": prereg_status,
            "file": PREREG,
            "file_sha256": context["file_sha"],
            "content_sha256": context["content_sha"],
            "pin_file": PREREG_PIN,
        },
        "evaluation": {
            "report": EVAL_REPORT,
            "schema": report.get("schema"),
            "status": report.get("status"),
            "admission": report.get("admission"),
            "candidate": "ENSEMBLE_INVERSE_MSE",
            "label": label.get("label"),
            "c4_w1_ext": {"status": c4.get("status"), "relative_loss": c4.get("relative_loss"),
                          "recession_origins": c4.get("recession_origins")},
            "c4_w2": {"status": context["c4_w2"].get("status"),
                      "relative_loss": context["c4_w2"].get("relative_loss")},
            "slices": {"real_time_only": (context["slices"].get("real_time_only") or {}).get(
                           "c4_status"),
                       "real_time_only_relative_loss": (context["slices"].get(
                           "real_time_only") or {}).get("relative_loss")},
            "criteria": context["criteria"],
            "pins_ok": report.get("pins_ok"),
            "slices_complete": report.get("slices_complete"),
        },
        "signature": {
            "id": SIGN_OFF_ID,
            "status": context["signoff"].get("status"),
            "date": context["signoff"].get("date"),
            "decision": signature.get("decision"),
            "scope": signature.get("scope"),
            "object_sha256": context["object_sha"],
            "packet_sha256": context["packet_sha"],
            "note": context["signoff"].get("note"),
        },
        "t1_status": {"candidate": context["feed"].get("candidate"),
                      "status": context["feed"].get("status"),
                      "c6_n_resolved": c6.get("n_resolved"),
                      "c6_min_forecasts": c6.get("min_forecasts"),
                      "c6_min_distinct_target_periods": c6.get("min_distinct_target_periods")},
        "gates": {"G4": "OPEN — prereg 0.3 PROPOSED; подпись docs/160 есть (SIGNED_BY_HUMAN); "
                        "независимого V2 по перевыпуску нет",
                  "G5": "STARTED — Client Pack v2 / MVP products, research-only",
                  "G6": "NOT_STARTED — beta 2.0 blocked by G4/G5 and prereg-confirmed evidence"},
        "status_table": status_table,
        "variants": variants,
        "blockers": blockers,
        "recommendation": recommendation,
        "requirements_for_0_4": requirements_0_4,
        "checks": checks,
        "disclaimer": ("Decision packet only: it does not change any status, does not activate the "
                       "preregistration and is not an admission or investment advice."),
        "sources": {
            "prereg": PREREG, "pin": PREREG_PIN, "evaluation_report": EVAL_REPORT,
            "signoff_registry": REGISTRY, "product_feed": FEED, "shadow": SHADOW,
            "docs": list(DOC_REFS.values()),
        },
    }


# -------------------------------------------------------------------------------------- rendering
def render_markdown(packet: dict) -> str:
    subject = packet["subject"]
    evaluation = packet["evaluation"]
    signature = packet["signature"]
    lines = [
        f"# Решение по preregistration 0.3 — {packet['generated_at']}",
        "",
        f"> Схема: `{packet['schema']}` · решение: **{packet['decision']}** · "
        f"авто-активация: **{packet['auto_activation']}** · авто-смена статусов: "
        f"**{packet['auto_status_change']}**.",
        "",
        "## 1. Предмет решения",
        "",
        f"- Preregistration `{subject['preregistration_id']}` v{subject['version']}, "
        f"статус **{subject['status']}**; файл `{subject['file']}`",
        f"- SHA-256 файла `{subject['file_sha256']}`; content_sha256 `{subject['content_sha256']}`; "
        f"пин `{subject['pin_file']}` — сверены",
        f"- Оценка: `{evaluation['schema']}`, статус **{evaluation['status']}**, "
        f"admission={evaluation['admission']}, pins_ok={evaluation['pins_ok']}, "
        f"slices_complete={evaluation['slices_complete']}",
        f"- Кандидат `{evaluation['candidate']}`, метка **{evaluation['label']}**; "
        f"C4 W1_EXT {evaluation['c4_w1_ext']['status']} "
        f"{evaluation['c4_w1_ext']['relative_loss']} "
        f"({evaluation['c4_w1_ext']['recession_origins']} начал); C4 W2 "
        f"{evaluation['c4_w2']['status']} {evaluation['c4_w2']['relative_loss']}; "
        f"REAL_TIME-only срез {evaluation['slices']['real_time_only']} "
        f"{evaluation['slices']['real_time_only_relative_loss']}",
        f"- Подпись `{signature['id']}`: **{signature['status']}**, decision={signature['decision']}, "
        f"дата {signature['date']}; область — только docs/160; это не допуск и не активация",
        f"- T1: `{packet['t1_status']['candidate']}` **{packet['t1_status']['status']}**; "
        f"C6 {packet['t1_status']['c6_n_resolved']}/"
        f"{packet['t1_status']['c6_min_forecasts']} (различных периодов "
        f"{packet['t1_status']['c6_min_distinct_target_periods']} требуются)",
        "",
        "## 2. Ворота",
        "",
    ]
    for gate, status in packet["gates"].items():
        lines.append(f"- **{gate}**: {status}")
    lines.extend(["", "## 3. Варианты решения", ""])
    for variant in packet["variants"]:
        lines.append(f"### {variant['id']}. {variant['title']}")
        lines.append("")
        lines.append(f"Действие: {variant['action']}")
        lines.append("")
        for gate in ("G4", "G5", "G6"):
            lines.append(f"- **{gate}**: {variant['consequences'][gate]}")
        lines.append(f"- Можно утверждать: " + "; ".join(variant["can_claim"]))
        lines.append(f"- Нельзя утверждать: " + "; ".join(variant["cannot_claim"]))
        lines.append(f"- Блокеры: " + ", ".join(variant["blockers"]))
        lines.append("")
    lines.extend(["## 4. Что изменится в статусах", "",
                  "| Статус | Сейчас | A | B | C |", "|---|---|---|---|---|"])
    for name, row in packet["status_table"].items():
        lines.append(f"| {name} | {row['current']} | {row['A']} | {row['B']} | {row['C']} |")
    lines.extend(["", "## 5. Блокеры", ""])
    for blocker in packet["blockers"]:
        lines.append(f"- **{blocker['severity']} `{blocker['id']}`** — {blocker['detail']}")
    recommendation = packet["recommendation"]
    lines.extend([
        "",
        "## 6. Рекомендация",
        "",
        f"**{recommendation['primary']}** (немедленно), далее **{recommendation['secondary']}**; "
        f"**{recommendation['rejected']}** не рекомендуется.",
        "",
        recommendation["headline"],
        "",
    ])
    lines.extend(f"{index}. {reason}" for index, reason in
                 enumerate(recommendation["rationale"], start=1))
    lines.extend(["", "Решение требуется от владельца:", ""])
    lines.extend(f"- {item}" for item in recommendation["owner_decisions_required"])
    lines.extend(["", "## 7. Требования к prereg 0.4 (если выбран C; 0.4 не создаётся этим пакетом)", ""])
    lines.extend(f"{index}. {item}" for index, item in
                 enumerate(packet["requirements_for_0_4"], start=1))
    lines.extend(["", "## 8. Проверки пакета", ""])
    lines.extend(f"- `{name}` = {value}" for name, value in packet["checks"].items())
    lines.extend(["", f"_{packet['disclaimer']}_", ""])
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------- CLI
def _inside_protocols(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to((Path(root) / "protocols").resolve())
        return True
    except ValueError:
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="G6-prep decision packet по preregistration 0.3 (статусы не меняются)")
    parser.add_argument("--root", default=str(ROOT), help="корень репозитория")
    parser.add_argument("--json", action="store_true", help="вывести JSON вместо Markdown")
    parser.add_argument("--out", default=None, help="записать пакет в файл (не внутрь protocols/)")
    parser.add_argument("--generated-at", default=None,
                        help="ISO 8601 дата-время снимка; по умолчанию текущее UTC")
    args = parser.parse_args(argv)
    root = Path(args.root)
    generated_at = args.generated_at
    if generated_at is not None:
        try:
            datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
        except ValueError:
            print(json.dumps({"ok": False, "error": f"--generated-at: ожидался ISO 8601, "
                                                   f"получено {generated_at!r}"},
                             ensure_ascii=False), file=sys.stderr)
            return 2
    if args.out is not None and _inside_protocols(Path(args.out), root):
        print(json.dumps({"ok": False, "error": "запись внутрь protocols/ запрещена: "
                                               "пакет не меняет статусы"},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    try:
        packet = build_packet(load_context(root), generated_at=generated_at)
    except DecisionError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    markdown = render_markdown(packet)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(markdown, encoding="utf-8", newline="\n")
    if args.json:
        print(json.dumps(packet, ensure_ascii=False, indent=2))
    else:
        print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
