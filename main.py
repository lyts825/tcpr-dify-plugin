"""Dify plugin entry point.

The real Dify runtime supplies dify_plugin. Local verification deliberately
does not install or emulate the daemon; it imports the fallback adapter instead.

（中文说明）本模块是 Dify 插件（tcpr，Typed Constraint-Preserving Retrieval，
即"类型约束保持检索"）的入口文件：manifest.yaml 中 runner.entrypoint 指向的
正是本文件的 main 函数。真实的 Dify 运行时（daemon）会注入 dify_plugin SDK；
本地验证环境刻意不安装、也不模拟该 daemon，而是走 fallback 适配层
（dify_plugins.tcpr 包内的可选适配模块与 InMemoryStorage 测试替身），
保证本地可测试，但绝不伪装成已安装 SDK。
"""

# 尝试导入 Dify 官方插件 SDK 的 Plugin 基类。
# 本地开发环境没有安装 dify_plugin（见 README"当前边界"），
# 因此采用可选导入：失败时把 Plugin 置为 None，由 main() 在真正需要
# 运行时再给出明确报错，而不是在 import 阶段就让整个模块崩溃。
try:
    from dify_plugin import DifyPluginEnv, Plugin  # type: ignore
    # 上面 type: ignore：本地缺少该包的 stub/类型信息，忽略静态类型检查。
except ImportError:
    # SDK 不可用时的降级占位：Plugin = None 表示"未安装"，
    # main() 会根据这个哨兵值抛出带指引信息的 RuntimeError。
    DifyPluginEnv = None
    Plugin = None


def main() -> None:
    """插件入口函数，由 Dify 运行时（runner entrypoint）调用。

    作用：检查 dify_plugin SDK 是否可用；可用则将控制权交给 Plugin().run()
    启动插件主循环，不可用则抛出带指引信息的 RuntimeError，提示改用
    内存测试替身（tests 中的 InMemoryStorage）做本地验证。

    参数：
        无。

    返回：
        None。进程生命周期由 Plugin().run() 接管。

    可能抛出的异常：
        RuntimeError：本地未安装 dify_plugin SDK 时抛出，错误信息会
        指引调用方改用 in-memory test double 进行本地验证。
    """
    # 兜底检查：SDK 未安装（Plugin 为 None）时立即失败，
    # 避免把问题推迟到 Plugin().run() 内部才暴露成难以定位的 AttributeError。
    if Plugin is None or DifyPluginEnv is None:
        raise RuntimeError(
            "dify_plugin SDK is not installed; use the in-memory test double for local verification"
        )
    # 实例化插件并进入其事件循环；search、import_products、
    # rebuild_index、get_schema 四个工具的实际分发由 provider 层接管。
    Plugin(DifyPluginEnv(MAX_REQUEST_TIMEOUT=120)).run()


# 仅当以脚本方式直接运行（python main.py）时调用入口；
# 被 Dify daemon 以模块方式加载时不会执行此分支。
if __name__ == "__main__":
    main()
