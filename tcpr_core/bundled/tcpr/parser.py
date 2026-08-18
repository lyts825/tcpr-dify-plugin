from __future__ import annotations

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


def parse_text(text: str, schema: Schema) -> tuple[list[Hard], list[Soft]]:
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
