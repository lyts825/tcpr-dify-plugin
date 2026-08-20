from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any, Mapping

from .model import AND, OR, Constraint, ConstraintKind, Hard, Soft
from .schema import Schema, SchemaError


class QueryError(ValueError):
    pass


def parse_ast(node: Mapping[str, Any], schema: Schema) -> Constraint:
    if "op" in node and str(node["op"]).upper() in {"AND", "OR", "NOT"}:
        op = str(node["op"]).upper()
        children = tuple(parse_ast(c, schema) for c in node.get("children", []))
        if op == "NOT":
            if len(children) != 1:
                raise QueryError("NOT requires one child")
            return Constraint(attr="NOT", children=children)
        return Constraint(attr=op, children=children)
    try:
        attr = schema.resolve_attr(str(node["attr"]))
        kind = ConstraintKind[str(node["op"]).upper()]
    except (KeyError, ValueError) as exc:
        raise QueryError(f"invalid atom: {node}") from exc
    spec = schema.field(attr)
    value = node.get("value")
    value2 = node.get("value2")
    if kind in {ConstraintKind.IN, ConstraintKind.NOT_IN}:
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise QueryError(f"{kind.value} requires a list")
        canonicalizer = (spec._canonical_multi_value
                         if spec.kind == "multi" else spec.canonical_value)
        value = frozenset(canonicalizer(v) for v in value)
    elif kind is ConstraintKind.SUPERSET:
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise QueryError("SUPERSET requires a list")
        canonicalizer = (spec._canonical_multi_value
                         if spec.kind == "multi" else spec.canonical_value)
        value = frozenset(canonicalizer(v) for v in value)
    elif kind is ConstraintKind.CONTAINS and spec.kind == "multi":
        # CONTAINS compares one canonical member of a multi-valued field.
        # Canonicalizing the whole scalar as a multi field would make the
        # operand a nested frozenset and cause every valid containment query
        # to miss.
        value = spec._canonical_multi_value(value)
    elif kind is ConstraintKind.EXISTS:
        value = None
    else:
        value = spec.canonical_value(value)
        if value2 is not None:
            value2 = spec.canonical_value(value2)
    return Constraint.atom(attr, kind, value, value2, str(node.get("label", "")))


_LEADING_RELATIONS = (
    ("不低于", ConstraintKind.GE), ("不少于", ConstraintKind.GE),
    ("至少", ConstraintKind.GE), ("最低", ConstraintKind.GE), ("以上", ConstraintKind.GE),
    ("不超过", ConstraintKind.LE), ("不高于", ConstraintKind.LE),
    ("最多", ConstraintKind.LE), ("上限", ConstraintKind.LE), ("以内", ConstraintKind.LE),
    ("以下", ConstraintKind.LE),
)
_TRAILING_RELATIONS = (
    ("以上", ConstraintKind.GE), ("以内", ConstraintKind.LE),
    ("以下", ConstraintKind.LE), ("上限", ConstraintKind.LE),
)
_SYMBOLIC = re.compile(r"^(?P<op>>=|<=|!=|=|>|<|包含|在)\s*(?P<value>.+?)\s*$")


def _split_attribute(text: str, schema: Schema) -> tuple[str, str]:
    """Resolve a schema-defined attribute prefix; never infer unknown fields."""
    candidates = sorted(schema.aliases, key=lambda item: len(item), reverse=True)
    lowered = text.lower()
    for alias in candidates:
        if lowered.startswith(alias.lower()):
            remainder = text[len(alias):].strip()
            if remainder:
                return schema.resolve_attr(alias), remainder
    raise QueryError(f"cannot resolve schema attribute in: {text}")


def _parse_relation(remainder: str) -> tuple[ConstraintKind, str]:
    symbolic = _SYMBOLIC.match(remainder)
    if symbolic:
        op = symbolic.group("op")
        kind = {">=": ConstraintKind.GE, "<=": ConstraintKind.LE,
                ">": ConstraintKind.GE, "<": ConstraintKind.LE,
                "!=": ConstraintKind.NEQ, "=": ConstraintKind.EQ,
                "包含": ConstraintKind.CONTAINS, "在": ConstraintKind.IN}[op]
        return kind, symbolic.group("value").strip()
    for relation, kind in _LEADING_RELATIONS:
        if remainder.startswith(relation):
            value = remainder[len(relation):].strip()
            if not value:
                raise QueryError(f"missing value after relation: {relation}")
            return kind, value
    for relation, kind in _TRAILING_RELATIONS:
        match = re.match(rf"^(.+?)\s*{re.escape(relation)}\s*$", remainder)
        if match:
            value = match.group(1).strip()
            if not value:
                raise QueryError(f"missing value before relation: {relation}")
            return kind, value
    # Bare values are safe only as schema-defined equality values. This allows
    # aliases such as GPU "4060" without inventing enum ordering.
    if remainder:
        return ConstraintKind.EQ, remainder
    raise QueryError("missing comparison")


def _parse_legacy_text(text: str, schema: Schema) -> tuple[list[Hard], list[Soft]]:
    """Parse the compact v0.4 query syntax kept for backward compatibility."""

    hard: list[Hard] = []
    soft: list[Soft] = []
    for part in re.split(r"\s*(?:且|并且|(?i:and))\s*", text.strip()):
        is_soft = bool(re.search(r"最好|优先|prefer|最好是", part, re.I))
        part = re.sub(r"(?:最好是?|优先|prefer(?:ably)?)", "", part, flags=re.I).strip()
        attr, remainder = _split_attribute(part, schema)
        kind, raw = _parse_relation(remainder)
        spec = schema.field(attr)
        val, val2 = raw, None
        if kind is ConstraintKind.IN:
            values = [item.strip() for item in re.split(r"[,，]", raw) if item.strip()]
            if not values:
                raise QueryError("IN requires at least one value")
            val = values
        elif kind in {ConstraintKind.GE, ConstraintKind.LE} and spec.kind not in {"numeric", "ordered_enum"}:
            raise QueryError(f"{attr} is {spec.kind}; ordered comparison is not schema-defined")

        def clean(v: str) -> Any:
            m = re.match(r"^([-+]?\d+(?:\.\d+)?)\s*([A-Za-z]+|元|兆|GB|MB)?$", v, re.I)
            if m and spec.kind == "numeric":
                return spec.canonical_value({"value": float(m.group(1)), "unit": m.group(2) or ""})
            try:
                return spec.canonical_value(v)
            except SchemaError as exc:
                raise QueryError(f"cannot parse value for {attr}: {v!r}") from exc
        if kind is ConstraintKind.IN:
            val = frozenset(clean(item) for item in val)
        else:
            val = clean(val)
        c = Constraint.atom(attr, kind, val, clean(val2) if val2 is not None else None, part)
        (soft if is_soft else hard).append(Soft(c) if is_soft else Hard(c))
    return hard, soft


@dataclass(frozen=True)
class _Token:
    """One token in the deterministic textual query DSL."""

    kind: str
    value: str
    position: int


def _tokenize_dsl(source: str) -> list[_Token]:
    """Tokenize the small query language without evaluating any user input."""

    tokens: list[_Token] = []
    position = 0
    symbols = {"(": "LPAREN", ")": "RPAREN", ",": "COMMA"}
    while position < len(source):
        char = source[position]
        if char.isspace():
            position += 1
            continue
        if char in symbols:
            tokens.append(_Token(symbols[char], char, position))
            position += 1
            continue
        if char in {"'", '"'}:
            quote = char
            start = position
            position += 1
            value: list[str] = []
            while position < len(source):
                current = source[position]
                if current == "\\":
                    position += 1
                    if position >= len(source):
                        raise QueryError(f"unterminated escape at position {start}")
                    escaped = source[position]
                    value.append({"n": "\n", "r": "\r", "t": "\t"}.get(escaped, escaped))
                    position += 1
                    continue
                if current == quote:
                    tokens.append(_Token("STRING", "".join(value), start))
                    position += 1
                    break
                value.append(current)
                position += 1
            else:
                raise QueryError(f"unterminated quoted value at position {start}")
            continue
        for operator in (">=", "<=", "!=", "<>", "=="):
            if source.startswith(operator, position):
                tokens.append(_Token("OP", operator, position))
                position += len(operator)
                break
        else:
            if char in {"=", ">", "<"}:
                tokens.append(_Token("OP", char, position))
                position += 1
                continue
            start = position
            while position < len(source) and not source[position].isspace() and source[position] not in "(),=<>!\"'":
                position += 1
            if start == position:
                raise QueryError(f"unexpected token at position {position}")
            tokens.append(_Token("WORD", source[start:position], start))
            continue
        # A multi-character symbolic operator was emitted by the ``for`` loop.
        continue
    return tokens


def _split_dsl_sections(text: str) -> list[str]:
    """Split top-level ``;`` sections while preserving values and groups."""

    sections: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    escaped = False
    for position, char in enumerate(text):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise QueryError("unbalanced parentheses in query DSL")
        elif char == ";" and depth == 0:
            section = text[start:position].strip()
            if not section:
                raise QueryError("query DSL contains an empty section")
            sections.append(section)
            start = position + 1
    if quote is not None or depth:
        raise QueryError("unbalanced quotes or parentheses in query DSL")
    section = text[start:].strip()
    if not section:
        raise QueryError("query DSL cannot end with a section separator")
    sections.append(section)
    return sections


_SECTION_RE = re.compile(
    r"^\s*(?P<kind>hard|soft)(?:\s*\(\s*(?:weight\s*=\s*)?"
    r"(?P<weight>[-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*\))?\s*:\s*(?P<expression>.+?)\s*$",
    re.IGNORECASE,
)
_SECTION_START_RE = re.compile(r"^\s*(?:hard|soft)\b", re.IGNORECASE)


class _QueryDSLParser:
    """Recursive-descent parser for the schema-aware textual query DSL.

    The grammar deliberately stays small and maps one-to-one onto the existing
    JSON constraint AST.  Attribute names are resolved only through ``Schema``
    aliases, so the DSL never introduces an inferred field.
    """

    _LOGICAL = {
        "AND": {"and", "且", "并且"},
        "OR": {"or", "或"},
        "NOT": {"not", "非"},
    }
    _RELATIONS = {
        "eq": ConstraintKind.EQ,
        "equals": ConstraintKind.EQ,
        "is": ConstraintKind.EQ,
        "neq": ConstraintKind.NEQ,
        "contains": ConstraintKind.CONTAINS,
        "superset": ConstraintKind.SUPERSET,
        "exists": ConstraintKind.EXISTS,
        "in": ConstraintKind.IN,
        "between": ConstraintKind.RANGE,
        "range": ConstraintKind.RANGE,
        "至少": ConstraintKind.GE,
        "不低于": ConstraintKind.GE,
        "不少于": ConstraintKind.GE,
        "以上": ConstraintKind.GE,
        "最多": ConstraintKind.LE,
        "不超过": ConstraintKind.LE,
        "不高于": ConstraintKind.LE,
        "以下": ConstraintKind.LE,
        "包含": ConstraintKind.CONTAINS,
        "存在": ConstraintKind.EXISTS,
    }

    def __init__(self, source: str, schema: Schema):
        self.tokens = _tokenize_dsl(source)
        self.schema = schema
        self.position = 0

    def parse(self) -> Constraint:
        if not self.tokens:
            raise QueryError("query expression is required")
        constraint = self._parse_or()
        if self._peek() is not None:
            token = self._peek()
            raise QueryError(f"unexpected token {token.value!r} at position {token.position}")
        return constraint

    def _peek(self, offset: int = 0) -> _Token | None:
        target = self.position + offset
        return self.tokens[target] if target < len(self.tokens) else None

    def _take(self) -> _Token:
        token = self._peek()
        if token is None:
            raise QueryError("unexpected end of query expression")
        self.position += 1
        return token

    @staticmethod
    def _word_matches(token: _Token | None, values: set[str]) -> bool:
        return token is not None and token.kind == "WORD" and token.value.casefold() in values

    def _take_logical(self, kind: str) -> bool:
        if self._word_matches(self._peek(), self._LOGICAL[kind]):
            self.position += 1
            return True
        return False

    def _parse_or(self) -> Constraint:
        children = [self._parse_and()]
        while self._take_logical("OR"):
            children.append(self._parse_and())
        return children[0] if len(children) == 1 else OR(*children)

    def _parse_and(self) -> Constraint:
        children = [self._parse_unary()]
        while self._take_logical("AND"):
            children.append(self._parse_unary())
        return children[0] if len(children) == 1 else AND(*children)

    def _parse_unary(self) -> Constraint:
        if self._take_logical("NOT"):
            return Constraint(attr="NOT", children=(self._parse_unary(),))
        token = self._peek()
        if token is not None and token.kind == "LPAREN":
            self._take()
            constraint = self._parse_or()
            closing = self._take()
            if closing.kind != "RPAREN":
                raise QueryError(f"expected ')' at position {closing.position}")
            return constraint
        return self._parse_predicate()

    def _parse_predicate(self) -> Constraint:
        attr = self._take_attribute()
        kind = self._take_relation()
        document: dict[str, Any] = {"attr": attr, "op": kind.value}
        if kind is ConstraintKind.EXISTS:
            return parse_ast(document, self.schema)
        if kind is ConstraintKind.RANGE:
            document["value"] = self._take_scalar()
            if not self._take_logical("AND"):
                token = self._peek()
                where = "end of expression" if token is None else f"position {token.position}"
                raise QueryError(f"BETWEEN requires AND before {where}")
            document["value2"] = self._take_scalar()
        elif kind in {ConstraintKind.IN, ConstraintKind.NOT_IN, ConstraintKind.SUPERSET}:
            document["value"] = self._take_values()
        else:
            document["value"] = self._take_scalar()
        return parse_ast(document, self.schema)

    def _take_attribute(self) -> str:
        token = self._peek()
        if token is not None and token.kind == "STRING":
            self.position += 1
            try:
                return self.schema.resolve_attr(token.value)
            except SchemaError as exc:
                raise QueryError(f"unknown attribute: {token.value}") from exc
        # Match the longest declared alias token sequence.  Quoting remains
        # available for aliases containing punctuation or ambiguous whitespace.
        matches: list[tuple[int, str]] = []
        for alias in self.schema.aliases:
            alias_tokens = _tokenize_dsl(alias)
            if not alias_tokens or any(item.kind != "WORD" for item in alias_tokens):
                continue
            candidate = self.tokens[self.position:self.position + len(alias_tokens)]
            if len(candidate) != len(alias_tokens):
                continue
            if all(
                actual.kind == "WORD" and actual.value.casefold() == expected.value.casefold()
                for actual, expected in zip(candidate, alias_tokens)
            ):
                matches.append((len(alias_tokens), alias))
        if not matches:
            token = self._peek()
            where = "end of expression" if token is None else f"position {token.position}"
            raise QueryError(f"cannot resolve schema attribute at {where}")
        length, alias = max(matches, key=lambda item: item[0])
        self.position += length
        return self.schema.resolve_attr(alias)

    def _take_relation(self) -> ConstraintKind:
        token = self._peek()
        if token is not None and token.kind == "OP":
            self.position += 1
            return {
                "=": ConstraintKind.EQ,
                "==": ConstraintKind.EQ,
                "!=": ConstraintKind.NEQ,
                "<>": ConstraintKind.NEQ,
                ">=": ConstraintKind.GE,
                ">": ConstraintKind.GE,
                "<=": ConstraintKind.LE,
                "<": ConstraintKind.LE,
            }[token.value]
        if self._word_matches(token, {"not"}):
            next_token = self._peek(1)
            if self._word_matches(next_token, {"in"}):
                self.position += 2
                return ConstraintKind.NOT_IN
        if token is not None and token.kind == "WORD":
            kind = self._RELATIONS.get(token.value.casefold())
            if kind is not None:
                self.position += 1
                return kind
        # The compact form ``color red`` remains valid equality syntax.
        return ConstraintKind.EQ

    def _take_scalar(self) -> str:
        token = self._take()
        if token.kind not in {"WORD", "STRING"}:
            raise QueryError(f"expected a value at position {token.position}")
        return token.value

    def _take_values(self) -> list[str]:
        if self._peek() is not None and self._peek().kind == "LPAREN":
            self._take()
            values: list[str] = []
            if self._peek() is not None and self._peek().kind == "RPAREN":
                raise QueryError("set operation requires at least one value")
            while True:
                values.append(self._take_scalar())
                token = self._take()
                if token.kind == "RPAREN":
                    return values
                if token.kind != "COMMA":
                    raise QueryError(f"expected ',' or ')' at position {token.position}")
        values = [self._take_scalar()]
        while self._peek() is not None and self._peek().kind == "COMMA":
            self._take()
            values.append(self._take_scalar())
        return values


def _parse_query_dsl(text: str, schema: Schema) -> tuple[list[Hard], list[Soft]]:
    """Parse labelled generic DSL sections into the existing query model.

    ``hard: <expression>`` filters results, while each
    ``soft(weight=N): <expression>`` contributes a score when it matches.  A
    query with no section header is parsed as one hard expression, which keeps
    the language useful outside the Dify adapter as well.
    """

    sections = _split_dsl_sections(text)
    matches = [_SECTION_RE.match(section) for section in sections]
    if any(match is not None for match in matches) and any(match is None for match in matches):
        raise QueryError("every ';'-separated query DSL section must start with hard: or soft:")

    if all(match is None for match in matches):
        if len(sections) != 1:
            raise QueryError("multiple query expressions require hard: or soft: section labels")
        return [Hard(_QueryDSLParser(sections[0], schema).parse())], []

    hard: list[Hard] = []
    soft: list[Soft] = []
    for match in matches:
        assert match is not None
        kind = match.group("kind").casefold()
        weight_text = match.group("weight")
        if kind == "hard" and weight_text is not None:
            raise QueryError("hard sections cannot declare a weight")
        weight = 1.0
        if weight_text is not None:
            weight = float(weight_text)
            if not math.isfinite(weight):
                raise QueryError("soft weight must be finite")
        constraint = _QueryDSLParser(match.group("expression"), schema).parse()
        if kind == "hard":
            hard.append(Hard(constraint))
        else:
            soft.append(Soft(constraint, weight))
    return hard, soft


def _requires_generic_dsl(text: str) -> bool:
    """Route unambiguous advanced syntax to the generic expression parser."""

    if _SECTION_START_RE.match(text) or any(char in text for char in "();"):
        return True
    return bool(re.search(
        r"(?i)\b(?:or|not|in|between|range|contains|superset|exists)\b|\s或\s",
        text,
    ))


def parse_text(text: str, schema: Schema) -> tuple[list[Hard], list[Soft]]:
    """Parse text as the generic query DSL or the compatible compact syntax.

    Generic examples::

        hard: (color IN (red, blue) OR NOT color = green) AND ram BETWEEN 8GB AND 16GB;
        soft(weight=2): features SUPERSET (thunderbolt, fingerprint)

    Existing compact conditions such as ``颜色=红色 且 count>=2`` retain their
    previous document shape and semantics.
    """

    if not isinstance(text, str) or not text.strip():
        raise QueryError("query text is required")
    if _requires_generic_dsl(text):
        return _parse_query_dsl(text, schema)
    return _parse_legacy_text(text, schema)
