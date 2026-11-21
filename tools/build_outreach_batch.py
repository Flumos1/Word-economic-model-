#!/usr/bin/env python3
"""Build the 2026-11-21 outreach batch.

Outputs (``outreach/batch_2026-11-21/``):
  * ``pilot_offer_{tatneft,taneco,gazpromneft}.docx`` + ``.pdf`` — three pilot offers based on
    ``outreach/legat/LEGAT_offer_USD`` (same sections, company-specific use cases and limits);
  * ``followup_legat_2026-11-27.eml`` — unsent follow-up draft (``X-Unsent: 1``, dated 27.11.2026);
  * ``manifest.json`` — SHA-256 and byte size for every file in the batch except the manifest itself.

Nothing is sent: the tool only writes files. Run from the repo root::

    python tools/build_outreach_batch.py
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

ROOT = Path(__file__).resolve().parents[1]
BATCH = ROOT / "outreach" / "batch_2026-11-21"
SOFFICE = Path(r"C:\Program Files\LibreOffice\program\soffice.exe")
BATCH_DATE = "2026-11-21"
MANIFEST = BATCH / "manifest.json"

DISCLAIMER = "AMFE · RESEARCH_ONLY · admission=false · не прогноз и не инвестиционная рекомендация"

PRICE_NOTE = (
    "Цены ориентировочные, в USD; налоги и курс — по договору. Стоимость сторонних данных "
    "(Platts/ICE/Consensus) в пилот не входит: если они нужны, оплачиваются отдельно. "
    "Пилот — 50/50, диагностика — 0."
)

COMMON_STEPS = (
    "Диагностика 20–30 минут: выбираем 1–2 решения и данные.",
    "NDA и передача данных (CSV/XLSX, можно обезличить).",
    "Проверка качества данных и фиксация допущений.",
    "Расчёт базы и двух стрессов; промежуточный показ.",
    "Итоговый отчёт и воркшоп; критерии успеха фиксируем до старта.",
)

COMMON_PROMISES = (
    "Точность выше рынка и «магическую» прогнозную силу.",
    "Замену Bloomberg/WoodMac/Platts или покупку данных за вас.",
    "Гарантированную экономию X% — считаем на ваших данных.",
    "Допуск модели, аудиторское или юридическое заключение.",
)

PACKAGE_HEADER = ("Пакет", "Что входит", "Срок", "Цена, USD")
CASE_HEADER = ("Направление", "Вопрос для проверки", "Что на выходе", "Срок", "Цена, USD")


OFFERS = (
    {
        "stem": "pilot_offer_tatneft",
        "company": "ПАО «Татнефть»",
        "tagline": (
            "Пилот по сценарной аналитике для бюджета, закупок и логистики: один проверяемый контур "
            "вместо набора Excel-файлов. Не прогноз и не инвестиционная рекомендация."
        ),
        "intro": (
            "Документ подготовлен для 20-минутной диагностики. В нём нет утверждений о финансовых "
            "показателях компании: всё, что относится к аккаунту, — рабочая гипотеза, которую "
            "проверяем на встрече."
        ),
        "hypotheses": (
            "Гипотеза 1. Бюджет-2027 нужно защищать при Brent в широком диапазоне: нужен ответ «что "
            "будет с выручкой, CAPEX и бюджетом закупок», который выдерживает вопросы совета директоров.",
            "Гипотеза 2. Сценарии живут в нескольких файлах с разными допущениями: цифру трудно "
            "объяснить и повторить через месяц.",
            "Что предлагаем. Один контур «цена → выручка/CAPEX → бюджет закупок → логистика»; у "
            "каждого допущения — источник и хеш, результат воспроизводится.",
        ),
        "cases": (
            ("Бюджет-2027 и Brent",
             "Что будет с выручкой, CAPEX и бюджетом закупок при Brent $120/150/170?",
             "База и стресс, путь по месяцам, диапазон чувствительности, триггеры",
             "2–4 нед.", "7 500–10 000"),
            ("FLAKS: TCO и бюджет закупок",
             "Кто дешевле по TCO в 2–4 категориях и как меняется бюджет закупок?",
             "7 компонент TCO, CAP/FLOOR, рейтинг поставщиков, CFO-отчёт",
             "4 нед.", "7 500–10 000"),
            ("Логистика и геополитика",
             "Где фрахт, обход маршрутов и тарифы бьют сильнее?",
             "Каналы, асимметрия «страна × отрасль», стресс-надбавка",
             "3–4 нед.", "7 000–10 000"),
        ),
        "demo": (
            "Иллюстрация механики (демо-набор, не ваши цифры): закрытие Ормуза на 6 месяцев даёт пик "
            "Brent ≈ $168,9 при диапазоне чувствительности $139,0–268,5; в том же демо бюджет "
            "закупок растёт на 4,35%, логистика — на 17,3%. Это расчёт при явных допущениях, "
            "не прогноз и не вероятность."
        ),
        "packages": (
            ("Диагностика", "1 встреча 20–30 минут, рамка решения, разбор 1–2 вопросов", "1 нед.", "0"),
            ("Пилот", "1–2 решения, база + 2 стресса, Excel/PDF/JSON, воркшоп", "4–6 нед.",
             "7 500–10 000"),
            ("Полный сценарий", "3–5 сценариев: бюджет, закупки, логистика; API/Excel", "8–12 нед.",
             "20 000–30 000"),
            ("Подписка/API", "обновления, 2–4 сценария/квартал, поддержка", "12 мес.",
             "25 000–80 000/год"),
        ),
        "limits": (
            "Модель исследовательская: RESEARCH_ONLY, admission=false. Не для регуляторных, "
            "аудиторских и инвестиционных решений без независимой проверки.",
            "Диапазон — чувствительность при явных допущениях, не вероятность и не доверительный "
            "интервал.",
            "Демо-наборы синтетические: цифры выше показывают механику, а не ваши показатели.",
            "Реальные данные — только по NDA, можно обезличить; сырые ряды не входят в отчёт и не "
            "передаются третьим лицам.",
            "Не заменяем Bloomberg/WoodMac/Platts и не покупаем данные за вас.",
            "Отдельного модуля маржи НПЗ нет: если разговор уйдёт в переработку, экономика собирается "
            "на ваших данных.",
        ),
    },
    {
        "stem": "pilot_offer_taneco",
        "company": "АО «ТАНЕКО»",
        "tagline": (
            "Пилот по экономике переработки и закупкам: сценарии цены сырья и фрахта плюс TCO "
            "поставщиков на ваших данных. Не прогноз и не инвестиционная рекомендация."
        ),
        "intro": (
            "Документ подготовлен для 20-минутной диагностики. В нём нет утверждений о финансовых "
            "показателях компании: всё, что относится к аккаунту, — рабочая гипотеза, которую "
            "проверяем на встрече."
        ),
        "hypotheses": (
            "Гипотеза 1. Экономику переработки при шоке цены сырья и логистики нужно считать от "
            "допущений: вопрос «при каких условиях маржа уходит в минус» требует расчёта, а не мнения.",
            "Гипотеза 2. В закупках «дешёвая цена ≠ дешёвый TCO»: фрахт, пошлины, риск страны, "
            "качество и отсрочка платежа не сведены в одно число.",
            "Что предлагаем. Два разреза: экономика переработки при шоке и бюджет закупок в 2–4 "
            "категориях; каждый вывод — с допущениями и границами.",
        ),
        "cases": (
            ("Экономика переработки при шоке",
             "Что происходит с экономикой переработки при шоке цены сырья и логистики?",
             "Цена сырья/дизеля/керосина, стресс-надбавка за фрахт и качество, порог убытка на ваших данных",
             "3–4 нед.", "7 500–10 000"),
            ("FLAKS: TCO и бюджет закупок",
             "Кто дешевле по TCO и как меняется бюджет закупок?",
             "TCO из 7 компонент, CAP/FLOOR по договорам, рейтинг поставщиков, CFO-отчёт",
             "4 нед.", "7 500–10 000"),
            ("Логистика сырья и отгрузки",
             "Где фрахт, маршруты и ограничения бьют сильнее?",
             "Каналы и направления, асимметрия, стресс-надбавка",
             "3–4 нед.", "7 000–10 000"),
        ),
        "demo": (
            "Иллюстрация механики (демо-набор, не ваши цифры): сценарий «Ормуз на 6 месяцев» даёт "
            "+4,35% к бюджету закупок и +17,3% по логистике; лидер по цене и лидер по TCO в "
            "демо-категории — разные поставщики. Это расчёт при явных допущениях, не прогноз."
        ),
        "packages": (
            ("Диагностика", "1 встреча 20–30 минут, рамка решения, разбор 1–2 вопросов", "1 нед.", "0"),
            ("Пилот", "1–2 решения, база + 2 стресса, Excel/PDF/JSON, воркшоп", "4 нед.",
             "7 500–10 000"),
            ("Полный сценарий", "3–5 сценариев: переработка, закупки, логистика; API/Excel",
             "8–12 нед.", "20 000–30 000"),
            ("Подписка/API", "обновления, 2–4 сценария/квартал, поддержка", "12 мес.",
             "25 000–80 000/год"),
        ),
        "limits": (
            "Отдельного «модуля маржи НПЗ» нет. Экономика переработки собирается на ваших данных из "
            "сценарного движка цены/фрахта и закупочного контура; это рамка расчёта, а не готовая "
            "отраслевая модель.",
            "RESEARCH_ONLY, admission=false: не для регуляторных и аудиторских решений.",
            "TCO считается в одной валюте по договору; курс не пересчитывается автоматически; один "
            "договор на поставщика.",
            "Риск страны и пошлины — демо-таблицы до замены на TARIC/OECD CRP/WGI; источник "
            "фиксируем на пилоте.",
            "Диапазон — чувствительность, не вероятность; демо-наборы синтетические.",
            "Данные — только по NDA, обезличивание до расчёта; сырые ряды не входят в отчёт.",
        ),
    },
    {
        "stem": "pilot_offer_gazpromneft",
        "company": "ПАО «Газпром нефть»",
        "tagline": (
            "Пилот по сценариям как API: паспорт модуля, версия, SHA-256 и JSON-агрегаты — чтобы "
            "результат можно было встроить в контур и защитить на архитектурном комитете. "
            "Не прогноз и не инвестиционная рекомендация."
        ),
        "intro": (
            "Документ подготовлен для 20-минутной диагностики. В нём нет утверждений о финансовых "
            "показателях компании: всё, что относится к аккаунту, — рабочая гипотеза, которую "
            "проверяем на встрече."
        ),
        "hypotheses": (
            "Гипотеза 1. Точечные пилоты моделей заканчиваются отчётом: нет API, паспорта модуля, "
            "версий и воспроизводимости — результат не встроить в контур и не защитить.",
            "Гипотеза 2. Цена, TCO, индексация и CAP/FLOOR живут в разных местах; нужен единый "
            "прозрачный расчёт, который выдерживает проверку.",
            "Что предлагаем. API отдаёт только агрегаты; у каждого ответа — допущения, проверки, "
            "ограничения и digest; возможен on-prem.",
        ),
        "cases": (
            ("Сценарии как API",
             "Как встроить сценарии в наш контур и защитить на архитектурном комитете?",
             "Паспорт модуля, версия, JSON-схема, digest, on-prem; одна интеграция",
             "4–6 нед.", "15 000–25 000"),
            ("FLAKS: TCO и бюджет закупок",
             "Кто дешевле по TCO и как меняется бюджет закупок?",
             "Скоринг с публикуемым вкладом компонент, TCO, индексация, CAP/FLOOR",
             "4 нед.", "7 500–10 000"),
            ("Бюджет, CAPEX и логистика",
             "Что будет с бюджетом и CAPEX при шоке цены и логистики?",
             "База и стресс, путь по месяцам, асимметрия «страна × отрасль»",
             "3–5 нед.", "8 000–12 000"),
        ),
        "demo": (
            "Иллюстрация механики (демо-набор, не ваши цифры): закрытие Ормуза на 6 месяцев даёт пик "
            "Brent ≈ $168,9 при диапазоне $139,0–268,5; API-ответ содержит карточки, путь по месяцам, "
            "допущения, проверки, ограничения и digest — без сырых рядов и имён поставщиков."
        ),
        "packages": (
            ("Диагностика", "1 встреча 20–30 минут, рамка интеграции, разбор 1–2 вопросов", "1 нед.", "0"),
            ("Пилот: паспорт + API",
             "1–2 модуля, паспорт, JSON-схема, digest, on-prem, одна интеграция", "4–6 нед.",
             "15 000–25 000"),
            ("Пилот: сценарии/закупки",
             "1–2 решения, база + 2 стресса, Excel/PDF/JSON, воркшоп", "4–6 нед.", "7 500–10 000"),
            ("Подписка/API", "обновления, 2–4 сценария/квартал, поддержка, квоты", "12 мес.",
             "25 000–80 000/год"),
        ),
        "limits": (
            "API v1-контракт заморожен; SLA и сертификаций нет. Требования ИБ и размещение "
            "согласуются отдельно; пентест и аттестация в пилот не входят.",
            "API отдаёт только агрегаты: карточки, путь по месяцам, допущения, проверки, ограничения, "
            "digest. Сырых рядов и имён поставщиков в ответе нет.",
            "RESEARCH_ONLY, admission=false: не для регуляторных и аудиторских решений.",
            "Диапазон — чувствительность, не вероятность; демо-наборы синтетические.",
            "Данные клиента — по NDA, обезличивание до расчёта; on-prem — по согласованию.",
            "Не заменяем Bloomberg/WoodMac/Platts и не покупаем данные за вас.",
        ),
    },
)


FOLLOWUP_TEXT = """Уважаемый Виктор Иванович,

Короткое напоминание о письме от 20 ноября: там программа исследований и цены в USD.

Если тема не в приоритете — просто скажите, и я не буду больше напоминать.

Предлагаю 20 минут: на одном примере покажу, как считается «цена → бюджет → закупки» и где \
границы применимости. Без презентации и без обязательств.

Удобно на неделе с 30 ноября? Предложите 2–3 слота — подстроюсь. Если вопрос не ваш, подскажите, \
к кому переадресовать.

С уважением,
Flumos
flumos@gmail.com

P.S. Это сценарная аналитика, не прогноз и не инвестиционная рекомендация.
"""


def _style(doc: Document) -> None:
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10)
    normal.paragraph_format.space_after = Pt(4)
    normal.paragraph_format.line_spacing = 1.0
    for section in doc.sections:
        section.page_width = Cm(21.0)
        section.page_height = Cm(29.7)
        section.left_margin = section.right_margin = Cm(2.0)
        section.top_margin = section.bottom_margin = Cm(1.8)


def _table(doc: Document, headers, rows) -> None:
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Light Grid Accent 1"
    for index, header in enumerate(headers):
        table.rows[0].cells[index].text = header
    for row in rows:
        cells = table.add_row().cells
        for index, value in enumerate(row):
            cells[index].text = str(value)
    # Repeat the header row when a table crosses a page and never split a row.
    header_props = table.rows[0]._tr.get_or_add_trPr()
    header_props.append(OxmlElement("w:tblHeader"))
    for row in table.rows:
        row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.font.size = Pt(9)


def build_offer(spec: dict) -> Path:
    """Write one DOCX pilot offer; returns the DOCX path (PDF conversion is separate)."""
    doc = Document()
    _style(doc)
    doc.add_heading(f"AMFE для {spec['company']}", level=0)
    doc.add_paragraph(spec["tagline"])
    doc.add_paragraph(
        f"Версия: {BATCH_DATE} · черновик для обсуждения, не отправлен · "
        "RESEARCH_ONLY · admission=false"
    )

    doc.add_heading("1. Зачем это может быть полезно", level=1)
    doc.add_paragraph(spec["intro"])
    for item in spec["hypotheses"]:
        doc.add_paragraph("• " + item)

    doc.add_heading("2. Сценарии для проверки", level=1)
    _table(doc, CASE_HEADER, spec["cases"])
    doc.add_paragraph(spec["demo"])

    doc.add_heading("3. Пакеты и цены", level=1)
    _table(doc, PACKAGE_HEADER, spec["packages"])
    doc.add_paragraph(PRICE_NOTE)

    doc.add_heading("4. Границы и ограничения", level=1)
    for item in spec["limits"]:
        doc.add_paragraph("• " + item)

    doc.add_heading("5. Как проходит пилот", level=1)
    for item in COMMON_STEPS:
        doc.add_paragraph("• " + item)

    doc.add_heading("6. Что не обещаем", level=1)
    for item in COMMON_PROMISES:
        doc.add_paragraph("• " + item)

    doc.add_heading("7. Контакты", level=1)
    doc.add_paragraph("Flumos · flumos@gmail.com")
    doc.add_paragraph(
        "Демо-пакет и NDA-шаблон — по запросу. Начать можно с бесплатной диагностики 20–30 минут."
    )
    doc.add_paragraph(f"Документ — черновик от {BATCH_DATE}; не отправлен.")

    for paragraph in doc.paragraphs:
        if paragraph.style.name.startswith("Heading"):
            paragraph.paragraph_format.keep_with_next = True

    doc.sections[0].footer.paragraphs[0].text = DISCLAIMER

    BATCH.mkdir(parents=True, exist_ok=True)
    path = BATCH / f"{spec['stem']}.docx"
    doc.save(path)
    return path


def convert_pdf(docx_path: Path) -> Path:
    """Convert one DOCX to PDF with LibreOffice; returns the PDF path."""
    if not SOFFICE.exists():
        raise FileNotFoundError(f"LibreOffice not found: {SOFFICE}")
    done = subprocess.run(
        [str(SOFFICE), "--headless", "--convert-to", "pdf", "--outdir", str(BATCH), str(docx_path)],
        capture_output=True, text=True, timeout=240,
    )
    pdf_path = docx_path.with_suffix(".pdf")
    if done.returncode != 0 or not pdf_path.exists():
        raise RuntimeError(f"PDF conversion failed for {docx_path.name}: {done.stderr or done.stdout}")
    return pdf_path


def build_followup() -> Path:
    """Write the unsent LEGAT follow-up as a standards-compliant .eml draft."""
    message = EmailMessage()
    message["From"] = "Flumos <flumos@gmail.com>"
    message["To"] = "tpolegat@gmail.com"
    message["Subject"] = "Re: ТПО «ЛЕГАТ»: программа исследований и стоимость в USD"
    message["Date"] = format_datetime(
        datetime(2026, 11, 27, 9, 0, tzinfo=timezone(timedelta(hours=2)))
    )
    message["X-Unsent"] = "1"
    message.set_content(FOLLOWUP_TEXT, subtype="plain", charset="utf-8", cte="8bit")
    path = BATCH / "followup_legat_2026-11-27.eml"
    path.write_bytes(message.as_bytes(policy=SMTP))
    return path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest() -> dict:
    """Deterministic manifest of every batch file except the manifest itself."""
    files = []
    for path in sorted(BATCH.iterdir(), key=lambda item: item.name):
        if not path.is_file() or path.name == MANIFEST.name:
            continue
        files.append({"path": path.name, "sha256": sha256(path), "bytes": path.stat().st_size})
    return {
        "schema": "amfe.outreach.batch.v1",
        "batch_id": BATCH.name,
        "created": BATCH_DATE,
        "status": {
            "sent": False,
            "stage": "DRAFT",
            "channel": "none",
            "verified_contacts": 0,
            "reason": "No mailbox/channel and no verified addresses: sending is human-only.",
        },
        "model_status": "RESEARCH_ONLY",
        "admission": False,
        "files": files,
    }


def write_manifest() -> Path:
    MANIFEST.write_text(
        json.dumps(build_manifest(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return MANIFEST


def main() -> int:
    BATCH.mkdir(parents=True, exist_ok=True)
    for spec in OFFERS:
        docx_path = build_offer(spec)
        pdf_path = convert_pdf(docx_path)
        print(f"offer: {docx_path.name} + {pdf_path.name} ({pdf_path.stat().st_size} bytes)")
    eml = build_followup()
    print(f"follow-up: {eml.name} ({eml.stat().st_size} bytes), X-Unsent=1")
    manifest = write_manifest()
    print(f"manifest: {manifest.name}, files={len(build_manifest()['files'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
