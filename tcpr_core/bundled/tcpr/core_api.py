"""Shared file-backed TCPR core.

The root package and the Dify plugin both call this module.  Adapters only
provide a byte-oriented key/value backend and translate tool messages; file
parsing, dynamic schema inference, indexing, query semantics, and snapshot
activation live here as one implementation.
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
from .parser import QueryError, parse_ast
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
        fields.append(FieldSpec(name, item["kind"], aliases=(name,),
                                unit_multipliers=item.get("units", {})))
    if not fields:
        raise CoreError("CORRUPT_RESOURCE", "index has no attributes")
    return Schema(fields)


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
    """Shared three-operation TCPR service."""

    def __init__(self, backend: KVBackend | None = None):
        self.backend = backend or InMemoryKV()
        self.store = SnapshotStore(self.backend)

    def _prepare(self, file: Any) -> tuple[str, str, dict[str, FieldSpec], list[dict[str, Any]], str]:
        records, fmt, explicit_primary = _read_file(file)
        primary = _resolve_primary_key(records, explicit_primary)
        fields, normalized = _infer_fields(records, primary)
        ids = [str(row[primary]).strip() for row in records]
        if any(not item for item in ids):
            raise CoreError("DUPLICATE_OR_INVALID_PRIMARY_KEY", "primary key values cannot be empty")
        if len(ids) != len(set(ids)):
            raise CoreError("DUPLICATE_PRIMARY_KEY", "primary key values must be unique")
        # Primary key is always normalized as a string, including numeric CSV IDs.
        for row, identifier in zip(normalized, ids):
            row[primary] = identifier
        return primary, fmt, fields, normalized, _digest(primary, normalized)

    def build_index(self, file: Any) -> str:
        primary, fmt, fields, records, digest = self._prepare(file)
        products = [SparseProduct(str(row[primary]), row, row) for row in records]
        index = InMemoryIndex(products)
        payload = {
            "schema_version": "tcpr-dynamic-v1",
            "primary_key": primary,
            "source_format": fmt,
            "record_count": len(records),
            "dataset_digest": digest,
            "attributes": _descriptor(fields, records),
            **index.to_payload(),
        }
        return self.store.write("index", payload)

    def build_database(self, file: Any, index_id: str) -> str:
        if not isinstance(index_id, str) or not index_id.strip():
            raise CoreError("INVALID_INPUT", "index_id is required")
        index = self.store.load("index", index_id)
        primary, fmt, fields, records, digest = self._prepare(file)
        if digest != index.get("dataset_digest") or primary != index.get("primary_key"):
            raise CoreError("INDEX_INPUT_MISMATCH", "database input does not match the indexed file")
        attrs = index.get("attributes", {})
        if set(attrs) != set(fields):
            raise CoreError("INDEX_INPUT_MISMATCH", "database attributes do not match the index")
        payload = {
            "schema_version": "tcpr-dynamic-v1",
            "index_id": index_id,
            "primary_key": primary,
            "source_format": fmt,
            "record_count": len(records),
            "dataset_digest": digest,
            "attributes": attrs,
            "records": [{"id": str(row[primary]), "attrs": row, "raw": row} for row in records],
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

    def search(self, query_json: str | dict[str, Any], index_id: str, database_id: str) -> dict[str, Any]:
        try:
            if not index_id or not database_id:
                raise CoreError("INVALID_INPUT", "index_id and database_id are required")
            index_payload = self.store.load("index", index_id)
            database_payload = self.store.load("database", database_id)
            if database_payload.get("index_id") != index_id:
                raise CoreError("RESOURCE_MISMATCH", "database was not built from index_id")
            if database_payload.get("dataset_digest") != index_payload.get("dataset_digest"):
                raise CoreError("RESOURCE_MISMATCH", "index and database snapshots differ")
            schema = _schema_from_payload(index_payload)
            hard, soft = self._parse_query(query_json, schema)
            fields = index_payload["attributes"]
            products = [SparseProduct(item["id"], _decode_attrs(item["attrs"], fields), item.get("raw", {}))
                        for item in database_payload.get("records", [])]
            persisted_index = InMemoryIndex.from_payload(products, index_payload)
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


def build_index(file: Any) -> str:
    return _DEFAULT_SERVICE.build_index(file)


def build_database(file: Any, index_id: str) -> str:
    return _DEFAULT_SERVICE.build_database(file, index_id)


def search(query_json: str | dict[str, Any], index_id: str, database_id: str) -> dict[str, Any]:
    return _DEFAULT_SERVICE.search(query_json, index_id, database_id)


__all__ = [
    "CoreError", "CoreService", "InMemoryKV", "FileKV", "DirectoryKV",
    "build_index", "build_database", "search",
]
