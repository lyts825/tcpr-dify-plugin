"""TCPR 插件工具层的公共辅助模块。

本模块为 ``tools`` 目录下的各个 Dify 工具提供共享的辅助函数：

- ``service_for``：根据 Dify 工具运行时对象构造 :class:`TcprService`，
  把 Dify 的 KV 存储适配成 TCPR 自己的 ``StorageAdapter`` 边界，
  屏蔽 Dify SDK 与本地测试环境在存储挂载点上的差异；
- ``read_file_parameter``：把 Dify 传入的各种形态的文件参数
  （bytes / Dify 文件对象 / dict / 文件流 / 本地路径）
  统一解析为 ``(data: bytes, filename: str)`` 二元组，
  供 ``import_products`` 工具后续解析 Excel 使用。

本模块只做字节层面的搬运与校验，不涉及业务解析；
Excel 表头与商品行的解析逻辑位于 ``tcpr_core.ingest``。
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tcpr_core.shared_core import CoreService
from tcpr_core.storage import RuntimeKVStorageAdapter

# 单个导入文件的大小上限：64 * 1024 * 1024 = 64 MiB。
# 仅约束 import_products 一次导入的 Excel 体积，防止超大文件拖垮索引构建，
# 与 manifest.yaml 中 256 MiB 的 persistent storage 配额无关。
MAX_FILE_BYTES = 64 * 1024 * 1024


class ModelFallbackError(ValueError):
    """Stable, non-sensitive failure from the single Dify model fallback."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _model_selector_mapping(selector: Any) -> dict[str, Any]:
    """Validate the public model-selector value without accepting credentials."""

    if selector is None or selector == "":
        raise ModelFallbackError("MODEL_REQUIRED", "fallback_model is required for model fallback")
    if isinstance(selector, Mapping):
        value = dict(selector)
    elif hasattr(selector, "model_dump"):
        try:
            value = dict(selector.model_dump())
        except Exception as exc:
            raise ModelFallbackError("MODEL_CONFIG_INVALID", "fallback_model selector is invalid") from exc
    else:
        raise ModelFallbackError("MODEL_CONFIG_INVALID", "fallback_model selector is invalid")
    provider = value.get("provider")
    name = value.get("name", value.get("model"))
    if not isinstance(provider, str) or not provider.strip() or not isinstance(name, str) or not name.strip():
        raise ModelFallbackError("MODEL_CONFIG_INVALID", "fallback_model requires provider and name")
    mode = value.get("mode", "chat")
    if hasattr(mode, "value"):
        mode = mode.value
    if not isinstance(mode, str) or mode not in {"chat", "completion"}:
        raise ModelFallbackError("MODEL_CONFIG_INVALID", "fallback_model mode must be chat or completion")
    completion_params = value.get("completion_params")
    if completion_params is None:
        completion_params = {}
    if not isinstance(completion_params, Mapping):
        raise ModelFallbackError("MODEL_CONFIG_INVALID", "fallback_model completion_params must be an object")
    return {
        "provider": provider.strip(),
        "name": name.strip(),
        "mode": mode,
        "completion_params": dict(completion_params),
    }


def _clean_one_code_fence(value: str) -> str:
    """Remove at most one outer Markdown code fence from model output."""

    text = value.strip()
    if not text.startswith("```"):
        return text
    newline = text.find("\n")
    if newline < 0:
        return text
    text = text[newline + 1:]
    if text.rstrip().endswith("```"):
        text = text.rstrip()[:-3].rstrip()
    return text


def invoke_parameter_extractor(
    tool: Any,
    selector: Any,
    query: str,
    instruction: str,
    *,
    output_key: str,
) -> Any:
    """Invoke Dify's Parameter Extractor exactly once and return its output.

    The helper deliberately sends only ``query`` and the caller-provided
    instruction (which may contain the necessary index definition). It never
    logs prompts, responses, files, database rows, or credentials.
    """

    config = _model_selector_mapping(selector)
    if not isinstance(query, str) or not query.strip() or not isinstance(instruction, str):
        raise ModelFallbackError("MODEL_CONFIG_INVALID", "model fallback input is invalid")
    try:
        from dify_plugin.entities.workflow_node import ModelConfig, ParameterConfig
        model_config = ModelConfig(
            provider=config["provider"],
            name=config["name"],
            mode=config["mode"],
            completion_params=config["completion_params"],
        )
        parameters = [ParameterConfig(
            name=output_key,
            type="string",
            description="Return only the requested JSON object or DSL result.",
            required=True,
        )]
    except Exception as exc:
        raise ModelFallbackError("MODEL_CONFIG_INVALID", "fallback_model cannot be configured") from exc
    session = getattr(tool, "session", None)
    workflow_node = getattr(session, "workflow_node", None)
    extractor = getattr(workflow_node, "parameter_extractor", None)
    invoke = getattr(extractor, "invoke", None)
    if not callable(invoke):
        raise ModelFallbackError("MODEL_CONFIG_INVALID", "Dify Parameter Extractor is unavailable")
    try:
        response = invoke(parameters, model_config, query, instruction)
    except Exception as exc:
        raise ModelFallbackError("MODEL_INVOCATION_FAILED", "Dify Parameter Extractor invocation failed") from exc
    outputs = getattr(response, "outputs", None)
    if outputs is None and isinstance(response, Mapping):
        outputs = response.get("outputs")
    if isinstance(outputs, Mapping):
        aliases = (output_key, "index_json", "index_definition", "query_json", "query")
        candidate = next((outputs[key] for key in aliases if key in outputs), None)
        if candidate is None and len(outputs) == 1:
            candidate = next(iter(outputs.values()))
        if candidate is None:
            candidate = outputs
    elif isinstance(outputs, str):
        candidate = outputs
    else:
        raise ModelFallbackError("MODEL_OUTPUT_INVALID", "Parameter Extractor returned no usable output")
    if isinstance(candidate, str):
        candidate = _clean_one_code_fence(candidate)
        if not candidate:
            raise ModelFallbackError("MODEL_OUTPUT_INVALID", "Parameter Extractor returned empty output")
    elif not isinstance(candidate, Mapping):
        raise ModelFallbackError("MODEL_OUTPUT_INVALID", "Parameter Extractor output must be JSON or text")
    return candidate


def service_for(tool: Any) -> CoreService:
    """根据 Dify 工具实例构造 TCPR 服务对象。

    Dify 插件运行时中持久化存储挂在 ``session.storage`` 上，
    本地测试桩则通过 ``tool.runtime`` 提供；
    两者都被包进 :class:`RuntimeKVStorageAdapter`，
    使上层业务代码只依赖 ``StorageAdapter`` 协议。

    :param tool: Dify 工具类实例（或其测试替身），
                 通常带有 ``session`` / ``runtime`` 属性。
    :return: 已绑定存储适配器的 :class:`CoreService` 实例。
    """
    # 优先取 session 上的 storage：这是 Dify 官方工具运行时的标准挂载点
    session = getattr(tool, "session", None)
    if session is not None and getattr(session, "storage", None) is not None:
        return CoreService(RuntimeKVStorageAdapter(session))
    # 回退到 tool.runtime（本地 fallback / 测试环境）。
    # 若 runtime 为 None，RuntimeKVStorageAdapter 会在真正读写时抛出 StorageNotConfigured，
    # 因此这里构造不会立刻失败。
    return CoreService(RuntimeKVStorageAdapter(getattr(tool, "runtime", None)))


def read_file_parameter(value: Any) -> tuple[bytes, str]:
    """把 Dify 工具收到的文件参数统一解析为 ``(data, filename)``。

    按参数形态依次匹配，兼容六种入参：

    1. 原始 ``bytes`` / ``bytearray``（文件名取默认值）；
    2. 带 ``blob`` 属性的 Dify 文件对象；
    3. 描述文件的 ``dict``（载荷支持 ``bytes`` / base64 ``data`` / ``path`` 三种键）；
    4. 带 ``read`` 方法的类文件对象（如 BytesIO、werkzeug FileStorage）；
    5. 本地磁盘上真实存在的文件路径字符串。

    :param value: Dify 传入的文件参数，形态见上。
    :return: ``(data, filename)`` 二元组；``data`` 为文件原始字节，
             ``filename`` 为推断出的文件名（默认 ``input.xlsx``）。
    :raises ValueError: 参数形态无法识别、blob 不是 bytes、dict 缺少可用载荷、
                        base64 解码失败（binascii.Error 是 ValueError 的子类）
                        或文件超过 64 MiB 上限时抛出。
    """
    # 无法从参数推断文件名时的兜底值，与 ingest 默认的 Excel 文件名保持一致
    filename = "input.xlsx"
    if isinstance(value, (bytes, bytearray)):
        # 形态 1：已经是原始字节，直接使用
        data = bytes(value)
    elif hasattr(value, "blob"):
        # 形态 2：Dify 官方文件对象（File 实体），实际内容挂在 blob 属性上
        blob = getattr(value, "blob")
        if not isinstance(blob, (bytes, bytearray)):
            raise ValueError("Dify file object blob must be bytes")
        data = bytes(blob)
        filename = str(getattr(value, "filename", None) or filename)
    elif isinstance(value, dict):
        # 形态 3：字典描述的文件，文件名按 filename -> name -> 默认值 的顺序取
        filename = str(value.get("filename") or value.get("name") or filename)
        if isinstance(value.get("bytes"), (bytes, bytearray)):
            data = bytes(value["bytes"])
        elif isinstance(value.get("data"), str):
            # validate=True 严格校验 base64 字符集与填充位，
            # 防止损坏的编码被静默解码成垃圾字节
            data = base64.b64decode(value["data"], validate=True)
        elif value.get("path"):
            data = Path(str(value["path"])).read_bytes()
        else:
            raise ValueError("file object must contain bytes, base64 data, or a path")
    elif hasattr(value, "read"):
        # 形态 4：类文件对象，read() 一次性读全；未提供 name 属性时沿用默认文件名
        data = value.read()
        filename = str(getattr(value, "name", filename))
    elif isinstance(value, str) and Path(value).is_file():
        # 形态 5：本地路径字符串；只有文件确实存在（is_file() 为真）才走这条分支
        path = Path(value)
        filename = path.name
        data = path.read_bytes()
    else:
        raise ValueError("unsupported Dify file parameter")
    # 超限保护：拒绝超过 64 MiB 的文件，对应上面的 MAX_FILE_BYTES
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("input file exceeds 64 MiB limit")
    return data, filename
