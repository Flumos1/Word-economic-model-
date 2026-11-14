#!/usr/bin/env python3
"""FIGARO inter-country input-output -> canonical AMFE MRIO dataset (docs/125).

Source: Eurostat FIGARO, dataset ``NAIO_10_FCP_IP3`` (EU inter-country input-output table,
product by product, 2018-2021), dissemination API, no key:
https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/naio_10_fcp_ip3

The full table is far too large to load into memory, so the download is split by destination
country (``c_dest``) and fetched in parallel; each response is a bounded JSON-stat document that
is parsed on its own.  The assembler turns the per-destination slices into the canonical
``amfe.mrio.dataset.v1`` document (``Z``, ``y``, ``x``, ``va`` plus provenance), validates it with
``amfe.mrio.schema`` (accounting identities, P2 scale gate), converts EUR million to USD million
with the World Bank official exchange rate, and writes:

    <root>/<dataset_id>/dataset.json   canonical dataset (third-party data: never committed)
    <root>/<dataset_id>/manifest.json  aggregate-only manifest with SHA-256 and GDP coverage

RoW: FIGARO's ``WRL_REST`` block is already an aggregate rest-of-world block; it is carried into
the canonical dataset as one country ``ROW``.  ``DOM`` (``Domestic country``) is not a country: in
this table it holds the domestic value-added rows of each destination column.

Usage:
    python tools/figaro_import.py --all --year 2019 --jobs 8
    python tools/figaro_import.py --download --year 2019 --jobs 8
    python tools/figaro_import.py --build --year 2019 --jobs 8
    python tools/figaro_import.py --coverage --year 2019
    python tools/figaro_import.py --validate
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amfe.mrio.loaders import (  # noqa: E402
    MANIFEST_SCHEMA,
    sha256_file,
    write_manifest,
)
from amfe.mrio.schema import (  # noqa: E402
    CANONICAL_UNITS,
    SCHEMA,
    SCHEMA_VERSION,
    MRIOError,
    dataset_from_document,
    dataset_to_bytes,
    validate_dataset,
)

DATASET_CODE = "NAIO_10_FCP_IP3"
API_BASE = ("https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
            + DATASET_CODE)
DEFAULT_ROOT = ROOT / "data" / "mrio" / "figaro"
DEFAULT_YEAR = 2019
USER_AGENT = "AMFE-FIGARO-import/1.0 (research; open data)"
WB_GDP_URL = "https://api.worldbank.org/v2/country/all/indicator/NY.GDP.MKTP.CD"
WB_FX_URL = "https://api.worldbank.org/v2/country/EMU/indicator/PA.NUS.FCRF"
WORLD_BANK_WORLD_ID = "1W"  # World aggregate inside country/all responses

ROW_CODE = "WRL_REST"
ROW_LABEL = "ROW"
DOM_CODE = "DOM"
FINAL_DEMAND_CODES = ("P3_S13", "P3_S14", "P3_S15", "P5M", "P51G")
VALUE_ADDED_CODES = ("B2A3G", "D1", "D21X31", "D29X39")
#: Eurostat ISO2 -> World Bank ISO2 differences.
ISO2_FIX = {"EL": "GR", "UK": "GB"}

PROBE_PRODUCT = "CPA_A01"
DEFAULT_TIMEOUT = 300.0
DEFAULT_TOLERANCE = 1e-4


class FigaroError(RuntimeError):
    """Fail-closed import error (missing data, broken response or invalid dataset)."""


# --------------------------------------------------------------------------------------- utils
def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _write_json(path: Path, document) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FigaroError(f"cannot read {path}: {exc!r}") from exc


def _http_get(url: str, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except Exception as exc:  # network/HTTP errors are fail-closed for this tool
        raise FigaroError(f"HTTP GET failed for {url}: {type(exc).__name__}: {exc}") from exc


def api_url(year: int, dest: str | None = None) -> str:
    params = {"format": "JSON", "lang": "EN", "time": str(year), "unit": "MIO_EUR"}
    if dest is not None:
        params["c_dest"] = dest
    return API_BASE + "?" + urllib.parse.urlencode(params)


# ------------------------------------------------------------------------------- JSON-stat core
def jsonstat_coordinates(flat: int, sizes) -> tuple:
    """Decode a JSON-stat flat index into dimension coordinates (last dimension varies fastest)."""
    if flat < 0:
        raise FigaroError(f"negative JSON-stat index: {flat}")
    total = 1
    for size in sizes:
        total *= int(size)
    if flat >= total:
        raise FigaroError(f"JSON-stat index {flat} outside size {sizes}")
    coords = []
    remainder = int(flat)
    for size in reversed(tuple(sizes)):
        size = int(size)
        coords.append(remainder % size)
        remainder //= size
    return tuple(reversed(coords))


def jsonstat_index(coordinates, sizes) -> int:
    """Inverse of :func:`jsonstat_coordinates` (useful for fixtures)."""
    index = 0
    for coordinate, size in zip(coordinates, sizes):
        index = index * int(size) + int(coordinate)
    return index


def iter_jsonstat_values(path: Path, chunk_size: int = 1 << 16):
    """Stream ``(flat_index, value)`` pairs out of the ``"value"`` member of a JSON-stat file.

    Bounded memory: the file is read in chunks and only the value container is tokenised, so a
    large single-file JSON-stat response never has to be loaded as one Python object.
    """
    path = Path(path)
    if not path.is_file():
        raise FigaroError(f"JSON-stat file not found: {path}")

    class _Stream:
        def __init__(self):
            self.handle = path.open("rb")
            self.buffer = b""

        def ensure(self, size: int) -> None:
            while len(self.buffer) < size:
                block = self.handle.read(chunk_size)
                if not block:
                    return
                self.buffer += block

        def peek(self) -> bytes:
            self.ensure(1)
            return self.buffer[:1]

        def take(self, count: int = 1) -> bytes:
            self.ensure(count)
            if len(self.buffer) < count:
                raise FigaroError("unexpected end of JSON-stat file in the value container")
            piece, self.buffer = self.buffer[:count], self.buffer[count:]
            return piece

        def skip_ws(self) -> None:
            while True:
                char = self.peek()
                if not char:
                    return
                if char in b" \t\r\n":
                    self.buffer = self.buffer[1:]
                    continue
                return

        def skip_to_value_container(self) -> str:
            """Advance past ``"value" :`` and return ``"{"`` or ``"["``."""
            marker = b'"value"'
            while marker not in self.buffer:
                block = self.handle.read(chunk_size)
                if not block:
                    raise FigaroError("no \"value\" member in the JSON-stat file")
                self.buffer += block
            position = self.buffer.index(marker) + len(marker)
            self.buffer = self.buffer[position:]
            self.skip_ws()
            if self.take(1) != b":":
                raise FigaroError("malformed JSON-stat: ':' expected after \"value\"")
            self.skip_ws()
            opener = self.take(1)
            if opener not in (b"{", b"["):
                raise FigaroError("JSON-stat \"value\" must be an object or an array")
            return opener.decode()

        def read_string(self) -> str:
            if self.take(1) != b'"':
                raise FigaroError("malformed JSON-stat: expected a quoted key")
            pieces = []
            while True:
                char = self.take(1)
                if char == b'"':
                    return "".join(pieces)
                if char == b"\\":
                    pieces.append(self.take(1).decode("latin-1"))
                else:
                    pieces.append(char.decode("utf-8", "replace"))

        def read_number(self) -> float:
            digits = bytearray()
            while True:
                char = self.peek()
                if not char:
                    break
                if char in b"+-0123456789.eE":
                    digits += self.take(1)
                    continue
                break
            if not digits:
                raise FigaroError(f"malformed JSON-stat number near {self.buffer[:20]!r}")
            return float(digits.decode())

    stream = _Stream()
    try:
        opener = stream.skip_to_value_container()
        if opener == "{":
            while True:
                stream.skip_ws()
                char = stream.peek()
                if not char:
                    raise FigaroError("unterminated JSON-stat value object")
                if char == b"}":
                    stream.take(1)
                    return
                if char == b",":
                    stream.take(1)
                    continue
                key = int(stream.read_string())
                stream.skip_ws()
                if stream.take(1) != b":":
                    raise FigaroError("malformed JSON-stat: ':' expected inside value")
                stream.skip_ws()
                value = stream.read_number()
                yield key, value
        else:
            index = 0
            while True:
                stream.skip_ws()
                char = stream.peek()
                if not char:
                    raise FigaroError("unterminated JSON-stat value array")
                if char == b"]":
                    stream.take(1)
                    return
                if char == b",":
                    stream.take(1)
                    continue
                value = stream.read_number()
                yield index, value
                index += 1
    finally:
        stream.handle.close()


def parse_jsonstat_document(document) -> dict:
    """Normalise one JSON-stat response into ids/sizes/category indexes."""
    if not isinstance(document, dict) or "id" not in document or "value" not in document:
        raise FigaroError("not a JSON-stat dataset document")
    ids = list(document["id"])
    sizes = [int(size) for size in document["size"]]
    if len(ids) != len(sizes):
        raise FigaroError("JSON-stat id/size length mismatch")
    indexes, labels = {}, {}
    for dim in ids:
        category = document["dimension"][dim]["category"]
        index = category.get("index")
        if isinstance(index, dict):
            index = {str(key): int(value) for key, value in index.items()}
        elif isinstance(index, list):
            index = {str(key): position for position, key in enumerate(index)}
        else:
            raise FigaroError(f"JSON-stat dimension {dim!r} has no index")
        if sorted(index.values()) != list(range(len(index))):
            raise FigaroError(f"JSON-stat dimension {dim!r} has a sparse index")
        indexes[dim] = dict(sorted(index.items(), key=lambda item: item[1]))
        labels[dim] = dict(category.get("label") or {})
    return {"ids": ids, "sizes": sizes, "indexes": indexes, "labels": labels,
            "value": document.get("value") or {}}


# ----------------------------------------------------------------------------------- downloading
def fetch_meta(root: Path, year: int, force: bool = False) -> dict:
    """Small probe request that returns the full country/product category lists."""
    cache_dir = Path(root) / "cache" / str(year)
    path = cache_dir / "_meta.json"
    if path.is_file() and not force:
        document = _read_json(path)
    else:
        url = api_url(year) + "&" + urllib.parse.urlencode(
            {"prd_use": PROBE_PRODUCT, "prd_ava": PROBE_PRODUCT})
        raw = _http_get(url, timeout=120.0)
        document = json.loads(raw)
        _write_json(path, document)
    parsed = parse_jsonstat_document(document)
    meta = {
        "dataset": DATASET_CODE,
        "year": int(year),
        "url": api_url(year),
        "destinations": list(parsed["indexes"]["c_dest"]),
        "origins": list(parsed["indexes"]["c_orig"]),
        "products_used": list(parsed["indexes"]["prd_use"]),
        "products_available": list(parsed["indexes"]["prd_ava"]),
        "times": list(parsed["indexes"]["time"]),
        "units": list(parsed["indexes"]["unit"]),
    }
    return meta


def fetch_dest_worker(args) -> dict:
    """Worker: download one destination-country slice of the inter-country matrix."""
    root, year, dest, force, timeout = args
    cache_path = Path(root) / "cache" / str(year) / f"{dest}.json"
    started = time.monotonic()
    if cache_path.is_file() and not force:
        return {"dest": dest, "cached": True, "bytes": cache_path.stat().st_size,
                "seconds": 0.0, "sha256": sha256_file(cache_path)}
    raw = _http_get(api_url(year, dest), timeout=timeout)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(raw)
    return {"dest": dest, "cached": False, "bytes": len(raw),
            "seconds": round(time.monotonic() - started, 2),
            "sha256": hashlib.sha256(raw).hexdigest()}


def download_year(root: Path, meta: dict, jobs: int, force: bool = False,
                  timeout: float = DEFAULT_TIMEOUT) -> list[dict]:
    """Download every destination slice with a process pool; returns per-file summaries."""
    if jobs < 1:
        raise FigaroError("--jobs must be positive")
    destinations = meta["destinations"]
    tasks = [(str(root), int(meta["year"]), dest, force, timeout) for dest in destinations]
    results = []
    workers = max(1, min(int(jobs), len(tasks)))
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_dest_worker, task): task[2] for task in tasks}
        for done, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            results.append(result)
            print(f"[download] {done}/{len(tasks)} c_dest={result['dest']} "
                  f"{result['bytes'] / 1e6:.2f} MB {result['seconds']:.1f}s", flush=True)
    return sorted(results, key=lambda item: destinations.index(item["dest"]))


# -------------------------------------------------------------------------------------- parsing
def parse_dest_worker(path: str) -> dict:
    """Worker: parse one cached destination slice into a dense (origin, ava, use) array."""
    parsed = parse_jsonstat_document(_read_json(Path(path)))
    ids, sizes = parsed["ids"], parsed["sizes"]
    if ids != ["freq", "prd_use", "prd_ava", "c_dest", "unit", "c_orig", "time"]:
        raise FigaroError(f"unexpected JSON-stat dimension order: {ids}")
    if sizes[3] != 1 or sizes[0] != 1 or sizes[4] != 1 or sizes[6] != 1:
        raise FigaroError(f"expected a single destination/unit/time slice, got sizes {sizes}")
    origins = list(parsed["indexes"]["c_orig"])
    products_used = list(parsed["indexes"]["prd_use"])
    products_available = list(parsed["indexes"]["prd_ava"])
    dimensions = {"origins": origins, "products_used": products_used,
                  "products_available": products_available,
                  "destinations": list(parsed["indexes"]["c_dest"])}
    array = np.zeros((len(origins), len(products_available), len(products_used)),
                     dtype=np.float64)
    values = parsed["value"]
    if isinstance(values, dict):
        items = ((int(key), float(value)) for key, value in values.items())
    else:  # dense list form
        items = ((index, float(value)) for index, value in enumerate(values))
    for flat_index, value in items:
        _, use_index, ava_index, dest_index, _, orig_index, _ = jsonstat_coordinates(
            flat_index, sizes)
        if dest_index != 0:
            raise FigaroError("multi-destination slice in a per-destination file")
        array[orig_index, ava_index, use_index] = value
    return {"dest": dimensions["destinations"][0] if dimensions["destinations"] else None,
            "array": array, "dimensions": dimensions, "bytes": Path(path).stat().st_size}


def parse_year(root: Path, meta: dict, jobs: int) -> dict:
    """Parse every cached destination slice in parallel; returns a compact bundle."""
    cache_dir = Path(root) / "cache" / str(meta["year"])
    files = [cache_dir / f"{dest}.json" for dest in meta["destinations"]]
    missing = [path.name for path in files if not path.is_file()]
    if missing:
        raise FigaroError(f"cache is incomplete, missing {len(missing)} slices: {missing[:5]}")
    workers = max(1, min(int(jobs), len(files)))
    chunks = []
    started = time.monotonic()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(parse_dest_worker, str(path)): path for path in files}
        for done, future in enumerate(as_completed(futures), start=1):
            chunks.append(future.result())
            if done % 10 == 0 or done == len(files):
                print(f"[parse] {done}/{len(files)} slices", flush=True)
    print(f"[parse] done in {time.monotonic() - started:.1f}s", flush=True)
    return {"chunks": chunks, "seconds": round(time.monotonic() - started, 2)}


# -------------------------------------------------------------------------------------- assembly
def assemble_document(meta: dict, parsed: dict, usd_per_eur: float,
                      tolerance: float = DEFAULT_TOLERANCE) -> dict:
    """Turn parsed slices into the canonical ``amfe.mrio.dataset.v1`` document (USD_mn)."""
    if not math.isfinite(usd_per_eur) or usd_per_eur <= 0:
        raise FigaroError(f"invalid FX rate USD per EUR: {usd_per_eur!r}")
    if not parsed.get("chunks"):
        raise FigaroError("no parsed FIGARO slices to assemble")
    chunk_dimensions = parsed["chunks"][0].get("dimensions") or {}
    origins = list(chunk_dimensions.get("origins") or meta["origins"])
    products_used = list(chunk_dimensions.get("products_used") or meta["products_used"])
    products_available = list(chunk_dimensions.get("products_available")
                              or meta["products_available"])
    destinations = list(meta["destinations"])
    if DOM_CODE not in origins:
        raise FigaroError("FIGARO origin list has no DOM (domestic value added) block")
    if ROW_CODE not in destinations or ROW_CODE not in origins:
        raise FigaroError("FIGARO table has no WRL_REST (rest of world) block")
    if [code for code in origins if code != DOM_CODE] != destinations:
        raise FigaroError("origin and destination country lists differ beyond DOM")
    products = [code for code in products_used if code in products_available]
    final_codes = [code for code in products_used if code not in products]
    if not products or set(final_codes) != set(FINAL_DEMAND_CODES):
        raise FigaroError(f"unexpected FIGARO product/final-demand split: {final_codes}")
    available = set(products_available)
    if any(code not in available for code in VALUE_ADDED_CODES):
        raise FigaroError("FIGARO table misses one of the value-added rows")

    countries = [ROW_LABEL if code == ROW_CODE else code for code in destinations]
    industries = list(products)
    n_countries, n_industries = len(countries), len(industries)
    size = n_countries * n_industries
    origin_index = {code: position for position, code in enumerate(origins)}
    dest_index = {code: position for position, code in enumerate(destinations)}
    product_use_index = {code: position for position, code in enumerate(products_used)}
    product_ava_index = {code: position for position, code in enumerate(products_available)}
    final_positions = [product_use_index[code] for code in FINAL_DEMAND_CODES]
    va_positions = [product_ava_index[code] for code in VALUE_ADDED_CODES]
    product_use_positions = [product_use_index[code] for code in products]
    product_ava_positions = [product_ava_index[code] for code in products]

    Z = np.zeros((size, size), dtype=np.float64)
    y = np.zeros(size, dtype=np.float64)
    va = np.zeros(size, dtype=np.float64)
    dom_position = origin_index[DOM_CODE]
    for chunk in parsed["chunks"]:
        dest = chunk["dest"]
        if dest not in dest_index:
            raise FigaroError(f"parsed slice for unknown destination {dest!r}")
        array = np.asarray(chunk["array"], dtype=np.float64)
        expected_shape = (len(origins), len(products_available), len(products_used))
        if array.shape != expected_shape:
            raise FigaroError(f"slice {dest}: shape {array.shape} != {expected_shape}")
        block = array[:, product_ava_positions, :][:, :, product_use_positions]
        column = dest_index[dest] * n_industries
        for position, origin in enumerate(origins):
            if origin == DOM_CODE:
                continue
            row = dest_index[origin] * n_industries

            Z[row:row + n_industries, column:column + n_industries] += block[position]
            final_values = array[position][np.ix_(product_ava_positions, final_positions)]
            y[row:row + n_industries] += final_values.sum(axis=1)
        domestic_va = array[dom_position][np.ix_(va_positions, product_use_positions)].sum(axis=0)
        va[column:column + n_industries] += domestic_va

    # Canonical schema requires y, x, va >= 0.  FIGARO carries negative changes in inventories
    # (and possibly negative value-added residuals) at (country, product) level; shift such cells
    # by d = max(0, -y, -va) into y, va and x together, which keeps every accounting identity.
    non_negativity_shift = float(np.maximum(0.0, np.maximum(-y, -va)).sum())
    shift = np.maximum(0.0, np.maximum(-y, -va))
    y = y + shift
    va = va + shift
    x = Z.sum(axis=1) + y
    Z = np.round(Z * usd_per_eur, 6)
    y = np.round(y * usd_per_eur, 6)
    x = np.round(Z.sum(axis=1) + y, 6)
    # Final rounding reconciliation: value added of a column is the row output total minus the
    # rounded intermediate column, so that the stored JSON satisfies x = 1'Z + va at the default
    # 1e-6 relative tolerance of the canonical schema as well.
    va_source = np.round(va * usd_per_eur, 6)
    va_reconciled = np.round(x - Z.sum(axis=0), 6)
    column_reconciliation = float(np.abs(va_reconciled - va_source).sum())
    # A tiny negative reconciled value added (rounding) is shifted into x and y, which keeps both
    # identities and the global identity and avoids a negative `va` in the stored document.
    for _ in range(3):
        negative_va = np.maximum(0.0, -va_reconciled)
        if not negative_va.any():
            break
        y = np.round(y + negative_va, 6)
        x = np.round(Z.sum(axis=1) + y, 6)
        va_reconciled = np.round(x - Z.sum(axis=0), 6)
    va = np.maximum(va_reconciled, 0.0)

    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "countries": countries,
        "industries": industries,
        "years": [int(meta["year"])],
        "units": CANONICAL_UNITS,
        "Z": Z.tolist(),
        "y": y.tolist(),
        "x": x.tolist(),
        "va": va.tolist(),
        "source": {
            "name": "Eurostat FIGARO - EU inter-country input-output table (product by product)",
            "provider": "Eurostat",
            "dataset": DATASET_CODE,
            "url": meta["url"],
            "year": int(meta["year"]),
            "licence": "Eurostat re-use policy (open data, attribution)",
            "third_party": True,
            "row_block": f"{ROW_CODE} -> {ROW_LABEL}",
            "non_negativity_shift_usd_mn": round(non_negativity_shift * usd_per_eur, 3),
            "column_reconciliation_usd_mn": round(column_reconciliation, 3),
            "retrieved_at": _utc_now(),
            "value_base": "EUR million, basic prices; converted to USD million with the World Bank "
                          "official exchange rate (period average)",
        },
        "fx": {"EUR_per_USD": round(1.0 / usd_per_eur, 9),
               "USD_per_EUR": round(usd_per_eur, 9),
               "source": "World Bank PA.NUS.FCRF, euro area (EMU), period average",
               "year": int(meta["year"])},
        "prices": {},
        "tolerance": float(tolerance),
        "notes": "FIGARO NAIO_10_FCP_IP3; WRL_REST is carried as the ROW block; DOM is not a "
                 "country (domestic value-added rows); negative inventory/value-added cells are "
                 "shifted into y/va/x by the recorded non-negativity shift. Third-party data: "
                 "never commit.",
    }


# ----------------------------------------------------------------------------- sparse storage
SPARSE_KEY = "Z_sparse"


def _read_json_array_stream(handle, initial: bytes, key: str, floating: bool):
    """Read one flat JSON number array (``"key": [ ... ]``) from a binary stream."""
    marker = b'"' + key.encode("ascii") + b'"'
    buffer = initial
    while marker not in buffer:
        block = handle.read(1 << 20)
        if not block:
            raise FigaroError(f"JSON array {key!r} not found")
        buffer += block
    buffer = buffer[buffer.index(marker) + len(marker):]
    while b"[" not in buffer:
        block = handle.read(1 << 20)
        if not block:
            raise FigaroError(f"JSON array {key!r} has no opening bracket")
        buffer += block
    buffer = buffer[buffer.index(b"[") + 1:]
    chunks = []
    while True:
        if b"]" in buffer:
            position = buffer.index(b"]")
            chunks.append(buffer[:position])
            buffer = buffer[position + 1:]
            break
        chunks.append(buffer)
        block = handle.read(1 << 20)
        if not block:
            raise FigaroError(f"JSON array {key!r} is not terminated")
        buffer = block
    raw = b"".join(chunks).strip()
    if not raw:
        return np.zeros(0, dtype=np.float64 if floating else np.int64), buffer
    text = raw.decode("ascii")
    if floating:
        return np.fromstring(text, sep=",", dtype=np.float64), buffer
    return np.fromstring(text, sep=",", dtype=np.float64).astype(np.int64), buffer


def read_sparse_document(path: Path) -> tuple[dict, dict]:
    """Stream ``Z_sparse`` arrays and metadata out of a sparse dataset file.

    The writer puts ``Z_sparse`` first, so the three large arrays are read in bounded chunks
    (one array at a time) and only then the small metadata block is decoded.
    """
    path = Path(path)
    if not path.is_file():
        raise FigaroError(f"dataset file not found: {path}")
    with path.open("rb") as handle:
        buffer = handle.read(4096)
        while b'"Z_sparse"' not in buffer:
            block = handle.read(1 << 20)
            if not block:
                raise FigaroError(f"{path}: no Z_sparse block")
            buffer += block
        buffer = buffer[buffer.index(b'"Z_sparse"'):]
        arrays = {}
        for key, floating in (("rows", False), ("cols", False), ("values", True)):
            array, buffer = _read_json_array_stream(handle, buffer, key, floating)
            arrays[key] = array
        rest = buffer
        while True:
            block = handle.read(1 << 20)
            if not block:
                break
            rest += block
    rest = rest.lstrip()
    if rest.startswith(b"}"):
        rest = rest[1:].lstrip()
    if rest.startswith(b","):
        rest = rest[1:].lstrip()
    try:
        metadata = json.loads(b"{" + rest)
    except ValueError as exc:
        raise FigaroError(f"{path}: broken metadata after Z_sparse: {exc!r}") from exc
    return metadata, arrays


def is_sparse_file(path: Path) -> bool:
    """Cheap format probe: the writer puts ``Z_sparse`` in the first bytes of the file."""
    try:
        with Path(path).open("rb") as handle:
            return b'"Z_sparse"' in handle.read(4096)
    except OSError:
        return False


def _layout(meta: dict, parsed: dict) -> dict:
    """Resolve and check the FIGARO dimension layout shared by the dense and sparse assemblers."""
    if not parsed.get("chunks"):
        raise FigaroError("no parsed FIGARO slices to assemble")
    chunk_dimensions = parsed["chunks"][0].get("dimensions") or {}
    origins = list(chunk_dimensions.get("origins") or meta["origins"])
    products_used = list(chunk_dimensions.get("products_used") or meta["products_used"])
    products_available = list(chunk_dimensions.get("products_available")
                              or meta["products_available"])
    destinations = list(meta["destinations"])
    if DOM_CODE not in origins:
        raise FigaroError("FIGARO origin list has no DOM (domestic value added) block")
    if ROW_CODE not in destinations or ROW_CODE not in origins:
        raise FigaroError("FIGARO table has no WRL_REST (rest of world) block")
    if [code for code in origins if code != DOM_CODE] != destinations:
        raise FigaroError("origin and destination country lists differ beyond DOM")
    products = [code for code in products_used if code in products_available]
    final_codes = [code for code in products_used if code not in products]
    if not products or set(final_codes) != set(FINAL_DEMAND_CODES):
        raise FigaroError(f"unexpected FIGARO product/final-demand split: {final_codes}")
    available = set(products_available)
    if any(code not in available for code in VALUE_ADDED_CODES):
        raise FigaroError("FIGARO table misses one of the value-added rows")
    countries = [ROW_LABEL if code == ROW_CODE else code for code in destinations]
    industries = list(products)
    return {
        "origins": origins, "destinations": destinations, "countries": countries,
        "industries": industries,
        "product_use_index": {code: i for i, code in enumerate(products_used)},
        "product_ava_index": {code: i for i, code in enumerate(products_available)},
        "dest_index": {code: i for i, code in enumerate(destinations)},
        "origin_index": {code: i for i, code in enumerate(origins)},
        "final_positions": [products_used.index(code) for code in FINAL_DEMAND_CODES],
        "va_positions": [products_available.index(code) for code in VALUE_ADDED_CODES],
        "product_use_positions": [products_used.index(code) for code in products],
        "product_ava_positions": [products_available.index(code) for code in products],
        "n_countries": len(countries), "n_industries": len(industries),
        "n": len(countries) * len(industries),
    }


def assemble_sparse_document(meta: dict, parsed: dict, usd_per_eur: float,
                             tolerance: float = DEFAULT_TOLERANCE) -> dict:
    """Assemble the canonical document with a **sparse** ``Z_sparse`` block (no dense matrix).

    Triplets ``(row, col, value)`` are accumulated into preallocated arrays while the
    per-destination chunks are consumed, so the peak working set is O(nnz), not O(n^2) Python
    floats.  ``x``, ``y`` and ``va`` are vectors; the accounting identities are enforced on the
    vectors and the sparse triplets (see :func:`validate_sparse_document`).
    """
    if not math.isfinite(usd_per_eur) or usd_per_eur <= 0:
        raise FigaroError(f"invalid FX rate USD per EUR: {usd_per_eur!r}")
    layout = _layout(meta, parsed)
    origins = layout["origins"]
    destinations = layout["destinations"]
    countries = layout["countries"]
    industries = layout["industries"]
    n_countries, n_industries = layout["n_countries"], layout["n_industries"]
    n = layout["n"]
    dest_index = layout["dest_index"]
    ava_positions = layout["product_ava_positions"]
    use_positions = layout["product_use_positions"]
    final_positions = layout["final_positions"]
    va_positions = layout["va_positions"]
    dom_position = layout["origin_index"][DOM_CODE]
    n_final, n_va = len(final_positions), len(va_positions)

    capacity = n * n
    rows = np.empty(capacity, dtype=np.int32)
    cols = np.empty(capacity, dtype=np.int32)
    values = np.empty(capacity, dtype=np.float64)
    cursor = 0
    y_eur = np.zeros(n, dtype=np.float64)
    va_eur = np.zeros(n, dtype=np.float64)
    expected_shape = (len(origins), len(layout["product_ava_index"]),
                      len(layout["product_use_index"]))
    for chunk in parsed["chunks"]:
        dest = chunk["dest"]
        if dest not in dest_index:
            raise FigaroError(f"parsed slice for unknown destination {dest!r}")
        array = np.asarray(chunk["array"], dtype=np.float64)
        if array.shape != expected_shape:
            raise FigaroError(f"slice {dest}: shape {array.shape} != {expected_shape}")
        block = array[:, ava_positions, :][:, :, use_positions]
        column_base = dest_index[dest] * n_industries
        for position, origin in enumerate(origins):
            if origin == DOM_CODE:
                continue
            country_base = dest_index[origin] * n_industries
            sub = block[position]
            ava_idx, use_idx = np.nonzero(sub)
            count = ava_idx.size
            if count:
                if cursor + count > capacity:  # pragma: no cover - n^2 is the exact bound
                    raise FigaroError("sparse accumulation overflow")
                rows[cursor:cursor + count] = country_base + ava_idx
                cols[cursor:cursor + count] = column_base + use_idx
                values[cursor:cursor + count] = sub[ava_idx, use_idx]
                cursor += count
            final = array[position][np.ix_(ava_positions, final_positions)]
            if n_final:
                y_eur[country_base:country_base + n_industries] += final.sum(axis=1)
        domestic = array[dom_position][np.ix_(va_positions, use_positions)]
        if n_va:
            va_eur[column_base:column_base + n_industries] += domestic.sum(axis=0)

    rows = rows[:cursor]
    cols = cols[:cursor]
    values = values[:cursor]
    shift = np.maximum(0.0, np.maximum(-y_eur, -va_eur))
    non_negativity_shift = float(shift.sum())
    y_eur = y_eur + shift
    va_eur = va_eur + shift

    values = np.round(values * usd_per_eur, 6)
    keep = values != 0.0
    rows, cols, values = rows[keep], cols[keep], values[keep]
    y = np.round(y_eur * usd_per_eur, 6)
    va_source = np.round(va_eur * usd_per_eur, 6)
    row_sum = np.bincount(rows, weights=values, minlength=n)
    column_sum = np.bincount(cols, weights=values, minlength=n)
    x = np.round(row_sum + y, 6)
    va_reconciled = np.round(x - column_sum, 6)
    column_reconciliation = float(np.abs(va_reconciled - va_source).sum())
    for _ in range(3):
        negative_va = np.maximum(0.0, -va_reconciled)
        if not negative_va.any():
            break
        y = np.round(y + negative_va, 6)
        x = np.round(row_sum + y, 6)
        va_reconciled = np.round(x - column_sum, 6)
    va = np.maximum(va_reconciled, 0.0)

    return {
        "Z_sparse": {"rows": rows, "cols": cols, "values": values},
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "countries": countries,
        "industries": industries,
        "years": [int(meta["year"])],
        "units": CANONICAL_UNITS,
        "y": y,
        "x": x,
        "va": va,
        "source": {
            "name": "Eurostat FIGARO - EU inter-country input-output table (product by product)",
            "provider": "Eurostat",
            "dataset": DATASET_CODE,
            "url": meta["url"],
            "year": int(meta["year"]),
            "licence": "Eurostat re-use policy (open data, attribution)",
            "third_party": True,
            "row_block": f"{ROW_CODE} -> {ROW_LABEL}",
            "storage": SPARSE_KEY,
            "non_negativity_shift_usd_mn": round(non_negativity_shift * usd_per_eur, 3),
            "column_reconciliation_usd_mn": round(column_reconciliation, 3),
            "retrieved_at": _utc_now(),
            "value_base": "EUR million, basic prices; converted to USD million with the World Bank "
                          "official exchange rate (period average)",
        },
        "fx": {"EUR_per_USD": round(1.0 / usd_per_eur, 9),
               "USD_per_EUR": round(usd_per_eur, 9),
               "source": "World Bank PA.NUS.FCRF, euro area (EMU), period average",
               "year": int(meta["year"])},
        "prices": {},
        "tolerance": float(tolerance),
        "notes": "FIGARO NAIO_10_FCP_IP3; WRL_REST is carried as the ROW block; DOM is not a "
                 "country (domestic value-added rows); negative inventory/value-added cells are "
                 "shifted into y/va/x by the recorded non-negativity shift; Z is stored as sparse "
                 "triplets in Z_sparse {rows, cols, values}. Third-party data: never commit.",
    }


def _sparse_arrays(document: dict):
    if SPARSE_KEY not in document:
        raise FigaroError("document has no Z_sparse block")
    block = document[SPARSE_KEY]
    try:
        rows = np.asarray(block["rows"], dtype=np.int64)
        cols = np.asarray(block["cols"], dtype=np.int64)
        values = np.asarray(block["values"], dtype=np.float64)
    except (KeyError, TypeError, ValueError) as exc:
        raise FigaroError(f"malformed Z_sparse block: {exc!r}") from exc
    if not (rows.size == cols.size == values.size):
        raise FigaroError("Z_sparse rows/cols/values must have the same length")
    n = len(np.asarray(document["x"]))
    if rows.size and (int(rows.min()) < 0 or int(rows.max()) >= n
                      or int(cols.min()) < 0 or int(cols.max()) >= n):
        raise FigaroError("Z_sparse index outside the matrix")
    if not np.all(np.isfinite(values)):
        raise FigaroError("Z_sparse values must be finite")
    return rows, cols, values, n


def validate_sparse_document(document: dict, enforce_p2_scale: bool = True,
                             tolerance: float | None = None) -> dict:
    """Validate a sparse canonical document (same identities as ``amfe.mrio.schema``)."""
    rows, cols, values, n = _sparse_arrays(document)
    tol = float(document.get("tolerance", DEFAULT_TOLERANCE) if tolerance is None else tolerance)
    if tol <= 0:
        raise FigaroError("tolerance must be positive")
    y = np.asarray(document["y"], dtype=np.float64)
    x = np.asarray(document["x"], dtype=np.float64)
    va = np.asarray(document["va"], dtype=np.float64)
    countries = tuple(document["countries"])
    industries = tuple(document["industries"])
    errors: list[str] = []
    checks: dict = {"schema": True, "dimensions": True, "labels_unique": True}
    if not (y.size == x.size == va.size == n):
        errors.append(f"vector lengths differ from n={n}")
        checks["dimensions"] = False
    if len(set(countries)) != len(countries) or len(set(industries)) != len(industries):
        errors.append("country/industry labels are not unique")
        checks["labels_unique"] = False
    checks["non_negative"] = bool(
        (values.size == 0 or float(values.min()) >= -tol)
        and float(y.min()) >= -tol and float(x.min()) >= -tol and float(va.min()) >= -tol)
    if not checks["non_negative"]:
        errors.append("negative Z/y/x/va value below tolerance")
    row_residual = np.bincount(rows, weights=values, minlength=n) + y - x
    column_residual = np.bincount(cols, weights=values, minlength=n) + va - x
    row_scale = np.maximum(1.0, np.abs(x))
    bad_rows = np.flatnonzero(np.abs(row_residual) > tol * row_scale)
    bad_columns = np.flatnonzero(np.abs(column_residual) > tol * row_scale)
    checks["row_balance"] = bad_rows.size == 0
    checks["column_balance"] = bad_columns.size == 0
    if bad_rows.size:
        errors.append(f"row balance violated at {bad_rows.size} sectors")
    if bad_columns.size:
        errors.append(f"column balance violated at {bad_columns.size} sectors")
    total_va, total_y, total_x = float(va.sum()), float(y.sum()), float(x.sum())
    total_z = float(values.sum())
    va_scale = max(1.0, abs(total_va), abs(total_y))
    global_ok = abs(total_va - total_y) <= tol * va_scale
    aggregate_ok = abs(total_x - (total_z + total_y)) <= tol * max(1.0, abs(total_x))
    checks["global_value_added_equals_final_demand"] = global_ok
    checks["aggregate_output_identity"] = aggregate_ok
    if not global_ok:
        errors.append(f"global identity violated: sum(va)={total_va:.6f} != sum(y)={total_y:.6f}")
    if not aggregate_ok:
        errors.append("aggregate output identity violated")
    p2_scale = bool(len(countries) >= 10 and len(industries) >= 30)
    checks["p2_scale"] = p2_scale
    if not p2_scale:
        message = (f"P2 scale gate: {len(countries)} countries / {len(industries)} industries "
                   f"(need 10+ x 30+)")
        if enforce_p2_scale:
            errors.append(message)
    metrics = {
        "countries": len(countries), "industries": len(industries), "n": n,
        "years": list(document.get("years", [])), "units": document.get("units"),
        "cells_Z": n * n, "stored_Z": int(values.size),
        "stored_share": round(float(values.size) / float(n * n), 6) if n else 0.0,
        "max_abs_row_residual": round(float(np.max(np.abs(row_residual))) if n else 0.0, 9),
        "max_abs_column_residual": round(float(np.max(np.abs(column_residual))) if n else 0.0, 9),
        "max_rel_row_residual": round(float(np.max(np.abs(row_residual) / row_scale)) if n else 0.0, 9),
        "total_output": round(total_x, 6),
        "total_intermediate": round(total_z, 6),
        "total_final_demand": round(total_y, 6),
        "total_value_added": round(total_va, 6),
        "global_difference_va_minus_y": round(total_va - total_y, 9),
        "global_difference_output": round(total_x - (total_z + total_y), 9),
        "tolerance": tol,
    }
    return {"ok": not errors, "checks": checks, "metrics": metrics,
            "errors": list(errors), "warnings": []}


def _write_json_numbers(handle, array, floating: bool, step: int = 20000) -> None:
    for start in range(0, array.size, step):
        piece = array[start:start + step]
        if start:
            handle.write(",")
        if floating:
            # shortest round-trip repr: deterministic and lossless (the values are already
            # rounded to 6 decimals by the assembler, so this stays compact for FIGARO)
            handle.write(",".join(repr(float(value)) for value in piece))
        else:
            handle.write(",".join(str(int(value)) for value in piece))


def write_sparse_dataset(document: dict, path: Path) -> str:
    """Write a sparse canonical dataset deterministically and return the SHA-256 of the bytes.

    The large arrays are streamed in bounded slices; metadata follows the arrays, so a reader can
    stream ``Z_sparse`` first and decode the (small) metadata block afterwards.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows, cols, values = (document[SPARSE_KEY]["rows"], document[SPARSE_KEY]["cols"],
                          document[SPARSE_KEY]["values"])
    metadata_keys = ["schema", "schema_version", "countries", "industries", "years", "units",
                     "y", "x", "va", "source", "fx", "prices", "tolerance", "notes"]
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write('{\n "Z_sparse": {\n')
        for index, (key, array, floating) in enumerate((("rows", rows, False),
                                                        ("cols", cols, False),
                                                        ("values", values, True))):
            handle.write(f'  "{key}": [')
            _write_json_numbers(handle, np.asarray(array), floating)
            handle.write("]" + ("," if index < 2 else "") + "\n")
        handle.write(" }")
        for key in metadata_keys:
            if key not in document:
                continue
            value = document[key]
            if isinstance(value, np.ndarray):
                value = value.tolist()
            handle.write(",\n " + json.dumps(key) + ": "
                         + json.dumps(value, ensure_ascii=False, sort_keys=True))
        handle.write("\n}\n")
    return sha256_file(path)


def write_sparse_pack(root: Path, document: dict, dataset_id: str,
                      extra_manifest: dict | None = None,
                      enforce_p2_scale: bool = True) -> dict:
    """Validate, write the sparse dataset + manifest and return aggregate metadata."""
    root = Path(root)
    out_dir = root / dataset_id
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    report = validate_sparse_document(document, enforce_p2_scale=enforce_p2_scale)
    if not report["ok"]:
        raise FigaroError("sparse dataset failed validation: "
                          + "; ".join(report["errors"][:5]))
    dataset_path = out_dir / "dataset.json"
    dataset_sha = write_sparse_dataset(document, dataset_path)
    size = dataset_path.stat().st_size
    write_seconds = round(time.monotonic() - started, 2)
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "generated_at": _utc_now(),
        "status": "AVAILABLE",
        "p2_requirements": {"min_countries": 10, "min_industries": 30, "units": CANONICAL_UNITS,
                            "schema": SCHEMA},
        "dataset": {
            "path": f"{dataset_id}/dataset.json",
            "sha256": dataset_sha,
            "bytes": size,
            "digest": dataset_sha,
            "storage": SPARSE_KEY,
            "countries": report["metrics"]["countries"],
            "industries": report["metrics"]["industries"],
            "years": report["metrics"]["years"],
            "units": report["metrics"]["units"],
            "cells_Z": report["metrics"]["cells_Z"],
            "stored_Z": report["metrics"]["stored_Z"],
            "stored_share": report["metrics"]["stored_share"],
        },
        "validation": report,
        "write_seconds": write_seconds,
    }
    if extra_manifest:
        manifest.update(extra_manifest)
    manifest_path = out_dir / "manifest.json"
    manifest_sha = write_manifest(manifest, manifest_path)
    return {"dataset_path": str(dataset_path), "dataset_sha256": dataset_sha,
            "dataset_bytes": size, "manifest_path": str(manifest_path),
            "manifest_sha256": manifest_sha, "dataset_id": dataset_id,
            "write_seconds": write_seconds, "manifest": manifest,
            "validation": report}


def _sparse_summary(document: dict, validation: dict) -> dict:
    metrics = validation["metrics"]
    return {
        "status": "OK",
        "dataset_id": None,
        "storage": SPARSE_KEY,
        "coverage_named_pct": None,
        "coverage_with_row_pct": None,
        "countries": metrics["countries"],
        "industries": metrics["industries"],
        "year": int(document["years"][0]),
        "dataset_bytes": None,
        "dataset_sha256": None,
        "stored_Z": metrics["stored_Z"],
        "stored_share": metrics["stored_share"],
        "validation_ok": validation["ok"],
        "max_rel_row_residual": metrics["max_rel_row_residual"],
        "max_abs_column_residual": metrics["max_abs_column_residual"],
    }



# --------------------------------------------------------------------------- GDP, FX, coverage
def fetch_worldbank(url: str, year: int, path: Path, force: bool = False) -> dict:
    """Fetch one World Bank indicator response and cache it as JSON."""
    path = Path(path)
    if path.is_file() and not force:
        return _read_json(path)
    query = f"{url}?format=json&per_page=400&date={int(year)}"
    raw = _http_get(query, timeout=120.0)
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise FigaroError(f"World Bank response is not JSON: {exc!r}") from exc
    if not isinstance(document, list) or len(document) < 2 or not isinstance(document[1], list):
        raise FigaroError(f"unexpected World Bank response for {url}")
    _write_json(path, document)
    return document


def parse_worldbank_gdp(document: dict, year: int) -> dict:
    """ISO2 -> GDP (current US$) plus the World aggregate."""
    rows = document[1] if isinstance(document, list) and len(document) >= 2 else []
    gdp, world = {}, None
    for row in rows:
        if not isinstance(row, dict) or row.get("value") is None:
            continue
        country = row.get("country") or {}
        code = country.get("id")
        value = float(row["value"])
        gdp[str(code)] = value
        if code == WORLD_BANK_WORLD_ID or (country.get("value") or "").strip().lower() == "world":
            world = value
    if world is None:
        raise FigaroError(f"World Bank GDP is missing the World aggregate for {year}")
    return {"year": int(year), "gdp_usd": gdp, "world_usd": float(world),
            "source": "World Bank NY.GDP.MKTP.CD (current US$)"}


def parse_worldbank_fx(document: dict, year: int) -> dict:
    """EUR/USD from the World Bank official exchange rate (LCU per USD, period average)."""
    rows = document[1] if isinstance(document, list) and len(document) >= 2 else []
    value = None
    for row in rows:
        if isinstance(row, dict) and row.get("value") is not None:
            value = float(row["value"])
            break
    if not value:
        raise FigaroError(f"World Bank FX is missing for euro area in {year}")
    return {"year": int(year), "lcu_per_usd": value, "usd_per_eur": 1.0 / value,
            "source": "World Bank PA.NUS.FCRF, euro area (EMU), period average"}


def wb_code(eurostat_code: str) -> str:
    return ISO2_FIX.get(eurostat_code, eurostat_code)


def gdp_coverage(countries, gdp_document: dict) -> dict:
    """Share of world GDP covered by the named FIGARO countries (ROW is reported separately)."""
    named = [code for code in countries if code != ROW_LABEL]
    gdp = gdp_document["gdp_usd"]
    world = gdp_document["world_usd"]
    covered, missing, per_country = 0.0, [], {}
    for code in named:
        value = gdp.get(wb_code(code))
        if value is None:
            missing.append(code)
        else:
            covered += value
            per_country[code] = value
    named_pct = 100.0 * covered / world if world else 0.0
    row_residual = 100.0 - named_pct
    return {
        "year": int(gdp_document["year"]),
        "source": gdp_document["source"],
        "world_gdp_usd": world,
        "named_countries": len(named),
        "named_countries_with_gdp": len(per_country),
        "covered_gdp_usd": covered,
        "named_share_pct": round(named_pct, 4),
        "row_share_pct": round(row_residual, 4),
        "coverage_with_row_pct": 100.0,
        "missing_gdp": missing,
        "per_country_gdp_usd": {code: round(value, 2) for code, value in sorted(per_country.items())},
        "named_only_min_80pct": named_pct >= 80.0,
        "p2_gate_min_80pct": named_pct >= 80.0,
    }


# --------------------------------------------------------------------------------------- output
def dataset_id_for(year: int) -> str:
    return f"figaro_{int(year)}"


def write_dataset_pack(root: Path, document: dict, dataset_id: str,
                       extra_manifest: dict | None = None,
                       enforce_p2_scale: bool = True) -> dict:
    """Validate, write dataset + manifest and return aggregate metadata (never raw values)."""
    root = Path(root)
    out_dir = root / dataset_id
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    dataset = dataset_from_document(document)
    report = validate_dataset(dataset, enforce_p2_scale=enforce_p2_scale)
    if not report.ok:
        raise FigaroError("dataset failed validation: " + "; ".join(report.errors[:5]))
    payload = dataset_to_bytes(dataset)
    dataset_path = out_dir / "dataset.json"
    dataset_path.write_bytes(payload)
    dataset_sha = hashlib.sha256(payload).hexdigest()
    write_seconds = round(time.monotonic() - started, 2)

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "generated_at": _utc_now(),
        "status": "AVAILABLE",
        "p2_requirements": {"min_countries": 10, "min_industries": 30, "units": CANONICAL_UNITS,
                            "schema": SCHEMA},
        "dataset": {
            "path": f"{dataset_id}/dataset.json",
            "sha256": dataset_sha,
            "bytes": len(payload),
            "digest": dataset.digest(),
            "countries": len(dataset.countries),
            "industries": len(dataset.industries),
            "years": list(dataset.years),
            "units": dataset.units,
            "cells_Z": dataset.n * dataset.n,
        },
        "validation": report.as_dict(),
        "write_seconds": write_seconds,
    }
    if extra_manifest:
        manifest.update(extra_manifest)
    manifest_path = out_dir / "manifest.json"
    manifest_sha = write_manifest(manifest, manifest_path)
    manifest["manifest_sha256"] = manifest_sha
    return {"dataset_path": str(dataset_path), "dataset_sha256": dataset_sha,
            "dataset_bytes": len(payload), "manifest_path": str(manifest_path),
            "manifest_sha256": manifest_sha, "dataset_id": dataset_id,
            "write_seconds": write_seconds, "manifest": manifest}


def peak_memory_mb() -> float:
    """Peak resident memory of this process in MB (psutil peak_wset if available)."""
    try:
        import psutil  # optional dependency, same as tools/world_pipeline.py
        info = psutil.Process(os.getpid()).memory_info()
        peak = getattr(info, "peak_wset", None) or getattr(info, "rss", 0)
        return round(float(peak) / (1024.0 * 1024.0), 1)
    except Exception:
        try:
            import tracemalloc
            current, peak = tracemalloc.get_traced_memory()
            return round(float(max(current, peak)) / (1024.0 * 1024.0), 1)
        except Exception:
            return 0.0


# ------------------------------------------------------------------------------------------- CLI
def _cache_paths(root: Path, year: int) -> tuple[Path, Path]:
    cache = Path(root) / "cache" / str(year)
    return cache / f"gdp_{year}.json", cache / f"fx_{year}.json"


def load_gdp_and_fx(root: Path, year: int, force: bool = False) -> tuple[dict, dict]:
    gdp_path, fx_path = _cache_paths(root, year)
    gdp_doc = fetch_worldbank(WB_GDP_URL, year, gdp_path, force=force)
    fx_doc = fetch_worldbank(WB_FX_URL, year, fx_path, force=force)
    return parse_worldbank_gdp(gdp_doc, year), parse_worldbank_fx(fx_doc, year)


def run(args) -> dict:
    root = Path(args.root)
    year = int(args.year)
    dataset_id = args.dataset_id or dataset_id_for(year)
    timings = {}
    started = time.monotonic()

    if args.coverage_only:
        meta = fetch_meta(root, year, force=args.force)
        gdp, _fx = load_gdp_and_fx(root, year, force=args.force)
        coverage = gdp_coverage(
            ["ROW" if code == ROW_CODE else code for code in meta["destinations"]], gdp)
        print(json.dumps({"mode": "coverage", "year": year, "coverage": coverage},
                         ensure_ascii=False, indent=1))
        return {"mode": "coverage", "year": year, "coverage": coverage}

    if args.validate_only:
        path = root / dataset_id / "dataset.json"
        if is_sparse_file(path):
            metadata, arrays = read_sparse_document(path)
            document = dict(metadata)
            document["Z_sparse"] = arrays
            validation = validate_sparse_document(document)
            if not validation["ok"]:
                raise FigaroError("sparse dataset failed validation: "
                                  + "; ".join(validation["errors"][:5]))
            summary = {"status": "OK", "storage": SPARSE_KEY, "dataset": {
                "schema": metadata.get("schema"),
                "countries": validation["metrics"]["countries"],
                "industries": validation["metrics"]["industries"],
                "years": validation["metrics"]["years"], "units": validation["metrics"]["units"],
                "stored_Z": validation["metrics"]["stored_Z"],
                "cells_Z": validation["metrics"]["cells_Z"],
                "max_rel_row_residual": validation["metrics"]["max_rel_row_residual"],
                "max_abs_column_residual": validation["metrics"]["max_abs_column_residual"]},
                "sha256": sha256_file(path), "bytes": path.stat().st_size}
            print(json.dumps(summary, ensure_ascii=False))
            return {"mode": "validate", "dataset_id": dataset_id, "summary": summary}
        from amfe.mrio.loaders import load_dataset
        loaded = load_dataset(dataset_id, root=root, strict=True)
        print(json.dumps({"status": "OK", "storage": "Z", "dataset": loaded.dataset.describe(),
                          "sha256": loaded.sha256, "bytes": loaded.size_bytes}, ensure_ascii=False))
        return {"mode": "validate", "dataset_id": dataset_id}

    started_meta = time.monotonic()
    meta = fetch_meta(root, year, force=args.force)
    timings["meta"] = round(time.monotonic() - started_meta, 2)
    if year not in [int(value) for value in meta["times"]]:
        raise FigaroError(f"{year} is not in FIGARO times {meta['times']}")

    if args.download or args.all:
        results = download_year(root, meta, jobs=args.jobs, force=args.force,
                                timeout=args.timeout)
        timings["download_seconds"] = round(sum(item["seconds"] for item in results), 2)
        timings["download_bytes"] = sum(item["bytes"] for item in results)

    if not (args.build or args.all):
        return {"mode": "download", "year": year, "meta": {
            "destinations": len(meta["destinations"]),
            "products": len(meta["products_used"]), "times": meta["times"]}, "timings": timings}

    gdp, fx = load_gdp_and_fx(root, year, force=args.force)
    parsed = parse_year(root, meta, jobs=args.jobs)
    timings["parse_seconds"] = parsed["seconds"]
    storage = str(getattr(args, "storage", "sparse"))
    extra = {
        "source": None,
        "fx": None,
        "coverage": None,
        "figaro": {"dataset": DATASET_CODE, "year": year,
                   "destinations": len(meta["destinations"]),
                   "domestic_value_added_rows": list(VALUE_ADDED_CODES),
                   "final_demand_columns": list(FINAL_DEMAND_CODES),
                   "row_block": f"{ROW_CODE} -> {ROW_LABEL}"},
        "cache": {"dir": f"cache/{year}", "files": len(meta["destinations"]) + 2}}
    validation_started = time.monotonic()
    if storage == "sparse":
        document = assemble_sparse_document(meta, parsed, usd_per_eur=fx["usd_per_eur"],
                                            tolerance=args.tolerance)
        extra.update({"source": document["source"], "fx": document["fx"],
                      "coverage": gdp_coverage(document["countries"], gdp),
                      "figaro": dict(extra["figaro"],
                                     products_used=len(document["industries"]),
                                     rows=len(document["industries"])
                                     + len(VALUE_ADDED_CODES) + 2)})
        result = write_sparse_pack(root, document, dataset_id, extra_manifest=extra)
        validation = result["validation"]
    else:
        document = assemble_document(meta, parsed, usd_per_eur=fx["usd_per_eur"],
                                     tolerance=args.tolerance)
        extra.update({"source": document["source"], "fx": document["fx"],
                      "coverage": gdp_coverage(document["countries"], gdp),
                      "figaro": dict(extra["figaro"],
                                     products_used=len(document["industries"]),
                                     rows=len(document["industries"])
                                     + len(VALUE_ADDED_CODES) + 2)})
        result = write_dataset_pack(root, document, dataset_id, extra_manifest=extra)
        validation = result["manifest"]["validation"]
    validation = result["manifest"]["validation"]
    timings["validation_seconds"] = round(time.monotonic() - validation_started, 2)
    timings["total_seconds"] = round(time.monotonic() - started, 2)
    result["timings"] = timings
    coverage = result["manifest"].get("coverage") or {}
    summary = {
        "status": "OK",
        "dataset_id": dataset_id,
        "storage": storage,
        "peak_memory_mb": peak_memory_mb(),
        "coverage_named_pct": coverage.get("named_share_pct"),
        "coverage_with_row_pct": coverage.get("coverage_with_row_pct"),
        "countries": len(document["countries"]),
        "industries": len(document["industries"]),
        "year": year,
        "dataset_bytes": result["dataset_bytes"],
        "dataset_sha256": result["dataset_sha256"],
        "validation_ok": validation["ok"],
        "max_rel_row_residual": validation["metrics"].get("max_rel_row_residual"),
        "max_abs_column_residual": validation["metrics"].get("max_abs_column_residual"),
        "timings": timings,
        "manifest_sha256": result["manifest_sha256"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    result["summary"] = summary
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="FIGARO inter-country IO -> canonical AMFE MRIO dataset (docs/125)")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="local MRIO root (git-ignored)")
    parser.add_argument("--year", type=int, default=DEFAULT_YEAR, choices=(2018, 2019, 2020, 2021))
    parser.add_argument("--dataset-id", default=None, help="default: figaro_<year>")
    parser.add_argument("--jobs", type=int, default=8, help="processes for download/parse")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE,
                        help="relative tolerance of the accounting identities")
    parser.add_argument("--force", action="store_true", help="ignore the download cache")
    parser.add_argument("--download", action="store_true", help="download slices only")
    parser.add_argument("--build", action="store_true", help="assemble from the cache")
    parser.add_argument("--all", action="store_true", help="download + build + validate + manifest")
    parser.add_argument("--coverage-only", action="store_true", help="GDP coverage report only")
    parser.add_argument("--validate-only", action="store_true", help="validate the written dataset")
    parser.add_argument("--storage", choices=("sparse", "dense"), default="sparse",
                        help="canonical Z storage: sparse Z_sparse block (default) or dense Z")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not any((args.download, args.build, args.all, args.coverage_only, args.validate_only)):
        args.all = True
    try:
        run(args)
    except (FigaroError, MRIOError, OSError) as error:
        print(json.dumps({"status": "BLOCKED_DATA", "error": str(error)}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
