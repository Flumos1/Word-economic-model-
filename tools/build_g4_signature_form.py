#!/usr/bin/env python3
"""Build the printable G4 human-signature form (DOCX + PDF) from the pinned packet.

This is a convenience copy for a human to read, sign and scan.  It does NOT replace
docs/185 (the pinned packet) and it does not sign anything: only a human can sign.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from docx import Document
from docx.shared import Pt

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "signoff"
OBJECT = ROOT / "docs" / "160_Внешняя_проверка_G2.md"
PACKET = ROOT / "docs" / "185_Подпись_docs_160.md"
REGISTRY = ROOT / "protocols" / "signoff_registry.json"


def lf_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def build() -> Path:
    entry = next(item for item in json.loads(REGISTRY.read_text(encoding="utf-8"))["signoffs"]
                 if item["id"] == "G4-SIGN-160")
    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(10.5)
    doc.add_heading("Форма подписи G4 — внешняя проверка G2", level=0)
    doc.add_paragraph("Отдельный экземпляр для человека. Не заменяет docs/185 и не является "
                      "допуском модели (admission=false).")

    doc.add_heading("1. Что подписывается", level=1)
    rows = [
        ("Объект", "docs/160_Внешняя_проверка_G2.md"),
        ("Версия / дата", f"{entry['object']['version']} / {entry['object']['document_date']}"),
        ("SHA-256 объекта", lf_sha(OBJECT)),
        ("Пакет", "docs/185_Подпись_docs_160.md"),
        ("SHA-256 пакета", lf_sha(PACKET)),
        ("Автоматическая проверка", "PASS: 9/9 проверок, 0 findings, 5/5 CONFIRMED"),
        ("Запись реестра", "protocols/signoff_registry.json → G4-SIGN-160"),
    ]
    table = doc.add_table(rows=0, cols=2)
    table.style = "Light Grid Accent 1"
    for key, value in rows:
        cells = table.add_row().cells
        cells[0].text = key
        cells[1].text = value

    doc.add_heading("2. Чек-лист человека-подписанта", level=1)
    for item in (
        "Прочитал docs/160 целиком, особенно §1, §4 (границы), §5 (тесты), §7 (ограничения).",
        "Проверил build/external_review_g2/review.json: status=PASS, checks=9, findings=0.",
        "Сверил SHA docs/160 и docs/185 с реестром; расхождений нет.",
        "Понимаю: подпись относится только к docs/160 и фиксированному прогону, не к модели.",
        "Понимаю: подпись не даёт admission, CANDIDATE/SHADOW_TEST, не подтверждает docs/170/V2.",
        "Понимаю: после подписи нельзя редактировать docs/160/185 без новой версии и перепина.",
    ):
        doc.add_paragraph("☐ " + item)

    doc.add_heading("3. Решение", level=1)
    for label in ("APPROVE", "APPROVE_WITH_NOTES", "REJECT"):
        doc.add_paragraph("☐ " + label)
    doc.add_paragraph("Замечания / принятые оговорки:")
    doc.add_paragraph("_" * 78)
    doc.add_paragraph("_" * 78)

    doc.add_heading("4. Данные подписанта (заполняет человек)", level=1)
    for label in ("Фамилия, имя, отчество:", "Роль / должность:", "Организация:",
                  "Дата (ISO 8601, YYYY-MM-DD):", "Подпись:"):
        doc.add_paragraph(label + " " + "_" * 58)
    doc.add_paragraph("Тип подписанта: человек (не агент, не LLM).")

    doc.add_heading("5. Что делать после подписи", level=1)
    for item in (
        "Поставить подпись на этом экземпляре (бумага/PDF).",
        "Отсканировать/сохранить как PDF и передать Flumos.",
        "Человек вносит signature/date/decision в protocols/signoff_registry.json, запись G4-SIGN-160;",
        "статус меняется на SIGNED_BY_HUMAN или REJECTED_BY_HUMAN.",
        "Не редактировать docs/160 и docs/185; при изменении пакета — новая версия и перепин.",
    ):
        doc.add_paragraph("• " + item)

    footer = doc.sections[0].footer.paragraphs[0]
    footer.text = ("AMFE · G4 · RESEARCH_ONLY · admission=false · подпись только человеком · "
                   "не заменяет docs/185")
    OUT.mkdir(exist_ok=True)
    docx_path = OUT / "G4_SIGN_160_form.docx"
    doc.save(docx_path)
    pdf_path = docx_path.with_suffix(".pdf")
    soffice = r"C:\Program Files\LibreOffice\program\soffice.exe"
    subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", str(OUT), str(docx_path)],
                   check=True, capture_output=True, timeout=180)
    return pdf_path


if __name__ == "__main__":
    pdf = build()
    print({"pdf": str(pdf), "bytes": pdf.stat().st_size})
