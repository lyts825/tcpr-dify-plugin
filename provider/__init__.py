"""Dify TCPR provider.

TCPR（Typed Constraint-Preserving Retrieval，类型约束保持检索）插件的
Provider 包（Provider 层）。

本包是 Dify 插件清单 provider/tcpr.yaml 所声明的 Provider 层容器，作用如下：
- 作为 Python 包容器，使 Dify 运行时能够导入 provider.tcpr 模块；
  tcpr.yaml 的 extra.python.source 指向 provider/tcpr.py，其中
  TcprProvider 是免凭证（credential-free）的 ToolProvider 实现：
  不接收任何外部凭证，底层存储由 Dify runtime 提供。
- 本文件自身不含业务逻辑，仅充当包标记（package marker）文件，
  使 provider 目录被 Python 识别为可导入的包；插件的实际加载入口由
  manifest.yaml 的 meta.runner.entrypoint（main）与 tcpr.yaml 的
  source 字段共同决定。

本模块不导出任何公共符号：Dify 加载插件时按上述配置路径定位
TcprProvider，而非依赖本 __init__.py 的导出。
"""
