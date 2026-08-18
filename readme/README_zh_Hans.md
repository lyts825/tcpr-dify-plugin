# TCPR Dify 工具插件

TCPR（Typed Constraint-Preserving Retrieval）是一个用于动态属性精确检索的 Dify Tool Plugin，仅提供三个公开工具：

- `search`：使用持久化 index/database 执行类型化 Hard/Soft 约束；
- `build_index`：从 CSV/XLSX/JSON/JSONL 构建并原子激活动态属性索引；
- `build_database`：按索引字段集合构建数据库并写入类型化缺失标记。

三项能力共享插件内置的 `tcpr_core/bundled/tcpr/core_api.py`，该副本是插件运行时的唯一事实来源。`build_index` 返回 `index_id`，
`build_database` 接收同一文件和该 ID 返回 `database_id`，`search` 接收查询 JSON
及两个 ID。主键可通过 `primary_key` 配置，否则按 `id`、`product_id`、
`product_code`、`sku`、`编号`、`产品编号` 推导，并且必须唯一。搜索直接消费已持久化
索引，不会按请求临时重建；索引/数据库构建失败不会切换已激活快照。

Provider 标识为 `lyts825/tcpr/tcpr`。

## 从 GitHub 安装

本插件通过 Dify 的 GitHub 安装器分发。仓库必须有一个已发布的 GitHub
Release，并在 Release Assets 中附带 `.difypkg` 文件。

1. 在 Dify 打开 **Plugins**；
2. 选择 **Install Plugin** → **From GitHub**；
3. 输入 `https://github.com/lyts825/tcpr-dify-plugin`；
4. 选择与插件版本对应的 Release，确认权限后安装。

当前 `0.0.2` 版本对应 tag `v0.0.2` 与资产
`tcpr-0.0.2.difypkg`。后续版本需先递增 `manifest.yaml` 中的 `version`，再重新打包并发布匹配的 Release。

## 数据与运行时

插件使用 Python 3.12，并将通过校验的商品快照、索引、Schema 元数据和导入警告写入 Dify 持久化存储。插件不调用外部服务，也不会把商品行发送给第三方。外部知识库绑定由目标 Dify 工作区配置，不随仓库或插件包发布。

插件申请 256 MiB 持久化存储，实际可用性取决于 Dify 运行环境。启用第三方签名校验的自托管 Dify 可能要求管理员先批准未签名包。

## 隐私与支持

数据处理政策见 [PRIVACY.md](../PRIVACY.md)。问题与功能建议请提交到
[GitHub Issues](https://github.com/lyts825/tcpr-dify-plugin/issues)。
