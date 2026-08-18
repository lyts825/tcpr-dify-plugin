from __future__ import annotations

from .model import Constraint, SparseProduct, TriValue


def validate(product: SparseProduct, constraint: Constraint) -> TriValue:
    return constraint.evaluate(product)


def project(product: SparseProduct, constraint: Constraint) -> SparseProduct:
    attrs = {atom.attr for atom in constraint.atoms() if atom.attr is not None}
    return SparseProduct(product.product_id, {k: v for k, v in product.attrs.items() if k in attrs}, product.raw)
