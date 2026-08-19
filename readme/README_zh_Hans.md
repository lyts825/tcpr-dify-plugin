# TCPR Dify 工具插件

TCPR（Typed Constraint-Preserving Retrieval）是一个用于动态属性精确检索的 Dify Tool Plugin，仅提供四个公开工具：

- `search`：使用持久化 index/database 执行类型化 Hard/Soft 约束；
- `structure_query`：基于索引定义确定性地将需求转换为已校验的查询 JSON；自然语言确定性失败时可最多调用一次回退模型；
- `build_index`：保存用户手工填写的逻辑索引定义并原子激活；
- `build_database`：按索引字段集合构建数据库并写入类型化缺失标记。

四项能力共享插件内置的 `tcpr_core/bundled/tcpr/core_api.py`，该副本是插件运行时的唯一事实来源。`build_index` 接收
`index_json` 或文档化中英 `index_requirement`；非空 `index_json` 始终绝对优先，非法时直接报错，不读取数据文件，返回 `index_id` 和 `parse_source`。
仅当需求 DSL 的确定性解析失败且用户选择了回退模型时，Dify Parameter Extractor 最多调用一次，候选仍须经核心 Schema 校验后保存。
`build_database` 接收数据文件和该 ID，按定义规范化、补齐类型化缺失值、校验字段集合与唯一主键，并生成持久化
postings/ranges 物理索引后返回 `database_id`。`search` 接收查询 JSON 及两个 ID，直接消费数据库快照中的物理索引，
不会按请求临时重建；失败不会切换已激活快照。
`structure_query` 只使用指定索引中的字段、别名和单位解析文本或校验已有 JSON；只有非空自然语言确定性失败时才允许一次回退模型，
显式 JSON/dict、空值、索引/存储错误和损坏定义不回退，也不推断未声明字段。模型只收到需求和必要索引定义，不收到商品行、数据库行、结果或凭据。

Provider 标识为 `lyts825/tcpr/tcpr`。

## 从 GitHub 安装

本插件通过 Dify 的 GitHub 安装器分发。仓库必须有一个已发布的 GitHub
Release，并在 Release Assets 中附带 `.difypkg` 文件。

1. 在 Dify 打开 **Plugins**；
2. 选择 **Install Plugin** → **From GitHub**；
3. 输入 `https://github.com/lyts825/tcpr-dify-plugin`；
4. 选择与插件版本对应的 Release，确认权限后安装。

当前 `0.0.4` 版本对应 tag `v0.0.4` 与资产
`tcpr-0.0.4.difypkg`。后续版本需先递增 `manifest.yaml` 中的 `version`，再重新打包并发布匹配的 Release。

## 数据与运行时

插件使用 Python 3.12，并将通过校验的商品快照、索引、Schema 元数据和导入警告写入 Dify 持久化存储。除明确启用的单次回退外，插件不调用外部服务；回退时仅由 Dify 将需求和必要索引定义发送给用户选择的模型，不发送商品行、数据库行、检索结果或凭据。

插件申请 256 MiB 持久化存储，实际可用性取决于 Dify 运行环境。启用第三方签名校验的自托管 Dify 可能要求管理员先批准未签名包。

## 隐私与支持

数据处理政策见 [PRIVACY.md](../PRIVACY.md)。问题与功能建议请提交到
[GitHub Issues](https://github.com/lyts825/tcpr-dify-plugin/issues)。
