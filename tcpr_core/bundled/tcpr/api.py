"""Public three-operation API; implementation lives in :mod:`tcpr.core_api`."""

from .core_api import CoreError, CoreService, DirectoryKV, FileKV, InMemoryKV, build_database, build_index, search

__all__ = [
    "CoreError", "CoreService", "InMemoryKV", "FileKV", "DirectoryKV",
    "build_index", "build_database", "search",
]
