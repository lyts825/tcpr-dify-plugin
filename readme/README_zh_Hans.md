# TCPR Dify 工具插件

TCPR（`lyts825/tcpr`）现在只公开一个工具：`remote_query`。它按本次调用
提供的连接信息访问 PostgreSQL 或 MySQL 远程数据库并返回有限行数的 JSON
结果；不再创建或持久化本地索引、数据库快照。

`database_type`、`host`、`port`、`database`、`username`、`password`、
`ssl_mode`、`table`、`tcpr_schema_json` 全部是 `form` 参数，不会交给 LLM。
默认 `query_mode=tcpr`：`tcpr_schema_json` 映射属性、白名单列和一个主键，
`tcpr_query_json` 由 LLM 提供 `hard`、`soft`、`select`、`top_k`。硬约束在
数据库端生成参数化 `WHERE`，软约束用 `SUM(CASE ...)` 加权排序，按得分降序、
主键升序稳定返回，并且服务端只取 `top_k+1` 行。NULL/缺失值遵循三值逻辑，
矛盾硬约束不会连接数据库。`raw_sql` 只有表单显式选择时才启用，`sql` 和独立的
`parameters_json` 才会生效；SQL 必须只有一条 `SELECT` 或纯查询
`WITH`，使用 `:name` 命名占位符；多语句、写入、DDL/DCL、修改型 CTE、
`SELECT INTO/OUTFILE` 以及锁定查询都会被拒绝，并通过外层 `LIMIT 101` 防止全量传输。

工具通过数据库服务端只读事务执行，不把客户端文本检查当作唯一保护。默认
连接超时 10 秒、查询超时 30 秒、最多返回 100 行（硬上限为 30/60 秒和
1000 行）。默认 TLS 模式为 `verify-full`。密码和连接信息只在本次调用中
使用，不写入 Dify storage、不回显、不记录日志；错误信息为稳定的脱敏错误。

请为数据库配置最小权限只读账号。PostgreSQL 使用 `pg8000`，MySQL 使用
`PyMySQL`。本地测试使用 fake driver，不连接真实数据库：

## 远程索引建议

插件绝不会创建或修改远程表、索引、Schema 或其他数据库对象。对于频繁使用的
TCPR 查询，数据库管理员可以根据实际查询计划，考虑为常用 hard `EQ`、`IN` 和
范围筛选列建立 PostgreSQL B-tree 或 MySQL BTREE 索引。多值 JSON 数组可根据
数据库版本和管理员策略考虑 PostgreSQL `GIN jsonb_path_ops` 或 MySQL 多值
JSON 索引；常用筛选列与主键排序也可以考虑复合索引。以上仅是远程数据库运维
建议，插件不会执行 `EXPLAIN`、创建索引或变更远程状态。

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -B verify_plugin.py
```

打包命令：

```powershell
dify plugin package . -o .\dist\tcpr-0.0.8.difypkg
```

隐私说明见 [`PRIVACY.md`](../PRIVACY.md)。

## 0.0.8 破坏性变更

0.0.8 以唯一的 `remote_query` 工具替换旧版本地索引工具（`search`、
`structure_query`、`build_index`、`build_database`）。已有工作流需要改为配置
PostgreSQL 或 MySQL 只读连接，并使用默认 TCPR JSON 查询或显式选中的有限
`raw_sql` 模式；旧版本地存储和索引数据不会迁移。
