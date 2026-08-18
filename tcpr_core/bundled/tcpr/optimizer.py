from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .model import AND, Constraint, ConstraintKind
from .schema import Schema


class UnsatisfiableQuery(ValueError):
    pass


@dataclass(frozen=True)
class OptimizedQuery:
    constraint: Constraint
    atoms: tuple[Constraint, ...]
    unsat: bool = False


def _flatten_and(c: Constraint) -> list[Constraint]:
    if c.attr == "AND" and c.kind is None:
        result: list[Constraint] = []
        for child in c.children:
            result.extend(_flatten_and(child))
        return result
    return [c]


def optimize(constraint: Constraint, schema: Schema) -> OptimizedQuery:
    if constraint.attr == "OR" or constraint.attr == "NOT":
        return OptimizedQuery(constraint, constraint.atoms())
    atoms = _flatten_and(constraint)
    grouped: dict[str, list[Constraint]] = {}
    for atom in atoms:
        if atom.kind is None:
            return OptimizedQuery(constraint, tuple(atoms))
        schema.field(atom.attr or "")
        grouped.setdefault(atom.attr or "", []).append(atom)
    result: list[Constraint] = []
    for attr, group in grouped.items():
        lowers = [c.value for c in group if c.kind is ConstraintKind.GE]
        uppers = [c.value for c in group if c.kind is ConstraintKind.LE]
        ranges = [c for c in group if c.kind is ConstraintKind.RANGE]
        if lowers or uppers or ranges:
            low = max([*lowers, *[c.value for c in ranges if c.value is not None]], default=None)
            high = min([*uppers, *[c.value2 for c in ranges if c.value2 is not None]], default=None)
            if low is not None and high is not None and low > high:
                raise UnsatisfiableQuery(f"{attr}: empty range")
            result.append(Constraint.atom(attr, ConstraintKind.RANGE, low, high))
        result.extend(c for c in group if c.kind not in {ConstraintKind.GE, ConstraintKind.LE, ConstraintKind.RANGE})
    return OptimizedQuery(AND(*result), tuple(result))
