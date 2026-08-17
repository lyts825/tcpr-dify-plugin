from collections.abc import Generator
from typing import Any

import tcpr_core.sdk_compat as _sdk_compat
from tools.common import read_file_parameter, service_for


"""商品数据导入工具。

本模块是 TCPR 插件的四个工具之一（search / import_products /
rebuild_index / get_schema）。ImportProductsTool 从 Dify 的 file 参数中读取
用户上传的商品 Excel 文件（生产环境为上架商品基础数据.xlsx 的 14 列），
交给 TcprService 完成完整校验与 staging 索引构建；只有全部成功后才切换
tcpr:current 快照，失败不会破坏旧数据。该工具本身只负责参数解析、调用
服务层并以变量消息形式返回结果契约。
"""


class ImportProductsTool(_sdk_compat.ToolBase):
    """导入商品 Excel 文件的 Dify 工具。

    对应 manifest 中声明的 import_products 工具：接收一个文件参数，
    解析其中的商品行并生成新的检索索引代（generation）。在服务层完成
    校验与索引构建成功后，该代才会成为当前生效快照（tcpr:current），
    因此调用失败不会影响线上已有的检索数据。
    """

    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[Any, None, None]:
        # 解析 file 参数：兼容原始字节、Dify file 对象和 dict 三种形态，
        # 返回 (Excel 文件字节内容, 文件名)，解析失败会抛出 ValueError。
        data, filename = read_file_parameter(tool_parameters.get("file"))
        # 调用服务层执行导入：完整校验商品行 → 生成 staging 索引 → 成功后
        # 原子切换当前代；返回包含 status / generation_id / record_count 等
        # 字段的契约 dict，失败时抛出异常（由 Dify 框架捕获并转为错误响应）。
        payload = service_for(self).import_products(data, filename)
        # 把契约 dict 的每个键值对逐一包装为变量消息（variable message），
        # 最后再附加一条完整 JSON 消息，供 LLM 在后续节点中引用。
        yield from _sdk_compat.emit_contract(self, payload)
