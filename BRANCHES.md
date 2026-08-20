# TCPR editions

The project is maintained as two alternative branches. They share the same
plugin identity, so install only one edition in a Dify workspace at a time.

| Branch | Edition | Public tools | Best for |
| --- | --- | --- | --- |
| `main` | Composable tool suite | `build_index`, `build_database`, `structure_query`, `search` | Users who need explicit control and flexible workflow composition |
| `codex/remote-query` | One-click remote query | `remote_query` | Users who want a short setup path and direct read-only PostgreSQL/MySQL access |

## Which edition should I choose?

Choose `main` when data preparation, query structuring, and retrieval need to
be separate workflow steps. Choose `codex/remote-query` when the data already
lives in PostgreSQL or MySQL and one bounded read-only query tool is enough.

The branches are independent release tracks. Changes should be applied to the
branch whose user experience they affect; shared security or core fixes should
be evaluated for both branches.

## 中文说明

项目维护两个可选分支，两者使用相同的插件标识，因此同一个 Dify 工作区内请只
安装其中一个版本。

- `main`：多工具组合版，公开 `build_index`、`build_database`、
  `structure_query`、`search`，适合需要显式控制和灵活编排工作流的用户。
- `codex/remote-query`：一键式远程查询版，仅公开 `remote_query`，适合数据已在
  PostgreSQL/MySQL 中、希望降低配置与学习成本的用户。
