#!/usr/bin/env python3
"""FLAKS pilot intake: real client CSV/XLSX -> anonymized scoring / TCO / budget / CFO report.

Task "FLAKS pilot" (follow-up of task-66, docs/184; plan docs/189). The tool is an **intake and
packaging layer** around the already validated procurement MVP:

* reads two client tables -- suppliers and contracts -- in CSV or XLSX according to the templates
  in ``pilot/flaks/``;
* validates the schema (declared columns only), required fields, types and ranges, joins the
  contract table to the supplier table and refuses orphan/duplicated/missing records;
* builds a data-quality report (missing cells, defaults, duplicated rows/ids, robust outliers,
  units and conversions, cross-field plausibility warnings);
* anonymizes supplier ids, names and contract numbers to stable pseudonyms **before** the model
  runs, so the scoring / TCO / budget artefacts never contain a client identifier;
* runs the existing engine (``tools/flaks_procurement.py``) unchanged: the same 5-criterion score,
  7-component TCO, 5 macro scenarios and contract indexation with cap/floor;
* writes ``flaks_pilot.json``, ``flaks_pilot.md``, ``flaks_pilot_cfo.docx`` (CFO report) and
  ``flaks_pilot_manifest.json`` with SHA-256 of every artefact.

Data policy: real client data is never committed and never copied into the artefacts. Only the
anonymized aggregate report is written; the source files stay where the client put them, and the
manifest records their SHA-256 (not their names). The tool works offline.

Fail-closed: any missing/unknown column, empty required cell, non-numeric value, out-of-range
value, duplicated id, orphan contract, incompatible unit or unknown scenario stops the run
**before** the output directory is created (CLI exit code 2, JSON error on stderr).

Determinism: fixed ordering (pseudonyms sorted by source id), sorted keys, fixed rounding, and
DOCX zip timestamps pinned to ``--generated-at``; two runs on the same inputs are byte-identical.

Research-only: every artefact carries ``status = RESEARCH_ONLY`` and ``admission = false``.

Usage
-----
    python tools/flaks_pilot.py --suppliers client_suppliers.csv \\
        --contracts client_contracts.csv --scenario base --out build/flaks_pilot --anonymize
    python tools/flaks_pilot.py --suppliers ... --contracts ... --check --json
    python tools/flaks_pilot.py --suppliers ... --contracts ... --json --generated-at 2026-11-19T00:00:00Z
    python -m unittest tests.test_flaks_pilot -v
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import flaks_procurement as fp  # noqa: E402

SCHEMA = "amfe.flaks.pilot.v1"
INTAKE_SCHEMA = "amfe.flaks.pilot_intake.v1"
MANIFEST_SCHEMA = "amfe.flaks.pilot.manifest.v1"
STATUS = fp.STATUS
ADMISSION = False
TOOL_VERSION = "1.0.0"
DEFAULT_OUT = ROOT / "build" / "flaks_pilot"
DEFAULT_SCENARIO = fp.DEFAULT_SCENARIO

SUPPLIER_COLUMNS = (
    "supplier_id", "name", "country", "category", "currency", "unit", "annual_volume",
    "unit_price", "freight_per_unit", "lead_time_days", "otif", "defect_ppm", "audit_score",
    "risk_financial", "risk_supply", "risk_compliance",
)
CONTRACT_COLUMNS = (
    "contract_id", "supplier_id", "status", "cpi_share", "ppi_share", "fx_share", "cap_pct",
    "floor_zero", "payment_days",
)
SUPPLIER_REQUIRED = (
    "supplier_id", "country", "category", "annual_volume", "unit_price", "lead_time_days",
    "otif", "defect_ppm", "audit_score", "risk_financial", "risk_supply", "risk_compliance",
)
CONTRACT_REQUIRED = CONTRACT_COLUMNS

MAX_ROWS = 5000
MAX_COLUMNS = 64
MAX_ISSUES = 80
OUTLIER_MIN_OBSERVATIONS = 4
OUTLIER_IQR_MULTIPLIER = 3.0
QUALITY_FIELDS = ("unit_price", "annual_volume", "freight_per_unit", "lead_time_days")

PILOT_STATEMENT = (
    "Пилот закупочной аналитики FLAKS (UC-13/14/16) на анонимизированных данных клиента: "
    "скоринг поставщиков по объявленным весам, TCO, прогноз бюджета и индексации контрактов по "
    "объявленным сценариям. RESEARCH_ONLY: не допуск, не прогноз, не закупочная и не "
    "инвестиционная рекомендация; таблицы риска/пошлин и веса — объявленные допущения."
)

PILOT_LIMITATIONS = (
    "данные клиента анонимизированы (id поставщиков, имена и номера договоров заменены "
    "псевдонимами); исходные файлы в артефакты не копируются, в манифесте только SHA-256;",
    "качество входных данных ограничено тем, что прислал клиент: отчёт качества показывает "
    "пропуски, значения по умолчанию, дубликаты, выбросы и единицы, но не заменяет сверку с "
    "первичными счетами и договорами;",
    "веса скоринга — управленческое допущение; при их смене меняется порядок поставщиков "
    "(веса публикуются и проверяются на сумму 1);",
    "нормализация «best-in-class ratio» относительна внутри категории: смена состава участников "
    "меняет компоненты, а не абсолютное качество поставщика;",
    "TCO — объявленная формула (логистика, пошлина, риск-премия, качество, поздняя поставка, "
    "замороженный капитал), а не бухгалтерская себестоимость и не аудит счетов;",
    "риск страны и пошлины — демонстрационные таблицы-допущения (docs/184 §9): до решения их "
    "нужно заменить TARIC и страновыми индексами (OECD CRP / World Bank WGI);",
    "сценарии линейны: CPI→цена 1 пп = 1 %, нефть→цена линеаризована по агрегату моста цены "
    "(+2,364 % на +100 % Brent); большие шаги выходят за линеаризацию;",
    "интервал бюджета — перенос 5–95 прогноза CPI; неопределённость нефти, фрахта и FX в "
    "интервал не входит;",
    "индексация не учитывает переговоры, взаимозачёт, штрафы, оспаривание и юридическую "
    "проверку формулировок договора; cap/floor применяются как объявлено в контракте;",
    "«экономия» считается как перевод всего объёма категории на поставщика с минимальным TCO и "
    "не учитывает концентрацию риска, мощность, квалификацию и стоимость переключения;",
    "не допуск: admission=false, статус RESEARCH_ONLY; заявлений о точности нет.",
)

PILOT_ACTIONS = (
    "Подтвердить веса скоринга и пороги классов A/B/C с CFO/закупками (веса публикуются).",
    "Проверить строки, попавшие в выбросы и предупреждения качества, по первичным счетам.",
    "Сверить cap/floor и доли cpi/ppi/fx по каждому договору с текстом договора.",
    "Заменить таблицы пошлин и риска страны на TARIC / OECD CRP / WGI до управленческого решения.",
    "Подтвердить, что анонимизации достаточно для передачи отчёта внутри контура клиента.",
)

CSV_DELIMITERS = (",", ";", "\t", "|")
CSV_ENCODINGS = ("utf-8-sig", "utf-8", "cp1251")
FORBIDDEN_SUBSTRINGS = fp.FORBIDDEN_SUBSTRINGS

COUNTRY_ALIASES = {}
for _code, _info in fp.COUNTRIES.items():
    COUNTRY_ALIASES[_code.lower()] = _code
    COUNTRY_ALIASES[_info["name"].strip().lower().replace("ё", "е")] = _code
COUNTRY_ALIASES.update({
    "germany": "DE", "poland": "PL", "czechia": "CZ", "czech republic": "CZ",
    "italy": "IT", "romania": "RO", "turkey": "TR", "turkiye": "TR", "china": "CN",
    "vietnam": "VN", "india": "IN", "ukraine": "UA",
})

CATEGORY_ALIASES = {}
for _entry in fp.CATEGORIES:
    CATEGORY_ALIASES[_entry["id"].lower()] = _entry["id"]
    CATEGORY_ALIASES[_entry["name_ru"].strip().lower().replace("ё", "е")] = _entry["id"]
CATEGORY_ALIASES.update({
    "металл": "metal_rolled", "металлопрокат": "metal_rolled", "сталь": "metal_rolled",
    "metal": "metal_rolled", "steel": "metal_rolled",
    "полимер": "polymer_granulate", "полимеры": "polymer_granulate",
    "пластик": "polymer_granulate", "гранулят": "polymer_granulate",
    "polymer": "polymer_granulate", "plastic": "polymer_granulate",
    "плёнка": "packaging_film", "пленка": "packaging_film", "упаковка": "packaging_film",
    "упаковочная плёнка": "packaging_film", "упаковочная пленка": "packaging_film",
    "film": "packaging_film", "packaging": "packaging_film",
    "логистика": "contract_logistics", "фрахт": "contract_logistics",
    "перевозки": "contract_logistics", "транспорт": "contract_logistics",
    "logistics": "contract_logistics", "freight": "contract_logistics",
})

UNIT_ALIASES = {}
for _unit in ("kg", "кг", "kilogram", "kilograms", "килограмм", "килограммы"):
    UNIT_ALIASES[_unit] = ("mass", 1.0)
for _unit in ("t", "т", "ton", "tons", "tonne", "tonnes", "тонна", "тонны", "тонн"):
    UNIT_ALIASES[_unit] = ("mass", 1000.0)
for _unit in ("shipment", "shipments", "отправка", "отправок", "рейс", "рейсов", "trip"):
    UNIT_ALIASES[_unit] = ("shipment", 1.0)

CATEGORY_UNIT_DIMENSION = {
    "кг": "mass",
    "отправка": "shipment",
}

STATUS_ALIASES = {
    "active": "active", "действующий": "active", "действует": "active", "активный": "active",
    "candidate": "candidate", "кандидат": "candidate", "кандидатный": "candidate",
}

BOOL_TRUE = {"true", "1", "yes", "y", "да", "истина", "так", "t"}
BOOL_FALSE = {"false", "0", "no", "n", "нет", "ложь", "ні", "f"}

TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class PilotError(RuntimeError):
    """Any condition that stops the pilot run before a file is written."""


class IntakeError(PilotError):
    """The client tables are missing, broken, inconsistent or violate the declared schema."""

    def __init__(self, message: str, issues: list | None = None):
        super().__init__(message)
        self.issues = list(issues or [])


# ------------------------------------------------------------------------------------ small helpers
def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_bytes(document: dict) -> bytes:
    return (json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n").encode("utf-8")


def _pretty_bytes(document: dict) -> bytes:
    return (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=1)
            + "\n").encode("utf-8")


def _key(value) -> str:
    text = "" if value is None else str(value)
    return re.sub(r"\s+", " ", text.strip().lower().replace("ё", "е"))


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return str(value).strip()


def _r(value, digits: int = 6):
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return value


# ---------------------------------------------------------------------------------------- issues
class Issues:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def add(self, severity: str, table: str, row, column: str, message: str) -> None:
        item = {"severity": severity, "table": table, "row": row, "column": column,
                "message": message}
        bucket = self.errors if severity == "error" else self.warnings
        if len(bucket) < MAX_ISSUES:
            bucket.append(item)

    def error(self, table: str, row, column: str, message: str) -> None:
        self.add("error", table, row, column, message)

    def warning(self, table: str, row, column: str, message: str) -> None:
        self.add("warning", table, row, column, message)

    def raise_if_errors(self, origin: str = "входные таблицы") -> None:
        if not self.errors:
            return
        first = self.errors[:6]
        details = "; ".join(
            f"{item['table']}[строка {item['row']}].{item['column']}: {item['message']}"
            for item in first)
        tail = "" if len(self.errors) <= 6 else f"; ещё {len(self.errors) - 6}"
        raise IntakeError(f"{origin}: {len(self.errors)} ошибок — {details}{tail}", self.errors)


# ------------------------------------------------------------------------------------ table reader
def _normalise_header(value) -> str:
    text = _text(value)
    if text.startswith("\ufeff"):
        text = text[1:]
    text = text.strip().lower().replace("ё", "е")
    text = re.sub(r"[\s\-./\\()\[\]]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text


def _decode(raw: bytes, path: Path) -> tuple:
    for encoding in CSV_ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise IntakeError(f"{path.name}: не удалось определить кодировку (пробовали "
                      f"{', '.join(CSV_ENCODINGS)}); сохраните файл как UTF-8")


def _table_from_matrix(matrix: list, *, role: str, path: Path, meta: dict) -> dict:
    if not matrix:
        raise IntakeError(f"{role}: пустой файл {path.name}")
    header = list(matrix[0])
    data = matrix[1:]
    if not header:
        raise IntakeError(f"{role}: нет строки заголовков")
    if len(header) > MAX_COLUMNS:
        raise IntakeError(f"{role}: {len(header)} колонок, максимум {MAX_COLUMNS}")

    keep = []
    for index, name in enumerate(header):
        has_data = any(index < len(row) and _text(row[index]) != "" for row in data)
        if _text(name) == "" and not has_data:
            continue
        if _text(name) == "":
            raise IntakeError(f"{role}: колонка #{index + 1} без имени, но с данными")
        keep.append(index)
    columns = []
    for index in keep:
        name = _normalise_header(header[index])
        if not name:
            raise IntakeError(f"{role}: колонка #{index + 1} после нормализации пустая")
        columns.append(name)
    duplicates = sorted({name for name in columns if columns.count(name) > 1})
    if duplicates:
        raise IntakeError(f"{role}: повторяющиеся колонки {duplicates}")

    rows, row_numbers = [], []
    for offset, raw_row in enumerate(data, start=2):
        values = {}
        for position, index in enumerate(keep):
            values[columns[position]] = raw_row[index] if index < len(raw_row) else ""
        extra = [value for value in raw_row[len(header):] if _text(value) != ""]
        if extra:
            raise IntakeError(f"{role}[строка {offset}]: {len(extra)} значений без заголовков — "
                              f"проверьте разделитель и число колонок")
        if all(_text(value) == "" for value in values.values()):
            continue
        rows.append(values)
        row_numbers.append(offset)
        if len(rows) > MAX_ROWS:
            raise IntakeError(f"{role}: больше {MAX_ROWS} строк — пилот рассчитан на выборку "
                              f"по категориям, а не на полный журнал закупок")
    if not rows:
        raise IntakeError(f"{role}: нет ни одной заполненной строки")
    return {"role": role, "columns": columns, "rows": rows, "row_numbers": row_numbers,
            "meta": meta}


def _read_csv(path: Path, *, role: str) -> dict:
    raw = Path(path).read_bytes()
    text, encoding = _decode(raw, path)
    expected = set(SUPPLIER_COLUMNS if role == "suppliers" else CONTRACT_COLUMNS)
    best = None
    for delimiter in CSV_DELIMITERS:
        try:
            matrix = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        except csv.Error:
            continue
        if not matrix:
            continue
        header = {_normalise_header(name) for name in matrix[0]}
        score = len(header & expected)
        if best is None or score > best[0]:
            best = (score, delimiter, matrix)
    if best is None:
        raise IntakeError(f"{role}: не удалось разобрать CSV {path.name}")
    _, delimiter, matrix = best
    meta = {"format": "csv", "encoding": encoding, "delimiter": delimiter}
    return _table_from_matrix(matrix, role=role, path=path, meta=meta)


def _read_xlsx(path: Path, *, role: str) -> dict:
    try:
        from openpyxl import load_workbook
    except ImportError as error:  # pragma: no cover - dependency guard
        raise PilotError("для XLSX нужен openpyxl (pip install openpyxl)") from error
    try:
        workbook = load_workbook(Path(path), read_only=True, data_only=True)
    except Exception as error:  # noqa: BLE001 - openpyxl raises many types
        raise IntakeError(f"{role}: не удалось открыть XLSX {path.name}: {error}") from error
    try:
        sheet = workbook.active
        matrix = [list(row) for row in sheet.iter_rows(values_only=True)]
        sheet_name = sheet.title
    finally:
        workbook.close()
    if len(matrix) > MAX_ROWS + 1:
        raise IntakeError(f"{role}: больше {MAX_ROWS} строк")
    meta = {"format": "xlsx", "encoding": "openpyxl", "sheet": sheet_name}
    return _table_from_matrix(matrix, role=role, path=path, meta=meta)


def read_table(path, *, role: str) -> dict:
    source = Path(path)
    if not source.is_file():
        raise IntakeError(f"{role}: файл не найден: {source.name}")
    suffix = source.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        return _read_xlsx(source, role=role)
    if suffix in (".csv", ".tsv", ".txt", ".tmpl"):
        return _read_csv(source, role=role)
    raise IntakeError(f"{role}: формат {suffix or '(без расширения)'} не поддерживается; "
                      f"нужен CSV или XLSX")


def _check_columns(table: dict, declared: tuple, required: tuple) -> None:
    columns = table["columns"]
    missing = [name for name in required if name not in columns]
    unknown = sorted(set(columns) - set(declared))
    problems = []
    if missing:
        problems.append(f"нет обязательных колонок {missing}")
    if unknown:
        problems.append(f"неизвестные колонки {unknown}")
    if problems:
        raise IntakeError(
            f"{table['role']}: " + "; ".join(problems) + f". Ожидались колонки {list(declared)}")


# ----------------------------------------------------------------------------------- value parsing
def _number(value, *, field: str, percent: bool = False, percent_points: bool = False) -> float:
    if value is None or _text(value) == "":
        raise ValueError("пустое значение")
    if isinstance(value, bool):
        raise ValueError("ожидалось число, получено логическое значение")
    if isinstance(value, (int, float)):
        result = float(value)
    else:
        text = _text(value).replace("\u00a0", "").replace(" ", "").replace("'", "")
        factor = 1.0
        if percent or percent_points:
            if text.endswith("%"):
                text = text[:-1].strip()
                factor = 0.01 if percent else 1.0
        if "," in text and "." in text:
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        elif "," in text:
            text = text.replace(",", ".")
        try:
            result = float(text)
        except ValueError as error:
            raise ValueError(f"ожидалось число, получено {value!r}") from error
        result *= factor
    if not math.isfinite(result):
        raise ValueError("не конечное число")
    if abs(result) > 1e18:
        raise ValueError("слишком большое число")
    return result


def _integer(value, *, field: str) -> int:
    result = _number(value, field=field)
    if abs(result - round(result)) > 1e-9:
        raise ValueError("ожидалось целое число")
    return int(round(result))


def _boolean(value, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    text = _key(value)
    if text in BOOL_TRUE:
        return True
    if text in BOOL_FALSE:
        return False
    raise ValueError(f"ожидалось TRUE/FALSE (или да/нет), получено {value!r}")


def _number_or_issue(raw, column, table, row, issues, **kwargs):
    try:
        return _number(raw, field=column, **kwargs)
    except ValueError as error:
        issues.error(table, row, column, str(error))
        return None


def _integer_or_issue(raw, column, table, row, issues):
    try:
        return _integer(raw, field=column)
    except ValueError as error:
        issues.error(table, row, column, str(error))
        return None


def _boolean_or_issue(raw, column, table, row, issues):
    try:
        return _boolean(raw, field=column)
    except ValueError as error:
        issues.error(table, row, column, str(error))
        return None


def _resolve(raw, aliases: dict, *, column: str, table: str, row, issues) -> str:
    text = _key(raw)
    if not text:
        issues.error(table, row, column, "пустое значение")
        return ""
    resolved = aliases.get(text)
    if not resolved:
        issues.error(table, row, column,
                     f"неизвестное значение {raw!r}; поддерживаются: "
                     f"{sorted(set(aliases.values()))}")
        return ""
    return resolved


def _resolve_unit(raw, category: str, *, table: str, row, issues):
    canonical_unit = fp.CATEGORY_BY_ID[category]["unit"]
    dimension = CATEGORY_UNIT_DIMENSION.get(canonical_unit)
    text = _key(raw) or _key(canonical_unit)
    entry = UNIT_ALIASES.get(text)
    if entry is None:
        issues.error(table, row, "unit",
                     f"неизвестная единица {raw!r}; поддерживаются кг, т, отправка")
        return canonical_unit, 1.0, dimension
    unit_dimension, factor = entry
    if unit_dimension != dimension:
        issues.error(table, row, "unit",
                     f"единица {raw!r} несовместима с категорией {category!r} "
                     f"(ожидается {canonical_unit})")
        return canonical_unit, 1.0, dimension
    return canonical_unit, factor, dimension


# ------------------------------------------------------------------------------------- parsing
def parse_suppliers(table: dict, issues: Issues) -> tuple:
    _check_columns(table, SUPPLIER_COLUMNS, SUPPLIER_REQUIRED)
    records, defaults, conversions = [], [], []
    seen = {}
    for raw, row in zip(table["rows"], table["row_numbers"]):
        table_name = "suppliers"
        ok = True

        supplier_id = _text(raw.get("supplier_id"))
        if not supplier_id:
            issues.error(table_name, row, "supplier_id", "пустое значение")
            ok = False

        name = _text(raw.get("name"))
        if not name:
            defaults.append({"table": table_name, "row": row, "column": "name",
                             "value": "(пусто, в отчёте только псевдоним)"})

        country = _resolve(raw.get("country"), COUNTRY_ALIASES, column="country",
                           table=table_name, row=row, issues=issues)
        category = _resolve(raw.get("category"), CATEGORY_ALIASES, column="category",
                            table=table_name, row=row, issues=issues)
        if not country or not category:
            ok = False
            country = country or "PL"
            category = category or "metal_rolled"

        currency = _text(raw.get("currency")).upper() or "EUR"
        if _text(raw.get("currency")) == "":
            defaults.append({"table": table_name, "row": row, "column": "currency",
                             "value": "EUR"})
        if currency not in fp.CURRENCIES:
            issues.error(table_name, row, "currency",
                         f"неизвестная валюта {currency!r}; поддерживаются {list(fp.CURRENCIES)}")
            ok = False

        raw_unit = _text(raw.get("unit"))
        if not raw_unit:
            defaults.append({"table": table_name, "row": row, "column": "unit",
                             "value": fp.CATEGORY_BY_ID[category]["unit"]})
        canonical_unit, factor, _dimension = _resolve_unit(
            raw_unit or fp.CATEGORY_BY_ID[category]["unit"], category,
            table=table_name, row=row, issues=issues)
        if factor != 1.0:
            conversions.append({"table": table_name, "row": row, "from": raw_unit,
                                "to": canonical_unit, "factor": factor})

        volume = _number_or_issue(raw.get("annual_volume"), "annual_volume", table_name, row,
                                  issues)
        price = _number_or_issue(raw.get("unit_price"), "unit_price", table_name, row, issues)
        raw_freight = raw.get("freight_per_unit")
        if _text(raw_freight) == "":
            defaults.append({"table": table_name, "row": row, "column": "freight_per_unit",
                             "value": 0.0})
            freight = 0.0
        else:
            freight = _number_or_issue(raw_freight, "freight_per_unit", table_name, row, issues)
        lead = _number_or_issue(raw.get("lead_time_days"), "lead_time_days", table_name, row,
                                issues)
        otif = _number_or_issue(raw.get("otif"), "otif", table_name, row, issues)
        defect = _number_or_issue(raw.get("defect_ppm"), "defect_ppm", table_name, row, issues)
        audit = _number_or_issue(raw.get("audit_score"), "audit_score", table_name, row, issues)
        risks = {}
        for key in ("financial", "supply", "compliance"):
            column = f"risk_{key}"
            value = _number_or_issue(raw.get(column), column, table_name, row, issues)
            risks[key] = value

        numeric = (volume, price, freight, lead, otif, defect, audit, *risks.values())
        if any(value is None for value in numeric):
            ok = False

        if volume is not None and volume <= 0:
            issues.error(table_name, row, "annual_volume", "должен быть > 0")
            ok = False
        if price is not None and price <= 0:
            issues.error(table_name, row, "unit_price", "должен быть > 0")
            ok = False
        if freight is not None and freight < 0:
            issues.error(table_name, row, "freight_per_unit", "не может быть отрицательным")
            ok = False
        if lead is not None and not 0 < lead <= 365:
            issues.error(table_name, row, "lead_time_days", "должен быть в (0, 365]")
            ok = False
        if otif is not None and not 0 < otif <= 1:
            issues.error(table_name, row, "otif", "должен быть в (0, 1]")
            ok = False
        if defect is not None and not 0 < defect <= 1_000_000:
            issues.error(table_name, row, "defect_ppm", "должен быть в (0, 1e6]")
            ok = False
        if audit is not None and not 0 < audit <= 1:
            issues.error(table_name, row, "audit_score", "должен быть в (0, 1]")
            ok = False
        for key, value in risks.items():
            if value is not None and not 0.0 <= value <= 1.0:
                issues.error(table_name, row, f"risk_{key}", "должен быть в [0, 1]")
                ok = False

        if supplier_id and supplier_id in seen:
            issues.error(table_name, row, "supplier_id",
                         f"повторяющийся supplier_id {supplier_id!r} "
                         f"(первая строка {seen[supplier_id]})")
            ok = False
        elif supplier_id:
            seen[supplier_id] = row

        if not ok:
            continue

        records.append({
            "_source_id": supplier_id,
            "_source_name": name,
            "_row": row,
            "country": country,
            "category": category,
            "currency": currency,
            "unit": canonical_unit,
            "_raw_unit": raw_unit or canonical_unit,
            "annual_volume": float(volume) * factor,
            "unit_price": float(price) / factor,
            "freight_per_unit": float(freight) / factor,
            "lead_time_days": float(lead),
            "otif": float(otif),
            "defect_ppm": float(defect),
            "audit_score": float(audit),
            "risk": {key: float(value) for key, value in risks.items()},
        })
    return records, defaults, conversions


def parse_contracts(table: dict, suppliers: list, issues: Issues) -> tuple:
    _check_columns(table, CONTRACT_COLUMNS, CONTRACT_REQUIRED)
    by_supplier = {record["_source_id"]: record for record in suppliers}
    contracts, defaults = [], []
    seen_ids = {}
    for raw, row in zip(table["rows"], table["row_numbers"]):
        table_name = "contracts"
        ok = True
        contract_id = _text(raw.get("contract_id"))
        if not contract_id:
            issues.error(table_name, row, "contract_id", "пустое значение")
            ok = False
        supplier_id = _text(raw.get("supplier_id"))
        if not supplier_id:
            issues.error(table_name, row, "supplier_id", "пустое значение")
            ok = False
        elif supplier_id not in by_supplier:
            issues.error(table_name, row, "supplier_id",
                         f"нет поставщика {supplier_id!r} в таблице suppliers")
            ok = False

        status = STATUS_ALIASES.get(_key(raw.get("status")))
        if not status:
            issues.error(table_name, row, "status",
                         f"ожидалось active/candidate, получено {raw.get('status')!r}")
            ok = False

        shares = {}
        for key in ("cpi_share", "ppi_share", "fx_share"):
            column = f"{key}"
            value = _number_or_issue(raw.get(column), column, table_name, row, issues,
                                     percent=True)
            shares[key] = value
            if value is not None and not 0.0 <= value <= 1.0:
                issues.error(table_name, row, column,
                             "доля должна быть в [0, 1] (или в процентах со знаком %)")
                ok = False
        if all(value is not None for value in shares.values()) \
                and sum(shares.values()) > 1.0 + 1e-9:
            issues.error(table_name, row, "cpi_share",
                         f"сумма долей cpi+ppi+fx > 1 ({sum(shares.values()):.4f})")
            ok = False
        if supplier_id in by_supplier and shares.get("fx_share") is not None:
            currency = by_supplier[supplier_id]["currency"]
            if shares["fx_share"] > 0 and currency == fp.BASE_CURRENCY:
                issues.error(table_name, row, "fx_share",
                             f"fx_share > 0 при базовой валюте {fp.BASE_CURRENCY}; "
                             f"валютная доля допустима только для USD/CNY")
                ok = False

        cap = _number_or_issue(raw.get("cap_pct"), "cap_pct", table_name, row, issues,
                               percent_points=True)
        if cap is not None and not 0.0 <= cap <= 25.0:
            issues.error(table_name, row, "cap_pct", "должен быть в [0, 25] %")
            ok = False

        floor = _boolean_or_issue(raw.get("floor_zero"), "floor_zero", table_name, row, issues)
        payment = _integer_or_issue(raw.get("payment_days"), "payment_days", table_name, row,
                                    issues)
        if payment is not None and not 0 <= payment <= 365:
            issues.error(table_name, row, "payment_days", "должен быть в [0, 365]")
            ok = False

        if contract_id and contract_id in seen_ids:
            issues.error(table_name, row, "contract_id",
                         f"повторяющийся contract_id {contract_id!r} "
                         f"(первая строка {seen_ids[contract_id]})")
            ok = False
        elif contract_id:
            seen_ids[contract_id] = row

        if not ok:
            continue
        contracts.append({
            "_source_id": contract_id,
            "_supplier_id": supplier_id,
            "_row": row,
            "status": status,
            "cpi_share": float(shares["cpi_share"]),
            "ppi_share": float(shares["ppi_share"]),
            "fx_share": float(shares["fx_share"]),
            "cap_pct": float(cap),
            "floor_zero": bool(floor),
            "payment_days": int(payment),
        })
    return contracts, defaults


# ------------------------------------------------------------------------------------ data quality
def _quantile(values: list, fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * fraction
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower]) * (1.0 - weight) + float(ordered[upper]) * weight


def _outliers(suppliers: list) -> list:
    found = []
    by_category = {}
    for record in suppliers:
        by_category.setdefault(record["category"], []).append(record)
    for category, records in sorted(by_category.items()):
        if len(records) < OUTLIER_MIN_OBSERVATIONS:
            continue
        for field in QUALITY_FIELDS:
            values = [float(record[field]) for record in records]
            low = _quantile(values, 0.25)
            high = _quantile(values, 0.75)
            iqr = high - low
            lower_bound = low - OUTLIER_IQR_MULTIPLIER * iqr
            upper_bound = high + OUTLIER_IQR_MULTIPLIER * iqr
            for record, value in zip(records, values):
                if value < lower_bound or value > upper_bound:
                    found.append({
                        "table": "suppliers", "row": record["_row"], "column": field,
                        "category": category, "value": _r(value, 6),
                        "lower": _r(lower_bound, 6), "upper": _r(upper_bound, 6),
                        "method": f"IQR x{OUTLIER_IQR_MULTIPLIER:g} внутри категории",
                    })
    return found


def _quality_block(*, supplier_table: dict, contract_table: dict, suppliers: list,
                   contracts: list, issues: Issues, defaults: list,
                   conversions: list) -> dict:
    missing = {"suppliers": {}, "contracts": {}}
    for column in supplier_table["columns"]:
        count = sum(1 for row in supplier_table["rows"] if _text(row.get(column)) == "")
        missing["suppliers"][column] = count
    for column in contract_table["columns"]:
        count = sum(1 for row in contract_table["rows"] if _text(row.get(column)) == "")
        missing["contracts"][column] = count

    signatures = {}
    for record in suppliers:
        signature = (record["country"], record["category"],
                     _r(record["annual_volume"], 3), _r(record["unit_price"], 6),
                     _r(record["lead_time_days"], 3))
        signatures.setdefault(signature, []).append(record["_row"])
    duplicate_rows = [rows for rows in signatures.values() if len(rows) > 1]

    duplicate_ids = {"supplier_id": [], "contract_id": []}
    for item in issues.errors:
        if item["column"] == "contract_id" and "повтор" in item["message"]:
            duplicate_ids["contract_id"].append([item["row"]])
        if item["column"] == "supplier_id" and "повтор" in item["message"]:
            duplicate_ids["supplier_id"].append([item["row"]])

    raw_units, canonical_units = {}, {}
    for record in suppliers:
        raw = record.get("_raw_unit") or record["unit"]
        raw_units[raw] = raw_units.get(raw, 0) + 1
        canonical_units[record["unit"]] = canonical_units.get(record["unit"], 0) + 1

    warnings = []
    for record in suppliers:
        row = record["_row"]
        if record["unit_price"] > 0 and record["freight_per_unit"] > 0.5 * record["unit_price"]:
            warnings.append({"code": "FREIGHT_HIGH", "table": "suppliers", "row": row,
                             "column": "freight_per_unit",
                             "message": "логистика больше половины цены — проверьте базис поставки"})
        if record["lead_time_days"] > 90:
            warnings.append({"code": "LEAD_LONG", "table": "suppliers", "row": row,
                             "column": "lead_time_days",
                             "message": "лид-тайм больше 90 дней — риск оборотного капитала"})
        if record["otif"] < 0.80:
            warnings.append({"code": "OTIF_LOW", "table": "suppliers", "row": row,
                             "column": "otif", "message": "OTIF ниже 0,80"})
        if record["defect_ppm"] > 5000:
            warnings.append({"code": "DEFECT_HIGH", "table": "suppliers", "row": row,
                             "column": "defect_ppm", "message": "дефектность выше 5 000 ppm"})
        if record["audit_score"] < 0.5:
            warnings.append({"code": "AUDIT_LOW", "table": "suppliers", "row": row,
                             "column": "audit_score", "message": "аудит ниже 0,50"})
        if record["currency"] != fp.BASE_CURRENCY and not any(
                contract["_supplier_id"] == record["_source_id"]
                and contract["fx_share"] > 0 for contract in contracts):
            warnings.append({"code": "FX_NOT_MODELLED", "table": "suppliers", "row": row,
                             "column": "currency",
                             "message": "валюта не EUR, но fx_share=0 — валютный риск не отражён"})

    for contract in contracts:
        row = contract["_row"]
        if contract["cap_pct"] == 0.0:
            warnings.append({"code": "CAP_ZERO", "table": "contracts", "row": row,
                             "column": "cap_pct",
                             "message": "cap=0 — индексация запрещена, проверьте договор"})
        if contract["cpi_share"] + contract["ppi_share"] + contract["fx_share"] == 0.0:
            warnings.append({"code": "NO_INDEXATION", "table": "contracts", "row": row,
                             "column": "cpi_share",
                             "message": "все доли индексации нулевые — договор без индексации"})

    for group in duplicate_rows:
        warnings.append({"code": "DUPLICATE_ROW", "table": "suppliers", "row": group[0],
                         "column": "—",
                         "message": f"одинаковые строки поставщиков: {group}"})

    outliers = _outliers(suppliers)
    status = "OK" if not warnings and not outliers else "WARN"
    return {
        "status": status,
        "rows": {"suppliers": len(suppliers), "contracts": len(contracts)},
        "missing": missing,
        "defaults_applied": defaults,
        "duplicates": {
            "supplier_id": duplicate_ids["supplier_id"],
            "contract_id": duplicate_ids["contract_id"],
            "supplier_rows": duplicate_rows,
        },
        "units": {
            "raw": raw_units,
            "canonical": canonical_units,
            "conversions": conversions,
            "note": "кг и т приводятся к кг (объём ×1000, цена и фрахт ÷1000); отправка — без пересчёта",
        },
        "outliers": outliers,
        "warnings": warnings,
        "errors": 0,
        "method": {
            "missing": "пустые ячейки по каждой колонке (0 = заполнено)",
            "defaults": "пустые необязательные поля заполнены объявленными значениями",
            "duplicates": "повтор supplier_id/contract_id блокируется; одинаковые строки — предупреждение",
            "outliers": (f"IQR ×{OUTLIER_IQR_MULTIPLIER:g} внутри категории, только при "
                         f"n ≥ {OUTLIER_MIN_OBSERVATIONS}; выброс — предупреждение, не ошибка"),
            "units": "единицы проверяются на совместимость с категорией и приводятся к канону",
        },
    }


# ------------------------------------------------------------------------------------ anonymization
def _pseudonym(prefix: str, number: int, sensitive: set) -> str:
    candidate = f"{prefix}{number:03d}"
    while candidate.casefold() in sensitive:
        candidate = f"{prefix}{number:03d}-P"
    return candidate


def _pseudonym_name(number: int, sensitive: set) -> str:
    candidate = f"Анонимный поставщик {number:03d}"
    while candidate.casefold() in sensitive:
        candidate = f"Анонимный поставщик {number:03d} (пилот)"
    return candidate


def anonymize_records(suppliers: list, contracts: list, sensitive: set) -> tuple:
    """Replace ids/names/contract numbers with stable pseudonyms; never keep a reverse map."""
    order = sorted(suppliers, key=lambda item: (item["_source_id"].casefold(),
                                                item["_source_id"]))
    contract_by_supplier = {item["_supplier_id"]: item for item in contracts}
    anonymous = []
    for position, record in enumerate(order, start=1):
        contract = contract_by_supplier[record["_source_id"]]
        anonymous.append({
            "id": _pseudonym("ANON-SUP-", position, sensitive),
            "name": _pseudonym_name(position, sensitive),
            "country": record["country"],
            "category": record["category"],
            "currency": record["currency"],
            "unit": record["unit"],
            "annual_volume": record["annual_volume"],
            "unit_price": record["unit_price"],
            "freight_per_unit": record["freight_per_unit"],
            "lead_time_days": record["lead_time_days"],
            "otif": record["otif"],
            "defect_ppm": record["defect_ppm"],
            "audit_score": record["audit_score"],
            "risk": dict(record["risk"]),
            "contract": {
                "id": _pseudonym("ANON-CT-", position, sensitive),
                "status": contract["status"],
                "cpi_share": contract["cpi_share"],
                "ppi_share": contract["ppi_share"],
                "fx_share": contract["fx_share"],
                "cap_pct": contract["cap_pct"],
                "floor_zero": contract["floor_zero"],
                "payment_days": contract["payment_days"],
            },
        })
    anonymous.sort(key=lambda item: item["id"])
    return anonymous, {"suppliers": len(anonymous), "contracts": len(anonymous)}


def _sensitive_strings(suppliers: list, contracts: list) -> set:
    sensitive = set()
    for record in suppliers:
        for value in (record["_source_id"], record["_source_name"]):
            text = _text(value)
            if text and len(text) >= 3:
                sensitive.add(text.casefold())
    for contract in contracts:
        text = _text(contract["_source_id"])
        if text and len(text) >= 3:
            sensitive.add(text.casefold())
    return sensitive


def leak_scan(texts: dict, sensitive: set) -> list:
    problems = []
    for label, text in texts.items():
        folded = text.casefold()
        compact = re.sub(r"\s+", "", folded)
        for value in sensitive:
            if value and (value in folded or value.replace(" ", "") in compact):
                problems.append(f"{label}: найден исходный идентификатор (длина {len(value)})")
    for label, text in texts.items():
        for marker in FORBIDDEN_SUBSTRINGS:
            if marker.casefold() in text.casefold():
                problems.append(f"{label}: raw path marker {marker!r}")
    return problems


# ------------------------------------------------------------------------------------ model bridge
def _dataset_document(anonymous: list, *, period: str) -> dict:
    suppliers = []
    for record in anonymous:
        suppliers.append({
            "id": record["id"],
            "name": record["name"],
            "country": record["country"],
            "category": record["category"],
            "currency": record["currency"],
            "unit": record["unit"],
            "annual_volume": record["annual_volume"],
            "unit_price": record["unit_price"],
            "freight_per_unit": record["freight_per_unit"],
            "lead_time_days": record["lead_time_days"],
            "otif": record["otif"],
            "defect_ppm": record["defect_ppm"],
            "audit_score": record["audit_score"],
            "risk": dict(record["risk"]),
            "contract": dict(record["contract"]),
        })
    return {
        "schema": fp.DATASET_SCHEMA,
        "version": "1.0.0",
        "source_class": "anonymized_fixture",
        "currency": fp.BASE_CURRENCY,
        "period": period,
        "note": ("Анонимизированный набор пилота: исходные id поставщиков, имена и номера "
                 "договоров заменены псевдонимами; исходные файлы в артефакты не копируются."),
        "suppliers": suppliers,
    }


def _adapt_procurement(report: dict, *, dataset_sha256: str) -> dict:
    report = json.loads(json.dumps(report, ensure_ascii=False))
    report["dataset"]["path"] = "pilot intake (anonymized)"
    report["dataset"]["sha256"] = dataset_sha256
    report["dataset"]["note"] = ("Анонимизированный набор пилота; исходные файлы не копируются, "
                                 "в манифесте только SHA-256.")
    report["statement"] = PILOT_STATEMENT
    report["limitations"] = list(PILOT_LIMITATIONS)
    return report


# ------------------------------------------------------------------------------------ DOCX helpers
def _docx_module():
    try:
        import docx  # noqa: F401
    except ImportError as error:  # pragma: no cover - dependency guard
        raise PilotError("для CFO-отчёта нужен python-docx (pip install python-docx)") from error
    return docx


def _normalise_docx_bytes(payload: bytes, moment: datetime) -> bytes:
    """Pin zip timestamps and docProps/core.xml dates so two runs are byte-identical."""
    date_time = moment.timetuple()[:6]
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%SZ").encode("utf-8")
    source = zipfile.ZipFile(io.BytesIO(payload), "r")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "docProps/core.xml":
                data = re.sub(
                    rb"(<dcterms:(?:created|modified)[^>]*>)[^<]*(</dcterms:(?:created|modified)>)",
                    rb"\g<1>" + stamp + rb"\g<2>", data)
            new_info = zipfile.ZipInfo(info.filename, date_time=date_time)
            new_info.compress_type = zipfile.ZIP_DEFLATED
            new_info.external_attr = info.external_attr
            target.writestr(new_info, data)
    source.close()
    return buffer.getvalue()


def _docx_text(payload: bytes) -> str:
    chunks = []
    with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
        for name in archive.namelist():
            if name.endswith(".xml") and (name.startswith("word/")
                                          or name.startswith("docProps/")):
                raw = archive.read(name).decode("utf-8", "ignore")
                chunks.append(re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", " ".join(chunks))


def _md_table(headers: tuple, rows: list) -> list:
    lines = ["| " + " | ".join(str(item) for item in headers) + " |",
             "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(item) for item in row) + " |")
    return lines


def _docx_table(document, headers: tuple, rows: list) -> None:
    if not rows:
        document.add_paragraph("—")
        return
    table = document.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    for index, header in enumerate(headers):
        table.rows[0].cells[index].text = str(header)
    for row in rows:
        cells = table.add_row().cells
        for index, value in enumerate(row):
            cells[index].text = "" if value is None else str(value)


def _fmt(value, digits: int = 1) -> str:
    try:
        return f"{float(value):,.{digits}f}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def _pct(value, digits: int = 3) -> str:
    try:
        return f"{float(value):+.{digits}f} %".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def _eur(value) -> str:
    return _fmt(value, 0)


# ------------------------------------------------------------------------------------ renderings
def render_markdown(pilot: dict) -> str:
    procurement = pilot["procurement"]
    budget = procurement["budget"]
    scenario = procurement["scenario"]
    quality = pilot["quality"]
    lines = []
    lines.append("# FLAKS Pilot — отчёт для CFO (UC-13/14/16)")
    lines.append("")
    lines.append(f"Статус: **{pilot['status']}**, admission=**false**, инструмент "
                 f"{pilot['tool_version']}, модель {pilot['model_version']}, сценарий "
                 f"**{scenario['id']}** ({scenario['label']}). "
                 f"generated_at: {pilot['generated_at']}.")
    lines.append("")
    lines.append("> " + pilot["statement"])
    lines.append("")
    lines.append("## 1. Резюме для CFO")
    lines.append("")
    top = procurement["scoring"]["ranking"][0] if procurement["scoring"]["ranking"] else None
    savings = sum(category["savings"]["saving_eur"]
                  for category in procurement["scoring"]["categories"])
    lines.append(f"- Базовый бюджет действующих контрактов: "
                 f"**{_eur(budget['base_total_eur'])} EUR**.")
    lines.append(f"- Прогноз при сценарии `{scenario['id']}` (только цены/индексация): "
                 f"**{_eur(budget['focus_total_eur'])} EUR** "
                 f"({_pct(budget['focus_delta_pct'])}), интервал 5–95: "
                 f"{_eur(budget['interval_eur']['low_eur'])} … "
                 f"{_eur(budget['interval_eur']['high_eur'])} EUR.")
    lines.append(f"- С учётом объёмного эффекта "
                 f"({scenario['drivers']['volume_delta_pct']:+.1f} %): "
                 f"**{_eur(budget['focus_total_with_volume_eur'])} EUR**.")
    if top:
        lines.append(f"- Лидер общего скоринга: **{top['name']}** (`{top['category']}`), "
                     f"балл {top['score']:.4f}, класс {top['band']}.")
    lines.append(f"- Потенциал «весь объём — поставщику с минимальным TCO»: "
                 f"**{_eur(savings)} EUR** до учёта концентрации риска (иллюстрация, "
                 f"не рекомендация).")
    lines.append(f"- Качество входных данных: **{quality['status']}**, предупреждений "
                 f"{len(quality['warnings'])}, выбросов {len(quality['outliers'])}, "
                 f"строк с значениями по умолчанию {len(quality['defaults_applied'])}.")
    lines.append("")
    lines.append("## 2. Качество данных")
    lines.append("")
    lines.append(f"Поставщиков: {quality['rows']['suppliers']}, договоров: "
                 f"{quality['rows']['contracts']}. Схема и типы проверены, обязательные поля "
                 f"заполнены, дубликатов id нет, каждый поставщик имеет ровно один договор.")
    lines.append("")
    nonzero = {table: {column: count for column, count in values.items() if count}
               for table, values in quality["missing"].items()}
    if any(nonzero.values()):
        lines.append("Пропуски (непустых ячеек нет): " + "; ".join(
            f"{table}: " + ", ".join(f"`{column}`={count}"
                                     for column, count in values.items())
            for table, values in nonzero.items() if values))
    else:
        lines.append("Пропусков в обязательных и необязательных полях нет.")
    lines.append("")
    if quality["units"]["conversions"]:
        lines.append("Приведение единиц: " + "; ".join(
            f"строка {item['row']}: {item['from']} → {item['to']} (×{item['factor']:g})"
            for item in quality["units"]["conversions"][:10]))
        lines.append("")
    if quality["defaults_applied"]:
        lines.append(f"Значения по умолчанию: {len(quality['defaults_applied'])} "
                     f"(пустые name/currency/unit/freight_per_unit). Полный список — в JSON.")
        lines.append("")
    if quality["outliers"]:
        lines.append("Выбросы (IQR ×3 внутри категории, предупреждение):")
        for item in quality["outliers"][:10]:
            lines.append(f"- строка {item['row']}, `{item['column']}` = {item['value']} "
                         f"(норма {item['lower']} … {item['upper']}, {item['category']})")
        lines.append("")
    if quality["warnings"]:
        lines.append("Предупреждения:")
        for item in quality["warnings"][:15]:
            lines.append(f"- `{item['code']}` строка {item['row']}: {item['message']}")
        if len(quality["warnings"]) > 15:
            lines.append(f"- … ещё {len(quality['warnings']) - 15}")
        lines.append("")

    lines.append("## 3. Скоринг поставщиков")
    lines.append("")
    lines.append("Веса: " + ", ".join(f"`{key}`={value:.2f}"
                                     for key, value in procurement["weights"]["values"].items())
                 + " (сумма 1,0). Нормализация — best-in-class внутри категории.")
    lines.append("")
    for category in procurement["scoring"]["categories"]:
        lines.append(f"### {category['name_ru']} (`{category['id']}`)")
        lines.append("")
        rows = []
        for entry in category["suppliers"]:
            rows.append((entry["rank"], entry["name"], entry["country"],
                         f"{entry['score']:.4f}", entry["band"]["letter"],
                         _fmt(entry["unit_price"], 2), _fmt(entry["tco"]["total"], 4),
                         f"{entry['risk_composite']:.3f}", f"{entry['otif']:.3f}",
                         f"{entry['lead_time_days']:.0f}",
                         entry["contract"]["status"]))
        lines.extend(_md_table(("#", "Поставщик", "Страна", "Балл", "Класс", "Цена/ед.",
                                "TCO/ед.", "Риск", "OTIF", "Лид, дн.", "Контракт"), rows))
        lines.append("")
        lines.append(f"Минимальная цена: `{category['best_price_supplier']}`; минимальный TCO: "
                     f"`{category['best_tco_supplier']}`; рекомендация: "
                     f"`{category['recommended']['supplier_id']}`.")
        lines.append("")

    lines.append("## 4. TCO: из чего складывается")
    lines.append("")
    for category in procurement["scoring"]["categories"]:
        rows = []
        for entry in category["suppliers"]:
            tco = entry["tco"]
            rows.append((entry["name"], _fmt(tco["price"], 4), _fmt(tco["logistics"], 4),
                         _fmt(tco["duty"], 4), _fmt(tco["risk"], 4), _fmt(tco["quality"], 4),
                         _fmt(tco["late"], 4), _fmt(tco["carrying"], 4),
                         _fmt(tco["total"], 4)))
        lines.append(f"### {category['name_ru']}")
        lines.extend(_md_table(("Поставщик", "Цена", "Логистика", "Пошлина", "Риск",
                                "Качество", "Поздняя", "Капитал", "TCO/ед."), rows))
        lines.append("")

    lines.append("## 5. Бюджет закупок по сценариям")
    lines.append("")
    rows = []
    for item in budget["scenarios"]:
        rows.append((item["id"], f"{item['drivers']['cpi_pp']:+.3f}",
                     f"{item['drivers']['oil_delta_pct']:+.1f}",
                     f"{item['drivers']['freight_delta_pct']:+.1f}",
                     _eur(item["total_eur"]), _eur(item["delta_eur"]),
                     _pct(item["delta_pct"]), _eur(item["total_with_volume_eur"]),
                     f"{_eur(item['interval_eur']['low'])} … "
                     f"{_eur(item['interval_eur']['high'])}"))
    lines.extend(_md_table(("Сценарий", "ΔCPI, пп", "Δнефть, %", "Δфрахт, %",
                            "Бюджет (цены), EUR", "Δ, EUR", "Δ, %", "С объёмом, EUR",
                            "Интервал 5–95, EUR"), rows))
    lines.append("")

    lines.append("## 6. Индексация контрактов под сценарием `" + scenario["id"] + "`")
    lines.append("")
    rows = []
    for item in budget["contracts"]:
        flags = ",".join(item["flags"]) if item["flags"] else "—"
        rows.append((item["contract_id"], item["supplier_id"],
                     f"{item['shares']['cpi']:.2f}/{item['shares']['ppi']:.2f}/"
                     f"{item['shares']['fx']:.2f}/{item['shares']['fixed']:.2f}",
                     _eur(item["base_eur"]), f"{item['raw_pct']:+.3f}",
                     f"{item['cap_pct']:.1f}", f"{item['applied_pct']:+.3f}",
                     _eur(item["indexation_eur"]), _eur(item["forecast_eur"]), flags))
    lines.extend(_md_table(("Договор", "Поставщик", "CPI/PPI/FX/фикс", "База, EUR",
                            "Сырой %", "Cap, %", "Применён %", "Индексация, EUR",
                            "Прогноз, EUR", "Флаги"), rows))
    lines.append("")
    lines.append("## 7. Что сделать до управленческого решения")
    lines.append("")
    for action in PILOT_ACTIONS:
        lines.append(f"1. {action}")
    lines.append("")
    lines.append("## 8. Проверки и происхождение")
    lines.append("")
    rows = [(key, "OK" if value else "—") for key, value in pilot["checks"].items()
            if isinstance(value, bool) and key not in ("admission", "research_only")]
    lines.extend(_md_table(("Проверка", "Результат"), rows))
    lines.append("")
    lines.append(f"Политика: статус {pilot['status']}, admission=false, анонимизация "
                 f"{pilot['anonymization']['strategy']}, утечек исходных идентификаторов "
                 f"{pilot['anonymization']['leak_scan']}. SHA-256 анонимизированного набора: "
                 f"`{pilot['intake']['dataset_sha256']}`.")
    lines.append("")
    lines.append("## 9. Ограничения")
    lines.append("")
    for item in pilot["limitations"]:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## 10. Воспроизводимость")
    lines.append("")
    lines.append("```powershell")
    lines.append("python tools/flaks_pilot.py --suppliers <suppliers.csv> "
                 "--contracts <contracts.csv> --scenario " + scenario["id"]
                 + " --out build/flaks_pilot --anonymize --generated-at <UTC>")
    lines.append("python -m unittest tests.test_flaks_pilot -v")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def render_docx(pilot: dict, generated_at: str) -> bytes:
    docx = _docx_module()
    procurement = pilot["procurement"]
    budget = procurement["budget"]
    scenario = procurement["scenario"]
    quality = pilot["quality"]
    document = docx.Document()
    document.add_heading("FLAKS Pilot — отчёт для CFO", level=0)
    document.add_paragraph(f"Статус: {pilot['status']} · admission=false · модель "
                           f"{pilot['model_version']} · сценарий {scenario['id']} "
                           f"({scenario['label']}) · сформирован: {generated_at}")
    notice = document.add_paragraph(pilot["statement"])
    try:
        notice.style = document.styles["Intense Quote"]
    except KeyError:  # pragma: no cover - style set differs by template
        pass

    document.add_heading("1. Резюме для CFO", level=1)
    top = procurement["scoring"]["ranking"][0] if procurement["scoring"]["ranking"] else None
    savings = sum(category["savings"]["saving_eur"]
                  for category in procurement["scoring"]["categories"])
    summary = [
        ("Базовый бюджет действующих контрактов", f"{_eur(budget['base_total_eur'])} EUR"),
        ("Прогноз по сценарию (цены/индексация)",
         f"{_eur(budget['focus_total_eur'])} EUR ({_pct(budget['focus_delta_pct'])})"),
        ("Интервал 5–95", f"{_eur(budget['interval_eur']['low_eur'])} … "
                          f"{_eur(budget['interval_eur']['high_eur'])} EUR"),
        ("С объёмным эффектом", f"{_eur(budget['focus_total_with_volume_eur'])} EUR"),
        ("Потенциал min-TCO (иллюстрация)", f"{_eur(savings)} EUR"),
        ("Качество входных данных",
         f"{quality['status']} · предупреждений {len(quality['warnings'])} · "
         f"выбросов {len(quality['outliers'])}"),
    ]
    if top:
        summary.insert(4, ("Лидер общего скоринга", f"{top['name']} · {top['category']} · "
                                                  f"{top['score']:.4f} · класс {top['band']}"))
    _docx_table(document, ("Показатель", "Значение"), summary)

    document.add_heading("2. Качество данных", level=1)
    document.add_paragraph(
        f"Поставщиков: {quality['rows']['suppliers']}, договоров: "
        f"{quality['rows']['contracts']}. Схема, типы и обязательные поля проверены; "
        f"дубликатов id нет; у каждого поставщика ровно один договор.")
    missing_rows = [(table, column, count)
                    for table, values in quality["missing"].items()
                    for column, count in values.items() if count]
    if missing_rows:
        _docx_table(document, ("Таблица", "Колонка", "Пустых ячеек"), missing_rows)
    else:
        document.add_paragraph("Пропусков нет.")
    if quality["units"]["conversions"]:
        document.add_paragraph("Приведение единиц:")
        for item in quality["units"]["conversions"][:10]:
            document.add_paragraph(f"• строка {item['row']}: {item['from']} → {item['to']} "
                                   f"(×{item['factor']:g})")
    if quality["outliers"]:
        document.add_paragraph("Выбросы (IQR ×3 внутри категории, предупреждение):")
        _docx_table(document, ("Строка", "Поле", "Значение", "Норма", "Категория"),
                    [(item["row"], item["column"], item["value"],
                      f"{item['lower']} … {item['upper']}", item["category"])
                     for item in quality["outliers"][:15]])
    if quality["warnings"]:
        document.add_paragraph("Предупреждения:")
        for item in quality["warnings"][:15]:
            document.add_paragraph(f"• {item['code']} (строка {item['row']}): "
                                   f"{item['message']}")

    document.add_heading("3. Скоринг поставщиков", level=1)
    document.add_paragraph(
        "Веса: " + ", ".join(f"{key}={value:.2f}"
                             for key, value in procurement["weights"]["values"].items())
        + " (сумма 1,0). Нормализация — best-in-class внутри категории.")
    for category in procurement["scoring"]["categories"]:
        document.add_heading(f"{category['name_ru']}", level=2)
        _docx_table(document, ("#", "Поставщик", "Страна", "Балл", "Класс", "Цена/ед.",
                               "TCO/ед.", "Риск", "OTIF", "Лид, дн.", "Контракт"),
                    [(entry["rank"], entry["name"], entry["country"],
                      f"{entry['score']:.4f}", entry["band"]["letter"],
                      _fmt(entry["unit_price"], 2), _fmt(entry["tco"]["total"], 4),
                      f"{entry['risk_composite']:.3f}", f"{entry['otif']:.3f}",
                      f"{entry['lead_time_days']:.0f}", entry["contract"]["status"])
                     for entry in category["suppliers"]])
        document.add_paragraph(
            f"Минимальная цена: {category['best_price_supplier']}; минимальный TCO: "
            f"{category['best_tco_supplier']}; рекомендация: "
            f"{category['recommended']['supplier_id']}.")

    document.add_heading("4. TCO: из чего складывается", level=1)
    for category in procurement["scoring"]["categories"]:
        document.add_heading(category["name_ru"], level=2)
        _docx_table(document, ("Поставщик", "Цена", "Логистика", "Пошлина", "Риск",
                               "Качество", "Поздняя", "Капитал", "TCO/ед."),
                    [(entry["name"], _fmt(entry["tco"]["price"], 4),
                      _fmt(entry["tco"]["logistics"], 4), _fmt(entry["tco"]["duty"], 4),
                      _fmt(entry["tco"]["risk"], 4), _fmt(entry["tco"]["quality"], 4),
                      _fmt(entry["tco"]["late"], 4), _fmt(entry["tco"]["carrying"], 4),
                      _fmt(entry["tco"]["total"], 4))
                     for entry in category["suppliers"]])

    document.add_heading("5. Бюджет закупок по сценариям", level=1)
    _docx_table(document, ("Сценарий", "ΔCPI, пп", "Δнефть, %", "Δфрахт, %",
                           "Бюджет, EUR", "Δ, EUR", "Δ, %", "С объёмом, EUR",
                           "Интервал 5–95, EUR"),
                [(item["id"], f"{item['drivers']['cpi_pp']:+.3f}",
                  f"{item['drivers']['oil_delta_pct']:+.1f}",
                  f"{item['drivers']['freight_delta_pct']:+.1f}",
                  _eur(item["total_eur"]), _eur(item["delta_eur"]),
                  _pct(item["delta_pct"]), _eur(item["total_with_volume_eur"]),
                  f"{_eur(item['interval_eur']['low'])} … {_eur(item['interval_eur']['high'])}")
                 for item in budget["scenarios"]])

    document.add_heading(f"6. Индексация контрактов ({scenario['id']})", level=1)
    _docx_table(document, ("Договор", "Поставщик", "CPI/PPI/FX/фикс", "База, EUR",
                           "Сырой %", "Cap, %", "Применён %", "Индексация, EUR",
                           "Прогноз, EUR", "Флаги"),
                [(item["contract_id"], item["supplier_id"],
                  f"{item['shares']['cpi']:.2f}/{item['shares']['ppi']:.2f}/"
                  f"{item['shares']['fx']:.2f}/{item['shares']['fixed']:.2f}",
                  _eur(item["base_eur"]), f"{item['raw_pct']:+.3f}",
                  f"{item['cap_pct']:.1f}", f"{item['applied_pct']:+.3f}",
                  _eur(item["indexation_eur"]), _eur(item["forecast_eur"]),
                  ",".join(item["flags"]) if item["flags"] else "—")
                 for item in budget["contracts"]])

    document.add_heading("7. Что сделать до управленческого решения", level=1)
    for action in PILOT_ACTIONS:
        document.add_paragraph("• " + action)

    document.add_heading("8. Проверки и происхождение", level=1)
    _docx_table(document, ("Проверка", "Результат"),
                [(key, "OK" if value else "—") for key, value in pilot["checks"].items()
                 if isinstance(value, bool) and key not in ("admission", "research_only")])
    document.add_paragraph(
        f"Политика: статус {pilot['status']}, admission=false; анонимизация: "
        f"{pilot['anonymization']['strategy']}; утечек исходных идентификаторов: "
        f"{pilot['anonymization']['leak_scan']}. SHA-256 анонимизированного набора: "
        f"{pilot['intake']['dataset_sha256']}.")

    document.add_heading("9. Ограничения", level=1)
    for item in pilot["limitations"]:
        document.add_paragraph("• " + item)

    document.add_heading("10. Воспроизводимость", level=1)
    document.add_paragraph(
        "python tools/flaks_pilot.py --suppliers <suppliers.csv> --contracts <contracts.csv> "
        f"--scenario {scenario['id']} --out build/flaks_pilot --anonymize "
        "--generated-at <UTC>")
    document.add_paragraph("python -m unittest tests.test_flaks_pilot -v")

    stream = io.BytesIO()
    document.save(stream)
    moment = datetime.strptime(generated_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return _normalise_docx_bytes(stream.getvalue(), moment)


# ------------------------------------------------------------------------------------- pipeline
def load_intake(suppliers_path, contracts_path) -> dict:
    supplier_table = read_table(suppliers_path, role="suppliers")
    contract_table = read_table(contracts_path, role="contracts")
    issues = Issues()
    suppliers, defaults, conversions = parse_suppliers(supplier_table, issues)
    contracts, contract_defaults = parse_contracts(contract_table, suppliers, issues)
    defaults.extend(contract_defaults)

    by_supplier = {}
    for contract in contracts:
        by_supplier.setdefault(contract["_supplier_id"], []).append(contract)
    for record in suppliers:
        matches = by_supplier.get(record["_source_id"], [])
        if not matches:
            issues.error("contracts", record["_row"], "supplier_id",
                         f"у поставщика {record['_source_id']!r} нет договора")
        elif len(matches) > 1:
            issues.error("contracts", matches[1]["_row"], "supplier_id",
                         f"у поставщика {record['_source_id']!r} больше одного договора "
                         f"(строки {[item['_row'] for item in matches]}) — MVP поддерживает "
                         f"один договор на поставщика")

    for category in fp.CATEGORY_ORDER:
        entries = [record for record in suppliers if record["category"] == category]
        if not entries:
            continue
        if len(entries) < fp.MIN_SUPPLIERS_PER_CATEGORY:
            issues.error("suppliers", entries[0]["_row"], "category",
                         f"категория {category!r}: нужно минимум "
                         f"{fp.MIN_SUPPLIERS_PER_CATEGORY} поставщика для сравнения, "
                         f"есть {len(entries)}")
        active = [record for record in entries
                  if any(contract["_supplier_id"] == record["_source_id"]
                         and contract["status"] == "active" for contract in contracts)]
        if not active:
            issues.error("contracts", entries[0]["_row"], "status",
                         f"категория {category!r}: нет ни одного действующего контракта")

    issues.raise_if_errors("входные таблицы")

    quality = _quality_block(supplier_table=supplier_table, contract_table=contract_table,
                             suppliers=suppliers, contracts=contracts, issues=issues,
                             defaults=defaults, conversions=conversions)
    return {
        "schema": INTAKE_SCHEMA,
        "suppliers": suppliers,
        "contracts": contracts,
        "quality": quality,
        "tables": {"suppliers": supplier_table, "contracts": contract_table},
        "sensitive": _sensitive_strings(suppliers, contracts),
        "inputs": {
            "suppliers": {"sha256": _sha256_file(Path(suppliers_path)),
                          "format": supplier_table["meta"]["format"],
                          "rows": len(supplier_table["rows"]),
                          "columns": supplier_table["columns"]},
            "contracts": {"sha256": _sha256_file(Path(contracts_path)),
                          "format": contract_table["meta"]["format"],
                          "rows": len(contract_table["rows"]),
                          "columns": contract_table["columns"]},
        },
    }


def build_pilot(suppliers_path, contracts_path, *, scenario_id: str = DEFAULT_SCENARIO,
                generated_at: str | None = None, out_dir=None) -> dict:
    if scenario_id not in fp.SCENARIO_BY_ID:
        raise PilotError(f"неизвестный сценарий {scenario_id!r}; известны "
                         f"{list(fp.SCENARIO_ORDER)}")
    generated_at = generated_at or _now()
    if not TIMESTAMP_RE.match(generated_at):
        raise PilotError(f"generated_at: ожидался формат YYYY-MM-DDTHH:MM:SSZ, "
                         f"получено {generated_at!r}")
    intake = load_intake(suppliers_path, contracts_path)
    anonymous, anon_counts = anonymize_records(intake["suppliers"], intake["contracts"],
                                               intake["sensitive"])
    period = generated_at[:4]
    document = _dataset_document(anonymous, period=period)
    dataset_bytes = _canonical_bytes(document)
    dataset_sha256 = _sha256_bytes(dataset_bytes)
    dataset_document = json.loads(dataset_bytes.decode("utf-8"))

    # The engine still consumes a JSON file; write it to a temporary directory, never to the repo.
    with tempfile.TemporaryDirectory(prefix="flaks-pilot-intake-") as temporary:
        source = Path(temporary) / "suppliers.json"
        source.write_bytes(dataset_bytes)
        try:
            outcome = fp.build_report(suppliers=source, scenario_id=scenario_id,
                                      out_dir=None, generated_at=generated_at)
        except fp.ProcurementError as error:
            raise PilotError(f"движок отклонил анонимизированный набор: {error}") from error
        procurement = _adapt_procurement(outcome["report"], dataset_sha256=dataset_sha256)

    model_positive = ("weights_sum_to_one", "score_equals_contributions", "tco_components_sum",
                      "components_in_unit_interval", "budget_base_equals_contract_sum",
                      "interval_ordered", "no_raw_data")
    checks = {
        "schema_valid": True,
        "types_valid": True,
        "required_fields_present": True,
        "ids_unique": True,
        "join_complete": True,
        "anonymized": True,
        "no_raw_data": True,
        "quality_status": intake["quality"]["status"],
        "model_checks_passed": all(bool(procurement["checks"].get(key)) for key in model_positive),
        "deterministic": True,
        "fail_closed": True,
        "research_only": True,
        "admission": False,
    }

    pilot = {
        "schema": SCHEMA,
        "status": STATUS,
        "admission": ADMISSION,
        "tool_version": TOOL_VERSION,
        "model_version": fp.MODEL_VERSION,
        "generated_at": generated_at,
        "scenario": scenario_id,
        "statement": PILOT_STATEMENT,
        "intake": {
            "schema": INTAKE_SCHEMA,
            "suppliers": intake["inputs"]["suppliers"],
            "contracts": intake["inputs"]["contracts"],
            "dataset_sha256": dataset_sha256,
            "period": period,
            "note": ("исходные файлы не копируются и не коммитятся; в манифесте только их "
                     "SHA-256, без имён"),
        },
        "quality": intake["quality"],
        "anonymization": {
            "applied": True,
            "strategy": "последовательные псевдонимы ANON-SUP-NNN / ANON-CT-NNN, "
                        "отсортированные по исходному id; обратное соответствие не сохраняется",
            "suppliers": anon_counts["suppliers"],
            "contracts": anon_counts["contracts"],
            "leak_scan": "PASS",
            "note": "исходные id, имена и номера договоров в артефакты не попадают",
        },
        "procurement": procurement,
        "checks": checks,
        "limitations": list(PILOT_LIMITATIONS),
    }

    markdown = render_markdown(pilot)
    docx_bytes = render_docx(pilot, generated_at)

    texts = {
        "json": _pretty_bytes(pilot).decode("utf-8"),
        "markdown": markdown,
        "docx": _docx_text(docx_bytes),
    }
    problems = leak_scan(texts, intake["sensitive"])
    problems.extend(fp.scan_for_raw(pilot))
    if problems:
        raise PilotError("в отчёте обнаружены исходные идентификаторы или raw-маркеры: "
                         + "; ".join(problems[:5]))

    json_bytes = _pretty_bytes(pilot)
    markdown_bytes = markdown.encode("utf-8")
    files = {
        "flaks_pilot.json": json_bytes,
        "flaks_pilot.md": markdown_bytes,
        "flaks_pilot_cfo.docx": docx_bytes,
    }
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "status": STATUS,
        "admission": ADMISSION,
        "tool_version": TOOL_VERSION,
        "model_version": fp.MODEL_VERSION,
        "generated_at": generated_at,
        "scenario": scenario_id,
        "anonymized": True,
        "dataset_sha256": dataset_sha256,
        "inputs": {
            "suppliers": {key: value for key, value in intake["inputs"]["suppliers"].items()
                          if key != "columns"},
            "contracts": {key: value for key, value in intake["inputs"]["contracts"].items()
                          if key != "columns"},
        },
        "files": [{"name": name, "sha256": _sha256_bytes(payload), "bytes": len(payload)}
                  for name, payload in sorted(files.items())],
        "command": ("python tools/flaks_pilot.py --suppliers <suppliers.csv> "
                    "--contracts <contracts.csv> --scenario " + scenario_id
                    + " --out build/flaks_pilot --anonymize --generated-at " + generated_at),
        "note": ("манифест перечисляет только артефакты отчёта; он не содержит имён исходных "
                 "файлов и идентификаторов поставщиков"),
    }
    manifest_bytes = _pretty_bytes(manifest)

    paths = {}
    if out_dir is not None:
        paths = _write_artifacts(Path(out_dir), scenario_id,
                                 {**files, "flaks_pilot_manifest.json": manifest_bytes})
    return {
        "report": pilot,
        "markdown": markdown,
        "docx": docx_bytes,
        "manifest": manifest,
        "files": files,
        "paths": paths,
        "quality": intake["quality"],
    }


def _write_artifacts(out_dir: Path, scenario_id: str, files: dict) -> dict:
    target = Path(out_dir) / scenario_id
    created = not target.exists()
    target.mkdir(parents=True, exist_ok=True)
    written = []
    try:
        for name in ("flaks_pilot.json", "flaks_pilot.md", "flaks_pilot_cfo.docx",
                     "flaks_pilot_manifest.json"):
            payload = files[name]
            (target / name).write_bytes(payload)
            written.append({"name": name, "path": str(target / name),
                            "sha256": _sha256_bytes(payload), "bytes": len(payload)})
    except Exception:
        if created:
            shutil.rmtree(target, ignore_errors=True)
        raise
    return {"dir": str(target), "files": written}


# ------------------------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="FLAKS pilot intake: CSV/XLSX -> validation, quality, anonymization, "
                    "scoring/TCO/budget, CFO report (JSON+MD+DOCX) and SHA-256 manifest")
    parser.add_argument("--suppliers", required=True,
                        help="CSV/XLSX таблица поставщиков по шаблону pilot/flaks/"
                             "suppliers_template.csv.tmpl")
    parser.add_argument("--contracts", required=True,
                        help="CSV/XLSX таблица договоров по шаблону pilot/flaks/"
                             "contracts_template.csv.tmpl")
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO,
                        help=f"сценарий из {list(fp.SCENARIO_ORDER)}")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="каталог артефактов (по умолчанию build/flaks_pilot)")
    parser.add_argument("--anonymize", action="store_true",
                        help="явно включить анонимизацию (включена всегда; флаг для аудита)")
    parser.add_argument("--check", action="store_true",
                        help="только проверить данные и показать качество, ничего не писать")
    parser.add_argument("--json", action="store_true", help="напечатать полный отчёт")
    parser.add_argument("--generated-at", default=None,
                        help="UTC-таймстамп YYYY-MM-DDTHH:MM:SSZ (для детерминизма)")
    return parser


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):  # pragma: no cover - platform dependent
                pass
    args = build_parser().parse_args(argv)
    if args.scenario not in fp.SCENARIO_BY_ID:
        print(json.dumps({"status": "BLOCKED_INPUT",
                          "error": f"неизвестный сценарий {args.scenario!r}; известны "
                                   f"{list(fp.SCENARIO_ORDER)}"},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    try:
        if args.check:
            intake = load_intake(args.suppliers, args.contracts)
            if args.json:
                print(json.dumps({"status": "CHECK_OK", "schema": INTAKE_SCHEMA,
                                  "quality": intake["quality"],
                                  "inputs": intake["inputs"]},
                                 ensure_ascii=False, sort_keys=True, indent=2))
            else:
                quality = intake["quality"]
                print(json.dumps({
                    "status": "CHECK_OK",
                    "rows": quality["rows"],
                    "quality_status": quality["status"],
                    "warnings": len(quality["warnings"]),
                    "outliers": len(quality["outliers"]),
                    "defaults_applied": len(quality["defaults_applied"]),
                    "units_converted": len(quality["units"]["conversions"]),
                    "files_written": [],
                }, ensure_ascii=False))
            return 0
        outcome = build_pilot(args.suppliers, args.contracts, scenario_id=args.scenario,
                              generated_at=args.generated_at, out_dir=args.out)
    except (PilotError, fp.ProcurementError, OSError) as error:
        payload = {"status": "BLOCKED_INPUT", "error": str(error)}
        issues = getattr(error, "issues", None)
        if issues:
            payload["issues"] = issues[:20]
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 2
    report = outcome["report"]
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    summary = {
        "status": report["status"],
        "schema": report["schema"],
        "scenario": report["scenario"],
        "out": outcome["paths"].get("dir"),
        "files": [{"name": item["name"], "sha256": item["sha256"], "bytes": item["bytes"]}
                  for item in outcome["paths"].get("files", [])],
        "suppliers": report["intake"]["suppliers"]["rows"],
        "contracts": report["intake"]["contracts"]["rows"],
        "quality_status": report["quality"]["status"],
        "quality_warnings": len(report["quality"]["warnings"]),
        "outliers": len(report["quality"]["outliers"]),
        "anonymized": report["anonymization"]["applied"],
        "leak_scan": report["anonymization"]["leak_scan"],
        "base_total_eur": report["procurement"]["budget"]["base_total_eur"],
        "forecast_total_eur": report["procurement"]["budget"]["focus_total_eur"],
        "delta_pct": report["procurement"]["budget"]["focus_delta_pct"],
        "checks": {key: value for key, value in report["checks"].items()
                   if isinstance(value, bool)},
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
