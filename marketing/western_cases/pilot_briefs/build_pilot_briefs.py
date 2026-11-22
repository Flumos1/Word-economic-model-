#!/usr/bin/env python3
"""Build one-page PDFs for the western pilot briefs from their Markdown source.

The Markdown file is the single source of truth: the PDF is rendered from it, so the
one-pager and its PDF cannot drift apart. Rendering goes through DOCX (python-docx) and
LibreOffice, because that gives exact control over A4 geometry and guarantees one page.

The builder refuses to write a PDF that is not exactly one page: it retries with a
slightly smaller type scale, and fails closed if even the smallest scale spills over.

Usage::

    python marketing/western_cases/pilot_briefs/build_pilot_briefs.py
    python marketing/western_cases/pilot_briefs/build_pilot_briefs.py --only shell
    python marketing/western_cases/pilot_briefs/build_pilot_briefs.py --check

``--check`` verifies the committed PDFs without rebuilding them (no LibreOffice needed).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

BRIEF_DIR = Path(__file__).resolve().parent
ROOT = BRIEF_DIR.parents[2]
SOFFICE_CANDIDATES = (
    Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
    Path(r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"),
)
BRIEFS = ("equinor", "shell", "conocophillips")
FONT = "Calibri"
INK = RGBColor(0x14, 0x20, 0x2A)
ACCENT = RGBColor(0x1B, 0x4F, 0x7A)
MUTED = RGBColor(0x55, 0x60, 0x6B)
BORDER = "C6D2DC"

SIZES = {
    "h1": 19.0,
    "meta": 8.8,
    "h2": 11.6,
    "h3": 10.4,
    "body": 10.2,
    "table": 9.2,
    "small": 8.2,
}
SCALES = (1.0, 0.97, 0.94, 0.91, 0.88)
SCALE = 1.0


def sz(key: str) -> float:
    """Point size of a named role under the current fit scale."""
    return round(SIZES[key] * SCALE, 2)


class BriefError(RuntimeError):
    """Raised when a brief cannot be rendered as one page."""


# --------------------------------------------------------------------------- parsing

def parse_markdown(text: str) -> list[dict]:
    """Parse the Markdown subset used by the briefs into a flat block list."""
    blocks: list[dict] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            blocks.append({"kind": "p", "text": " ".join(paragraph)})
            paragraph.clear()

    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].rstrip()
        stripped = line.strip()
        if not stripped:
            flush()
            index += 1
            continue
        if stripped.startswith("# "):
            flush()
            blocks.append({"kind": "h1", "text": stripped[2:].strip()})
            index += 1
            continue
        if stripped.startswith("## "):
            flush()
            blocks.append({"kind": "h2", "text": stripped[3:].strip()})
            index += 1
            continue
        if stripped.startswith("### "):
            flush()
            blocks.append({"kind": "h3", "text": stripped[4:].strip()})
            index += 1
            continue
        if stripped.startswith("|"):
            flush()
            rows = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append([cell.strip() for cell in lines[index].strip().strip("|").split("|")])
                index += 1
            header, rest = rows[0], rows[1:]
            aligns = ["left"] * len(header)
            if rest and all(re.fullmatch(r":?-{3,}:?", cell) for cell in rest[0] if cell):
                aligns = [
                    "right" if cell.endswith(":") and not cell.startswith(":") else "left"
                    for cell in rest[0]
                ]
                rest = rest[1:]
            blocks.append({"kind": "table", "header": header, "rows": rest, "aligns": aligns})
            continue
        if stripped.startswith("- "):
            flush()
            items = []
            while index < len(lines) and lines[index].strip().startswith("- "):
                items.append(lines[index].strip()[2:].strip())
                index += 1
            blocks.append({"kind": "bullets", "items": items})
            continue
        paragraph.append(stripped)
        index += 1
    flush()
    return blocks


def split_inline(text: str) -> list[tuple[str, str, str]]:
    """Split a line into (kind, text, target) pieces: text, bold and link."""
    pieces: list[tuple[str, str, str]] = []
    pattern = re.compile(r"(\*\*(?P<bold>.+?)\*\*)|(\[(?P<label>[^\]]+)\]\((?P<target>[^)]+)\))")
    cursor = 0
    for match in pattern.finditer(text):
        if match.start() > cursor:
            pieces.append(("text", text[cursor:match.start()], ""))
        if match.group("bold"):
            pieces.append(("bold", match.group("bold"), ""))
        else:
            pieces.append(("link", match.group("label"), match.group("target")))
        cursor = match.end()
    if cursor < len(text):
        pieces.append(("text", text[cursor:], ""))
    return pieces


# ---------------------------------------------------------------------------- docx

def style_doc(document: Document) -> None:
    section = document.sections[0]
    section.page_width = Cm(21)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(1.15)
    section.bottom_margin = Cm(1.0)
    section.left_margin = Cm(1.35)
    section.right_margin = Cm(1.35)
    section.start_type = WD_SECTION.NEW_PAGE

    normal = document.styles["Normal"]
    normal.font.name = FONT
    normal.font.size = Pt(sz("body"))
    normal.font.color.rgb = INK
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(3)
    normal.paragraph_format.line_spacing = 1.08


def set_run(run, size: float, *, bold: bool = False, color: RGBColor | None = None) -> None:
    run.font.name = FONT
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = color or INK


def add_hyperlink(paragraph, target: str, label: str, size: float) -> None:
    rel_id = paragraph.part.relate_to(target, RT.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), rel_id)
    run = OxmlElement("w:r")
    props = OxmlElement("w:rPr")
    fonts = OxmlElement("w:rFonts")
    fonts.set(qn("w:ascii"), FONT)
    fonts.set(qn("w:hAnsi"), FONT)
    props.append(fonts)
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "1155CC")
    props.append(color)
    size_el = OxmlElement("w:sz")
    size_el.set(qn("w:val"), str(int(round(size * 2))))
    props.append(size_el)
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    props.append(underline)
    run.append(props)
    text_el = OxmlElement("w:t")
    text_el.set(qn("xml:space"), "preserve")
    text_el.text = label
    run.append(text_el)
    link.append(run)
    paragraph._p.append(link)


def add_inline(paragraph, text: str, size: float, *, bold: bool = False) -> None:
    for kind, piece, target in split_inline(text):
        if kind == "link":
            add_hyperlink(paragraph, target, piece, size)
        else:
            set_run(paragraph.add_run(piece), size, bold=bold or kind == "bold")


def shade(cell, fill: str) -> None:
    props = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    props.append(shd)


def table_borders(table) -> None:
    props = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        element = OxmlElement(f"w:{edge}")
        element.set(qn("w:val"), "single")
        element.set(qn("w:sz"), "4")
        element.set(qn("w:space"), "0")
        element.set(qn("w:color"), BORDER)
        borders.append(element)
    props.append(borders)
    margins = OxmlElement("w:tblCellMar")
    for edge, width in (("top", 22), ("left", 60), ("bottom", 22), ("right", 60)):
        element = OxmlElement(f"w:{edge}")
        element.set(qn("w:w"), str(width))
        element.set(qn("w:type"), "dxa")
        margins.append(element)
    props.append(margins)


def add_table(document: Document, block: dict, widths_cm: list[float]) -> None:
    columns = len(block["header"])
    table = document.add_table(rows=1, cols=columns)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    table_borders(table)
    for index, cell in enumerate(table.rows[0].cells):
        cell.width = Cm(widths_cm[index])
        cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        shade(cell, "EAF0F5")
        paragraph = cell.paragraphs[0]
        paragraph.paragraph_format.space_before = Pt(0)
        paragraph.paragraph_format.space_after = Pt(0)
        if block["aligns"][index] == "right":
            paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        set_run(paragraph.add_run(block["header"][index]), sz("table"), bold=True, color=ACCENT)
    for row in block["rows"]:
        cells = table.add_row().cells
        for index, cell in enumerate(cells):
            cell.width = Cm(widths_cm[index])
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            paragraph = cell.paragraphs[0]
            paragraph.paragraph_format.space_before = Pt(0)
            paragraph.paragraph_format.space_after = Pt(0)
            if block["aligns"][index] == "right":
                paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            add_inline(paragraph, row[index], sz("table"))


def bottom_rule(paragraph, color: str = BORDER, size: str = "6") -> None:
    props = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    rule = OxmlElement("w:bottom")
    rule.set(qn("w:val"), "single")
    rule.set(qn("w:sz"), size)
    rule.set(qn("w:space"), "4")
    rule.set(qn("w:color"), color)
    borders.append(rule)
    props.append(borders)


def add_footer(document: Document) -> None:
    """Repeat the status line at the foot of the page, above the bottom margin."""
    section = document.sections[0]
    section.footer_distance = Cm(0.5)
    paragraph = section.footer.paragraphs[0]
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(0)
    top_rule = OxmlElement("w:pBdr")
    rule = OxmlElement("w:top")
    rule.set(qn("w:val"), "single")
    rule.set(qn("w:sz"), "6")
    rule.set(qn("w:space"), "3")
    rule.set(qn("w:color"), BORDER)
    top_rule.append(rule)
    paragraph._p.get_or_add_pPr().append(top_rule)
    footer_text = ("AMFE · pilot brief · RESEARCH_ONLY · admission=false · "
                   "not_client=true · flumos@gmail.com")
    set_run(paragraph.add_run(footer_text), sz("small"), color=MUTED)


def render_docx(blocks: list[dict], out_path: Path, scale: float = 1.0) -> None:
    global SCALE
    SCALE = scale
    document = Document()
    style_doc(document)

    for block in blocks:
        kind = block["kind"]
        if kind == "h1":
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.space_after = Pt(0)
            set_run(paragraph.add_run(block["text"]), sz("h1"), bold=True, color=ACCENT)
        elif kind == "meta":
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.space_after = Pt(6)
            bottom_rule(paragraph)
            set_run(paragraph.add_run(block["text"]), sz("meta"), color=MUTED)
        elif kind == "h2":
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.space_before = Pt(9)
            paragraph.paragraph_format.space_after = Pt(3)
            paragraph.paragraph_format.keep_with_next = True
            set_run(paragraph.add_run(block["text"]), sz("h2"), bold=True, color=ACCENT)
        elif kind == "h3":
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.space_before = Pt(5)
            paragraph.paragraph_format.space_after = Pt(2)
            paragraph.paragraph_format.keep_with_next = True
            set_run(paragraph.add_run(block["text"]), sz("h3"), bold=True, color=INK)
        elif kind == "p":
            paragraph = document.add_paragraph()
            if block["text"].startswith("**Question.**"):
                paragraph.paragraph_format.space_before = Pt(2)
            add_inline(paragraph, block["text"], sz("body"))
        elif kind == "bullets":
            for item in block["items"]:
                paragraph = document.add_paragraph()
                indent = Cm(0.45)
                paragraph.paragraph_format.left_indent = indent
                paragraph.paragraph_format.first_line_indent = -indent
                paragraph.paragraph_format.space_after = Pt(2.5)
                set_run(paragraph.add_run("•\t"), sz("body"), color=ACCENT)
                add_inline(paragraph, item, sz("body"))
        elif kind == "table":
            if len(block["header"]) == 3 and block["header"][1] == "Time":
                widths = [9.3, 4.4, 4.4]
            else:
                widths = [7.6, 4.2, 6.3]
            add_table(document, block, widths)
        else:  # pragma: no cover - defensive
            raise BriefError(f"unknown block: {kind}")

    add_footer(document)
    document.save(str(out_path))


# ----------------------------------------------------------------------------- pdf

def find_soffice() -> Path:
    for candidate in SOFFICE_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise BriefError("LibreOffice not found in the default locations")


def convert(docx_path: Path, out_dir: Path) -> Path:
    soffice = find_soffice()
    completed = subprocess.run(
        [str(soffice), "--headless", "--nolockcheck", "--norestore", "--convert-to", "pdf",
         "--outdir", str(out_dir), str(docx_path)],
        capture_output=True, text=True, timeout=240,
    )
    produced = out_dir / f"{docx_path.stem}.pdf"
    if not produced.is_file():
        raise BriefError(f"PDF conversion failed: {completed.stdout} {completed.stderr}")
    return produced


def page_count(path: Path) -> int:
    from pypdf import PdfReader

    return len(PdfReader(str(path)).pages)


def build_one(stem: str, *, verbose: bool = True) -> Path:
    source = BRIEF_DIR / f"{stem}.md"
    if not source.is_file():
        raise BriefError(f"missing source: {source}")
    blocks = parse_markdown(source.read_text(encoding="utf-8"))
    # The status line right under the title is the meta line in the Markdown:
    # keep it visually smaller than body text.
    for block in blocks:
        if block["kind"] == "p" and block["text"].startswith("**One page ·"):
            block["kind"] = "meta"
            block["text"] = block["text"].replace("**", "")

    target = BRIEF_DIR / f"{stem}.pdf"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        docx_path = tmp_dir / f"{stem}.docx"
        pdf_path = None
        used = None
        for scale in SCALES:
            render_docx(blocks, docx_path, scale=scale)
            candidate = convert(docx_path, tmp_dir)
            pages = page_count(candidate)
            if pages == 1:
                pdf_path, used = candidate, scale
                break
        if pdf_path is None:
            raise BriefError(f"{stem}: does not fit on one page at any fit scale")
        if target.exists():
            target.unlink()
        # The temp directory may sit on another volume, so copy rather than rename.
        target.write_bytes(pdf_path.read_bytes())
    if verbose:
        print(f"{stem}: 1 page at fit scale {used:.2f} -> {target.relative_to(ROOT)}")
    return target


def check_one(stem: str) -> dict:
    pdf = BRIEF_DIR / f"{stem}.pdf"
    md = BRIEF_DIR / f"{stem}.md"
    if not pdf.is_file() or not md.is_file():
        raise BriefError(f"{stem}: missing .md or .pdf")
    from pypdf import PdfReader

    reader = PdfReader(str(pdf))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    return {
        "stem": stem,
        "pages": len(reader.pages),
        "markdown_bytes": md.stat().st_size,
        "pdf_bytes": pdf.stat().st_size,
        "has_disclaimer": all(token in text for token in
                              ("RESEARCH_ONLY", "admission=false", "flumos@gmail.com")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", choices=BRIEFS, help="build a single brief")
    parser.add_argument("--check", action="store_true", help="verify committed PDFs only")
    parser.add_argument("--json", action="store_true", help="print a JSON summary")
    args = parser.parse_args(argv)

    stems = (args.only,) if args.only else BRIEFS
    try:
        if args.check:
            summary = [check_one(stem) for stem in stems]
            for item in summary:
                print(f"{item['stem']}: {item['pages']} page(s), "
                      f"disclaimer={'yes' if item['has_disclaimer'] else 'NO'}")
                if item["pages"] != 1 or not item["has_disclaimer"]:
                    return 1
        else:
            built = [build_one(stem, verbose=not args.json) for stem in stems]
            summary = [{"stem": path.stem, "pdf_bytes": path.stat().st_size} for path in built]
    except BriefError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
