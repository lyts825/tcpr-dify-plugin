from collections.abc import Generator
from typing import Any

import tcpr_core.sdk_compat as _sdk_compat
from tcpr_core.bundled.tcpr.core_api import CoreError
from tools.common import ModelFallbackError, invoke_parameter_extractor, service_for


class BuildIndexTool(_sdk_compat.ToolBase):
    """Save and atomically activate a user-authored logical index definition."""

    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[Any, None, None]:
        service = None
        try:
            index_json = tool_parameters.get("index_json")
            manual = isinstance(index_json, str) and bool(index_json.strip())
            if isinstance(index_json, dict):
                manual = True
            if index_json is not None and not manual and not isinstance(index_json, str):
                raise CoreError("INVALID_INPUT", "index_json must be a non-empty JSON string")
            service = service_for(self)
            parse_source = "manual"
            if manual:
                index_id = service.build_index(index_json)
            else:
                requirement = tool_parameters.get("index_requirement")
                if not isinstance(requirement, str) or not requirement.strip():
                    raise CoreError("INVALID_INPUT", "index_json or index_requirement is required")
                try:
                    structured = service.structure_index(requirement)
                    candidate = structured["index_json"]
                    parse_source = "deterministic"
                except Exception as deterministic_error:
                    code = getattr(deterministic_error, "code", "")
                    if code not in {"INVALID_INPUT"}:
                        raise
                    candidate = invoke_parameter_extractor(
                        self,
                        tool_parameters.get("fallback_model"),
                        requirement,
                        "Return only a complete index definition JSON object with primary_key and attributes. "
                        "Declare every field and its kind; declare numeric units when they are known. "
                        "Do not infer omitted fields, types, units, or a primary key.",
                        output_key="index_json",
                    )
                    # A structured output wrapper is accepted, but still goes
                    # through the core's strict validator before persistence.
                    if isinstance(candidate, dict) and "index_json" in candidate:
                        candidate = candidate["index_json"]
                    try:
                        index_id = service.build_index(candidate)
                    except Exception as model_validation_error:
                        raise ModelFallbackError(
                            "MODEL_OUTPUT_INVALID",
                            "Parameter Extractor output failed index validation",
                        ) from model_validation_error
                    parse_source = "model"
                else:
                    index_id = service.build_index(candidate)
            definition = service.get_index_definition(index_id)
            payload = {
                "status": "OK",
                "index_id": index_id,
                "parse_source": parse_source,
                "index_json": definition["index_json"],
                "index_definition": definition["index_definition"],
            }
        except Exception as exc:
            code = getattr(exc, "code", None) or "ERROR"
            message = getattr(exc, "message", None) or str(exc)
            payload = {
                "status": "ERROR",
                "index_id": "",
                "parse_source": "",
                "index_json": "",
                "index_definition": {},
                "error": {"code": code, "message": message},
            }
        yield from _sdk_compat.emit_contract(self, payload)
