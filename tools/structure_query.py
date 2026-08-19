"""Canonical Dify adapter for deterministic query structuring."""

from collections.abc import Generator
import json
from typing import Any

import tcpr_core.sdk_compat as _sdk_compat
from tcpr_core.bundled.tcpr.core_api import CoreError
from tools.common import ModelFallbackError, invoke_parameter_extractor, service_for


class StructureQueryTool(_sdk_compat.ToolBase):
    """Prefer deterministic parsing, with one explicit model fallback."""

    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[Any, None, None]:
        try:
            requirement = tool_parameters.get("requirement")
            if requirement is None:
                # Keep local callers compatible with the descriptive alias;
                # the public Dify descriptor uses the single canonical field.
                requirement = tool_parameters.get("query_text")
            index_id = str(tool_parameters.get("index_id") or "")
            service = service_for(self)
            parse_source = "deterministic"
            try:
                payload = service.structure_query(requirement, index_id)
            except CoreError as deterministic_error:
                # Explicit JSON/dict input is never sent to a model for repair.
                natural_language = (
                    isinstance(requirement, str)
                    and bool(requirement.strip())
                    and requirement.strip()[:1] not in {"{", "["}
                )
                if deterministic_error.code != "INVALID_QUERY" or not natural_language:
                    raise
                definition = service.get_index_definition(index_id)["index_definition"]
                candidate = invoke_parameter_extractor(
                    self,
                    tool_parameters.get("fallback_model"),
                    requirement.strip(),
                    "Return only a JSON query object with hard and/or soft constraints. "
                    "Use only the following declared index definition; do not invent fields, "
                    "operators, values, or units: "
                    + json.dumps(definition, ensure_ascii=False, separators=(",", ":")),
                    output_key="query_json",
                )
                if isinstance(candidate, dict) and "query_json" in candidate:
                    candidate = candidate["query_json"]
                try:
                    payload = service.structure_query(candidate, index_id)
                except Exception as model_validation_error:
                    raise ModelFallbackError(
                        "MODEL_OUTPUT_INVALID",
                        "Parameter Extractor output failed query validation",
                    ) from model_validation_error
                parse_source = "model"
            payload["parse_source"] = parse_source
        except Exception as exc:
            code = getattr(exc, "code", None) or "ERROR"
            message = getattr(exc, "message", None) or str(exc)
            payload = {
                "status": "ERROR",
                "index_id": str(tool_parameters.get("index_id", "") or ""),
                "query_json": "",
                "query": {},
                "parse_source": "",
                "error": {"code": code, "message": message},
            }
        yield from _sdk_compat.emit_contract(self, payload)


__all__ = ["StructureQueryTool"]
