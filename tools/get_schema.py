"""get_schema 工具模块。

本模块定义 GetSchemaTool，是 TCPR 插件对外提供的四个工具之一
（search / import_products / rebuild_index / get_schema）。
该工具不接受任何输入参数，执行时直接读取当前处于激活状态
（tcpr:current）的索引快照中保存的商品 Schema（生产环境 14 列
基础数据字段定义），并通过 emit_contract 以「逐字段变量消息 +
整体 JSON 消息」的形式输出，供 LLM 在生成精确属性检索查询之前
了解可用的字段名、类型与约束。
"""

from collections.abc import Generator
from typing import Any

import tcpr_core.sdk_compat as _sdk_compat
from tools.common import service_for


class GetSchemaTool(_sdk_compat.ToolBase):
    """查询当前生效索引 Schema 的 Dify 工具。

    本工具在 Dify 工具定义中不声明任何入参；执行时从插件持久化
    存储中读取当前 generation 的快照 schema，并附带 status、
    current_generation、index_capabilities 等元信息一并返回，
    供下游 LLM 了解当前可检索的字段集合。
    """

    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[Any, None, None]:
        """执行工具调用，产出 Dify 消息流（生成器）。

        参数:
            tool_parameters: Dify 运行时传入的工具参数字典；
                本工具不声明入参，因此忽略其内容。

        产出:
            Generator[Any, None, None]: 依次 yield 每条 schema 字段
                对应的变量消息（variable message），最后再 yield 一条
                包含完整 payload 的 JSON 消息。

        可能抛出的异常:
            由 service_for / get_schema 底层抛出，例如持久化存储
            （RuntimeKVStorageAdapter）不可用或快照损坏时的异常。
        """
        # 根据工具自身携带的 session/runtime 构造 TcprService 实例，
        # 并读取当前激活 generation 的 schema 快照（dict 形式）
        payload = service_for(self).get_schema()
        # 将 payload 逐字段生成为 Dify 变量消息，并追加一条完整 JSON 消息
        yield from _sdk_compat.emit_contract(self, payload)
