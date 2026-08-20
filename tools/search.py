"""Canonical Dify adapter for the public ``search`` capability."""

from collections.abc import Generator
from typing import Any

import tcpr_core.sdk_compat as _sdk_compat
from tools.common import service_for


class SearchTool(_sdk_compat.ToolBase):
    """Search a persisted index/database with the typed query contract."""

    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[Any, None, None]:
        try:
            payload = service_for(self).search(
                tool_parameters.get("query_json", ""),
                tool_parameters.get("index_id", ""),
                tool_parameters.get("database_id", ""),
            )
        except Exception as exc:
            payload = {
                "status": "ERROR",
                "index_id": str(tool_parameters.get("index_id", "") or ""),
                "database_id": str(tool_parameters.get("database_id", "") or ""),
                "results": [],
                "error": {"code": "ERROR", "message": str(exc)},
            }
        yield from _sdk_compat.emit_contract(self, payload)


__all__ = ["SearchTool"]
