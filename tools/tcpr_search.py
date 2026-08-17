"""TCPR 精确检索工具（search）。

该模块定义 TcprSearchTool，是 Dify 侧的薄适配层：接收 Dify 注入的
规范化约束 JSON（TCPR IR，由工作流规范化节点产生）与 top_k 参数，
转发给 TcprService.search()，对当前激活的生产商品 generation 执行
类型化 Hard/Soft 约束检索，最后通过 emit_contract 把服务返回的契约
字典展开为 Dify 可识别的变量消息与整体 JSON 消息。

真正的检索、约束校验、快照加载等业务逻辑位于 tcpr_core/service.py；
本工具的元数据（参数定义、输出 schema）声明在 tools/tcpr_search.yaml。
"""

from collections.abc import Generator
from typing import Any

import tcpr_core.sdk_compat as _sdk_compat
from tools.common import service_for


class TcprSearchTool(_sdk_compat.ToolBase):
    """TCPR 检索工具：对当前生产 generation 执行类型化 Hard/Soft 约束检索。

    对应 tools/tcpr_search.yaml 声明的 search 工具。参数来源：
    normalized_constraints_json 由 LLM 按 form=llm 传递（禁止编造属性），
    top_k 由表单提供（YAML 侧约束为 1..100）。
    """

    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[Any, None, None]:
        """执行一次检索，并以消息形式逐条产出契约字段。

        参数:
            tool_parameters: Dify 注入的工具参数字典，包含：
                normalized_constraints_json (str, 必填): 工作流规范化节点
                    产出的严格 TCPR IR JSON 字符串。
                top_k (number, 可选): 最大返回候选数，缺省为 20；service
                    层内部还会再做一次 max(1, min(top_k, 100)) 钳制。

        返回:
            Generator: 依次产出 emit_contract 生成的 Dify 变量消息
                （message、status、candidate_ids_json、candidates_json、
                soft_constraints_json、debug_json、kb_query），最后产出
                一份包含全部字段的整体 JSON 消息。

        异常:
            正常路径不抛异常：service.search() 会把存储未配置
            （CONFIG_REQUIRED）、无激活 generation（NOT_READY）、约束 JSON
            解析失败（ERROR）等情形统一折叠为带 status 字段的契约字典返回。
        """
        # 构造绑定到当前工具会话的 TcprService 实例：优先复用 session 的
        # 持久化存储，否则回退到 runtime 存储（见 tools/common.py）。
        payload = service_for(self).search(
            # 约束 JSON 缺省按空字符串处理，解析与校验交给 service 层
            tool_parameters.get("normalized_constraints_json", ""),
            # top_k 缺省 20，与 tcpr_search.yaml 中的 default: 20 保持一致
            tool_parameters.get("top_k", 20),
        )
        # 把服务返回的契约字典展开为 Dify 可识别的变量消息与 JSON 消息。
        # 使用 yield from 而非 return 转发 generator：保留 _invoke 的惰性
        # 求值语义，保证每一条消息都能被 Dify runtime 逐条消费。
        yield from _sdk_compat.emit_contract(self, payload)
