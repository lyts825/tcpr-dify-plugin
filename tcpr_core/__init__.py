"""Standalone TCPR core and storage adapters.

# tcpr_core —— 与 Dify SDK 解耦的 TCPR 核心包

本包是整个 tcpr 插件（Typed Constraint-Preserving Retrieval，即
“带类型约束保持的精确属性检索”）的核心实现层，负责承载与 Dify 运行时
无关的全部业务逻辑与数据模型，供 `provider/tcpr.py` 中的公开工具调用。

## 设计原则

- **独立可测**：本包不依赖 `dify_plugin` SDK（本地开发环境不可用），
  因此可以脱离 Dify 运行时单独进行单元测试；SDK 的适配层集中在
  `sdk_compat.py`，通过可选导入的方式挂接，本地测试则使用
  `InMemoryStorage` 等确定性测试替身，不表示 SDK 已安装。
- **类型约束保持（Typed Constraint-Preserving）**：检索全程使用
  `schema.py` 中定义的 14 个字段（对应生产环境“上架产品基础数据.xlsx”
  的 14 列）及其类型（string / enum / numeric / boolean / text）、
  单位与别名，保证查询条件在解析、匹配、重写（如单位换算）过程中
  类型信息不丢失。

## 子模块职责概览

- `schema`：定义字段元数据（Field 数据类与 FIELDS 列表），
  以及 schema 序列化、归一化（normalize）、别名解析等纯函数。
- `ingest`：负责将商品行数据清洗、校验并转换为可索引的内部表示，
  是文件工具的数据入口。
- `engine`：检索引擎，实现带约束的匹配、打分与排序逻辑。
- `storage`：存储抽象层（StorageAdapter 协议、InMemoryStorage 本地
  测试替身、面向 Dify Runtime KV 的 RuntimeKVStorageAdapter 等），
  以及索引快照的序列化/压缩（gzip + base64）读写。
- `sdk_compat`：dify_plugin SDK 的可选适配层，仅在 Dify 运行时
  环境中生效；本地 fallback 不会伪装为可安装的 SDK。
- `service`：对 engine 与 storage 的编排封装，为上层工具提供
  统一的业务服务接口（如导入商品、重建索引、执行检索等）。

注意：本包内的模块不应直接 import dify_plugin 的必需依赖；
任何与 Dify 平台的交互都应通过 sdk_compat 或运行时注入的
存储/日志接口完成。
"""
