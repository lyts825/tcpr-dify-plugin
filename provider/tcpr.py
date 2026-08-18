"""TCPR 插件的 Provider 入口模块。

该模块定义 TcprProvider——TCPR 插件在 Dify 平台中的 Provider 类，
由 provider/tcpr.yaml 的 extra.python.source 字段注册，Dify 运行时
加载本文件来实例化 Provider。TCPR 采用无凭据（credential-free）
设计：它不调用任何需要 API 密钥的外部服务，索引数据全部存放在
Dify 持久化存储（persistent storage，manifest.yaml 中申请 256 MiB）
中，因此本类只承载凭据校验逻辑，不持有任何配置或业务状态。

本 Provider 通过 provider/tcpr.yaml 仅暴露三个工具：
search、build_index、build_database；业务逻辑统一委托给根包共享核心。
"""

from typing import Any

import tcpr_core.sdk_compat as _sdk_compat


class TcprProvider(_sdk_compat.ToolProvider):
    """Credential-free provider; storage is supplied by the Dify runtime.

    中文翻译：无凭据的 Provider；存储由 Dify 运行时提供。

    TCPR 的检索索引存放在 Dify 持久化存储中，不需要用户配置任何
    外部服务凭据（如 API 密钥），因此本 Provider 不声明 credential
    表单，仅复用 Dify SDK 的 ToolProvider 基类作为注册载体。
    """

    def _validate_credentials(self, credentials: dict[str, Any]) -> None:
        """校验 Dify 传入的凭据字典，确保 TCPR 的“零凭据”约定不被破坏。

        Dify 在实例化 Provider 时会调用本方法；若 Provider 声明了
        credential 表单，则表单内容会以 dict 形式传入。TCPR 未声明
        任何凭据字段，因此合法输入只能是空字典。

        参数:
            credentials: Dify 运行时传入的凭据字典（正常情况为空 dict）。

        返回:
            None：校验通过时无返回值。

        异常:
            ValueError: 当凭据字典非空时抛出——TCPR 不接受任何外部
                凭据，非空输入说明配置与插件约定不符。
        """
        # 空字典在 Python 中为假值：凭据非空即视为违反“零凭据”约定。
        if credentials:
            raise ValueError("TCPR does not accept external credentials")
