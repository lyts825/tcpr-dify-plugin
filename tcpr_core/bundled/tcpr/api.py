"""Public TCPR API; implementation lives in :mod:`tcpr.core_api`."""

from .core_api import (
    CoreError,
    CoreService,
    DirectoryKV,
    FileKV,
    InMemoryKV,
    build_database,
    build_index,
    get_index_definition,
    search,
    structure_index,
    structure_query,
)

__all__ = [
    "CoreError", "CoreService", "InMemoryKV", "FileKV", "DirectoryKV",
    "build_index", "structure_index", "get_index_definition", "build_database", "search", "structure_query",
]
