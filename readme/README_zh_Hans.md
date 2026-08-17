# TCPR Dify 工具插件

TCPR（Typed Constraint-Preserving Retrieval）是一个用于稀疏商品属性精确检索的 Dify Tool Plugin，提供四个工具：

- `tcpr_search`：解析并执行类型化 Hard/Soft 约束；
- `import_products`：校验并导入 CSV 或 XLSX 商品文件；
- `rebuild_index`：安全重建当前商品索引；
- `get_schema`：查看已导入商品 Schema 与索引能力。

Provider 标识为 `lyts825/tcpr/tcpr`。

## 从 GitHub 安装

本插件通过 Dify 的 GitHub 安装器分发。仓库必须有一个已发布的 GitHub
Release，并在 Release Assets 中附带 `.difypkg` 文件。

1. 在 Dify 打开 **Plugins**；
2. 选择 **Install Plugin** → **From GitHub**；
3. 输入 `https://github.com/lyts825/tcpr-dify-plugin`；
4. 选择与插件版本对应的 Release，确认权限后安装。

当前 `0.0.1` 版本对应 tag `v0.0.1` 与资产
`tcpr-0.0.1.difypkg`。后续版本需先递增 `manifest.yaml` 中的 `version`，再重新打包并发布匹配的 Release。

## 数据与运行时

插件使用 Python 3.12，并将通过校验的商品快照、索引、Schema 元数据和导入警告写入 Dify 持久化存储。插件不调用外部服务，也不会把商品行发送给第三方。生产环境的 Product_KB 绑定由目标 Dify 工作区配置，不随仓库或插件包发布。

插件申请 256 MiB 持久化存储，实际可用性取决于 Dify 运行环境。启用第三方签名校验的自托管 Dify 可能要求管理员先批准未签名包。

## 隐私与支持

数据处理政策见 [PRIVACY.md](../PRIVACY.md)。问题与功能建议请提交到
[GitHub Issues](https://github.com/lyts825/tcpr-dify-plugin/issues)。
