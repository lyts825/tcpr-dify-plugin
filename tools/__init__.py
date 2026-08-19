"""Dify TCPR tool implementations.

本模块是 TCPR（Typed Constraint-Preserving Retrieval，类型化约束保持检索）
Dify 工具插件的 tools 包入口，集中管理暴露给 Dify 工作流调用的三个工具实现：

- SearchTool（search.py）：使用 query_json、index_id、database_id
  搜索持久化快照；
- StructureQueryTool（structure_query.py）：基于已保存索引的定义，确定性地
  将文本或 JSON 需求转换为 query_json；
- BuildIndexTool（build_index.py）：保存用户手工填写的逻辑索引定义；
- BuildDatabaseTool（build_database.py）：按索引属性集合构建数据库快照并补齐缺失值。

本 __init__.py 不导出任何符号，也不定义任何类或函数：各工具类由 Dify
provider（provider/tcpr.yaml）按文件路径直接注册。所有工具类均继承
tcpr_core.sdk_compat.ToolBase，通过 _invoke 方法返回 Generator，并借助
tools.common 中的公共辅助函数（service_for、read_file_parameter）接入
TcprService 与 Dify 运行时存储；最终由 emit_contract 把结果负载统一转换为
Dify SDK 要求的消息事件流。
"""
