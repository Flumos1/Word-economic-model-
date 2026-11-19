#!/usr/bin/env python3
"""Build a one-page DOCX/PDF sales sheet for the oil & gas marketing kit.

Reads ``marketing/one_pager.md`` and writes ``build/marketing/AMFE_oil_gas_one_pager.docx`` and,
when LibreOffice is available, a PDF.  The document is a marketing asset, not a model artefact; it
must keep the RESEARCH_ONLY / admission=false wording.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from pathlib import Path

from docx import Document
from docx.shared import Pt

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "marketing" / "one_pager.md"
DEFAULT_OUT = ROOT / "build" / "marketing"


def _add_runs(paragraph, text: str) -> None:
    for part in re.split(r"(\*\*[^*]+\*\*)", text):
        if part.startswith("**") and part.endswith("**"):
            run = paragraph.add_run(part[2:-2])
            run.bold = True
        else:
            paragraph.add_run(part)


def build_docx(source: Path, out_dir: Path) -> Path:
    lines = source.read_text(encoding="utf-8").splitlines()
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10.5)
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("# "):
            doc.add_heading(line[2:], level=0)
        elif line.startswith("## "):
            doc.add_heading(line[3:], level=2)
        elif line.startswith("- "):
            paragraph = doc.add_paragraph(style="List Bullet")
            _add_runs(paragraph, line[2:])
        elif re.match(r"^\d+\.\s", line):
            paragraph = doc.add_paragraph(style="List Number")
            _add_runs(paragraph, re.sub(r"^\d+\.\s", "", line))
        else:
            paragraph = doc.add_paragraph()
            _add_runs(paragraph, line)
    footer = doc.sections[0].footer.paragraphs[0]
    footer.text = "AMFE · RESEARCH_ONLY · admission=false · не прогноз и не инвестиционная рекомендация"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "AMFE_oil_gas_one_pager.docx"
    doc.save(path)
    return path


def convert_pdf(docx_path: Path) -> Path | None:
    candidates = [shutil.which("soffice"), shutil.which("libreoffice"),
                  r"C:\Program Files\LibreOffice\program\soffice.exe",
                  r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"]
    soffice = next((item for item in candidates if item and Path(item).exists()), None)
    if soffice is None:
        return None
    subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir",
                    str(docx_path.parent), str(docx_path)],
                   check=True, capture_output=True, timeout=180)
    pdf = docx_path.with_suffix(".pdf")
    return pdf if pdf.exists() else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build oil & gas marketing one-pager (DOCX/PDF)")
    parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--no-pdf", action="store_true")
    args = parser.parse_args(argv)
    docx_path = build_docx(Path(args.source), Path(args.out))
    pdf = None if args.no_pdf else convert_pdf(docx_path)
    print({"docx": str(docx_path), "pdf": str(pdf) if pdf else None})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
