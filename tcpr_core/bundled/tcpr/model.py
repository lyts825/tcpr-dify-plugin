from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class MissingValue:
    """Typed marker used when a dynamic-schema record lacks an attribute.

    It is deliberately an object rather than a magic string/number, so a
    business value can never collide with the missing-value representation.
    """

    kind: str


def is_missing_value(value: Any) -> bool:
    return isinstance(value, MissingValue)


class TriValue(Enum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"

    def __and__(self, other: "TriValue") -> "TriValue":
        if self is TriValue.FALSE or other is TriValue.FALSE:
            return TriValue.FALSE
        if self is TriValue.UNKNOWN or other is TriValue.UNKNOWN:
            return TriValue.UNKNOWN
        return TriValue.TRUE

    def __or__(self, other: "TriValue") -> "TriValue":
        if self is TriValue.TRUE or other is TriValue.TRUE:
            return TriValue.TRUE
        if self is TriValue.UNKNOWN or other is TriValue.UNKNOWN:
            return TriValue.UNKNOWN
        return TriValue.FALSE

    def negate(self) -> "TriValue":
        return {TriValue.TRUE: TriValue.FALSE, TriValue.FALSE: TriValue.TRUE,
                TriValue.UNKNOWN: TriValue.UNKNOWN}[self]


TruthValue = TriValue


class ConstraintKind(Enum):
    EQ = "EQ"
    NEQ = "NEQ"
    IN = "IN"
    NOT_IN = "NOT_IN"
    RANGE = "RANGE"
    GE = "GE"
    LE = "LE"
    EXISTS = "EXISTS"
    CONTAINS = "CONTAINS"
    SUPERSET = "SUPERSET"


@dataclass(frozen=True)
class Constraint:
    attr: str | None = None
    kind: ConstraintKind | None = None
    value: Any = None
    value2: Any = None
    children: tuple["Constraint", ...] = ()
    label: str = ""

    @staticmethod
    def atom(attr: str, kind: ConstraintKind, value: Any = None,
             value2: Any = None, label: str = "") -> "Constraint":
        return Constraint(attr=attr, kind=kind, value=value, value2=value2, label=label)

    def evaluate(self, product: "SparseProduct") -> TriValue:
        if self.kind is None:
            if self.attr == "AND":
                result = TriValue.TRUE
                for child in self.children:
                    result = result & child.evaluate(product)
                return result
            if self.attr == "OR":
                result = TriValue.FALSE
                for child in self.children:
                    result = result | child.evaluate(product)
                return result
            if self.attr == "NOT":
                return self.children[0].evaluate(product).negate()
            raise ValueError("invalid compound constraint")
        present = self.attr in product.attrs
        if not present or is_missing_value(product.attrs[self.attr]):
            return TriValue.UNKNOWN
        actual = product.attrs[self.attr]
        k = self.kind
        if k is ConstraintKind.EXISTS:
            return TriValue.TRUE
        if k is ConstraintKind.EQ:
            return TriValue.TRUE if actual == self.value else TriValue.FALSE
        if k is ConstraintKind.NEQ:
            return TriValue.TRUE if actual != self.value else TriValue.FALSE
        if k is ConstraintKind.IN:
            return TriValue.TRUE if actual in self.value else TriValue.FALSE
        if k is ConstraintKind.NOT_IN:
            return TriValue.TRUE if actual not in self.value else TriValue.FALSE
        if k is ConstraintKind.GE:
            return TriValue.TRUE if actual >= self.value else TriValue.FALSE
        if k is ConstraintKind.LE:
            return TriValue.TRUE if actual <= self.value else TriValue.FALSE
        if k is ConstraintKind.RANGE:
            low_ok = self.value is None or actual >= self.value
            high_ok = self.value2 is None or actual <= self.value2
            return TriValue.TRUE if low_ok and high_ok else TriValue.FALSE
        actual_set = set(actual) if isinstance(actual, (set, frozenset, list, tuple)) else {actual}
        if k is ConstraintKind.CONTAINS:
            return TriValue.TRUE if self.value in actual_set else TriValue.FALSE
        if k is ConstraintKind.SUPERSET:
            return TriValue.TRUE if set(self.value).issubset(actual_set) else TriValue.FALSE
        raise ValueError(f"unsupported operation: {k}")

    def atoms(self) -> tuple["Constraint", ...]:
        if self.kind is not None:
            return (self,)
        result: list[Constraint] = []
        for child in self.children:
            result.extend(child.atoms())
        return tuple(result)

    def to_dict(self) -> dict[str, Any]:
        if self.kind is None:
            return {"op": self.attr, "children": [x.to_dict() for x in self.children]}
        data = {"attr": self.attr, "op": self.kind.value, "value": self.value}
        if self.value2 is not None:
            data["value2"] = self.value2
        if self.label:
            data["label"] = self.label
        return data


def AND(*children: Constraint) -> Constraint:
    return Constraint(attr="AND", children=tuple(children))


def OR(*children: Constraint) -> Constraint:
    return Constraint(attr="OR", children=tuple(children))


def NOT(child: Constraint) -> Constraint:
    return Constraint(attr="NOT", children=(child,))


@dataclass(frozen=True)
class Hard:
    constraint: Constraint


@dataclass(frozen=True)
class Soft:
    constraint: Constraint
    weight: float = 1.0


@dataclass(frozen=True)
class SparseProduct:
    product_id: str
    attrs: Mapping[str, Any]
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False)

    def has(self, attr: str) -> bool:
        return attr in self.attrs


def conjunction(parts: Iterable[Constraint]) -> Constraint:
    items = tuple(parts)
    return AND(*items) if items else AND()
