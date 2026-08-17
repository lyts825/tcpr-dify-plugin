"""SDK 兼容适配层。

本模块为 tcpr 插件提供 Dify Plugin SDK 的可选适配：

- 在 Dify 生产运行时（已安装 dify_plugin），ToolBase / ToolProvider 直接继承
  SDK 的 Tool / ToolProvider，消息工厂方法走 SDK 原实现，保证与 Dify 运行时
  的消息协议完全一致；
- 在本地开发 / 测试环境（SDK 不可用，见 README 中"当前边界"一节），通过
  FallbackMessage 提供本地降级实现，使核心逻辑（如 emit_contract）无需 SDK
  也能被单元测试验证。

模块刻意不做"伪装成已安装 SDK"：SDK_AVAILABLE 标志真实反映导入结果，
本地 fallback 与 SDK 行为是两条显式分支。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 标记 Dify Plugin SDK 是否已成功导入；默认为 False（未导入）
SDK_AVAILABLE = False

try:
    # 尝试导入 Dify 官方 SDK 的基类；type: ignore 用于屏蔽本地环境中
    # dify_plugin 未安装时静态类型检查器的报错
    from dify_plugin import Tool as _DifyTool  # type: ignore
    from dify_plugin import ToolProvider as _DifyToolProvider  # type: ignore
    SDK_AVAILABLE = True
except ImportError:
    # 导入失败（本地无 SDK）时降级为 object，使 ToolBase/ToolProvider 的
    # 类定义仍可执行，只是失去 SDK 基类行为
    _DifyTool = object
    _DifyToolProvider = object


@dataclass
class FallbackMessage:
    """本地降级模式下代替 SDK 消息对象的轻量消息容器。

    仅用于 SDK 不可用的本地测试环境，携带消息类型与载荷，
    便于测试代码按 kind 断言。

    属性:
        kind: 消息类型，取值 "variable"（变量消息）或 "json"（JSON 消息）。
        value: 消息载荷；variable 消息为 {"name": ..., "value": ...}，
               json 消息为任意可序列化字典。
    """

    kind: str
    value: Any


class ToolBase(_DifyTool):
    """SDK-compatible base with a deliberately local-only fallback.

    与 Dify SDK 兼容的工具基类，并刻意提供"仅本地生效"的降级实现。
    中文说明：SDK 可用时继承 SDK 的 Tool 并透传其消息工厂方法；
    SDK 不可用时（本地测试环境）返回 FallbackMessage 使核心逻辑仍可运行，
    且该降级行为不会泄漏到生产运行时。
    """

    def create_variable_message(self, variable_name: str, variable_value: Any) -> Any:
        """创建一条 Dify"变量"类型消息。

        参数:
            variable_name: 变量名。
            variable_value: 变量值（任意类型，交由 SDK 或降级容器承载）。

        返回:
            SDK 可用时返回 Dify SDK 的变量消息对象；SDK 不可用时返回
            FallbackMessage("variable", {"name": ..., "value": ...})。

        异常:
            SDK 可用时可能抛出 SDK 内部异常；降级分支不抛异常。
        """
        if SDK_AVAILABLE:
            # SDK 可用：透传 SDK 原生实现，保证生产环境消息协议一致
            return super().create_variable_message(variable_name, variable_value)  # type: ignore[misc]
        # SDK 不可用：返回本地降级消息，把变量名与值打包为 {"name": ..., "value": ...}
        return FallbackMessage("variable", {"name": variable_name, "value": variable_value})

    def create_json_message(self, value: dict[str, Any]) -> Any:
        """创建一条 Dify"JSON"类型消息。

        参数:
            value: 待序列化的字典载荷。

        返回:
            SDK 可用时返回 Dify SDK 的 JSON 消息对象；SDK 不可用时返回
            FallbackMessage("json", value)。

        异常:
            SDK 可用时可能抛出 SDK 内部异常；降级分支不抛异常。
        """
        if SDK_AVAILABLE:
            # SDK 可用：透传 SDK 原生实现
            return super().create_json_message(value)  # type: ignore[misc]
        # SDK 不可用：直接以原字典作为降级消息载荷
        return FallbackMessage("json", value)


class ToolProvider(_DifyToolProvider):
    """工具提供者基类。

    SDK 可用时等价于 Dify SDK 的 ToolProvider，用于在 Dify 中注册本插件的
    工具集合；SDK 不可用时退化为 object 的派生类，仅保证模块可导入。
    """

    pass


def emit_contract(tool: ToolBase, payload: dict[str, Any]):
    """Use the Dify message factories while remaining testable without the SDK.

    借助 Dify 消息工厂把一次工具调用的"契约"（contract）输出为一组消息，
    同时在无 SDK 的本地环境仍可测试。
    中文说明：该函数是生成器（generator）。它先把 payload 浅拷贝为 contract，
    再以 payload 中每个键生成一条变量消息（value 统一转为字符串，保证消息
    载荷可显示），最后追加一条包含完整 contract 的 JSON 消息。

    参数:
        tool: 工具实例，用于调用消息工厂方法（create_variable_message /
              create_json_message）。
        payload: 工具执行结果字典，至少约定含 "status" 键（缺省按 "OK" 处理）。

    生成（yield）:
        依次产出：payload 每个键对应的一条变量消息，以及一条包含完整
        contract 的 JSON 消息；具体消息类型取决于 SDK 是否可用。

    异常:
        不主动抛出异常；SDK 可用时可能透传 SDK 消息工厂抛出的异常。
    """
    # 浅拷贝 payload，避免在后续 setdefault / 字符串化时修改调用方传入的字典
    contract = dict(payload)
    # 约定消息文本取自 "status" 键；payload 未提供 status 时按 "OK" 处理，
    # 同时确保 "message" 键至少存在且值为字符串
    contract.setdefault("message", str(contract.get("status", "OK")))
    for name, value in contract.items():
        # 契约中的每个键值对都输出一条变量消息（值统一转字符串）
        yield tool.create_variable_message(name, value)
    # 最后输出一条包含完整契约的 JSON 消息，便于整包消费
    yield tool.create_json_message(contract)
