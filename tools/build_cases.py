#!/usr/bin/env python3
"""Build the commercial case pack (``marketing/cases/``).

Sources of truth are the three Markdown cases; the tool renders each one to DOCX
(``build/cases/docx``, git-ignored) and then to PDF (LibreOffice) directly in
``marketing/cases``, and writes ``manifest.json`` with SHA-256 for every deliverable.

Nothing is sent or committed: the tool only writes files. Run from the repo root::

    python tools/build_cases.py
    python tools/build_cases.py --no-pdf      # Markdown + manifest only
    python tools/build_cases.py --check       # verify manifest, write nothing

Rules baked into the pack:
  * every case is a two-page document built from the same Markdown that ships with it;
  * the disclaimer footer is added by the renderer, so it cannot be forgotten in the body;
  * the PDF is not byte-deterministic (LibreOffice metadata), the manifest is regenerated instead.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt, RGBColor

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "marketing" / "cases"
DOCX_DIR = ROOT / "build" / "cases" / "docx"
MANIFEST = CASES / "manifest.json"
SOFFICE = Path(r"C:\Program Files\LibreOffice\program\soffice.exe")

CREATED = "2026-11-21"
SCHEMA = "amfe.marketing.cases.v1"
DISCLAIMER = (
    "AMFE · RESEARCH_ONLY · admission=false · демо на публичных сценариях и синтетических данных · "
    "не прогноз и не инвестиционная рекомендация"
)

CASES_SPEC = (
    {
        "stem": "case_tatneft_budget",
        "footer": (
            "AMFE · RESEARCH_ONLY · admission=false · демо на публичных сценариях, не данные "
            "ПАО «Татнефть» · гипотеза, не факт · не прогноз"
        ),
        "sources": ["docs/186_Ормуз_нефть_США_последствия.md",
                    "docs/190_Клиентский_пакет_Ормуз_США.md",
                    "docs/184_FLAKS_Procurement_MVP.md"],
    },
    {
        "stem": "case_taneco_refining",
        "footer": (
            "AMFE · RESEARCH_ONLY · admission=false · демо на публичных сценариях, не данные "
            "АО «ТАНЕКО» · гипотеза, не факт · модуля маржи НПЗ нет"
        ),
        "sources": ["docs/186_Ормуз_нефть_США_последствия.md",
                    "docs/190_Клиентский_пакет_Ормуз_США.md",
                    "docs/184_FLAKS_Procurement_MVP.md"],
    },
    {
        "stem": "case_flaks_procurement",
        "footer": (
            "AMFE · RESEARCH_ONLY · admission=false · синтетический демо-набор "
            "(source_class=synthetic_demo), не данные клиента · не прогноз"
        ),
        "sources": ["docs/184_FLAKS_Procurement_MVP.md", "docs/189_FLAKS_пилот.md"],
    },
)

PACK_SOURCES = (
    "docs/186_Ормуз_нефть_США_последствия.md",
    "docs/188_Газ_LNG.md",
    "docs/189_FLAKS_пилот.md",
    "docs/190_Клиентский_пакет_Ормуз_США.md",
    "docs/192_Simulator_UI.md",
    "docs/203_Блок_15_институты.md",
    "docs/204_Блок_17_технологии_ИИ.md",
    "docs/208_Коммерческие_кейсы.md",
    "marketing/outreach_5.md",
)

INLINE_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
TABLE_SEP_RE = re.compile(r"^\|[\s:|-]+\|$")
NUMBERED_RE = re.compile(r"^\d+\.\s+")


def _plain(text: str) -> str:
    """Strip inline Markdown emphasis/links so DOCX/PDF keep the same words."""
    text = INLINE_RE.sub(r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return text.strip()


def _style(document: Document) -> None:
    normal = document.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10.5)
    normal.paragraph_format.space_after = Pt(4)
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.line_spacing = 1.15
    for name, size in (("Heading 1", 12.0), ("Heading 2", 10.5), ("Title", 16.0)):
        style = document.styles[name]
        style.font.name = "Calibri"
        style.font.size = Pt(size)
        style.font.color.rgb = RGBColor(0x1F, 0x33, 0x55)
        style.font.bold = True
        style.paragraph_format.space_before = Pt(7 if name != "Title" else 0)
        style.paragraph_format.space_after = Pt(2)
        style.paragraph_format.keep_with_next = True
    for section in document.sections:
        section.page_width = Cm(21.0)
        section.page_height = Cm(29.7)
        section.left_margin = section.right_margin = Cm(1.9)
        section.top_margin = Cm(1.5)
        section.bottom_margin = Cm(1.4)
        section.header_distance = Cm(0.8)
        section.footer_distance = Cm(0.7)


def _table(document: Document, rows: list[list[str]]) -> None:
    """Add a bordered table; the first row is a bold header repeated across pages."""
    from docx.oxml import OxmlElement

    width = max(len(row) for row in rows)
    table = document.add_table(rows=0, cols=width)
    table.style = "Table Grid"
    table.autofit = True
    for position, row in enumerate(rows):
        cells = table.add_row().cells
        for index in range(width):
            cells[index].text = row[index] if index < len(row) else ""
        for cell in cells:
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.space_after = Pt(0)
                for run in paragraph.runs:
                    run.font.size = Pt(8.5)
                    run.font.bold = position == 0
        table.rows[position]._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
    table.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))


def render_markdown(markdown: str, footer: str) -> Document:
    """Render the small Markdown subset used by the case files into a DOCX document."""
    document = Document()
    _style(document)

    lines = markdown.splitlines()
    index = 0
    pending: list[str] = []

    def flush() -> None:
        if pending:
            paragraph = document.add_paragraph(_plain(" ".join(pending)))
            paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            pending.clear()

    while index < len(lines):
        line = lines[index].rstrip()
        stripped = line.strip()

        if not stripped:
            flush()
            index += 1
            continue

        if stripped.startswith("|") and index + 1 < len(lines) and TABLE_SEP_RE.match(
            lines[index + 1].strip()
        ):
            flush()
            rows: list[list[str]] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                candidate = lines[index].strip()
                if not TABLE_SEP_RE.match(candidate):
                    cells = [cell.strip() for cell in candidate.strip("|").split("|")]
                    rows.append([_plain(cell) for cell in cells])
                index += 1
            _table(document, rows)
            document.add_paragraph().paragraph_format.space_after = Pt(0)
            continue

        if stripped.startswith("# "):
            flush()
            document.add_heading(_plain(stripped[2:]), level=0)
            index += 1
            continue
        if stripped.startswith("## "):
            flush()
            document.add_heading(_plain(stripped[3:]), level=1)
            index += 1
            continue
        if stripped.startswith("### "):
            flush()
            document.add_heading(_plain(stripped[4:]), level=2)
            index += 1
            continue
        if stripped in {"---", "***"}:
            flush()
            index += 1
            continue
        if stripped.startswith(("- ", "* ")):
            flush()
            document.add_paragraph(_plain(stripped[2:]), style="List Bullet")
            index += 1
            continue
        if stripped.startswith("> "):
            flush()
            paragraph = document.add_paragraph(_plain(stripped[2:]))
            paragraph.paragraph_format.left_indent = Cm(0.4)
            for run in paragraph.runs:
                run.italic = True
            index += 1
            continue
        if NUMBERED_RE.match(stripped):
            flush()
            paragraph = document.add_paragraph(_plain(stripped))
            paragraph.paragraph_format.left_indent = Cm(0.55)
            paragraph.paragraph_format.first_line_indent = Cm(-0.55)
            index += 1
            continue

        pending.append(stripped)
        index += 1

    flush()

    footer_paragraph = document.sections[0].footer.paragraphs[0]
    footer_paragraph.text = footer
    footer_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for run in footer_paragraph.runs:
        run.font.size = Pt(7)
        run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)

    header = document.sections[0].header.paragraphs[0]
    header.text = "AMFE · коммерческий кейс · публичная версия · не для распространения без дисклеймера"
    header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    for run in header.runs:
        run.font.size = Pt(7)
        run.font.color.rgb = RGBColor(0x77, 0x77, 0x77)
    return document


def build_docx(spec: dict) -> Path:
    source = CASES / f"{spec['stem']}.md"
    markdown = source.read_text(encoding="utf-8")
    if DISCLAIMER.split(" · ")[1] not in markdown:  # RESEARCH_ONLY must be in the body too
        raise SystemExit(f"{source.name}: body must state RESEARCH_ONLY")
    DOCX_DIR.mkdir(parents=True, exist_ok=True)
    document = render_markdown(markdown, spec["footer"])
    path = DOCX_DIR / f"{spec['stem']}.docx"
    document.save(path)
    return path


def convert_pdf(docx_path: Path) -> Path:
    """Convert one DOCX to PDF, always rebuilding so the PDF cannot lag behind the Markdown."""
    if not SOFFICE.exists():
        raise FileNotFoundError(f"LibreOffice not found: {SOFFICE}")
    pdf_path = CASES / f"{docx_path.stem}.pdf"
    source = CASES / f"{docx_path.stem}.md"
    if pdf_path.exists():
        pdf_path.unlink()
    last_error = ""
    for attempt in range(3):
        try:
            done = subprocess.run(
                [
                    str(SOFFICE),
                    "--headless",
                    "--nolockcheck",
                    "--norestore",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(CASES),
                    str(docx_path),
                ],
                capture_output=True,
                text=True,
                timeout=180,
            )
            last_error = (done.stderr or done.stdout or "").strip() or f"exit {done.returncode}"
        except subprocess.TimeoutExpired:
            last_error = "timeout"
        # LibreOffice may hand the job to an already running instance and return immediately,
        # so wait for the file instead of trusting the exit code alone.
        deadline = time.time() + 120
        while time.time() < deadline:
            if pdf_path.exists() and pdf_path.stat().st_size > 0:
                if pdf_path.stat().st_mtime + 2 < source.stat().st_mtime:
                    raise RuntimeError(f"PDF older than its Markdown: {pdf_path.name}")
                return pdf_path
            time.sleep(2)
        time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"PDF conversion failed for {docx_path.name}: {last_error}")


def page_count(path: Path) -> int | None:
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - optional dependency
        return None
    return len(PdfReader(str(path)).pages)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def deliverable_paths() -> list[Path]:
    paths = []
    for spec in CASES_SPEC:
        paths.append(CASES / f"{spec['stem']}.md")
        paths.append(CASES / f"{spec['stem']}.pdf")
    return sorted(paths, key=lambda item: item.name)


def build_manifest() -> dict:
    files = []
    for path in deliverable_paths():
        if not path.is_file():
            raise FileNotFoundError(f"missing deliverable: {path}")
        entry = {
            "path": path.name,
            "kind": "pdf" if path.suffix == ".pdf" else "markdown",
            "sha256": sha256(path),
            "bytes": path.stat().st_size,
        }
        if path.suffix == ".pdf":
            pages = page_count(path)
            if pages is not None:
                entry["pages"] = pages
        files.append(entry)
    return {
        "schema": SCHEMA,
        "created": CREATED,
        "status": {
            "stage": "DRAFT",
            "sent": False,
            "channel": "none",
            "client_specific": False,
            "data_basis": "public_scenarios_and_synthetic_demo",
        },
        "model_status": "RESEARCH_ONLY",
        "admission": False,
        "causal_claim": False,
        "cases": [
            {
                "stem": spec["stem"],
                "markdown": f"{spec['stem']}.md",
                "pdf": f"{spec['stem']}.pdf",
                "sources": list(spec["sources"]),
            }
            for spec in CASES_SPEC
        ],
        "sources": list(PACK_SOURCES),
        "rules": {
            "demo_never_presented_as_client_data": True,
            "no_refining_margin_numbers_without_client_data": True,
            "no_raw_rows": True,
            "pdf_deterministic": False,
            "manifest_excludes_itself": True,
        },
        "files": files,
    }


def write_manifest() -> Path:
    CASES.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(
        json.dumps(build_manifest(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return MANIFEST


def check() -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    ok = True
    listed = {entry["path"] for entry in manifest["files"]}
    on_disk = {path.name for path in deliverable_paths()}
    for name in sorted(listed ^ on_disk):
        print(f"mismatch in file list: {name}")
        ok = False
    for entry in manifest["files"]:
        path = CASES / entry["path"]
        if not path.is_file():
            print(f"missing: {entry['path']}")
            ok = False
            continue
        digest = sha256(path)
        if digest != entry["sha256"]:
            print(f"sha256 mismatch: {entry['path']}")
            ok = False
        if path.stat().st_size != entry["bytes"]:
            print(f"size mismatch: {entry['path']}")
            ok = False
        if path.suffix == ".pdf":
            pages = page_count(path)
            if pages is not None and pages != entry.get("pages"):
                print(f"page count mismatch: {entry['path']} ({pages} != {entry.get('pages')})")
                ok = False
    print("OK" if ok else "FAIL")
    return 0 if ok else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the AMFE commercial case pack")
    parser.add_argument("--no-pdf", action="store_true", help="skip LibreOffice conversion")
    parser.add_argument("--check", action="store_true", help="verify manifest only, write nothing")
    args = parser.parse_args(argv)

    if args.check:
        return check()

    CASES.mkdir(parents=True, exist_ok=True)
    for spec in CASES_SPEC:
        docx_path = build_docx(spec)
        if args.no_pdf:
            print(f"case: {spec['stem']}.md -> {docx_path.name} (no pdf)")
        else:
            pdf_path = convert_pdf(docx_path)
            print(f"case: {spec['stem']}.md -> {pdf_path.name} ({page_count(pdf_path)} pages)")
    manifest = write_manifest()
    print(f"manifest: {manifest.name}, files={len(build_manifest()['files'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
