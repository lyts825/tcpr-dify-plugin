from collections.abc import Generator
from typing import Any

import tcpr_core.sdk_compat as _sdk_compat
from tools.common import read_file_parameter, service_for


class BuildDatabaseTool(_sdk_compat.ToolBase):
    """Build a database snapshot whose fields exactly match an index."""

    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[Any, None, None]:
        try:
            data, filename = read_file_parameter(tool_parameters.get("file"))
            source: Any = {"data": data, "filename": filename}
            database_id = service_for(self).build_database(source, str(tool_parameters.get("index_id") or ""))
            payload = {"status": "OK", "database_id": database_id}
        except Exception as exc:
            payload = {
                "status": "ERROR",
                "database_id": "",
                "error": {"code": "ERROR", "message": str(exc)},
            }
        yield from _sdk_compat.emit_contract(self, payload)
