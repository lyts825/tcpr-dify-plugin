"""Load the bundled TCPR core used by every packaged plugin runtime.

The Dify repository is deliberately standalone.  This adapter never probes a
parent checkout or imports a separately installed ``tcpr`` package; the
versioned copy under ``tcpr_core/bundled`` is the repository's runtime source
of truth.
"""

from .bundled.tcpr.core_api import (
    CoreError,
    CoreService,
    DirectoryKV,
    FileKV,
    InMemoryKV,
    build_database,
    build_index,
    search,
)

__all__ = [
    "CoreError", "CoreService", "InMemoryKV", "FileKV", "DirectoryKV",
    "build_index", "build_database", "search",
]
