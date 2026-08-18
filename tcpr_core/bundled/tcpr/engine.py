from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .index import InMemoryIndex
from .model import Hard, Soft, SparseProduct, TriValue
from .optimizer import UnsatisfiableQuery, optimize
from .schema import Schema
from .validator import validate


@dataclass(frozen=True)
class SearchHit:
    product: SparseProduct
    soft_score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class SearchResult:
    hits: tuple[SearchHit, ...]
    plan: dict


class Retriever:
    def __init__(self, products: Iterable[SparseProduct], schema: Schema, index: InMemoryIndex | None = None):
        self.products = list(products)
        self.schema = schema
        self.index = index if index is not None else InMemoryIndex(self.products)

    def search(self, hard: Iterable[Hard], soft: Iterable[Soft] = ()) -> SearchResult:
        hard = tuple(hard)
        soft = tuple(soft)
        from .model import AND
        combined = AND(*(x.constraint for x in hard))
        try:
            optimized = optimize(combined, self.schema)
        except UnsatisfiableQuery as exc:
            return SearchResult((), {"status": "UNSAT", "reason": str(exc), "candidate_count": 0})
        candidates, operations = self.index.candidates(optimized.constraint)
        hits: list[SearchHit] = []
        rejected: dict[str, int] = {}
        for row in sorted(candidates):
            product = self.products[row]
            values = [validate(product, item.constraint) for item in hard]
            if all(value is TriValue.TRUE for value in values):
                score = 0.0
                reasons: list[str] = []
                for item in soft:
                    value = validate(product, item.constraint)
                    if value is TriValue.TRUE:
                        score += item.weight
                        reasons.append(item.constraint.label or item.constraint.to_dict().__str__())
                hits.append(SearchHit(product, score, tuple(reasons)))
            else:
                for item, value in zip(hard, values):
                    if value is not TriValue.TRUE:
                        key = item.constraint.label or item.constraint.attr or "constraint"
                        rejected[key] = rejected.get(key, 0) + 1
        hits.sort(key=lambda hit: (-hit.soft_score, hit.product.product_id))
        return SearchResult(tuple(hits), {
            "status": "OK", "candidate_count": len(candidates), "verified_count": len(hits),
            "index_operations": operations, "rejected": rejected,
            "hard_constraints": [x.constraint.to_dict() for x in hard],
            "soft_constraints": [x.constraint.to_dict() for x in soft],
        })

    def full_scan(self, hard: Iterable[Hard], soft: Iterable[Soft] = ()) -> SearchResult:
        hard, soft = tuple(hard), tuple(soft)
        hits = []
        for product in self.products:
            if all(validate(product, item.constraint) is TriValue.TRUE for item in hard):
                score = sum(item.weight for item in soft if validate(product, item.constraint) is TriValue.TRUE)
                hits.append(SearchHit(product, score, ()))
        hits.sort(key=lambda hit: (-hit.soft_score, hit.product.product_id))
        return SearchResult(tuple(hits), {"status": "FULL_SCAN", "candidate_count": len(self.products)})
