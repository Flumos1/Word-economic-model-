#!/usr/bin/env python3
"""Two ready-to-adapt pilot offers generated from the 25-offer catalogue (M1, track D).

Each offer is a one-page hypothesis: fixed scope, fixed USD list price, deliverables, what is
excluded and what is never promised.  Nothing is personalised, addressed or sent: the buyer is an
organization type plus a role, the only contact is the owner's public mailbox.

Usage::

    python tools/pilot_offer_pack.py --check     # validate the selection against the catalogue
    python tools/pilot_offer_pack.py --build     # write MD + DOCX into docs/pilot_offers/
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE = ROOT / "marketing" / "commercial_offers_25" / "offers.json"
OUT_DIR = ROOT / "docs" / "pilot_offers"
CONTACT = "flumos@gmail.com"

#: Two segments an M1 pilot can realistically reach without any licence and without personal data.
SELECTION = (
    {
        "slug": "pilot_utility_procurement",
        "offer_id": "C25-007",
        "segment_ru": "государственная электро- или газовая компания",
        "why_ru": ("Закупка топлива и оборудования — понятный бюджетный контур: у организации есть "
                   "категории, поставщики и сроки, а решение проверяется на её же агрегированных данных."),
        "meeting_ru": "60 минут: показать TCO-матрицу на обезличенном примере и границы применимости.",
    },
    {
        "slug": "pilot_fund_macro_stress",
        "offer_id": "C25-001",
        "segment_ru": "суверенный или пенсионный фонд",
        "why_ru": ("Портфельный стресс на публичных макроданных не требует раскрытия позиций: "
                   "сценарии строятся на агрегатах и допущениях, а не на частных данных."),
        "meeting_ru": "60 минут: разобрать три сценария нефти/газа и увидеть, где заканчивается модель.",
    },
)

NEVER_PROMISED = (
    "допущенную модель, статус ACTIVE или LIMITED_DEPLOYMENT",
    "точность прогноза, вероятность события или превосходство над рынком",
    "гарантированную экономию, ROI или доходность",
    "данные, которые требуют платной лицензии (ICE/NYMEX, IEA, Kpler, Vortexa)",
    "контакты или персональные данные сотрудников организации",
)


def load_offers() -> dict:
    return json.loads(CATALOGUE.read_text(encoding="utf-8"))


def select() -> list[dict]:
    offers = {offer["id"]: offer for offer in load_offers()["offers"]}
    picked = []
    for item in SELECTION:
        offer = offers.get(item["offer_id"])
        if offer is None:
            raise SystemExit(f"unknown offer id {item['offer_id']}")
        picked.append({**item, "offer": offer})
    return picked


def validate() -> list[str]:
    errors: list[str] = []
    catalogue = load_offers()
    if catalogue.get("schema") != "amfe.commercial_offers.v1":
        errors.append("catalogue schema changed")
    if len(SELECTION) != 2:
        errors.append("exactly two pilot offers are expected for M1")
    offers = {offer["id"]: offer for offer in catalogue["offers"]}
    seen: set[str] = set()
    for item in SELECTION:
        offer = offers.get(item["offer_id"])
        if offer is None:
            errors.append(f"{item['offer_id']}: not in the catalogue")
            continue
        if item["offer_id"] in seen:
            errors.append(f"{item['offer_id']}: duplicate selection")
        seen.add(item["offer_id"])
        if offer["evidence_status"] != "HYPOTHESIS" or offer["not_client"] is not True:
            errors.append(f"{item['offer_id']}: offer must stay HYPOTHESIS/not_client")
        if not isinstance(offer["fixed_price_usd"], int) or offer["fixed_price_usd"] <= 0:
            errors.append(f"{item['offer_id']}: fixed price must be a positive integer")
        if not item["slug"].startswith("pilot_"):
            errors.append(f"{item['offer_id']}: slug must start with pilot_")
    return errors


def _price(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _weeks(value: int) -> str:
    """Russian plural for a week count: 1 неделя, 2-4 недели, 5+ недель."""
    if value % 10 == 1 and value % 100 != 11:
        return f"{value} неделя"
    if value % 10 in (2, 3, 4) and value % 100 not in (12, 13, 14):
        return f"{value} недели"
    return f"{value} недель"


#: Owner rule 2026-11-26: paid licences are not purchased at all, so no pilot can promise them.
LICENCE_RULE = ("Платные лицензии не приобретаются (правило владельца 2026-11-26): работаем только "
                "на открытых данных S1–S3 и данных организации.")


def render_markdown(item: dict) -> str:
    offer = item["offer"]
    lines = [
        f"# Пилотное предложение: {offer['title_ru']}",
        "",
        "**Статус: HYPOTHESIS · не клиент · не отправлено · RESEARCH_ONLY · admission=false**",
        "",
        f"**Сегмент:** {item['segment_ru']}. **Покупательский центр:** {offer['buyer_role']} "
        f"(роль, без имён и контактов).",
        "",
        "## Зачем это может быть полезно",
        "",
        item["why_ru"],
        "",
        "## Что входит в пилот",
        "",
        f"- Вопрос, на который отвечаем: {offer['question_ru']}",
        f"- Результат: {offer['deliverables_ru']}",
        f"- Модули: {', '.join(offer['modules'])}",
        f"- Срок: {_weeks(offer['duration_weeks'])}",
        f"- **Фиксированная цена: {_price(offer['fixed_price_usd'])} USD** до налогов, за указанный объём; "
        "изменение объёма — отдельная смета",
        "",
        "## Допущения",
        "",
    ]
    lines += [f"- {item}" for item in offer["assumptions_ru"]]
    lines.append(f"- {LICENCE_RULE}")
    lines += ["", "## Не входит", ""]
    lines += [f"- {item}" for item in offer["exclusions_ru"]]
    lines += [
        "",
        "## Чего мы не обещаем",
        "",
    ]
    lines += [f"- {item}" for item in NEVER_PROMISED]
    lines += [
        "",
        "## Следующий шаг",
        "",
        item["meeting_ru"],
        f"Контакт: {CONTACT} (единственный канал; письма вручную, без рассылок).",
        "",
        "> Все числа — сценарная арифметика на объявленных допущениях, не прогноз и не инвестиционная",
        "> рекомендация. Модель не допущена: `admission=false`, C6 = 0/12 прогнозов (1/4 периода).",
        "",
    ]
    return "\n".join(lines)


def _normalise_docx(path: Path, moment: str = "2026-11-26T00:00:00Z") -> None:
    """Pin DOCX zip timestamps and core.xml dates so two rebuilds are byte-identical."""
    import re
    import zipfile
    from datetime import datetime

    stamp = datetime.strptime(moment, "%Y-%m-%dT%H:%M:%SZ")
    date_time = stamp.timetuple()[:6]
    stamp_bytes = moment.encode("utf-8")
    with zipfile.ZipFile(path, "r") as source:
        entries = [(info, source.read(info.filename)) for info in source.infolist()]
    temporary = path.with_name(path.name + ".tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as target:
        for info, data in entries:
            if info.filename == "docProps/core.xml":
                data = re.sub(
                    rb"(<dcterms:(?:created|modified)[^>]*>)[^<]*(</dcterms:(?:created|modified)>)",
                    rb"\g<1>" + stamp_bytes + rb"\g<2>", data)
            new_info = zipfile.ZipInfo(info.filename, date_time=date_time)
            new_info.compress_type = zipfile.ZIP_DEFLATED
            new_info.external_attr = info.external_attr
            target.writestr(new_info, data)
    temporary.replace(path)


def build_docx(item: dict, path: Path) -> None:
    import docx
    from docx.shared import Pt

    offer = item["offer"]
    document = docx.Document()
    document.add_heading(f"Пилотное предложение: {offer['title_ru']}", level=1)
    note = document.add_paragraph(
        "Статус: HYPOTHESIS · не клиент · не отправлено · RESEARCH_ONLY · admission=false")
    note.runs[0].italic = True
    document.add_paragraph(
        f"Сегмент: {item['segment_ru']}. Покупательский центр: {offer['buyer_role']} "
        f"(роль, без имён и контактов).")
    document.add_heading("Зачем это может быть полезно", level=2)
    document.add_paragraph(item["why_ru"])
    document.add_heading("Что входит в пилот", level=2)
    document.add_paragraph(f"Вопрос: {offer['question_ru']}")
    document.add_paragraph(f"Результат: {offer['deliverables_ru']}")
    document.add_paragraph(f"Модули: {', '.join(offer['modules'])}")
    document.add_paragraph(f"Срок: {_weeks(offer['duration_weeks'])}")
    price = document.add_paragraph(
        f"Фиксированная цена: {_price(offer['fixed_price_usd'])} USD до налогов, "
        "за указанный объём; изменение объёма — отдельная смета")
    price.runs[0].bold = True
    document.add_heading("Допущения", level=2)
    for line in offer["assumptions_ru"]:
        document.add_paragraph(line, style="List Bullet")
    document.add_paragraph(LICENCE_RULE, style="List Bullet")
    document.add_heading("Не входит", level=2)
    for line in offer["exclusions_ru"]:
        document.add_paragraph(line, style="List Bullet")
    document.add_heading("Чего мы не обещаем", level=2)
    for line in NEVER_PROMISED:
        document.add_paragraph(line, style="List Bullet")
    document.add_heading("Следующий шаг", level=2)
    document.add_paragraph(item["meeting_ru"])
    contact = document.add_paragraph(f"Контакт: {CONTACT} (единственный канал; письма вручную, без рассылок).")
    contact.runs[0].font.size = Pt(10)
    disclaimer = document.add_paragraph(
        "Все числа — сценарная арифметика на объявленных допущениях, не прогноз и не инвестиционная "
        "рекомендация. Модель не допущена: admission=false, C6 = 0/12 прогнозов (1/4 периода).")
    disclaimer.runs[0].italic = True
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(path))
    _normalise_docx(path)


def build() -> list[Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for item in select():
        markdown = OUT_DIR / f"{item['slug']}.md"
        markdown.write_text(render_markdown(item), encoding="utf-8", newline="\n")
        written.append(markdown)
        docx_path = OUT_DIR / f"{item['slug']}.docx"
        build_docx(item, docx_path)
        written.append(docx_path)
    index = OUT_DIR / "README.md"
    rows = ["# Пилотные предложения M1 (2 шт.)", "",
            "Черновики, сгенерированные из каталога 25 офферов. **Не отправлены.**", "",
            "| Файл | Сегмент | Оффер | Цена, USD |", "|---|---|---|---:|"]
    for item in select():
        offer = item["offer"]
        rows.append(f"| `{item['slug']}` | {item['segment_ru']} | {offer['title_ru']} | "
                    f"{_price(offer['fixed_price_usd'])} |")
    rows += ["", "Пересобрать: `python tools/pilot_offer_pack.py --build`. "
                 "Проверить: `python tools/pilot_offer_pack.py --check`.", ""]
    index.write_text("\n".join(rows), encoding="utf-8", newline="\n")
    written.append(index)
    return written


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args(argv)
    errors = validate()
    if errors:
        print("FAIL: " + "; ".join(errors), file=sys.stderr)
        return 2
    if args.check:
        print(json.dumps({"status": "OK", "offers": len(SELECTION),
                          "ids": [item["offer_id"] for item in SELECTION]},
                         ensure_ascii=False, sort_keys=True))
        return 0
    if args.build:
        written = build()
        print(f"OK: {len(written)} files in {OUT_DIR.relative_to(ROOT)}")
        return 0
    print(json.dumps({"usage": "python tools/pilot_offer_pack.py --check|--build"},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
