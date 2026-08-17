"""TCPR 插件的「重建索引」工具。

本模块实现 Dify 工具 RebuildIndexTool（对应 tools/rebuild_index.yaml 中
identity.name=rebuild_index）：从现有快照重新构建 staging generation 并
事务式激活，重建失败时不会破坏已有的当前快照（tcpr:current）。

工具本身不承载业务逻辑，仅负责：
1. 从 tool_parameters 中提取可选的 generation_id 参数；
2. 通过 tools.common.service_for 获取绑定当前会话/运行时存储的 TcprService；
3. 调用 service.rebuild_index 重建索引；
4. 借助 emit_contract 将结果字典转换为 Dify 变量/JSON 消息流。

_invoke 是生成器，其产出会被 Dify 插件运行时（或本地 fallback 测试）逐条
消费，因此方法主体通过 `yield from` 转发消息。
"""

from collections.abc import Generator
from typing import Any

import tcpr_core.sdk_compat as _sdk_compat
from tools.common import service_for


class RebuildIndexTool(_sdk_compat.ToolBase):
    """重建索引工具：基于现有 generation 重建检索索引并原子切换。

    继承自 SDK 兼容基类 ToolBase（dify_plugin.Tool 的本地 fallback），
    通过 _invoke 暴露给 Dify Tool 运行时调用。
    """

    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[Any, None, None]:
        """执行重建索引并流式产出 Dify 消息。

        参数:
            tool_parameters: Dify 传入的工具参数字典。可选键：
                generation_id (str): 要重建的 generation ID；缺省或为空串时
                    由服务层回退到存储中的当前 generation（见
                    TcprService.rebuild_index）。

        返回:
            Generator[Any, None, None]: 逐条产出由 emit_contract 生成的
                Dify 变量消息与一条 JSON 消息（本地 fallback 时为
                FallbackMessage），内容包含 status、generation_id、
                record_count、previous_generation_id 等字段。

        异常:
            本方法不直接抛出业务异常；底层服务若因存储读写失败抛错，
            异常会随生成器向上传播给 Dify 运行时。
        """
        # 通过 service_for 绑定当前工具的 session/runtime 存储构造 TcprService；
        # generation_id 缺省取空串，由服务层回退到 current generation。
        payload = service_for(self).rebuild_index(tool_parameters.get("generation_id", ""))
        # emit_contract 将结果字典展开为逐字段的变量消息，并在最后追加一条
        # JSON 消息；用 yield from 透传，使 _invoke 保持为生成器。
        yield from _sdk_compat.emit_contract(self, payload)
