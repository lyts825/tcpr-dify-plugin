"""Shared file-backed TCPR core.

The root package and the Dify plugin both call this module. Adapters only
provide a byte-oriented key/value backend and translate tool messages. The
user-authored logical index definition, data-file normalization, physical
index generation, query semantics, and snapshot activation live here as one
implementation.
"""

from __future__ import annotations

import csv
import base64
import binascii
import hashlib
import io
import json
import math
import os
import re
import tempfile
import time
import uuid
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol
from xml.etree import ElementTree as ET

from .engine import Retriever
from .index import InMemoryIndex
from .model import Hard, MissingValue, Soft, SparseProduct
from .parser import QueryError, parse_ast, parse_text
from .schema import FieldSpec, Schema, SchemaError


class CoreError(ValueError):
    """Deterministic user-facing error with a stable status code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class KVBackend(Protocol):
    def get(self, key: str) -> bytes | str | None: ...
    def put(self, key: str, value: bytes) -> None: ...


class InMemoryKV:
    """Small local backend used by the root API and tests."""

    def __init__(self):
        self.values: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    def put(self, key: str, value: bytes) -> None:
        if not isinstance(value, bytes):
            raise TypeError("KV values must be bytes")
        self.values[key] = value


class FileKV:
    """Atomic JSON-file KV backend for independent local CLI processes.

    Keys are encoded into URL-safe filenames, and values are base64 encoded
    inside JSON so no pickle or executable serialization is involved. Writes
    happen through a same-directory temporary file followed by ``os.replace``.
    """

    def __init__(self, directory: str | Path):
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _filename(key: str) -> str:
        if not isinstance(key, str) or not key:
            raise ValueError("KV key must be a non-empty string")
        encoded = base64.urlsafe_b64encode(key.encode("utf-8")).decode("ascii").rstrip("=")
        return encoded + ".json"

    def _path(self, key: str) -> Path:
        path = (self.directory / self._filename(key)).resolve()
        if path.parent != self.directory:
            raise ValueError("KV key resolved outside state directory")
        return path

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("key") != key or not isinstance(payload.get("value_b64"), str):
                raise ValueError
            return base64.b64decode(payload["value_b64"], validate=True)
        except (OSError, ValueError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
            raise CoreError("CORRUPT_STORAGE", f"invalid KV entry for key: {key}") from exc

    def put(self, key: str, value: bytes) -> None:
        if not isinstance(value, bytes):
            raise TypeError("KV values must be bytes")
        path = self._path(key)
        payload = json.dumps({
            "key": key,
            "value_b64": base64.b64encode(value).decode("ascii"),
        }, ensure_ascii=True, separators=(",", ":"))
        fd, temporary_name = tempfile.mkstemp(prefix=".tcpr-", suffix=".tmp", dir=str(self.directory))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

    set = put

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink()
        except FileNotFoundError:
            pass


DirectoryKV = FileKV


def _json_default(value: Any):
    if isinstance(value, MissingValue):
        return {"__tcpr_missing__": value.kind}
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _pack(value: Any) -> bytes:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default).encode("utf-8")
    import base64
    import gzip
    return base64.b64encode(gzip.compress(raw, compresslevel=6))


def _restore_missing(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"__tcpr_missing__"}:
            return MissingValue(str(value["__tcpr_missing__"]))
        return {key: _restore_missing(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore_missing(item) for item in value]
    return value


def _unpack(raw: bytes | str) -> Any:
    import base64
    import gzip
    encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
    try:
        value = json.loads(gzip.decompress(base64.b64decode(encoded)).decode("utf-8"))
    except Exception as exc:  # corrupted snapshots have one stable failure type
        raise CoreError("CORRUPT_RESOURCE", "stored TCPR snapshot is invalid") from exc
    # Keep the wire representation JSON-compatible.  Database attributes are
    # converted to MissingValue only at the typed retrieval boundary, while
    # index descriptions remain inspectable JSON objects.
    return value


class SnapshotStore:
    """Versioned resources with a final pointer write acting as activation."""

    def __init__(self, backend: KVBackend):
        self.backend = backend

    @staticmethod
    def _pointer(kind: str) -> str:
        return f"tcpr:core:active:{kind}"

    def active(self, kind: str) -> str | None:
        raw = self.backend.get(self._pointer(kind))
        if raw is None:
            return None
        return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)

    def write(self, kind: str, payload: dict[str, Any]) -> str:
        resource_id = f"{kind}-v1-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:12]}"
        base = f"tcpr:core:{kind}:{resource_id}"
        manifest = {
            "resource_id": resource_id,
            "kind": kind,
            "status": "staged",
            "created_at_epoch": time.time(),
            "schema_version": payload.get("schema_version", "tcpr-dynamic-v1"),
        }
        # No pointer is touched until every staged object is ready.
        self.backend.put(base + ":manifest", _pack(manifest))
        self.backend.put(base + ":data", _pack(payload))
        manifest["status"] = "ready"
        self.backend.put(base + ":manifest", _pack(manifest))
        self.backend.put(self._pointer(kind), resource_id.encode("utf-8"))
        return resource_id

    def load(self, kind: str, resource_id: str | None = None) -> dict[str, Any]:
        resource_id = resource_id or self.active(kind)
        if not resource_id:
            raise CoreError("NOT_READY", f"no active {kind} resource")
        base = f"tcpr:core:{kind}:{resource_id}"
        manifest_raw = self.backend.get(base + ":manifest")
        data_raw = self.backend.get(base + ":data")
        if manifest_raw is None or data_raw is None:
            raise CoreError("NOT_FOUND", f"{kind} resource not found: {resource_id}")
        manifest = _unpack(manifest_raw)
        if manifest.get("status") != "ready":
            raise CoreError("NOT_READY", f"{kind} resource is not ready: {resource_id}")
        payload = _unpack(data_raw)
        payload["_manifest"] = manifest
        return payload


_NUMERIC_RE = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*([A-Za-zµμ²^./-]+|元|米|厘米|毫米|公斤|千克|克|秒|毫秒)?\s*$")
_BOOL_TRUE = {"true", "yes", "y", "是", "有", "支持", "1"}
_BOOL_FALSE = {"false", "no", "n", "否", "无", "不支持", "0"}
_UNITS: dict[str, tuple[str, float]] = {
    "": ("number", 1), "元": ("number", 1),
    "kb": ("bytes", 1024), "mb": ("bytes", 1024**2), "gb": ("bytes", 1024**3), "tb": ("bytes", 1024**4),
    "kib": ("bytes", 1024), "mib": ("bytes", 1024**2), "gib": ("bytes", 1024**3), "tib": ("bytes", 1024**4),
    "mm": ("length", 1), "厘米": ("length", 10), "cm": ("length", 10), "米": ("length", 1000), "m": ("length", 1000),
    "毫秒": ("time", 1), "ms": ("time", 1), "秒": ("time", 1000), "s": ("time", 1000),
    "克": ("mass", 1), "g": ("mass", 1), "公斤": ("mass", 1000), "千克": ("mass", 1000), "kg": ("mass", 1000),
}


def _numeric(value: Any) -> tuple[float | int, str] | None:
    if isinstance(value, bool) or value is None or isinstance(value, (list, tuple, set, frozenset, dict)):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            return None
        return (int(value) if float(value).is_integer() else float(value)), "number"
    if not isinstance(value, str):
        return None
    # Leading-zero strings are identifiers, not numbers.
    if re.fullmatch(r"\s*[-+]?0\d+\s*", value):
        return None
    match = _NUMERIC_RE.fullmatch(value)
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "").lower()
    family, multiplier = _UNITS.get(unit, ("", 0))
    if not multiplier:
        return None
    result = number * multiplier
    return (int(result) if result.is_integer() else result), family


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    if key in _BOOL_TRUE:
        return True
    if key in _BOOL_FALSE:
        return False
    return None


def _canonical_attr(name: Any) -> str:
    value = str(name).strip()
    if not value:
        raise CoreError("INVALID_INPUT", "attribute names cannot be empty")
    return value


def _parse_xlsx(data: bytes) -> list[dict[str, Any]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            for item in root.findall("x:si", ns):
                shared.append("".join(item.itertext()))
        sheet_name = "xl/worksheets/sheet1.xml"
        if sheet_name not in archive.namelist():
            raise CoreError("INVALID_INPUT", "XLSX has no first worksheet")
        root = ET.fromstring(archive.read(sheet_name))
        ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        rows: list[list[Any]] = []
        for row in root.findall(".//x:sheetData/x:row", ns):
            cells: dict[int, Any] = {}
            for cell in row.findall("x:c", ns):
                ref = cell.attrib.get("r", "")
                letters = re.match(r"([A-Za-z]+)", ref)
                if not letters:
                    continue
                col = 0
                for char in letters.group(1).upper():
                    col = col * 26 + ord(char) - 64
                col -= 1
                value_node = cell.find("x:v", ns)
                inline = cell.find("x:is", ns)
                value: Any = ""
                if inline is not None:
                    value = "".join(inline.itertext())
                elif value_node is not None:
                    value = value_node.text or ""
                    if cell.attrib.get("t") == "s":
                        value = shared[int(value)]
                    elif cell.attrib.get("t") == "b":
                        value = value == "1"
                cells[col] = value
            if cells:
                rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
        if not rows or not any(str(item).strip() for item in rows[0]):
            raise CoreError("INVALID_INPUT", "XLSX has no header row")
        headers = [_canonical_attr(item) for item in rows[0]]
        if len(headers) != len(set(x.lower() for x in headers)):
            raise CoreError("INVALID_INPUT", "duplicate XLSX headers")
        return [{headers[i]: (value if value != "" else None) for i, value in enumerate(row)} for row in rows[1:]]
    except CoreError:
        raise
    except Exception as exc:
        raise CoreError("INVALID_INPUT", "invalid XLSX file") from exc


def _read_file(file: Any) -> tuple[list[dict[str, Any]], str, str | None]:
    primary_key: str | None = None
    if isinstance(file, Mapping) and ("data" in file or "file" in file or "path" in file):
        primary_key = file.get("primary_key")
        filename = str(file.get("filename") or file.get("name") or "input.json")
        file = file.get("data", file.get("file", file.get("path")))
    else:
        filename = "input.json"
    if isinstance(file, (list, tuple)):
        records = list(file)
        return _validate_raw_records(records), filename, primary_key
    if isinstance(file, Mapping):
        return _validate_raw_records([dict(file)]), filename, primary_key
    if isinstance(file, Path) or isinstance(file, str) and _is_path(file):
        path = Path(file)
        filename = path.name
        data = path.read_bytes()
    elif isinstance(file, (bytes, bytearray)):
        data = bytes(file)
    elif isinstance(file, str):
        # Accept in-memory textual JSON/CSV as a convenience; an existing
        # path was handled above and still retains its extension for format
        # detection.
        data = file.encode("utf-8")
    elif hasattr(file, "read"):
        data = file.read()
        filename = str(getattr(file, "name", filename))
    else:
        raise CoreError("INVALID_INPUT", "file must be a path, bytes, file object, or file wrapper")
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not isinstance(data, bytes) or not data:
        raise CoreError("INVALID_INPUT", "file is empty")
    suffix = Path(filename).suffix.lower()
    try:
        if suffix == ".xlsx" or data[:2] == b"PK":
            records = _parse_xlsx(data)
            fmt = "xlsx"
        elif suffix == ".jsonl":
            records = [json.loads(line) for line in data.decode("utf-8-sig").splitlines() if line.strip()]
            fmt = "jsonl"
        elif (suffix == ".json" and data.lstrip()[:1] in {b"[", b"{"}) or data.lstrip()[:1] in {b"[", b"{"}:
            text = data.decode("utf-8-sig")
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict) and isinstance(parsed.get("records"), list):
                    records = parsed["records"]
                elif isinstance(parsed, list):
                    records = parsed
                elif isinstance(parsed, dict):
                    records = [parsed]
                else:
                    raise CoreError("INVALID_INPUT", "JSON must contain an object or array of records")
                fmt = "json"
            except json.JSONDecodeError:
                # Extension-less in-memory JSONL is still unambiguous when
                # every non-empty line is a JSON object.
                records = [json.loads(line) for line in text.splitlines() if line.strip()]
                fmt = "jsonl"
        else:
            text = data.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            if not reader.fieldnames:
                raise CoreError("INVALID_INPUT", "CSV has no header row")
            headers = [_canonical_attr(x) for x in reader.fieldnames]
            if len(headers) != len(set(x.lower() for x in headers)):
                raise CoreError("INVALID_INPUT", "duplicate CSV headers")
            records = [{headers[i]: (value if value != "" else None) for i, value in enumerate(row.values())}
                       for row in reader]
            fmt = "csv"
    except CoreError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, csv.Error, ValueError) as exc:
        raise CoreError("INVALID_INPUT", "file cannot be parsed") from exc
    return _validate_raw_records(records), fmt, primary_key


def _is_path(value: str) -> bool:
    try:
        return Path(value).is_file()
    except (OSError, ValueError):
        return False


def _validate_raw_records(records: list[Any]) -> list[dict[str, Any]]:
    if not records:
        raise CoreError("INVALID_INPUT", "file contains no records")
    output: list[dict[str, Any]] = []
    for row in records:
        if not isinstance(row, Mapping):
            raise CoreError("INVALID_INPUT", "every record must be an object")
        normalized: dict[str, Any] = {}
        seen: set[str] = set()
        for key, value in row.items():
            attr = _canonical_attr(key)
            if attr.lower() in seen:
                raise CoreError("INVALID_INPUT", f"duplicate attribute in record: {attr}")
            seen.add(attr.lower())
            normalized[attr] = value
        output.append(normalized)
    return output


def _resolve_primary_key(records: list[dict[str, Any]], explicit: str | None) -> str:
    names = list(records[0])
    by_lower = {name.lower(): name for name in names}
    if explicit:
        key = by_lower.get(str(explicit).strip().lower())
        if not key:
            raise CoreError("INVALID_INPUT", f"configured primary key not found: {explicit}")
        return key
    for candidate in ("id", "product_id", "product_code", "sku", "编号", "产品编号"):
        if candidate in by_lower:
            return by_lower[candidate]
    raise CoreError("INVALID_INPUT", "primary key is required; configure primary_key in the file wrapper")


def _infer_fields(records: list[dict[str, Any]], primary_key: str) -> tuple[dict[str, FieldSpec], list[dict[str, Any]]]:
    attr_names: list[str] = []
    seen: set[str] = set()
    for row in records:
        for key in row:
            if key.lower() not in seen:
                attr_names.append(key)
                seen.add(key.lower())
    if primary_key not in attr_names:
        raise CoreError("INVALID_INPUT", "primary key is absent from a record")
    fields: dict[str, FieldSpec] = {}
    for attr in attr_names:
        if attr == primary_key:
            kind = "string"
        else:
            observed = [row[attr] for row in records if attr in row and row[attr] is not None]
            if any(isinstance(value, (list, tuple, set, frozenset)) for value in observed):
                kind = "multi"
            elif observed and all(_bool(value) is not None for value in observed):
                kind = "boolean"
            elif observed and all(_numeric(value) is not None for value in observed):
                kind = "numeric"
            elif observed:
                distinct = {str(value).strip() for value in observed}
                kind = "enum" if len(distinct) <= max(16, len(observed) // 2) else "text"
            else:
                kind = "text"
        units = ({variant: multiplier
                  for unit, (_family, multiplier) in _UNITS.items()
                  for variant in ({unit, unit.upper()} if unit else {""})}
                 if kind == "numeric" else {})
        fields[attr] = FieldSpec(attr, kind, aliases=(attr,), unit_multipliers=units)

    normalized: list[dict[str, Any]] = []
    for row in records:
        result: dict[str, Any] = {}
        for attr, spec in fields.items():
            value = row.get(attr)
            if value is None:
                result[attr] = MissingValue(spec.kind)
                continue
            try:
                if spec.kind == "numeric":
                    parsed = _numeric(value)
                    if parsed is None:
                        raise SchemaError(f"{attr}: invalid number")
                    result[attr] = parsed[0]
                elif spec.kind == "boolean":
                    parsed_bool = _bool(value)
                    if parsed_bool is None:
                        raise SchemaError(f"{attr}: invalid boolean")
                    result[attr] = parsed_bool
                elif spec.kind == "multi":
                    values = value if isinstance(value, (list, tuple, set, frozenset)) else [value]
                    result[attr] = frozenset(str(item).strip() for item in values)
                else:
                    result[attr] = str(value).strip()
            except (SchemaError, TypeError, ValueError) as exc:
                raise CoreError("INVALID_INPUT", f"cannot normalize attribute {attr}") from exc
        normalized.append(result)
    return fields, normalized


def _schema_from_payload(payload: dict[str, Any]) -> Schema:
    fields = []
    for name, item in payload.get("attributes", {}).items():
        fields.append(FieldSpec(
            name,
            item["kind"],
            aliases=tuple(item.get("aliases", ())),
            value_aliases=item.get("value_aliases", {}),
            unit_multipliers=item.get("units", {}),
            enum_order=tuple(item.get("enum_order", ())),
        ))
    if not fields:
        raise CoreError("CORRUPT_RESOURCE", "index has no attributes")
    return Schema(fields)


_INDEX_KINDS = frozenset({"string", "text", "numeric", "boolean", "enum", "ordered_enum", "multi"})
_INDEX_SPEC_KEYS = frozenset({"kind", "units", "aliases", "value_aliases", "enum_order"})


def _strict_json_loads(value: str) -> Any:
    """Decode JSON without accepting JavaScript NaN/Infinity extensions."""

    def reject_constant(token: str) -> Any:
        raise ValueError(f"invalid JSON constant: {token}")

    return json.loads(value, parse_constant=reject_constant)


def _split_dsl_segments(text: str, separator: str = ";") -> list[str]:
    """Split a small documented DSL while preserving quoted/JSON values."""

    segments: list[str] = []
    start = 0
    quote: str | None = None
    escaped = False
    depth = 0
    for position, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char in "[{(":
            depth += 1
        elif char in "]})":
            depth -= 1
            if depth < 0:
                raise CoreError("INVALID_INPUT", "index requirement has unbalanced brackets")
        elif char == separator and depth == 0:
            segment = text[start:position].strip()
            if segment:
                segments.append(segment)
            start = position + 1
    if quote or depth:
        raise CoreError("INVALID_INPUT", "index requirement has unbalanced quotes or brackets")
    segment = text[start:].strip()
    if segment:
        segments.append(segment)
    return segments


def _dsl_value(value: str, *, option: str, field_name: str) -> Any:
    """Parse one documented DSL option value, rejecting trailing text."""

    value = value.strip()
    if not value:
        raise CoreError("INVALID_INPUT", f"attribute {field_name} has empty {option}")
    # JSON arrays/objects are the unambiguous form for values containing spaces,
    # commas, or non-string enum members.
    if value[:1] in "[{\"" or value in {"true", "false", "null"}:
        try:
            return _strict_json_loads(value)
        except (ValueError, json.JSONDecodeError) as exc:
            raise CoreError("INVALID_INPUT", f"attribute {field_name} has invalid {option}") from exc
    if option in {"aliases", "enum_order"}:
        return [item.strip() for item in re.split(r"[|,]", value) if item.strip()]
    if option == "value_aliases":
        output: dict[str, str] = {}
        for item in re.split(r"[|,]", value):
            item = item.strip()
            if not item:
                continue
            match = re.match(r"^(.+?)\s*(?:->|=>|:)\s*(.+)$", item)
            if not match:
                raise CoreError("INVALID_INPUT", f"attribute {field_name} has invalid value_aliases")
            output[match.group(1).strip()] = match.group(2).strip()
        return output
    if option == "units":
        output: dict[str, float | int] = {}
        for item in re.split(r"[|,]", value):
            item = item.strip()
            if not item:
                continue
            match = re.match(r"^(.+?)\s*(?:=|:)\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))$", item)
            if not match:
                raise CoreError("INVALID_INPUT", f"attribute {field_name} has invalid units")
            number = float(match.group(2))
            output[match.group(1).strip()] = int(number) if number.is_integer() else number
        return output
    raise CoreError("INVALID_INPUT", f"unknown index option: {option}")


def _parse_index_requirement_dsl(text: str) -> dict[str, Any]:
    """Parse the documented English/Chinese line-oriented index DSL.

    Supported statements are ``primary_key: id`` and field declarations such
    as ``field color: enum aliases=颜色 value_aliases=红色->red``.  A field
    block may be introduced by ``attributes:``/``fields:``/``字段:``; within
    it the shorter ``color: enum`` form is accepted.  Every non-empty line or
    semicolon-delimited statement must match one of these forms: unknown text
    is rejected instead of being silently ignored.
    """

    if not isinstance(text, str) or not text.strip():
        raise CoreError("INVALID_INPUT", "index requirement is required")
    primary_key: str | None = None
    attributes: dict[str, dict[str, Any]] = {}
    in_attributes = False
    saw_statement = False
    # Newlines are statement boundaries; semicolons allow compact one-line DSL.
    statements: list[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        statements.extend(_split_dsl_segments(line))

    for statement in statements:
        saw_statement = True
        # A block header can optionally carry its first field declaration.
        header_match = re.match(r"^(?:attributes?|fields?|字段|属性)\s*(?::|：|=)?\s*(.*)$", statement, re.I)
        if header_match and not re.match(r"^(?:attributes?|fields?)\s+\w+\s*[:：=]", statement, re.I):
            in_attributes = True
            remainder = header_match.group(1).strip()
            if not remainder:
                continue
            statement = remainder

        primary_match = re.match(r"^(?:primary[_ -]?key|主键(?:字段)?)\s*(?::|：|=)\s*(.+)$", statement, re.I)
        if primary_match:
            if primary_key is not None:
                raise CoreError("INVALID_INPUT", "index requirement declares primary_key more than once")
            primary_key = primary_match.group(1).strip().strip("`\"'")
            if not primary_key:
                raise CoreError("INVALID_INPUT", "primary_key must be a non-empty string")
            continue

        field_match = re.match(
            r"^(?:(?:field|attribute|字段|属性)\s+)?([`\"']?[^:=：\s][^:=：]*?[`\"']?)\s*(?::|：|=|\s+)\s*([A-Za-z_][A-Za-z0-9_-]*)(?:\s+(.*))?$",
            statement,
            re.I,
        )
        if not field_match or (not in_attributes and re.match(r"^(?:attributes?|fields?|字段|属性)$", field_match.group(1), re.I)):
            raise CoreError("INVALID_INPUT", f"unrecognized index requirement statement: {statement}")
        raw_name, raw_kind, raw_options = field_match.groups()
        name = raw_name.strip().strip("`\"'")
        kind = raw_kind.strip().lower()
        if not name or not kind:
            raise CoreError("INVALID_INPUT", f"invalid field declaration: {statement}")
        spec: dict[str, Any] = {"kind": kind}
        if raw_options:
            # Options are whitespace separated key=value tokens. JSON values
            # are accepted as one token and therefore must be quoted when they
            # contain spaces; the compact pipe notation avoids that concern.
            options = re.findall(r"(aliases|value_aliases|units|enum_order)\s*=\s*(\[[^]]*\]|\{[^}]*\}|\S+)", raw_options, re.I)
            remainder = re.sub(r"(aliases|value_aliases|units|enum_order)\s*=\s*(\[[^]]*\]|\{[^}]*\}|\S+)", "", raw_options, flags=re.I).strip()
            if remainder:
                raise CoreError("INVALID_INPUT", f"unrecognized field options: {remainder}")
            for option, value in options:
                option = option.lower()
                if option in spec:
                    raise CoreError("INVALID_INPUT", f"attribute {name} repeats {option}")
                spec[option] = _dsl_value(value, option=option, field_name=name)
        if name in attributes or name.lower() in {existing.lower() for existing in attributes}:
            raise CoreError("INVALID_INPUT", f"duplicate attribute name: {name}")
        attributes[name] = spec

    if not saw_statement or primary_key is None or not attributes:
        raise CoreError("INVALID_INPUT", "index requirement must declare primary_key and attributes")
    return {"primary_key": primary_key, "attributes": attributes}


def _load_index_definition(index_json: Any, *, allow_dsl: bool = False) -> dict[str, Any]:
    """Parse and canonicalize the user-authored logical index definition.

    This function deliberately accepts JSON text as JSON only.  In particular,
    a string is never treated as a path and this function never reads a data
    file or database configuration.
    """

    if isinstance(index_json, str):
        try:
            document = _strict_json_loads(index_json)
        except (json.JSONDecodeError, ValueError) as exc:
            if not allow_dsl or index_json.lstrip()[:1] in {"{", "["}:
                raise CoreError("INVALID_INPUT", "index_json must be valid JSON") from exc
            document = _parse_index_requirement_dsl(index_json)
    elif isinstance(index_json, Mapping):
        document = dict(index_json)
    else:
        raise CoreError("INVALID_INPUT", "index_json must be a JSON object or string")
    if not isinstance(document, dict):
        raise CoreError("INVALID_INPUT", "index_json must be a JSON object")
    if set(document) != {"primary_key", "attributes"}:
        raise CoreError("INVALID_INPUT", "index_json must contain only primary_key and attributes")

    raw_primary = document.get("primary_key")
    if not isinstance(raw_primary, str) or not raw_primary.strip():
        raise CoreError("INVALID_INPUT", "primary_key must be a non-empty string")
    raw_attributes = document.get("attributes")
    if not isinstance(raw_attributes, Mapping) or not raw_attributes:
        raise CoreError("INVALID_INPUT", "attributes must be a non-empty object")

    attributes: dict[str, dict[str, Any]] = {}
    names_by_lower: dict[str, str] = {}
    for raw_name, raw_spec in raw_attributes.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise CoreError("INVALID_INPUT", "attribute names must be non-empty strings")
        name = raw_name.strip()
        name_key = name.lower()
        if name_key in names_by_lower:
            raise CoreError("INVALID_INPUT", f"duplicate attribute name: {name}")
        names_by_lower[name_key] = name
        if not isinstance(raw_spec, Mapping):
            raise CoreError("INVALID_INPUT", f"attribute {name} must be an object")
        if set(raw_spec) - _INDEX_SPEC_KEYS:
            raise CoreError("INVALID_INPUT", f"attribute {name} has unknown settings")
        kind = raw_spec.get("kind")
        if not isinstance(kind, str) or kind.strip().lower() not in _INDEX_KINDS:
            raise CoreError("INVALID_INPUT", f"attribute {name} has an unsupported kind")
        kind = kind.strip().lower()
        normalized: dict[str, Any] = {"kind": kind}

        if "units" in raw_spec:
            raw_units = raw_spec["units"]
            if kind != "numeric" or not isinstance(raw_units, Mapping) or not raw_units:
                raise CoreError("INVALID_INPUT", f"attribute {name} has invalid units")
            units: dict[str, int | float] = {}
            for raw_unit, raw_multiplier in raw_units.items():
                if not isinstance(raw_unit, str):
                    raise CoreError("INVALID_INPUT", f"attribute {name} has an unsupported unit")
                unit = raw_unit.strip()
                unit_key = next((candidate for candidate in _UNITS if candidate.lower() == unit.lower()), None)
                if unit_key is None:
                    raise CoreError("INVALID_INPUT", f"attribute {name} has an unsupported unit")
                if isinstance(raw_multiplier, bool) or not isinstance(raw_multiplier, (int, float)):
                    raise CoreError("INVALID_INPUT", f"attribute {name} has an invalid unit multiplier")
                if not math.isfinite(float(raw_multiplier)) or float(raw_multiplier) <= 0:
                    raise CoreError("INVALID_INPUT", f"attribute {name} has an invalid unit multiplier")
                units[unit] = int(raw_multiplier) if float(raw_multiplier).is_integer() else float(raw_multiplier)
                if unit.isascii() and unit:
                    units.setdefault(unit.lower(), units[unit])
                    units.setdefault(unit.upper(), units[unit])
            normalized["units"] = units

        if "aliases" in raw_spec:
            aliases = raw_spec["aliases"]
            if not isinstance(aliases, (list, tuple)) or any(not isinstance(item, str) or not item.strip() for item in aliases):
                raise CoreError("INVALID_INPUT", f"attribute {name} has invalid aliases")
            normalized["aliases"] = list(dict.fromkeys(item.strip() for item in aliases))
        if "value_aliases" in raw_spec:
            value_aliases = raw_spec["value_aliases"]
            if not isinstance(value_aliases, Mapping):
                raise CoreError("INVALID_INPUT", f"attribute {name} has invalid value_aliases")
            normalized["value_aliases"] = dict(value_aliases)
        if "enum_order" in raw_spec:
            enum_order = raw_spec["enum_order"]
            if kind != "ordered_enum" or not isinstance(enum_order, (list, tuple)) or not enum_order:
                raise CoreError("INVALID_INPUT", f"attribute {name} has invalid enum_order")
            if len(set(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in enum_order)) != len(enum_order):
                raise CoreError("INVALID_INPUT", f"attribute {name} has duplicate enum values")
            normalized["enum_order"] = list(enum_order)
        elif kind == "ordered_enum":
            raise CoreError("INVALID_INPUT", f"attribute {name} requires enum_order")
        attributes[name] = normalized

    aliases_by_lower: dict[str, str] = {}
    for name, spec in attributes.items():
        for alias in (name, *spec.get("aliases", ())):
            key = alias.strip().lower()
            previous = aliases_by_lower.get(key)
            if previous is not None and previous != name:
                raise CoreError("INVALID_INPUT", f"duplicate attribute alias: {alias}")
            aliases_by_lower[key] = name

    primary_key = names_by_lower.get(raw_primary.strip().lower())
    if primary_key is None:
        raise CoreError("INVALID_INPUT", "primary_key must name an attribute")
    if attributes[primary_key]["kind"] != "string":
        raise CoreError("INVALID_INPUT", "primary_key attribute must have kind string")
    return {"primary_key": primary_key, "attributes": attributes}


def _definition_digest(definition: Mapping[str, Any]) -> str:
    raw = json.dumps(definition, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _definition_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    try:
        definition = _load_index_definition({
            "primary_key": payload["primary_key"],
            "attributes": payload["attributes"],
        })
    except (KeyError, TypeError) as exc:
        raise CoreError("CORRUPT_RESOURCE", "stored index definition is invalid") from exc
    expected = payload.get("definition_digest")
    if not isinstance(expected, str) or expected != _definition_digest(definition):
        raise CoreError("CORRUPT_RESOURCE", "stored index definition digest is invalid")
    return definition


def _normalize_database_records(
    records: list[dict[str, Any]], definition: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Normalize records against the declared schema and return attrs/raw rows."""

    attribute_names = list(definition["attributes"])
    by_lower = {name.lower(): name for name in attribute_names}
    data_names: dict[str, str] = {}
    for record in records:
        for name in record:
            canonical = by_lower.get(name.lower())
            if canonical is None:
                raise CoreError("INDEX_INPUT_MISMATCH", f"database contains undeclared attribute: {name}")
            data_names[name.lower()] = canonical
    if set(data_names.values()) != set(attribute_names):
        raise CoreError("INDEX_INPUT_MISMATCH", "database attributes do not match the index")

    schema = _schema_from_payload({"attributes": definition["attributes"]})
    normalized_rows: list[dict[str, Any]] = []
    raw_rows: list[dict[str, Any]] = []
    for record in records:
        row: dict[str, Any] = {}
        raw_row: dict[str, Any] = {}
        record_by_lower = {name.lower(): value for name, value in record.items()}
        for name in attribute_names:
            spec = schema.field(name)
            if name.lower() not in record_by_lower or record_by_lower[name.lower()] is None:
                row[name] = MissingValue(spec.kind)
                raw_row[name] = None
                continue
            value = record_by_lower[name.lower()]
            try:
                canonical = spec.canonical_value(value)
                if spec.kind == "numeric" and (isinstance(canonical, bool) or not math.isfinite(float(canonical))):
                    raise SchemaError(f"{name}: invalid number")
            except (SchemaError, TypeError, ValueError) as exc:
                raise CoreError("INVALID_INPUT", f"cannot normalize attribute {name}") from exc
            row[name] = canonical
            raw_row[name] = _jsonable(value)
        normalized_rows.append(row)
        raw_rows.append(raw_row)

    primary_key = str(definition["primary_key"])
    identifiers: list[str] = []
    for row in normalized_rows:
        value = row[primary_key]
        if isinstance(value, MissingValue):
            raise CoreError("DUPLICATE_OR_INVALID_PRIMARY_KEY", "primary key values cannot be empty")
        identifier = str(value).strip()
        if not identifier:
            raise CoreError("DUPLICATE_OR_INVALID_PRIMARY_KEY", "primary key values cannot be empty")
        row[primary_key] = identifier
        identifiers.append(identifier)
    if len(identifiers) != len(set(identifiers)):
        raise CoreError("DUPLICATE_PRIMARY_KEY", "primary key values must be unique")
    return normalized_rows, raw_rows


def _jsonable(value: Any) -> Any:
    if isinstance(value, MissingValue):
        return {"__tcpr_missing__": value.kind}
    if isinstance(value, (set, frozenset, tuple)):
        return sorted((_jsonable(item) for item in value), key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _digest(primary_key: str, attrs: list[dict[str, Any]]) -> str:
    payload = {"primary_key": primary_key, "records": [_jsonable(item) for item in attrs]}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _values(value: Any) -> list[Any]:
    if isinstance(value, (set, frozenset, list, tuple)):
        return list(value)
    return [value]


def _descriptor(fields: dict[str, FieldSpec], records: list[dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for attr, spec in fields.items():
        unique: dict[str, Any] = {}
        for row in records:
            for item in _values(row[attr]):
                encoded = json.dumps(_stable_value(item), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                unique[encoded] = _stable_value(item)
        output[attr] = {
            "kind": spec.kind,
            "units": dict(spec.unit_multipliers),
            "values": [unique[key] for key in sorted(unique)],
            "missing": {"type": "missing", "kind": spec.kind},
        }
    return output


def _stable_value(value: Any) -> Any:
    return _jsonable(value)


def _decode_attrs(attrs: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for attr, value in attrs.items():
        if isinstance(value, dict) and set(value) == {"__tcpr_missing__"}:
            output[attr] = MissingValue(str(value["__tcpr_missing__"]))
        elif fields.get(attr, {}).get("kind") == "multi" and isinstance(value, list):
            output[attr] = frozenset(value)
        else:
            output[attr] = value
    return output


class CoreService:
    """Shared TCPR service for index, database, query, and search operations."""

    def __init__(self, backend: KVBackend | None = None):
        self.backend = backend or InMemoryKV()
        self.store = SnapshotStore(self.backend)

    def build_index(self, index_json: Any) -> str:
        """Persist only the user-authored logical index definition.

        No data file is read here.  Physical postings and range entries are
        produced later by ``build_database`` from the declared definition.
        """

        definition = _load_index_definition(index_json)
        payload = {
            "schema_version": "tcpr-dynamic-v1",
            "primary_key": definition["primary_key"],
            "attributes": definition["attributes"],
            "definition_digest": _definition_digest(definition),
            "record_count": 0,
        }
        return self.store.write("index", payload)

    def structure_index(self, index_requirement: Any) -> dict[str, Any]:
        """Convert a strict JSON/English/Chinese DSL requirement to a definition.

        JSON is attempted first.  Non-JSON text is accepted only by the
        documented line-oriented DSL and is then passed through the exact same
        schema validator used by manual ``index_json`` input.
        """

        definition = _load_index_definition(index_requirement, allow_dsl=True)
        return {
            "status": "OK",
            "index_json": json.dumps(definition, ensure_ascii=False, separators=(",", ":")),
            "index_definition": definition,
        }

    def get_index_definition(self, index_id: str) -> dict[str, Any]:
        """Load and validate one persisted logical index definition."""

        if not isinstance(index_id, str) or not index_id.strip():
            raise CoreError("INVALID_INPUT", "index_id is required")
        payload = self.store.load("index", index_id)
        definition = _definition_from_payload(payload)
        return {
            "status": "OK",
            "index_id": index_id,
            "index_json": json.dumps(definition, ensure_ascii=False, separators=(",", ":")),
            "index_definition": definition,
        }

    def build_database(self, file: Any, index_id: str) -> str:
        if not isinstance(index_id, str) or not index_id.strip():
            raise CoreError("INVALID_INPUT", "index_id is required")
        index = self.store.load("index", index_id)
        definition = _definition_from_payload(index)
        records, fmt, _ = _read_file(file)
        normalized, raw_rows = _normalize_database_records(records, definition)
        primary = definition["primary_key"]
        products = [SparseProduct(row[primary], row, raw)
                    for row, raw in zip(normalized, raw_rows)]
        physical_index = InMemoryIndex(products)
        payload = {
            "schema_version": "tcpr-dynamic-v1",
            "index_id": index_id,
            "index_definition_digest": index["definition_digest"],
            "primary_key": primary,
            "source_format": fmt,
            "record_count": len(normalized),
            "attributes": definition["attributes"],
            "records": [{"id": product.product_id, "attrs": product.attrs, "raw": product.raw}
                        for product in products],
            **physical_index.to_payload(),
        }
        return self.store.write("database", payload)

    @staticmethod
    def _parse_query(query_json: str | dict[str, Any], schema: Schema) -> tuple[list[Hard], list[Soft]]:
        if isinstance(query_json, str):
            try:
                doc = json.loads(query_json)
            except json.JSONDecodeError as exc:
                raise CoreError("INVALID_QUERY", "query_json must be valid JSON") from exc
        elif isinstance(query_json, dict):
            doc = query_json
        else:
            raise CoreError("INVALID_QUERY", "query_json must be a JSON object or string")
        if not isinstance(doc, dict):
            raise CoreError("INVALID_QUERY", "query_json must be an object")
        if "op" in doc:
            doc = {"hard": [doc], "soft": []}
        allowed = {"hard", "soft", "unparsed"}
        if set(doc) - allowed:
            raise CoreError("INVALID_QUERY", "unknown query fields")
        if doc.get("unparsed"):
            raise CoreError("INVALID_QUERY", "unparsed constraints are not searchable")
        hard_doc, soft_doc = doc.get("hard", []), doc.get("soft", [])
        if not isinstance(hard_doc, list) or not isinstance(soft_doc, list) or not hard_doc and not soft_doc:
            raise CoreError("INVALID_QUERY", "query requires hard or soft constraints")
        try:
            hard = [Hard(parse_ast(item, schema)) for item in hard_doc]
            soft: list[Soft] = []
            for item in soft_doc:
                if not isinstance(item, dict):
                    raise QueryError("soft constraint must be object")
                weight = item.get("weight", 1.0)
                constraint_doc = item.get("constraint", item)
                if not isinstance(weight, (int, float)) or isinstance(weight, bool) or not math.isfinite(float(weight)):
                    raise QueryError("soft weight must be finite")
                soft.append(Soft(parse_ast(constraint_doc, schema), float(weight)))
            return hard, soft
        except (QueryError, SchemaError, KeyError, TypeError, ValueError) as exc:
            raise CoreError("INVALID_QUERY", str(exc)) from exc

    @staticmethod
    def _query_document(hard: list[Hard], soft: list[Soft]) -> dict[str, Any]:
        """Serialize parsed constraints into the stable search input shape."""

        def constraint_document(constraint: Any) -> dict[str, Any]:
            document = constraint.to_dict()
            document.pop("label", None)
            if "children" in document:
                document["children"] = [
                    constraint_document(child) for child in constraint.children
                ]
            return _jsonable(document)

        return {
            "hard": [constraint_document(item.constraint) for item in hard],
            "soft": [
                {
                    "constraint": constraint_document(item.constraint),
                    "weight": item.weight,
                }
                for item in soft
            ],
        }

    def structure_query(self, requirement: Any, index_id: str) -> dict[str, Any]:
        """Deterministically convert a text or JSON requirement to query JSON.

        Text requirements use the bundled schema-aware parser. JSON
        requirements are parsed and validated through the same path as
        ``search``. No model, remote service, or implicit schema inference is
        involved; the referenced logical index is the only source of fields,
        aliases, value aliases, and units.
        """

        if not isinstance(index_id, str) or not index_id.strip():
            raise CoreError("INVALID_INPUT", "index_id is required")
        index_payload = self.store.load("index", index_id)
        definition = _definition_from_payload(index_payload)
        schema = _schema_from_payload({"attributes": definition["attributes"]})
        if isinstance(requirement, str):
            text = requirement.strip()
            if not text:
                raise CoreError("INVALID_QUERY", "requirement is required")
            if text[:1] in {"{", "["}:
                try:
                    parsed = _strict_json_loads(text)
                except (ValueError, json.JSONDecodeError) as exc:
                    raise CoreError("INVALID_QUERY", "requirement must be valid JSON") from exc
                hard, soft = self._parse_query(parsed, schema)
                document = self._query_document(hard, soft)
                if not document["hard"] and not document["soft"]:
                    raise CoreError("INVALID_QUERY", "requirement requires a constraint")
                return {
                    "status": "OK",
                    "index_id": index_id,
                    "query_json": json.dumps(document, ensure_ascii=False, separators=(",", ":")),
                    "query": document,
                }
            try:
                parsed = _strict_json_loads(text)
            except json.JSONDecodeError:
                try:
                    hard, soft = parse_text(text, schema)
                except (QueryError, SchemaError, KeyError, TypeError, ValueError) as exc:
                    raise CoreError("INVALID_QUERY", str(exc)) from exc
            except ValueError as exc:
                raise CoreError("INVALID_QUERY", "requirement must be valid JSON") from exc
            else:
                hard, soft = self._parse_query(parsed, schema)
        elif isinstance(requirement, dict):
            hard, soft = self._parse_query(requirement, schema)
        else:
            raise CoreError("INVALID_QUERY", "requirement must be text or a JSON object")
        document = self._query_document(hard, soft)
        if not document["hard"] and not document["soft"]:
            raise CoreError("INVALID_QUERY", "requirement requires a constraint")
        return {
            "status": "OK",
            "index_id": index_id,
            "query_json": json.dumps(document, ensure_ascii=False, separators=(",", ":")),
            "query": document,
        }

    def search(self, query_json: str | dict[str, Any], index_id: str, database_id: str) -> dict[str, Any]:
        try:
            if not index_id or not database_id:
                raise CoreError("INVALID_INPUT", "index_id and database_id are required")
            index_payload = self.store.load("index", index_id)
            database_payload = self.store.load("database", database_id)
            if database_payload.get("index_id") != index_id:
                raise CoreError("RESOURCE_MISMATCH", "database was not built from index_id")
            definition = _definition_from_payload(index_payload)
            if database_payload.get("index_definition_digest") != index_payload.get("definition_digest"):
                raise CoreError("RESOURCE_MISMATCH", "index and database snapshots differ")
            if database_payload.get("attributes") != definition["attributes"]:
                raise CoreError("RESOURCE_MISMATCH", "database schema differs from index")
            if not isinstance(database_payload.get("postings"), dict) or not isinstance(database_payload.get("ranges"), dict):
                raise CoreError("CORRUPT_RESOURCE", "database has no persisted physical index")
            schema = _schema_from_payload({"attributes": definition["attributes"]})
            hard, soft = self._parse_query(query_json, schema)
            fields = definition["attributes"]
            products = [SparseProduct(item["id"], _decode_attrs(item["attrs"], fields), item.get("raw", {}))
                        for item in database_payload.get("records", [])]
            persisted_index = InMemoryIndex.from_payload(products, database_payload)
            result = Retriever(products, schema, index=persisted_index).search(hard, soft)
            hits = [
                {
                    "id": hit.product.product_id,
                    "score": hit.soft_score,
                    "attributes": _jsonable(dict(hit.product.attrs)),
                    "raw": _jsonable(dict(hit.product.raw)),
                    "reasons": list(hit.reasons),
                }
                for hit in result.hits
            ]
            return {
                "status": "OK",
                "index_id": index_id,
                "database_id": database_id,
                "count": len(hits),
                "results": hits,
                "debug": {
                    "record_count": len(products),
                    "candidate_count": result.plan.get("candidate_count", 0),
                    "plan": result.plan,
                    "hard_unknown_policy": "UNKNOWN never satisfies Hard",
                },
            }
        except CoreError as exc:
            return {
                "status": exc.code,
                "index_id": index_id,
                "database_id": database_id,
                "count": 0,
                "results": [],
                "error": {"code": exc.code, "message": exc.message},
            }
        except Exception as exc:
            return {
                "status": "ERROR",
                "index_id": index_id,
                "database_id": database_id,
                "count": 0,
                "results": [],
                "error": {"code": "ERROR", "message": str(exc)},
            }


_DEFAULT_BACKEND = InMemoryKV()
_DEFAULT_SERVICE = CoreService(_DEFAULT_BACKEND)


def build_index(index_json: Any) -> str:
    return _DEFAULT_SERVICE.build_index(index_json)


def structure_index(index_requirement: Any) -> dict[str, Any]:
    return _DEFAULT_SERVICE.structure_index(index_requirement)


def get_index_definition(index_id: str) -> dict[str, Any]:
    return _DEFAULT_SERVICE.get_index_definition(index_id)


def build_database(file: Any, index_id: str) -> str:
    return _DEFAULT_SERVICE.build_database(file, index_id)


def search(query_json: str | dict[str, Any], index_id: str, database_id: str) -> dict[str, Any]:
    return _DEFAULT_SERVICE.search(query_json, index_id, database_id)


def structure_query(requirement: Any, index_id: str) -> dict[str, Any]:
    return _DEFAULT_SERVICE.structure_query(requirement, index_id)


__all__ = [
    "CoreError", "CoreService", "InMemoryKV", "FileKV", "DirectoryKV",
    "build_index", "structure_index", "get_index_definition", "build_database", "search",
    "structure_query",
]
