from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from .model import SparseProduct
from .schema import Schema


def normalize_product(raw: Mapping[str, Any], schema: Schema, product_id: str | None = None) -> SparseProduct:
    pid = str(product_id or raw.get("id") or raw.get("product_id") or "")
    if not pid:
        raise ValueError("product requires id or product_id")
    normalized: dict[str, Any] = {}
    for key, value in raw.items():
        if key in {"id", "product_id", "name", "title", "description"} or value is None:
            continue
        try:
            attr = schema.resolve_attr(key)
        except ValueError:
            continue
        spec = schema.field(attr)
        normalized[attr] = spec.canonical_value(value)
    return SparseProduct(pid, normalized, dict(raw))


def normalize_jsonl(text: str, schema: Schema) -> list[SparseProduct]:
    return [normalize_product(json.loads(line), schema) for line in text.splitlines() if line.strip()]


def products_to_jsonl(products: Iterable[SparseProduct]) -> str:
    return "\n".join(json.dumps({"id": p.product_id, **p.attrs}, ensure_ascii=False, sort_keys=True, default=list)
                   for p in products)
