"""TCPR 插件本地验证入口（verify_plugin）。

该模块不属于 Dify 运行时代码，而是插件开发阶段的离线验证脚本（README 中的本地验证入口）：
`python -B -m dify_plugins.tcpr.verify_plugin`。

它完成两件事：
1. verify_runtime_contracts：用一组哑对象（Dummy*）模拟 Dify 的 Tool/Session/Storage/File
   接口，在本地不安装 Dify SDK 的前提下，验证 dify_plugin 可选适配层
   （tools.common.service_for 与 RuntimeKVStorageAdapter）、消息契约工厂
   （tcpr_core.sdk_compat.emit_contract）以及导入 / 建 schema 主流程的行为契约。
2. main：针对用户提供的生产环境 Excel（生产环境上架产品基础数据.xlsx）执行一次全量
   导入验证：记录数、列数、索引检索与全表扫描结果一致性、失败回滚、重建索引切换等
   关键性质，全部通过后打印 PLUGIN_FULL_IMPORT_VERIFY_OK。

验证失败时通过 assert / SystemExit 直接终止进程，便于接入 CI 或本地冒烟测试。
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

# ---- 被验证对象：TCPR 核心引擎与 Dify 运行时适配层 ----
from tcpr_core.engine import TriValue, evaluate, search_products  # 三值逻辑、约束求值与检索引擎
from tcpr_core.sdk_compat import emit_contract  # 消息契约工厂：把负载展开为变量消息 + JSON 消息
from tcpr_core.service import TcprService  # 面向工具的领域服务：导入 / 检索 / 建 schema / 重建索引
from tcpr_core.storage import InMemoryStorage  # 确定性内存存储测试替身（不代表 Dify 持久化）
from tcpr_core.ingest import SOURCE_COLUMNS  # 生产 Schema 固定的 14 列列名
# tools.common 是 Dify 工具运行时的薄封装：真实环境由 service_for 路由到
# RuntimeKVStorageAdapter，本文件用 DummyTool / DummySession 驱动同一套代码
from tools.common import read_file_parameter, service_for


class DummyByteStorage:
    """模拟 Dify Runtime KV 存储的最小哑实现。

    真实运行环境中 Dify 通过 session.storage 提供字节型 KV 持久化；本地没有 SDK，
    故用进程内字典模拟 get/set 语义，供 RuntimeKVStorageAdapter 的契约测试使用。
    set 中的 assert 用于严格校验适配层只会写入 bytes 类型的值。
    """

    def __init__(self):
        # 键 -> 原始字节值的进程内映射，模拟 Dify 的持久化 KV 存储
        self.values: dict[str, bytes] = {}

    def set(self, key: str, value: bytes) -> None:
        # Dify 存储契约：value 必须是 bytes；类型不符说明适配层存在 bug
        assert isinstance(value, bytes)
        self.values[key] = value

    def get(self, key: str) -> bytes | None:
        # 与 dict.get 一致：键不存在时返回 None（对应 Dify 存储的未写入状态）
        return self.values.get(key)


class DummySession:
    """模拟 Dify 插件运行时的 Session 对象。

    仅暴露 storage 属性，足以驱动 tools.common.service_for 选择
    RuntimeKVStorageAdapter 分支（session 存在且带 storage 时走该分支）。
    """

    def __init__(self):
        # 每个会话持有一个独立的字节存储，模拟 Dify 的按会话持久化隔离
        self.storage = DummyByteStorage()


class DummyTool:
    """模拟 Dify Tool 实例。

    提供 session 属性，使 service_for(tool) 能像在真实 Dify 运行时一样构造出
    TcprService；本地 fallback 不伪装成可安装的 SDK。
    """

    def __init__(self):
        self.session = DummySession()


class DummyFile:
    """模拟 Dify 文件上传对象（File 接口的最小替身）。

    提供 filename 与 blob 两个属性，覆盖 tools.common.read_file_parameter 中
    "带 blob 属性的类文件对象"这一读取分支。
    """

    def __init__(self, filename: str, blob: bytes):
        # 原始文件名（如 blob.csv），read_file_parameter 会原样透传
        self.filename = filename
        # 文件内容的原始字节，模拟 Dify 上传文件的 blob 字段
        self.blob = blob


class CapturingTool:
    """记录消息工厂调用结果的哑 Tool，用于验证 emit_contract 的产出契约。

    与 DummyTool 不同，它不提供 session，而是实现 Dify 消息工厂的两个方法
    （create_variable_message / create_json_message），把每次调用的参数记入列表，
    便于断言 emit_contract 究竟发出了哪些变量与 JSON 消息。
    """

    def __init__(self):
        # 按调用顺序记录 (变量名, 变量值)
        self.variables: list[tuple[str, object]] = []
        # 按调用顺序记录 JSON 消息负载
        self.json_payloads: list[dict[str, object]] = []

    def create_variable_message(self, name: str, value: object):
        # 模拟 Dify 的变量消息工厂：记录调用参数并返回一个可辨识的元组占位
        self.variables.append((name, value))
        return ("variable", name, value)

    def create_json_message(self, payload: dict[str, object]):
        # 模拟 Dify 的 JSON 消息工厂：记录负载并返回元组占位
        self.json_payloads.append(payload)
        return ("json", payload)


def verify_runtime_contracts() -> None:
    """验证本地运行时适配层的各项行为契约（无需生产数据即可运行）。

    覆盖四点：
    1. service_for 在哑 Session 上构造出的服务，其底层存储与 DummySession 的存储是
       同一个字节 KV（bytes 值可双向往返读取）；
    2. emit_contract 的消息契约：payload 的每个键产生一个变量消息，另自动补一个
       "message" 变量；JSON 消息恰好一条，其 message 字段等于 payload 的 status；
    3. read_file_parameter 对带 blob 的类文件对象返回 (文件内容字节, 文件名)；
    4. InMemoryStorage 上的 import_products 与 get_schema 全链路可用，schema 载荷中
       source / fields 为 dict、index_capabilities 为 list、message 为 "OK"。

    无参数、无返回值；任何断言失败都会直接抛出 AssertionError 终止验证。
    """
    # 用哑 Tool 驱动 service_for：验证可选 Dify 适配层在无 SDK 环境下可正常构造服务
    dummy = DummyTool()
    service = service_for(dummy)
    # 写入 bytes 键值，验证适配层的 put 链路
    service.store.storage.put("bytes-roundtrip", b"roundtrip")
    # 两个方向都应读回同一段字节：DummySession.storage 与适配层包装的是同一后端
    assert dummy.session.storage.get("bytes-roundtrip") == b"roundtrip"
    assert service.store.storage.get("bytes-roundtrip") == b"roundtrip"

    # 构造一个字段齐全的搜索响应负载（值可最小化，字段名必须与契约一致）
    payload = {
        "status": "OK",
        "candidate_ids_json": "[]",
        "candidates_json": "[]",
        "soft_constraints_json": "[]",
        "debug_json": "{}",
        "kb_query": "",
    }
    capturing = CapturingTool()
    messages = list(emit_contract(capturing, payload))
    # emit_contract 为 payload 的每个键创建变量消息，并额外补一个 "message" 变量
    assert {name for name, _ in capturing.variables} == set(payload) | {"message"}
    assert len(capturing.variables) == len(payload) + 1
    # JSON 消息恰好一条，且其 message 字段取自 payload 的 status
    assert len(capturing.json_payloads) == 1
    assert len(messages) == len(payload) + 2  # 变量消息 N+1 条 + JSON 消息 1 条
    assert capturing.json_payloads[0]["message"] == "OK"

    # 用 csv 模块构造一份只有表头 + 一行 "blob-product" 的 CSV（列数与 SOURCE_COLUMNS 一致）
    csv_data = io.StringIO()
    writer = csv.writer(csv_data)
    writer.writerow(SOURCE_COLUMNS)
    writer.writerow(["blob-product"] + [""] * (len(SOURCE_COLUMNS) - 1))
    # DummyFile 模拟 Dify 上传文件；read_file_parameter 应原样返回字节内容与文件名
    file_data, filename = read_file_parameter(DummyFile("blob.csv", csv_data.getvalue().encode("utf-8")))
    assert filename == "blob.csv"
    assert file_data == csv_data.getvalue().encode("utf-8")
    # 导入单条记录：状态 OK 且记录数为 1
    blob_import = TcprService(InMemoryStorage()).import_products(file_data, filename)
    assert blob_import["status"] == "OK" and blob_import["record_count"] == 1
    # 另一个独立服务实例先导入再取 schema，验证 schema 输出的类型契约
    schema_service = TcprService(InMemoryStorage())
    schema_service.import_products(file_data, filename)
    schema_capture = CapturingTool()
    schema_payload = schema_service.get_schema()
    # 这里只关心 emit_contract 能完整枚举 schema 载荷（结果本身在下方断言）
    list(emit_contract(schema_capture, schema_payload))
    # variables 按 (name, value) 顺序记录；dict 化后便于按键断言类型
    schema_values = dict(schema_capture.variables)
    assert isinstance(schema_values["source"], dict)
    assert isinstance(schema_values["fields"], dict)
    assert isinstance(schema_values["index_capabilities"], list)
    assert schema_values["message"] == "OK"


def main() -> None:
    """执行生产数据全量导入验证（插件安装前的最终冒烟测试）。

    步骤：
    1. 先跑 verify_runtime_contracts 验证适配层契约；
    2. 定位用户提供的生产 Excel（约定位于本文件上溯 4 级目录的 1/data/ 下），
       文件缺失时直接 SystemExit，绝不伪造数据；
    3. 全量导入并断言生产基线：25,803 条记录、14 个源列；
    4. 以第一条记录的 product_id 构造 EQ 硬约束检索，并用 evaluate 做全表扫描
       交叉验证索引结果一致；
    5. 构造含重复 product_id 的 CSV，断言导入必须失败（ValueError）且当前代际
       （tcpr:current）不被破坏，即失败回滚；
    6. 基于旧代际重建索引，断言代际指针切换到新代际。

    全部通过后打印 PLUGIN_FULL_IMPORT_VERIFY_OK 与一行汇总指标。
    任何断言失败都会抛出 AssertionError / SystemExit 终止进程。
    """
    verify_runtime_contracts()
    # 本文件位于 .../dify_plugins/tcpr/verify_plugin.py，parents[3] 上溯 4 级即仓库根；
    # 生产 Excel 是用户提供的输入，约定放在仓库根的 1/data/ 下，不随插件打包
    excel = Path(__file__).resolve().parents[3] / "1" / "data" / "生产环境上架产品基础数据.xlsx"
    if not excel.is_file():
        # 生产数据缺失时直接退出而不是伪造基线，保证验证结论可信
        raise SystemExit("verification requires the user-provided production XLSX: " + str(excel))
    data = excel.read_bytes()
    storage = InMemoryStorage()
    service = TcprService(storage)
    imported = service.import_products(data, excel.name)
    assert imported["status"] == "OK"
    assert imported["record_count"] == 25803, imported  # 生产基线：25,803 条商品记录
    assert imported["source_columns"] == 14  # 生产 Schema 固定为 Excel 的 14 列
    assert imported["warning_count"] >= 0
    # 读取刚写入的代际快照，取第一条商品作为检索探针
    first_snapshot = service.store.load_generation(imported["generation_id"])
    first_id = first_snapshot["products"][0]["product_id"]
    # 构造最小检索文档：仅一条 product_id EQ 硬约束（EQ 是索引能力之一）
    doc = {"hard": [{"attr": "product_id", "op": "EQ", "value": first_id}], "soft": [], "unparsed": []}
    indexed = service.search(json.dumps(doc, ensure_ascii=False), 20)
    assert indexed["status"] == "OK", indexed
    assert json.loads(indexed["candidate_ids_json"]) == [first_id]
    # 交叉验证：用 evaluate 对全部商品做全表扫描，索引结果必须与扫描结果一致
    products = service._products(first_snapshot["products"])
    expected = [
        product.product_id for product in products
        if evaluate(product, doc["hard"][0]) is TriValue.TRUE
    ]
    assert json.loads(indexed["candidate_ids_json"]) == expected
    # 导入成功后，当前代际指针应已切换到新代际（tcpr:current）
    assert imported["generation_id"] == service.store.current_generation()

    # 记录当前代际，用于验证失败的导入不会破坏旧快照
    previous = service.store.current_generation()
    bad = io.StringIO()
    writer = csv.writer(bad)
    writer.writerow(SOURCE_COLUMNS)
    duplicate = ["duplicate"] + [""] * 13  # 14 列：product_id 为 "duplicate" 的脏数据行
    writer.writerow(duplicate)
    writer.writerow(duplicate)  # 写两行完全相同的行，触发 product_id 唯一性校验
    try:
        service.import_products(bad.getvalue().encode("utf-8"), "duplicate.csv")
    except ValueError:
        pass  # 预期行为：重复 product_id 必须被拒绝
    else:
        raise AssertionError("duplicate import must fail")
    # 关键性质：失败的导入不得切换 tcpr:current，旧快照保持可用（回滚验证）
    assert service.store.current_generation() == previous

    # 基于 previous 代际重建索引，并验证重建后代际指针切换到新代际
    rebuilt = service.rebuild_index(previous or "")
    assert rebuilt["status"] == "OK", rebuilt
    assert service.store.current_generation() == rebuilt["generation_id"]
    print("PLUGIN_FULL_IMPORT_VERIFY_OK")
    print("records=25803 columns=14 indexed_equals_full_scan=True rollback=True rebuild_switch=True")


# 本地验证入口：python -B -m dify_plugins.tcpr.verify_plugin（见 README）
if __name__ == "__main__":
    main()
