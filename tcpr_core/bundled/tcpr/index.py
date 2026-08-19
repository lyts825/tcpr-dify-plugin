from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from typing import Iterable

from .model import Constraint, ConstraintKind, MissingValue, SparseProduct


def _jsonable_value(value):
    if isinstance(value, MissingValue):
        return {"__tcpr_missing__": value.kind}
    if isinstance(value, (set, frozenset, tuple)):
        return [_jsonable_value(item) for item in value]
    return value


def value_key(value) -> str:
    """Stable, JSON-compatible key for persisted postings."""
    import json
    return json.dumps(_jsonable_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def value_from_key(value: str):
    import json
    parsed = json.loads(value)
    if isinstance(parsed, dict) and set(parsed) == {"__tcpr_missing__"}:
        return MissingValue(str(parsed["__tcpr_missing__"]))
    return parsed


class RangeIndex:
    def __init__(self, entries: Iterable[tuple[float, int]] = ()):
        self.entries = sorted(entries)
        self.values = [x[0] for x in self.entries]

    def between(self, low=None, high=None) -> set[int]:
        left = bisect_left(self.values, low) if low is not None else 0
        right = bisect_right(self.values, high) if high is not None else len(self.entries)
        return {row for _, row in self.entries[left:right]}


class InMemoryIndex:
    """Set postings backend; a RoaringBitmap backend can implement the same operations."""
    def __init__(self, products: list[SparseProduct]):
        self.products = products
        self.all_rows = set(range(len(products)))
        self.postings: dict[str, dict[object, set[int]]] = defaultdict(lambda: defaultdict(set))
        self.ranges: dict[str, RangeIndex] = {}
        for row, product in enumerate(products):
            for attr, value in product.attrs.items():
                values = value if isinstance(value, (set, frozenset, list, tuple)) else (value,)
                for item in values:
                    self.postings[attr][item].add(row)
        for attr in {a for p in products for a, v in p.attrs.items()
                     if isinstance(v, (int, float)) and not isinstance(v, bool)}:
            self.ranges[attr] = RangeIndex((p.attrs[attr], row) for row, p in enumerate(products)
                                           if attr in p.attrs and isinstance(p.attrs[attr], (int, float))
                                           and not isinstance(p.attrs[attr], bool))

    @classmethod
    def from_payload(cls, products: list[SparseProduct], payload: dict) -> "InMemoryIndex":
        """Hydrate an index from a persisted payload without rebuilding it.

        Search uses this path so a persisted posting/range index is actually
        consumed; it never silently regenerates postings from database rows.
        """
        result = cls.__new__(cls)
        result.products = products
        result.all_rows = set(range(len(products)))
        result.postings = defaultdict(lambda: defaultdict(set))
        for attr, values in payload.get("postings", {}).items():
            for encoded, rows in values.items():
                result.postings[attr][value_from_key(encoded)] = set(int(row) for row in rows)
        result.ranges = {
            attr: RangeIndex((item[0], int(item[1])) for item in entries)
            for attr, entries in payload.get("ranges", {}).items()
        }
        return result

    def to_payload(self) -> dict:
        return {
            "postings": {
                attr: {value_key(value): sorted(rows) for value, rows in values.items()}
                for attr, values in self.postings.items()
            },
            "ranges": {
                attr: [[value, row] for value, row in index.entries]
                for attr, index in self.ranges.items()
            },
        }

    def lookup_atom(self, atom: Constraint) -> tuple[set[int], str]:
        attr, kind = atom.attr, atom.kind
        if kind is ConstraintKind.EXISTS:
            return {
                i for i, p in enumerate(self.products)
                if attr in p.attrs and not isinstance(p.attrs[attr], MissingValue)
            }, [f"EXISTS({attr})"]
        if kind is ConstraintKind.RANGE:
            if attr not in self.ranges:
                return set(self.all_rows), [f"FULL({attr})"]
            return self.ranges[attr].between(atom.value, atom.value2), [f"RANGE({attr})"]
        if kind in {ConstraintKind.GE, ConstraintKind.LE}:
            return self.lookup_atom(Constraint.atom(attr, ConstraintKind.RANGE,
                                                    atom.value if kind is ConstraintKind.GE else None,
                                                    atom.value if kind is ConstraintKind.LE else None))
        if attr not in self.postings:
            return set(self.all_rows), [f"FULL({attr})"]
        if kind is ConstraintKind.EQ:
            return set(self.postings[attr].get(atom.value, set())), [f"EQ({attr})"]
        if kind is ConstraintKind.IN:
            out = set()
            for value in atom.value:
                out |= self.postings[attr].get(value, set())
            return out, [f"IN({attr})"]
        if kind is ConstraintKind.CONTAINS:
            return set(self.postings[attr].get(atom.value, set())), [f"CONTAINS({attr})"]
        if kind is ConstraintKind.SUPERSET:
            out = self.all_rows.copy()
            for value in atom.value:
                out &= self.postings[attr].get(value, set())
            return out, [f"SUPERSET({attr})"]
        # NEQ and NOT_IN are not safe to infer from sparse postings because missing is UNKNOWN.
        return set(self.all_rows), [f"FULL({attr})"]

    def candidates(self, constraint: Constraint) -> tuple[set[int], list[str]]:
        if constraint.kind is not None:
            return self.lookup_atom(constraint)
        if constraint.attr == "AND":
            rows, ops = self.all_rows.copy(), []
            for child in constraint.children:
                child_rows, child_ops = self.candidates(child)
                rows &= child_rows
                ops.extend(child_ops)
            return rows, ops
        if constraint.attr == "OR":
            rows, ops = set(), []
            for child in constraint.children:
                child_rows, child_ops = self.candidates(child)
                rows |= child_rows
                ops.extend(child_ops)
            return rows, ops
        return self.all_rows.copy(), ["FULL(compound)"]
