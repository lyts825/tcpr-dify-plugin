"""Dify TCPR tool implementations.

本模块是 TCPR（Typed Constraint-Preserving Retrieval，类型化约束保持检索）
Dify 工具插件的 tools 包入口，集中管理暴露给 Dify 工作流调用的四个工具实现：

- TcprSearchTool（tcpr_search.py）：属性精确检索工具，把模型输出的规范化约束
  JSON 交给 TcprService.search 执行检索；
- ImportProductsTool（import_products.py）：商品基础数据（Excel 文件）导入工具，
  仅在完整校验并成功重建索引后才切换当前快照，失败不会破坏旧快照；
- RebuildIndexTool（rebuild_index.py）：按 generation_id 重建索引的工具；
- GetSchemaTool（get_schema.py）：返回生产环境上架产品基础数据 14 列 Schema
  定义的工具。

本 __init__.py 不导出任何符号，也不定义任何类或函数：各工具类由 Dify
provider（provider/tcpr.yaml）按文件路径直接注册。所有工具类均继承
tcpr_core.sdk_compat.ToolBase，通过 _invoke 方法返回 Generator，并借助
tools.common 中的公共辅助函数（service_for、read_file_parameter）接入
TcprService 与 Dify 运行时存储；最终由 emit_contract 把结果负载统一转换为
Dify SDK 要求的消息事件流。
"""
