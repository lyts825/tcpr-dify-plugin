"""Read-only remote database query tool.

The tool deliberately keeps connection settings in the invocation scope.  No
credential is copied to Dify storage, a model prompt, a result payload, or a
log message.  SQL is tokenised before it is sent to a driver so that comments,
quoted strings, and quoted identifiers cannot bypass the read-only policy.
"""

from __future__ import annotations

import base64
import datetime as _datetime
import importlib
import json
import re
import ssl
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import tcpr_core.sdk_compat as _sdk_compat
from tcpr_core.bundled.tcpr.model import Constraint, ConstraintKind, AND
from tcpr_core.bundled.tcpr.optimizer import UnsatisfiableQuery, optimize
from tcpr_core.bundled.tcpr.parser import QueryError, parse_ast
from tcpr_core.bundled.tcpr.schema import FieldSpec, Schema, SchemaError


DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_QUERY_TIMEOUT = 30
DEFAULT_MAX_ROWS = 100
DEFAULT_TOP_K = 20
HARD_CONNECT_TIMEOUT = 30
HARD_QUERY_TIMEOUT = 60
HARD_MAX_ROWS = 1000

_PARAMETER_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SENSITIVE_NAME = re.compile(
    r"(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential|authorization)",
    re.IGNORECASE,
)
_WRITE_WORDS = {
    "INSERT", "UPDATE", "DELETE", "MERGE", "REPLACE", "UPSERT",
    "CREATE", "ALTER", "DROP", "TRUNCATE", "GRANT", "REVOKE",
    "COMMIT", "ROLLBACK", "BEGIN", "START", "SET", "RESET",
    "CALL", "DO", "EXEC", "EXECUTE", "VACUUM", "ANALYZE", "COPY",
}
_LOCK_WORDS = {"FOR", "LOCK"}


class RemoteQueryError(ValueError):
    """An expected failure with a stable public code and safe message."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    start: int
    end: int


@dataclass(frozen=True)
class _QueryConfig:
    database_type: str
    host: str
    port: int
    database: str
    username: str
    password: str
    ssl_mode: str
    query_mode: str = "tcpr"
    table: str = ""


def _tokenize(sql: str) -> tuple[list[_Token], list[tuple[int, int]]]:
    """Tokenise SQL without interpreting values as executable SQL.

    The scanner intentionally treats quoted identifiers and literals as opaque
    tokens.  A semicolon is retained as punctuation so the caller can reject
    multiple statements while allowing one final delimiter.
    """

    tokens: list[_Token] = []
    semicolons: list[tuple[int, int]] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
            continue
        if sql.startswith("--", i):
            end = sql.find("\n", i + 2)
            i = n if end < 0 else end + 1
            continue
        if sql.startswith("/*!", i):
            # MySQL executable comments can turn an apparently harmless
            # SELECT into a write or a second statement on the server.
            raise RemoteQueryError("SQL_NOT_READ_ONLY", "executable SQL comments are not allowed")
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end < 0:
                raise RemoteQueryError("INVALID_SQL", "SQL contains an unterminated comment")
            i = end + 2
            continue
        if ch == ";":
            semicolons.append((i, i + 1))
            tokens.append(_Token("punct", ";", i, i + 1))
            i += 1
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
            start = i
            i += 1
            while i < n:
                if sql[i] == quote:
                    # SQL escapes a quote by doubling it.
                    if i + 1 < n and sql[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                if sql[i] == "\\" and quote in {"'", "`"} and i + 1 < n:
                    i += 2
                    continue
                i += 1
            else:
                raise RemoteQueryError("INVALID_SQL", "SQL contains an unterminated quoted value")
            tokens.append(_Token("quoted", sql[start:i], start, i))
            continue
        # PostgreSQL dollar-quoted strings are values, not SQL tokens.
        if ch == "$":
            match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", sql[i:])
            if match:
                tag = match.group(0)
                end = sql.find(tag, i + len(tag))
                if end < 0:
                    raise RemoteQueryError("INVALID_SQL", "SQL contains an unterminated quoted value")
                finish = end + len(tag)
                tokens.append(_Token("quoted", sql[i:finish], i, finish))
                i = finish
                continue
        if ch == ":" and not sql.startswith("::", i):
            match = _PARAMETER_NAME.match(sql, i + 1)
            if match:
                end = match.end()
                tokens.append(_Token("parameter", match.group(0), i, end))
                i = end
                continue
        if ch.isalpha() or ch == "_":
            start = i
            i += 1
            while i < n and (sql[i].isalnum() or sql[i] in {"_", "$"}):
                i += 1
            tokens.append(_Token("word", sql[start:i].upper(), start, i))
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and sql[i + 1].isdigit()):
            start = i
            i += 1
            while i < n and (sql[i].isalnum() or sql[i] in {".", "_"}):
                i += 1
            tokens.append(_Token("number", sql[start:i], start, i))
            continue
        # Operators and punctuation are safe to retain for structural checks.
        tokens.append(_Token("punct", ch, i, i + 1))
        i += 1
    return tokens, semicolons


def _validate_sql(sql: Any) -> tuple[str, list[_Token]]:
    if not isinstance(sql, str) or not sql.strip():
        raise RemoteQueryError("INVALID_SQL", "sql is required")
    if len(sql) > 1_000_000:
        raise RemoteQueryError("INVALID_SQL", "sql is too long")
    tokens, semicolons = _tokenize(sql)
    if not tokens:
        raise RemoteQueryError("INVALID_SQL", "sql is required")
    # One optional trailing semicolon is a delimiter, not a second statement.
    non_delimiter = [t for t in tokens if t.value != ";"]
    if not non_delimiter:
        raise RemoteQueryError("INVALID_SQL", "only a SELECT query is allowed")
    if len(semicolons) > 1 or (semicolons and tokens[-1].value != ";"):
        raise RemoteQueryError("SQL_NOT_READ_ONLY", "multiple SQL statements are not allowed")
    first = non_delimiter[0]
    if first.kind != "word" or first.value not in {"SELECT", "WITH"}:
        raise RemoteQueryError("SQL_NOT_READ_ONLY", "only SELECT or read-only WITH queries are allowed")

    words = [t.value for t in non_delimiter if t.kind == "word"]
    if first.value == "WITH":
        # CTE bodies can contain SELECT, but any mutating CTE is forbidden.
        if any(word in _WRITE_WORDS - {"BEGIN"} for word in words[1:]):
            raise RemoteQueryError("SQL_NOT_READ_ONLY", "mutating statements are not allowed")
    else:
        # SELECT INTO/OUTFILE/DUMPFILE changes or exports data and is rejected.
        for index, word in enumerate(words):
            if word == "INTO" and index + 1 < len(words):
                raise RemoteQueryError("SQL_NOT_READ_ONLY", "SELECT INTO or file export is not allowed")
            if word in _WRITE_WORDS:
                raise RemoteQueryError("SQL_NOT_READ_ONLY", "mutating statements are not allowed")
    # Locking clauses are unsafe even though they begin with SELECT.
    for index, word in enumerate(words):
        if word == "FOR" and index + 1 < len(words) and words[index + 1] in {"UPDATE", "SHARE", "NO"}:
            raise RemoteQueryError("SQL_NOT_READ_ONLY", "locking queries are not allowed")
        if word == "LOCK":
            raise RemoteQueryError("SQL_NOT_READ_ONLY", "locking queries are not allowed")
    # A trailing semicolon is omitted for placeholder replacement.
    return sql, tokens


def _compile_named_parameters(sql: str, tokens: Iterable[_Token], parameters: Mapping[str, Any]) -> tuple[str, list[Any]]:
    if not isinstance(parameters, Mapping):
        raise RemoteQueryError("PARAMETERS_INVALID", "parameters_json must be a JSON object")
    normalized = {str(key): value for key, value in parameters.items()}
    replacements: list[str] = []
    args: list[Any] = []
    last = 0
    used: set[str] = set()
    for token in tokens:
        if token.kind != "parameter":
            continue
        # PostgreSQL casts use ::type, and are not placeholders.
        if token.start and sql[token.start - 1] == ":":
            continue
        name = token.value
        if name not in normalized:
            raise RemoteQueryError("PARAMETERS_INVALID", "a named SQL parameter is missing")
        replacements.append(sql[last:token.start])
        replacements.append("%s")
        args.append(normalized[name])
        used.add(name)
        last = token.end
    replacements.append(sql[last:])
    if set(normalized) - used:
        raise RemoteQueryError("PARAMETERS_INVALID", "parameters_json contains an unused parameter")
    return "".join(replacements), args


def _parse_parameters(value: Any) -> Mapping[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        raise RemoteQueryError("PARAMETERS_INVALID", "parameters_json must be a JSON object")
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise RemoteQueryError("PARAMETERS_INVALID", "parameters_json is invalid JSON") from exc
    if not isinstance(parsed, Mapping):
        raise RemoteQueryError("PARAMETERS_INVALID", "parameters_json must be a JSON object")
    return parsed


def _config(parameters: Mapping[str, Any]) -> _QueryConfig:
    database_type = str(parameters.get("database_type") or "").strip().lower()
    if database_type in {"postgres", "postgresql"}:
        database_type = "postgresql"
    elif database_type == "mysql":
        database_type = "mysql"
    else:
        raise RemoteQueryError("INVALID_INPUT", "database_type must be postgresql or mysql")
    host = parameters.get("host")
    database = parameters.get("database")
    username = parameters.get("username")
    password = parameters.get("password")
    if not all(isinstance(value, str) and value.strip() for value in (host, database, username, password)):
        raise RemoteQueryError("INVALID_INPUT", "database connection fields are required")
    try:
        port = int(parameters.get("port") or (5432 if database_type == "postgresql" else 3306))
    except (TypeError, ValueError) as exc:
        raise RemoteQueryError("INVALID_INPUT", "port must be a valid TCP port") from exc
    if not 1 <= port <= 65535:
        raise RemoteQueryError("INVALID_INPUT", "port must be a valid TCP port")
    ssl_mode = str(parameters.get("ssl_mode") or "verify-full").strip().lower().replace("_", "-")
    if ssl_mode not in {"verify-full", "verify-ca", "require", "disable"}:
        raise RemoteQueryError("INVALID_INPUT", "ssl_mode is invalid")
    query_mode = str(parameters.get("query_mode") or "tcpr").strip().lower()
    if query_mode in {"raw", "sql"}:
        query_mode = "raw_sql"
    if query_mode not in {"tcpr", "raw_sql"}:
        raise RemoteQueryError("INVALID_INPUT", "query_mode must be tcpr or raw_sql")
    table = str(parameters.get("table") or "").strip()
    if query_mode == "tcpr" and not table:
        raise RemoteQueryError("INVALID_INPUT", "table is required in tcpr mode")
    return _QueryConfig(database_type, host.strip(), port, database.strip(), username.strip(), password, ssl_mode,
                        query_mode, table)


# ---------------------------------------------------------------------------
# TCPR query compilation
# ---------------------------------------------------------------------------

_MAX_TCPR_JSON = 512_000
_MAX_TCPR_NODES = 128
_MAX_TCPR_DEPTH = 20
_TCpr_SCORE = "__tcpr_score"
_TCPR_PK = "__tcpr_pk"


@dataclass(frozen=True)
class _TcprField:
    attr: str
    column: str
    kind: str
    primary_key: bool = False


@dataclass(frozen=True)
class _CompiledQuery:
    sql: str | None
    args: list[Any]
    columns: list[str]
    top_k: int
    unsat: bool = False


def _json_object(value: Any, name: str, *, required: bool = True) -> Mapping[str, Any]:
    if value is None or value == "":
        if required:
            raise RemoteQueryError("INVALID_INPUT", f"{name} is required")
        return {}
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str) or len(value) > _MAX_TCPR_JSON:
        raise RemoteQueryError("INVALID_INPUT", f"{name} must be a JSON object")
    try:
        result = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise RemoteQueryError("INVALID_INPUT", f"{name} is invalid JSON") from exc
    if not isinstance(result, Mapping):
        raise RemoteQueryError("INVALID_INPUT", f"{name} must be a JSON object")
    return result


def _walk_json(node: Any, *, depth: int = 0, count: list[int] | None = None) -> None:
    """Bound recursive user JSON before handing it to the TCPR parser."""

    count = count if count is not None else [0]
    count[0] += 1
    if count[0] > _MAX_TCPR_NODES or depth > _MAX_TCPR_DEPTH:
        raise RemoteQueryError("INVALID_INPUT", "TCPR query is too large or deeply nested")
    if isinstance(node, Mapping):
        for key, value in node.items():
            if not isinstance(key, str) or len(key) > 128:
                raise RemoteQueryError("INVALID_INPUT", "TCPR query contains an invalid key")
            _walk_json(value, depth=depth + 1, count=count)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _walk_json(value, depth=depth + 1, count=count)


def _identifier(name: Any, label: str) -> str:
    text = str(name or "").strip()
    if not _safe_identifier(text):
        raise RemoteQueryError("INVALID_INPUT", f"{label} contains an invalid identifier")
    return text


def _safe_identifier(text: str) -> bool:
    """Allow one Unicode identifier, including common Chinese names.

    The whitelist is intentionally narrower than SQL's full identifier
    grammar: no dots, quotes, whitespace, control characters, or punctuation
    are accepted.  The database dialect quoting is applied only after this
    check, so a form value cannot become an expression or qualified fragment.
    """

    if not text or "." in text or not (text[0].isalpha() or text[0] == "_"):
        return False
    return all(char.isalnum() or char in {"_", "$"} for char in text)


def _quote_identifier(name: str, database_type: str) -> str:
    quote = '"' if database_type == "postgresql" else "`"
    # Dotted table names are the only qualified identifiers accepted.  A
    # column mapping is intentionally one identifier so a form cannot smuggle
    # an expression or a second table into generated SQL.
    parts = name.split(".")
    if not all(_safe_identifier(part) for part in parts):
        raise RemoteQueryError("INVALID_INPUT", "identifier is not allowed")
    return ".".join(f"{quote}{part}{quote}" for part in parts)


def _schema_fields(value: Any) -> tuple[dict[str, _TcprField], str]:
    document = _json_object(value, "tcpr_schema_json")
    raw_fields = document.get("fields", document.get("attributes"))
    if isinstance(raw_fields, list):
        entries: list[tuple[Any, Any]] = []
        for item in raw_fields:
            if not isinstance(item, Mapping):
                raise RemoteQueryError("INVALID_INPUT", "tcpr schema fields must be objects")
            entries.append((item.get("name"), item))
    elif isinstance(raw_fields, Mapping):
        entries = list(raw_fields.items())
    else:
        raise RemoteQueryError("INVALID_INPUT", "tcpr_schema_json.fields is required")

    fields: dict[str, _TcprField] = {}
    primary_candidates: list[str] = []
    for raw_name, raw_spec in entries:
        attr = _identifier(raw_name, "schema field")
        if attr in fields:
            raise RemoteQueryError("INVALID_INPUT", "duplicate TCPR schema field")
        if isinstance(raw_spec, str):
            spec: Mapping[str, Any] = {"column": raw_spec, "kind": "string"}
        elif isinstance(raw_spec, Mapping):
            spec = raw_spec
        else:
            raise RemoteQueryError("INVALID_INPUT", "schema field definition must be an object")
        column = _identifier(spec.get("column", spec.get("db_column", attr)), "schema column")
        kind = str(spec.get("kind", spec.get("type", "string"))).strip().lower()
        if kind == "integer":
            kind = "numeric"
        if kind not in {"numeric", "string", "text", "boolean", "enum", "ordered_enum", "multi"}:
            raise RemoteQueryError("INVALID_INPUT", f"unsupported TCPR field kind: {kind}")
        aliases = spec.get("aliases", ())
        if isinstance(aliases, str):
            aliases = [aliases]
        if not isinstance(aliases, (list, tuple)) or any(not isinstance(item, str) for item in aliases):
            raise RemoteQueryError("INVALID_INPUT", "schema aliases must be strings")
        value_aliases = spec.get("value_aliases", {})
        units = spec.get("units", spec.get("unit_multipliers", {}))
        enum_order = spec.get("enum_order", ())
        if not isinstance(value_aliases, Mapping) or not isinstance(units, Mapping) or not isinstance(enum_order, (list, tuple)):
            raise RemoteQueryError("INVALID_INPUT", "schema value mappings are invalid")
        # Build the bundled Schema now, so aliases and values are canonicalized
        # identically to the in-memory TCPR implementation.
        fields[attr] = _TcprField(attr, column, kind, bool(spec.get("primary_key", spec.get("pk", False))))
        if fields[attr].primary_key:
            primary_candidates.append(attr)

    top_pk = document.get("primary_key", document.get("primaryKey"))
    if top_pk is not None:
        if isinstance(top_pk, (list, tuple)):
            if len(top_pk) != 1:
                raise RemoteQueryError("INVALID_INPUT", "TCPR requires one primary_key for stable ordering")
            top_pk = top_pk[0]
        primary_candidates.append(_identifier(top_pk, "primary_key"))
    if not primary_candidates:
        if "id" in fields:
            primary_candidates.append("id")
        else:
            raise RemoteQueryError("INVALID_INPUT", "tcpr schema requires a primary_key")
    primary_key = primary_candidates[0]
    if primary_key not in fields:
        raise RemoteQueryError("INVALID_INPUT", "primary_key is not a schema field")
    if len(set(primary_candidates)) != 1:
        raise RemoteQueryError("INVALID_INPUT", "tcpr schema must declare exactly one primary_key")
    return fields, primary_key


def _bundled_schema(value: Any) -> tuple[Schema, dict[str, _TcprField], str]:
    document = _json_object(value, "tcpr_schema_json")
    raw_fields = document.get("fields", document.get("attributes"))
    if isinstance(raw_fields, Mapping):
        entries = list(raw_fields.items())
    elif isinstance(raw_fields, list):
        entries = [(item.get("name"), item) for item in raw_fields if isinstance(item, Mapping)]
    else:
        raise RemoteQueryError("INVALID_INPUT", "tcpr_schema_json.fields is required")
    specs: list[FieldSpec] = []
    fields, pk = _schema_fields(value)
    for raw_name, raw_spec in entries:
        attr = str(raw_name)
        spec = raw_spec if isinstance(raw_spec, Mapping) else {"column": raw_spec}
        aliases = spec.get("aliases", ())
        if isinstance(aliases, str):
            aliases = [aliases]
        specs.append(FieldSpec(
            attr,
            fields[attr].kind,
            tuple(aliases),
            spec.get("value_aliases", {}),
            spec.get("units", spec.get("unit_multipliers", {})),
            tuple(spec.get("enum_order", ())),
        ))
    return Schema(specs), fields, pk


def _constraint_from_node(node: Any, schema: Schema, *, wrapper: bool = False) -> Constraint:
    if not isinstance(node, Mapping):
        raise RemoteQueryError("INVALID_INPUT", "TCPR constraints must be JSON objects")
    candidate = node.get("constraint") if wrapper and "constraint" in node else node
    if not isinstance(candidate, Mapping):
        raise RemoteQueryError("INVALID_INPUT", "TCPR constraint is invalid")
    try:
        return parse_ast(candidate, schema)
    except (QueryError, SchemaError, KeyError, TypeError, ValueError) as exc:
        raise RemoteQueryError("INVALID_INPUT", "TCPR constraint is invalid") from exc


def _hard_constraint(query: Mapping[str, Any], schema: Schema) -> Constraint:
    raw = query.get("hard", [])
    if raw in (None, ""):
        return AND()
    if isinstance(raw, list):
        children = tuple(_constraint_from_node(item, schema, wrapper=True) for item in raw)
        return AND(*children)
    if isinstance(raw, Mapping) and "constraints" in raw:
        items = raw.get("constraints")
        if not isinstance(items, list):
            raise RemoteQueryError("INVALID_INPUT", "hard.constraints must be an array")
        return AND(*(_constraint_from_node(item, schema, wrapper=True) for item in items))
    return _constraint_from_node(raw, schema)


def _soft_constraints(query: Mapping[str, Any], schema: Schema) -> list[tuple[Constraint, float]]:
    raw = query.get("soft", [])
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raw = [raw]
    result: list[tuple[Constraint, float]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise RemoteQueryError("INVALID_INPUT", "soft constraints must be objects")
        try:
            weight = float(item.get("weight", 1.0))
        except (TypeError, ValueError) as exc:
            raise RemoteQueryError("INVALID_INPUT", "soft weight is invalid") from exc
        if not _is_finite(weight) or abs(weight) > 1_000_000:
            raise RemoteQueryError("INVALID_INPUT", "soft weight is invalid")
        result.append((_constraint_from_node(item, schema, wrapper=True), weight))
    return result


def _is_finite(value: float) -> bool:
    return value == value and value not in {float("inf"), float("-inf")}


def _json_value(value: Any) -> str:
    if isinstance(value, (set, frozenset, tuple)):
        value = sorted(value, key=lambda item: (type(item).__name__, repr(item)))
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class _SqlParts:
    def __init__(self, database_type: str):
        self.database_type = database_type
        self.args: list[Any] = []

    def bind(self, value: Any, *, json_value: bool = False) -> str:
        self.args.append(_json_value(value) if json_value else value)
        return "%s"

    def quote(self, name: str) -> str:
        return _quote_identifier(name, self.database_type)


def _field_schema_for_kind(fields: Mapping[str, _TcprField], attr: str) -> _TcprField:
    try:
        return fields[attr]
    except KeyError as exc:
        raise RemoteQueryError("INVALID_INPUT", "TCPR query references an unknown attribute") from exc


def _compile_predicate(constraint: Constraint, schema: Schema, fields: Mapping[str, _TcprField], sql: _SqlParts, source_alias: str) -> str:
    if constraint.kind is None:
        op = constraint.attr
        children = [_compile_predicate(child, schema, fields, sql, source_alias) for child in constraint.children]
        if op == "AND":
            return "(" + " AND ".join(children) + ")" if children else "(TRUE)"
        if op == "OR":
            return "(" + " OR ".join(children) + ")" if children else "(FALSE)"
        if op == "NOT" and len(children) == 1:
            return f"(NOT {children[0]})"
        raise RemoteQueryError("INVALID_INPUT", "invalid TCPR logical constraint")
    attr = constraint.attr or ""
    field = _field_schema_for_kind(fields, attr)
    column = f"{sql.quote(source_alias)}.{sql.quote(field.column)}"
    kind = constraint.kind
    if kind is ConstraintKind.EXISTS:
        # CASE keeps NULL/missing as UNKNOWN, so NOT does not turn an unknown
        # value into TRUE (the same three-valued semantics as TCPR core).
        return f"(CASE WHEN {column} IS NULL THEN NULL ELSE TRUE END)"
    if kind is ConstraintKind.EQ:
        return f"({column} = {sql.bind(constraint.value)})"
    if kind is ConstraintKind.NEQ:
        return f"({column} <> {sql.bind(constraint.value)})"
    if kind in {ConstraintKind.GE, ConstraintKind.LE}:
        operator = ">=" if kind is ConstraintKind.GE else "<="
        return f"({column} {operator} {sql.bind(constraint.value)})"
    if kind is ConstraintKind.RANGE:
        clauses: list[str] = []
        if constraint.value is not None:
            clauses.append(f"{column} >= {sql.bind(constraint.value)}")
        if constraint.value2 is not None:
            clauses.append(f"{column} <= {sql.bind(constraint.value2)}")
        return "(" + " AND ".join(clauses) + ")" if clauses else "(TRUE)"
    if kind in {ConstraintKind.CONTAINS, ConstraintKind.SUPERSET}:
        if field.kind != "multi":
            raise RemoteQueryError("INVALID_INPUT", f"{attr} is not a multi-valued field")
        candidate = [constraint.value] if kind is ConstraintKind.CONTAINS else constraint.value
        if sql.database_type == "postgresql":
            return f"({column} @> {sql.bind(candidate, json_value=True)}::jsonb)"
        return f"(JSON_CONTAINS({column}, {sql.bind(candidate, json_value=True)}))"
    if kind in {ConstraintKind.IN, ConstraintKind.NOT_IN}:
        values = sorted(constraint.value or (), key=lambda item: (type(item).__name__, repr(item)))
        if not values:
            if kind is ConstraintKind.IN:
                return "(FALSE)"
            return f"(CASE WHEN {column} IS NULL THEN NULL ELSE TRUE END)"
        if field.kind == "multi":
            parts: list[str] = []
            for value in values:
                if sql.database_type == "postgresql":
                    parts.append(f"({column} @> {sql.bind([value], json_value=True)}::jsonb)")
                else:
                    parts.append(f"JSON_CONTAINS({column}, {sql.bind([value], json_value=True)})")
            expression = "(" + " OR ".join(parts) + ")"
        else:
            placeholders = ", ".join(sql.bind(value) for value in values)
            expression = f"({column} IN ({placeholders}))"
        return f"(NOT {expression})" if kind is ConstraintKind.NOT_IN else expression
    raise RemoteQueryError("INVALID_INPUT", "unsupported TCPR constraint operation")


def _constraint_always_false(constraint: Constraint) -> bool:
    if constraint.kind is not None:
        if constraint.kind is ConstraintKind.IN and not constraint.value:
            return True
        if constraint.kind is ConstraintKind.RANGE and constraint.value is not None and constraint.value2 is not None:
            try:
                return constraint.value > constraint.value2
            except TypeError:
                return False
        return False
    if constraint.attr == "AND":
        return any(_constraint_always_false(child) for child in constraint.children)
    if constraint.attr == "OR":
        return bool(constraint.children) and all(_constraint_always_false(child) for child in constraint.children)
    if constraint.attr == "NOT" and constraint.children:
        return _constraint_always_true(constraint.children[0])
    return False


def _constraint_always_true(constraint: Constraint) -> bool:
    if constraint.kind is not None:
        if constraint.kind is ConstraintKind.NOT_IN and not constraint.value:
            return True
        return False
    if constraint.attr == "AND":
        return all(_constraint_always_true(child) for child in constraint.children)
    if constraint.attr == "OR":
        return any(_constraint_always_true(child) for child in constraint.children)
    if constraint.attr == "NOT" and constraint.children:
        return _constraint_always_false(constraint.children[0])
    return False


def _hard_contradiction(constraint: Constraint) -> bool:
    """Detect common AND contradictions without inspecting remote data."""

    if constraint.kind is not None:
        return _constraint_always_false(constraint)
    if constraint.attr == "AND":
        children: list[Constraint] = []
        for child in constraint.children:
            if child.attr == "AND" and child.kind is None:
                children.extend(child.children)
            else:
                children.append(child)
        if any(_hard_contradiction(child) for child in children):
            return True
        grouped: dict[str, list[Constraint]] = {}
        for child in children:
            if child.kind is not None and child.attr:
                grouped.setdefault(child.attr, []).append(child)
        for atoms in grouped.values():
            equals = [item.value for item in atoms if item.kind is ConstraintKind.EQ]
            if equals and any(value != equals[0] for value in equals[1:]):
                return True
            if equals and any(item.kind is ConstraintKind.NEQ and item.value == equals[0] for item in atoms):
                return True
            memberships = [set(item.value) for item in atoms if item.kind is ConstraintKind.IN and item.value]
            if memberships and set.intersection(*memberships) == set():
                return True
            if equals and any(item.kind is ConstraintKind.IN and equals[0] not in item.value for item in atoms):
                return True
            if equals and any(item.kind is ConstraintKind.NOT_IN and equals[0] in item.value for item in atoms):
                return True
        return False
    if constraint.attr == "OR":
        return bool(constraint.children) and all(_hard_contradiction(child) for child in constraint.children)
    if constraint.attr == "NOT" and constraint.children:
        return _constraint_always_true(constraint.children[0])
    return False


def _parse_top_k(query: Mapping[str, Any]) -> int:
    value = query.get("top_k", DEFAULT_TOP_K)
    try:
        if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
            raise ValueError
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RemoteQueryError("INVALID_INPUT", "top_k must be an integer") from exc
    if not 1 <= result <= HARD_MAX_ROWS:
        raise RemoteQueryError("INVALID_INPUT", f"top_k must be between 1 and {HARD_MAX_ROWS}")
    return result


def compile_tcpr_query(database_type: str, table: str, schema_json: Any, query_json: Any) -> _CompiledQuery:
    """Compile a TCPR JSON query into one bounded, parameterized SQL statement.

    This function performs every schema and query validation before a database
    connection is opened.  Returned SQL contains no user-controlled identifier
    or literal; values and LIMIT are all driver-bound parameters.
    """

    _walk_json(_json_object(schema_json, "tcpr_schema_json"))
    query = _json_object(query_json, "tcpr_query_json")
    _walk_json(query)
    schema, fields, primary_key = _bundled_schema(schema_json)
    table_name = str(table or "").strip()
    if not table_name or not all(_safe_identifier(part) for part in table_name.split(".")):
        raise RemoteQueryError("INVALID_INPUT", "table contains an invalid identifier")
    table_sql = _quote_identifier(table_name, database_type)
    hard = _hard_constraint(query, schema)
    try:
        hard = optimize(hard, schema).constraint
    except UnsatisfiableQuery:
        hard = hard
        unsat = True
    else:
        unsat = _constraint_always_false(hard) or _hard_contradiction(hard)
    selected = query.get("select", list(fields))
    if not isinstance(selected, (list, tuple)) or not selected:
        raise RemoteQueryError("INVALID_INPUT", "tcpr_query_json.select must be a non-empty array")
    selected_attrs: list[str] = []
    selected_columns: set[str] = set()
    for item in selected:
        if not isinstance(item, str):
            attr = ""
        else:
            try:
                attr = schema.resolve_attr(item)
            except SchemaError as exc:
                raise RemoteQueryError("INVALID_INPUT", "select contains an unknown attribute") from exc
        if not attr or attr in selected_attrs:
            raise RemoteQueryError("INVALID_INPUT", "select contains a duplicate or unknown attribute")
        field = fields[attr]
        if field.column in selected_columns:
            raise RemoteQueryError("INVALID_INPUT", "select contains duplicate physical columns")
        selected_attrs.append(attr)
        selected_columns.add(field.column)
    top_k = _parse_top_k(query)
    soft = _soft_constraints(query, schema)
    if unsat:
        return _CompiledQuery(None, [], selected_attrs, top_k, True)

    parts = _SqlParts(database_type)
    source = "__tcpr_src"
    source_prefix = parts.quote(source)
    inner_select: list[str] = []
    for attr in selected_attrs:
        field = fields[attr]
        inner_select.append(f"{source_prefix}.{parts.quote(field.column)} AS {parts.quote(attr)}")
    pk_field = fields[primary_key]
    if primary_key not in selected_attrs:
        inner_select.append(f"{source_prefix}.{parts.quote(pk_field.column)} AS {parts.quote(_TCPR_PK)}")
    from_sql = f"FROM {table_sql} AS {source_prefix}"
    if soft:
        score_terms: list[str] = []
        for constraint, weight in soft:
            predicate = _compile_predicate(constraint, schema, fields, parts, source)
            score_terms.append(f"SUM(CASE WHEN {predicate} THEN {parts.bind(weight)} ELSE 0 END)")
        inner_select.append(f"({ ' + '.join(score_terms) }) AS {parts.quote(_TCpr_SCORE)}")
        # Placeholder order follows the SQL text: score predicates first,
        # then hard WHERE values, then the server-side LIMIT.
        where_sql = _compile_predicate(hard, schema, fields, parts, source)
        group_fields = [f"{source_prefix}.{parts.quote(fields[attr].column)}" for attr in selected_attrs]
        if primary_key not in selected_attrs:
            group_fields.append(f"{source_prefix}.{parts.quote(pk_field.column)}")
        inner_sql = (
            "SELECT " + ", ".join(inner_select) + " " + from_sql +
            " WHERE " + where_sql + " GROUP BY " + ", ".join(group_fields)
        )
        ranked = parts.quote("__tcpr_ranked")
        outer_columns = ", ".join(f"{ranked}.{parts.quote(attr)}" for attr in selected_attrs)
        order_pk = parts.quote(primary_key if primary_key in selected_attrs else _TCPR_PK)
        sql_text = (
            f"SELECT {outer_columns} FROM ({inner_sql}) AS {ranked} "
            f"ORDER BY {ranked}.{parts.quote(_TCpr_SCORE)} DESC, {ranked}.{order_pk} ASC "
            f"LIMIT {parts.bind(top_k + 1)}"
        )
    else:
        where_sql = _compile_predicate(hard, schema, fields, parts, source)
        selected_sql = ", ".join(inner_select[:len(selected_attrs)])
        sql_text = (
            f"SELECT {selected_sql} {from_sql} WHERE {where_sql} "
            f"ORDER BY {source_prefix}.{parts.quote(pk_field.column)} ASC LIMIT {parts.bind(top_k + 1)}"
        )
    return _CompiledQuery(sql_text, parts.args, selected_attrs, top_k, False)


def _compile_raw_query(sql: Any, tokens: Iterable[_Token], parameters: Mapping[str, Any], database_type: str) -> _CompiledQuery:
    validated, _ = _validate_sql(sql)
    compiled, args = _compile_named_parameters(validated, tokens, parameters)
    trimmed = compiled.strip()
    if tokens and tokens[-1].value == ";":
        # ``_validate_sql`` ignores trailing comments, so slice by the token
        # position rather than searching the raw string for the last ';'.
        semicolon = next((token for token in reversed(list(tokens)) if token.value == ";"), None)
        if semicolon is not None:
            trimmed = compiled[:semicolon.start].rstrip()
    # Wrapping preserves an existing ORDER BY/LIMIT while ensuring the driver
    # can never stream an unbounded result.  The outer LIMIT itself is bound.
    alias = _quote_identifier("__tcpr_raw", database_type)
    return _CompiledQuery(f"SELECT * FROM ({trimmed}\n) AS {alias} LIMIT {DEFAULT_MAX_ROWS + 1}", args, [], DEFAULT_MAX_ROWS)


def _ssl_context(mode: str) -> ssl.SSLContext | None:
    if mode == "disable":
        return None
    context = ssl.create_default_context()
    context.check_hostname = mode == "verify-full"
    return context


def _connect(config: _QueryConfig):
    """Connect using a driver-specific read-only startup configuration."""

    if config.database_type == "postgresql":
        try:
            driver = importlib.import_module("pg8000.dbapi")
        except ImportError as exc:
            raise RemoteQueryError("DRIVER_UNAVAILABLE", "PostgreSQL driver is unavailable") from exc
        kwargs: dict[str, Any] = {
            "host": config.host,
            "port": config.port,
            "database": config.database,
            "user": config.username,
            "password": config.password,
            "timeout": DEFAULT_CONNECT_TIMEOUT,
            # These startup parameters are enforced by PostgreSQL itself
            # before the user statement.  ``options`` is not a pg8000 API.
            "startup_params": {
                "default_transaction_read_only": "on",
                "statement_timeout": str(DEFAULT_QUERY_TIMEOUT * 1000),
            },
        }
        context = _ssl_context(config.ssl_mode)
        if context is not None:
            kwargs["ssl_context"] = context
        else:
            kwargs["ssl_context"] = False
        try:
            return driver.connect(**kwargs)
        except RemoteQueryError:
            raise
        except Exception as exc:
            raise RemoteQueryError("CONNECTION_FAILED", "unable to connect to the remote database") from exc

    try:
        driver = importlib.import_module("pymysql")
    except ImportError as exc:
        raise RemoteQueryError("DRIVER_UNAVAILABLE", "MySQL driver is unavailable") from exc
    kwargs = {
        "host": config.host,
        "port": config.port,
        "database": config.database,
        "user": config.username,
        "password": config.password,
        "connect_timeout": DEFAULT_CONNECT_TIMEOUT,
        "read_timeout": DEFAULT_QUERY_TIMEOUT,
        "write_timeout": DEFAULT_QUERY_TIMEOUT,
        "autocommit": False,
    }
    if config.ssl_mode != "disable":
        kwargs["ssl"] = {"check_hostname": config.ssl_mode == "verify-full"}
    try:
        return driver.connect(**kwargs)
    except RemoteQueryError:
        raise
    except Exception as exc:
        raise RemoteQueryError("CONNECTION_FAILED", "unable to connect to the remote database") from exc


def _execute_read_only(connection: Any, config: _QueryConfig, sql: str, args: list[Any], *, max_rows: int):
    cursor = None
    try:
        # PostgreSQL's startup options cover read-only and statement timeout;
        # BEGIN READ ONLY makes the transaction boundary explicit.  MySQL
        # requires the server-side transaction mode command before the query.
        cursor = connection.cursor()
        if config.database_type == "postgresql":
            cursor.execute("BEGIN READ ONLY")
        else:
            # START TRANSACTION READ ONLY is enforced by MySQL server; it is
            # deliberately not a client-side SQL prefix check.
            cursor.execute("START TRANSACTION READ ONLY")
        cursor.execute(sql, args)
        description = getattr(cursor, "description", None) or []
        columns = [str(item[0]) for item in description]
        if len({column.casefold() for column in columns}) != len(columns):
            raise RemoteQueryError("DUPLICATE_COLUMN", "query returned duplicate column names")
        fetchmany = getattr(cursor, "fetchmany", None)
        if callable(fetchmany):
            rows = list(fetchmany(max_rows + 1))
        else:
            rows = list(cursor.fetchall())[: max_rows + 1]
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        return columns, rows, truncated
    except RemoteQueryError:
        raise
    except TimeoutError as exc:
        raise RemoteQueryError("QUERY_TIMEOUT", "remote query timed out") from exc
    except Exception as exc:
        # Do not expose server error strings: they commonly contain hostnames,
        # SQL fragments, or credential-bearing connection details.
        text = str(exc).lower()
        if "timeout" in text or "timed out" in text:
            raise RemoteQueryError("QUERY_TIMEOUT", "remote query timed out") from exc
        raise RemoteQueryError("QUERY_FAILED", "remote query failed") from exc
    finally:
        close_cursor = getattr(cursor, "close", None)
        if callable(close_cursor):
            try:
                close_cursor()
            except Exception:
                pass


def _rollback(connection: Any) -> None:
    """End the read-only transaction without ever committing remote state."""

    rollback = getattr(connection, "rollback", None)
    if callable(rollback):
        try:
            rollback()
        except Exception:
            # The connection is closed immediately after this call; a driver
            # error while rolling back must not replace the stable result.
            pass


def _serialize(value: Any, *, key: str | None = None) -> Any:
    if key and _SENSITIVE_NAME.search(key):
        return "[REDACTED]"
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (_datetime.datetime, _datetime.date, _datetime.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"base64": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, Mapping):
        return {str(k): _serialize(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_serialize(item) for item in value]
    # Driver-specific values should not make a successful response fail.  A
    # stable string is preferable to repr(), which may include memory paths.
    return str(value)


def run_remote_query(tool_parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Validate, compile, execute, and serialize one bounded remote query."""

    if not isinstance(tool_parameters, Mapping):
        raise RemoteQueryError("INVALID_INPUT", "tool parameters must be an object")
    config = _config(tool_parameters)
    if config.query_mode == "tcpr":
        # TCPR is the default and is deliberately strict: missing/invalid
        # schema or query never falls through to a raw SQL field.
        if tool_parameters.get("sql") not in (None, "") or tool_parameters.get("parameters_json") not in (None, "", {}):
            raise RemoteQueryError("INVALID_INPUT", "sql and parameters_json require explicit raw_sql mode")
        try:
            compiled = compile_tcpr_query(
                config.database_type,
                config.table,
                tool_parameters.get("tcpr_schema_json"),
                tool_parameters.get("tcpr_query_json"),
            )
        except RemoteQueryError:
            raise
        except Exception as exc:
            raise RemoteQueryError("INVALID_INPUT", "tcpr query is invalid") from exc
        if compiled.unsat:
            return {
                "status": "OK",
                "database_type": config.database_type,
                "query_mode": config.query_mode,
                "rows": [],
                "row_count": 0,
                "columns": compiled.columns,
                "truncated": False,
            }
        compiled_sql = compiled.sql
        args = compiled.args
        output_limit = compiled.top_k
    else:
        # raw_sql is an explicit form choice; it is never selected as a
        # compatibility fallback when TCPR inputs are missing.
        sql, tokens = _validate_sql(tool_parameters.get("sql"))
        params = _parse_parameters(tool_parameters.get("parameters_json"))
        compiled = _compile_raw_query(sql, tokens, params, config.database_type)
        compiled_sql = compiled.sql
        args = compiled.args
        output_limit = DEFAULT_MAX_ROWS
    assert compiled_sql is not None
    connection = _connect(config)
    try:
        columns, raw_rows, truncated = _execute_read_only(connection, config, compiled_sql, args, max_rows=output_limit)
        rows: list[dict[str, Any]] = []
        for raw in raw_rows:
            if isinstance(raw, Mapping):
                row = {str(key): _serialize(value, key=str(key)) for key, value in raw.items()}
            else:
                values = list(raw) if isinstance(raw, (tuple, list)) else [raw]
                row = {
                    columns[index] if index < len(columns) else f"column_{index + 1}": _serialize(value)
                    for index, value in enumerate(values)
                }
            rows.append(row)
        return {
            "status": "OK",
            "database_type": config.database_type,
            "query_mode": config.query_mode,
            "rows": rows,
            "row_count": len(rows),
            "columns": columns,
            "truncated": truncated,
        }
    finally:
        _rollback(connection)
        close = getattr(connection, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


class RemoteQueryTool(_sdk_compat.ToolBase):
    """Dify adapter exposing only the bounded remote query operation."""

    def _invoke(self, tool_parameters: dict[str, Any]):
        try:
            payload = run_remote_query(tool_parameters)
        except Exception as exc:
            code = getattr(exc, "code", None) or "ERROR"
            message = getattr(exc, "message", None) or "remote query failed"
            payload = {
                "status": "ERROR",
                "database_type": str(tool_parameters.get("database_type") or ""),
                "rows": [],
                "row_count": 0,
                "columns": [],
                "truncated": False,
                "error": {"code": str(code), "message": str(message)},
            }
        yield from _sdk_compat.emit_contract(self, payload)


__all__ = [
    "DEFAULT_CONNECT_TIMEOUT", "DEFAULT_QUERY_TIMEOUT", "DEFAULT_MAX_ROWS",
    "DEFAULT_TOP_K", "RemoteQueryError", "RemoteQueryTool", "compile_tcpr_query",
    "run_remote_query",
]
