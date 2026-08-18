"""TCPR 插件核心服务层。

本模块定义 TcprService 类，是整个 TCPR（Typed Constraint-Preserving
Retrieval，类型化约束保持检索）插件对外的核心服务入口：它协调 ingest
（表格读取与规范化）、engine（索引构建与检索）、schema（模式与约束
规范化）和 storage（代际快照持久化）四个子模块，向三个公开工具提供
索引构建、数据库构建与按约束检索能力。
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

from .engine import Product, build_index_payload, search_products
from .ingest import normalize_product_rows, read_product_rows
from .schema import SOURCE_COLUMNS, normalize_constraint_doc, schema_payload
from .storage import GenerationStore, InMemoryStorage, StorageAdapter, StorageNotConfigured

# The plugin adapter delegates the public operations to the bundled core.
from .shared_core import CoreService as SharedCoreService


def normalize_top_k(value: Any) -> int:
    """Validate the public Top K contract instead of silently clamping it."""
    if value is None or value == "":
        return 20
    if isinstance(value, bool):
        raise ValueError("top_k must be an integer from 1 to 100")
    if isinstance(value, int):
        result = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError("top_k must be an integer from 1 to 100")
        result = int(value)
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        result = int(value.strip())
    else:
        raise ValueError("top_k must be an integer from 1 to 100")
    if not 1 <= result <= 100:
        raise ValueError("top_k must be an integer from 1 to 100")
    return result


class TcprService:
    """TCPR 核心服务类。

    封装商品导入、索引重建、模式查询与约束检索的完整流程。所有持久化
    读写都通过构造时注入的 StorageAdapter 完成，并采用“代际
    （generation）”快照语义：只有完整构建成功的新代际才会被原子切换为
    当前代际（tcpr:current），任何失败都不会破坏已有快照。
    """

    def __init__(self, storage: StorageAdapter):
        """初始化服务。

        Args:
            storage: 存储适配器，抽象底层持久化介质；本地测试可注入
                InMemoryStorage，Dify 运行时使用 RuntimeKVStorageAdapter。
                本方法本身不触发任何 IO，仅包装出 GenerationStore。
        """
        self.store = GenerationStore(storage)
        self.shared = SharedCoreService(storage)

    @staticmethod
    def _products(records: list[dict[str, Any]]) -> list[Product]:
        """把规范化后的商品字典记录转换为 Product 对象列表。

        Args:
            records: 规范化后的商品记录，每条至少包含 product_id 键，
                可选包含 attrs（属性字典）与 raw（原始行数据）。

        Returns:
            Product 对象列表。product_id 统一强制转为字符串，避免表格
            解析产生数值型 ID 后与检索键类型不一致；attrs/raw 通过
            dict(...) 浅拷贝隔离，防止后续索引构建意外改动存储中的
            原始数据。
        """
        return [
            Product(
                product_id=str(record["product_id"]),
                attrs=dict(record.get("attrs", {})),
                raw=dict(record.get("raw", {})),
            )
            for record in records
        ]

    def import_products(self, data: bytes, filename: str) -> dict[str, Any]:
        """导入商品表格数据，构建索引并写入新一代际快照。

        完整流水线：解析表格得到表头与原始行 -> 规范化行数据（去重、
        类型校验，产出商品记录与警告列表）-> 转换为 Product 对象 ->
        生成检索模式 -> 构建索引负载 -> 一次性写入新代际。只有
        write_generation 成功，新代际才会被切换为当前代际，因此失败
        不会破坏旧快照。

        Args:
            data: 表格文件的原始字节内容（如 .xlsx 二进制流）。
            filename: 文件名，供解析器推断表格格式。

        Returns:
            状态字典，包含状态码、新代际 ID、记录数、警告数及序列化
            后的警告/表头详情等，供上层工具直接透传给用户。

        Raises:
            表格解析或校验失败时，read_product_rows 与
            normalize_product_rows 会抛出 ValueError/TypeError 等异常，
            本方法不捕获，由调用方（工具层）处理。
        """
        headers, rows = read_product_rows(data, filename)
        products, warnings = normalize_product_rows(rows)
        product_objects = self._products(products)
        schema = schema_payload(len(product_objects))
        # 原子写入新代际：构建全部成功后新代际才切换为 tcpr:current，
        # 中途失败不会破坏旧快照（见 GenerationStore.write_generation）
        generation = self.store.write_generation(
            products,
            build_index_payload(product_objects),
            schema,
            warnings,
        )
        return {
            "status": "OK",
            "generation_id": generation,
            "record_count": len(product_objects),
            "warning_count": len(warnings),
            # 警告只回传前 1000 条，避免响应体积失控；truncated 标记是否存在截断
            "warnings_json": json.dumps(
                {"count": len(warnings), "items": warnings[:1000], "truncated": len(warnings) > 1000},
                ensure_ascii=False,  # 保留中文等非 ASCII 字符原样输出
            ),
            "headers_json": json.dumps(headers, ensure_ascii=False),
            "source_columns": len(headers),
        }

    def rebuild_index(self, generation_id: str = "") -> dict[str, Any]:
        """基于已有代际数据重建索引，而不重新读取原始表格。

        复用目标代际中已保存的商品记录、模式与警告，重新生成索引负载
        并写入一个新代际。适用于索引算法升级或索引负载损坏后的修复
        场景。

        Args:
            generation_id: 目标代际 ID；为空时回退到当前代际。

        Returns:
            状态字典，成功时包含新代际 ID、记录数与上一代际 ID；若目标
            代际不存在且无当前代际，返回 NOT_READY 状态提示。
        """
        # 空字符串在 Python 中为假值：未指定代际时自动取当前代际
        active = generation_id or self.store.current_generation()
        if not active:
            return {"status": "NOT_READY", "message": "no current generation"}
        snapshot = self.store.load_generation(active)
        products = snapshot["products"]
        product_objects = self._products(products)
        generation = self.store.write_generation(
            products,
            build_index_payload(product_objects),
            snapshot["schema"],
            snapshot["warnings"],
        )
        return {
            "status": "OK",
            "generation_id": generation,
            "record_count": len(products),
            "previous_generation_id": active,
        }

    def get_schema(self) -> dict[str, Any]:
        """获取当前代际的检索模式（schema）。

        模式描述了商品可检索的属性维度与来源列定义；若尚未导入任何
        数据（无当前代际），返回基于 0 条记录的空模式并标记 NOT_READY。

        Returns:
            模式字典。成功时额外附带状态码、当前代际 ID、索引能力列表
            与警告数；未就绪时附带 NOT_READY 与 current_generation=None。
        """
        active = self.store.current_generation()
        if not active:
            payload = schema_payload(0)
            payload.update({"status": "NOT_READY", "current_generation": None})
            return payload
        snapshot = self.store.load_generation(active)
        payload = dict(snapshot["schema"])
        payload.update({
            "status": "OK",
            "current_generation": active,
            # 声明索引支持的检索能力：EQ 等值 / IN 集合成员 / RANGE 区间 /
            # GE 大于等于 / LE 小于等于 / EXISTS 存在性 / FULL_SCAN_VERIFY 全扫描校验
            "index_capabilities": ["EQ", "IN", "RANGE", "GE", "LE", "EXISTS", "FULL_SCAN_VERIFY"],
            "warning_count": snapshot["manifest"].get("warning_count", 0),
        })
        return payload

    def search(self, normalized_constraints_json: str | dict[str, Any] | None = None, top_k: Any = 20,
               database_id: str | None = None, *, query_json: str | dict[str, Any] | None = None,
               index_id: str | None = None) -> dict[str, Any]:
        """按规范化约束检索商品。

        约束是带类型的属性谓词（如等值、范围），检索全程保持类型与
        约束语义，绝不把约束降级为自由文本相似度匹配；文档中若存在
        无法解析的约束，则拒绝执行检索而不是近似兜底，以保证结果
        “可验证、可解释”。

        Args:
            normalized_constraints_json: 规范化约束文档；可以是已解析的
                dict，也可以是 JSON 字符串（空字符串视为空文档，由
                normalize_constraint_doc 兜底）。
            top_k: 期望返回的候选数量，默认 20；必须是 [1, 100]
                范围内的整数，越界或非整数输入会返回 ERROR。

        Returns:
            检索结果字典，包含状态码、候选 ID、候选详情、软约束回显、
            调试信息与知识库查询串。所有失败路径均以状态码形式返回，
            不向调用方抛出异常。

        Raises:
            对外不抛异常：JSON 解析/类型/键错误（ValueError、TypeError、
            KeyError、JSONDecodeError）与存储未配置（StorageNotConfigured）
            均在内部捕获并转换为 ERROR / CONFIG_REQUIRED 状态响应。
        """
        # New public contract: search(query_json, index_id, database_id), also
        # accepted by keyword as search(query_json=..., index_id=...).
        # Keep the two-argument legacy form below solely for old local
        # verification helpers; provider YAML never exposes that entry point.
        if query_json is not None:
            normalized_constraints_json = query_json
        if index_id is not None:
            top_k = index_id
        if database_id is not None:
            return self.shared.search(normalized_constraints_json, str(top_k), database_id)
        try:
            # 第一步先确定当前代际；存储未配置会在此处直接失败
            active = self.store.current_generation()
        except StorageNotConfigured as exc:
            return self._search_status("CONFIG_REQUIRED", str(exc))
        if not active:
            return self._search_status("NOT_READY", "no active generation; import_products is required")
        try:
            snapshot = self.store.load_generation(active)
            top_k_value = normalize_top_k(top_k)
            # dict 直接使用；字符串走 JSON 解析；空串回退为空字符串，
            # 避免 json.loads("") 抛出 JSONDecodeError
            raw_doc = (
                normalized_constraints_json
                if isinstance(normalized_constraints_json, dict)
                else json.loads(normalized_constraints_json or "")
            )
            doc = normalize_constraint_doc(raw_doc)
            # 安全策略：存在未解析约束时拒绝检索，避免产生不可解释的近似结果
            if doc["unparsed"]:
                return self._search_status("ERROR", "unparsed constraints are not safe to search", active, doc)
            result = search_products(
                self._products(snapshot["products"]),
                doc,
                top_k_value,
            )
            return {
                "status": result["status"],
                "candidate_ids_json": json.dumps(result["candidate_ids"], ensure_ascii=False),
                "candidates_json": json.dumps(result["candidates"], ensure_ascii=False),
                "soft_constraints_json": json.dumps(doc["soft"], ensure_ascii=False),
                "debug_json": json.dumps({
                    "generation_id": active,
                    "source_record_count": snapshot["manifest"]["product_count"],
                    # 展开引擎返回的调试字段，与代际信息合并为一份调试文档
                    **result["debug"],
                }, ensure_ascii=False),
                "kb_query": self._kb_query(doc, result["candidate_ids"]),
            }
        # 约束文档解析失败、类型/键错误等统一转换为 ERROR 状态
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            return self._search_status("ERROR", str(exc), active)
        # load_generation 阶段才暴露的存储未配置错误，单独返回 CONFIG_REQUIRED
        except StorageNotConfigured as exc:
            return self._search_status("CONFIG_REQUIRED", str(exc), active)

    def build_index(self, file: Any) -> str:
        return self.shared.build_index(file)

    def build_database(self, file: Any, index_id: str) -> str:
        return self.shared.build_database(file, index_id)

    @staticmethod
    def _kb_query(doc: dict[str, Any], candidate_ids: list[str]) -> str:
        """构造下游知识库（KB）查询串。

        把硬约束与候选商品 ID 打包成紧凑 JSON，供后续知识库检索节点
        使用；policy 字段声明 KB 文本只能补充命中
        metadata.product_id 的记录，确保自由文本召回不会冲淡属性级
        精确匹配。

        Args:
            doc: 规范化后的约束文档。
            candidate_ids: 本轮属性检索命中的候选商品 ID。

        Returns:
            紧凑格式（无空格分隔符）的 JSON 字符串。
        """
        return json.dumps({
            "hard_constraints": doc.get("hard", []),
            "candidate_ids": candidate_ids,
            "policy": "KB text may supplement only matching metadata.product_id",
        }, ensure_ascii=False, separators=(",", ":"))  # 紧凑分隔符，减少查询串体积

    @staticmethod
    def _search_status(
        status: str,
        reason: str,
        generation: str | None = None,
        doc: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """构造统一结构的错误/中间状态响应。

        保证失败路径与成功路径返回同构的字典，方便上层工具统一解析。

        Args:
            status: 状态码，如 CONFIG_REQUIRED（未配置存储）、
                NOT_READY（尚未导入数据）、ERROR（检索失败）。
            reason: 人类可读的原因说明。
            generation: 相关代际 ID，可为 None。
            doc: 约束文档，用于回显其中的软约束，可为 None。

        Returns:
            状态字典：候选相关字段均为空，kb_query 明确声明
            “未授权任何候选查询”，避免下游节点据此误发起知识库检索。
        """
        return {
            "status": status,
            "candidate_ids_json": "[]",
            "candidates_json": "[]",
            # doc 为 None 时以空字典兜底，再提取软约束回显
            "soft_constraints_json": json.dumps((doc or {}).get("soft", []), ensure_ascii=False),
            "debug_json": json.dumps({"reason": reason, "generation_id": generation}, ensure_ascii=False),
            "kb_query": "TCPR status " + status + "; no candidate query is authorized",
        }
