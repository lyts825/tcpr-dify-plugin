from collections.abc import Generator
from typing import Any

import tcpr_core.sdk_compat as _sdk_compat
from tcpr_core.service import TcprService
from tools.common import read_file_parameter, service_for


class BuildIndexTool(_sdk_compat.ToolBase):
    """Build and atomically activate a versioned dynamic-attribute index."""

    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[Any, None, None]:
        try:
            data, filename = read_file_parameter(tool_parameters.get("file"))
            primary_key = str(tool_parameters.get("primary_key") or "").strip()
            source: Any = {"data": data, "filename": filename}
            if primary_key:
                source["primary_key"] = primary_key
            index_id = service_for(self).build_index(source)
            payload = {"status": "OK", "index_id": index_id}
        except Exception as exc:
            payload = {"status": "ERROR", "index_id": "", "error": str(exc)}
        yield from _sdk_compat.emit_contract(self, payload)
