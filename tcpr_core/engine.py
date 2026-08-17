"""TCPR 插件检索引擎核心模块。

本模块是 Typed Constraint-Preserving Retrieval（类型化约束保持检索）
插件的引擎层，负责三件事：

1. 三值逻辑约束求值：以 Kleene 三值逻辑（TRUE/FALSE/UNKNOWN）判定
   商品是否满足一条约束；属性缺失或比较失败记为 UNKNOWN，绝不因
   信息缺失而误判为满足/不满足（即"约束保持"语义）。
2. 倒排索引与范围索引：把商品属性预构建为等值倒排表与数值有序区间
   表，支持 EQ/IN/CONTAINS/SUPERSET 的集合运算与 GE/LE/RANGE 的
   二分裁剪，避免每次检索都全表扫描。
3. 检索编排：search_products 将硬约束（必须全部满足）与软约束
   （命中条数打分）组合为完整检索流程，返回候选商品与 debug 统计，
   供 provider 层的 search 工具调用。

对外主要入口：search_products 与 build_index_payload。
"""

from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import Any


class TriValue:
    """三值逻辑常量集合。

    约束求值采用 Kleene 三值逻辑：TRUE（满足）、FALSE（不满足）、
    UNKNOWN（属性缺失或比较失败，无法判定）。
    """

    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Product:
    """商品（检索对象）的不可变数据模型。

    Attributes:
        product_id: 商品唯一标识。
        attrs: 参与检索与约束判定的规范化属性表（属性名 -> 标量或多值列表）。
        raw: 商品原始数据，用于透传未被规范化的字段（如输出展示所需字段）。
    """

    product_id: str
    attrs: dict[str, Any]
    raw: dict[str, Any]


def _values(value: Any) -> list[Any]:
    """将任意属性值规范化为列表形式。

    多值属性（列表/元组/集合）直接转换，标量包装成单元素列表，
    便于后续建索引与 CONTAINS/SUPERSET 判定时统一遍历。

    Args:
        value: 属性的原始值（标量或可迭代容器）。

    Returns:
        值列表；标量返回长度为 1 的列表。
    """

    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


def tri_not(value: str) -> str:
    """三值逻辑取非（NOT）。

    Args:
        value: 输入真值，必须是 TriValue.TRUE/FALSE/UNKNOWN 之一。

    Returns:
        取反后的真值；UNKNOWN 取反仍为 UNKNOWN（信息缺失不可逆推）。

    Raises:
        KeyError: value 不是合法真值常量时由查表字典抛出。
    """

    # 查表映射：TRUE 与 FALSE 互换，UNKNOWN 保持不变。
    return {TriValue.TRUE: TriValue.FALSE, TriValue.FALSE: TriValue.TRUE,
            TriValue.UNKNOWN: TriValue.UNKNOWN}[value]


def tri_and(values: list[str]) -> str:
    """三值逻辑与（AND）。

    按 Kleene 逻辑：任一为 FALSE 则结果为 FALSE；否则任一为 UNKNOWN
    则结果为 UNKNOWN；全部为 TRUE 才为 TRUE。

    Args:
        values: 待合取的输入真值列表。

    Returns:
        合取后的三值真值。
    """

    if any(value == TriValue.FALSE for value in values):
        return TriValue.FALSE
    # 无 FALSE：只要存在 UNKNOWN 就整体无法确定，否则全 TRUE。
    return TriValue.UNKNOWN if any(value == TriValue.UNKNOWN for value in values) else TriValue.TRUE


def tri_or(values: list[str]) -> str:
    """三值逻辑或（OR）。

    按 Kleene 逻辑：任一为 TRUE 则结果为 TRUE；否则任一为 UNKNOWN
    则结果为 UNKNOWN；全部为 FALSE 才为 FALSE。

    Args:
        values: 待析取的输入真值列表。

    Returns:
        析取后的三值真值。
    """

    if any(value == TriValue.TRUE for value in values):
        return TriValue.TRUE
    # 无 TRUE：只要存在 UNKNOWN 就整体无法确定，否则全 FALSE。
    return TriValue.UNKNOWN if any(value == TriValue.UNKNOWN for value in values) else TriValue.FALSE


def evaluate(product: Product, item: dict[str, Any]) -> str:
    """对单个商品求值一条约束（条件节点），返回三值真值。

    递归处理 AND/OR/NOT 组合节点；叶节点按操作符对商品属性做比较。
    属性缺失或比较类型不兼容时返回 UNKNOWN（Kleene 三值逻辑），
    保证缺失信息不会误判为满足或不满足硬约束（约束保持）。

    Args:
        product: 待判定的商品。
        item: 约束节点字典，含 op（操作符）、attr（属性名）、
            value/value2（比较值）或 children（子节点列表）。

    Returns:
        TriValue.TRUE / TriValue.FALSE / TriValue.UNKNOWN 之一。
    """

    op = str(item.get("op", "")).upper()
    if op == "AND":
        return tri_and([evaluate(product, child) for child in item.get("children", [])])
    if op == "OR":
        return tri_or([evaluate(product, child) for child in item.get("children", [])])
    if op == "NOT":
        return tri_not(evaluate(product, item["children"][0]))
    attr = item.get("attr")
    if op == "EXISTS":
        # 存在性判定只看属性是否出现，与具体值无关。
        return TriValue.TRUE if attr in product.attrs else TriValue.FALSE
    if attr not in product.attrs:
        # 属性缺失：三值逻辑下无法判定，返回 UNKNOWN。
        return TriValue.UNKNOWN
    actual = product.attrs[attr]
    wanted = item.get("value")
    try:
        # 逐操作符执行比较，结果为布尔值 result。
        if op == "EQ":
            result = actual == wanted
        elif op == "NEQ":
            result = actual != wanted
        elif op == "IN":
            result = actual in wanted
        elif op == "NOT_IN":
            result = actual not in wanted
        elif op == "GE":
            result = actual >= wanted
        elif op == "LE":
            result = actual <= wanted
        elif op == "RANGE":
            # RANGE 为闭区间 [value, value2]；任一端为 None 表示该侧不设界。
            result = (wanted is None or actual >= wanted) and (
                item.get("value2") is None or actual <= item.get("value2")
            )
        elif op == "CONTAINS":
            # CONTAINS 判定多值属性（列表等）是否包含 wanted 这一元素。
            result = wanted in _values(actual)
        elif op == "SUPERSET":
            # SUPERSET 要求实际多值集合完全覆盖 wanted 集合。
            result = set(item.get("value", [])).issubset(set(_values(actual)))
        else:
            # 未知操作符：保守返回 UNKNOWN。
            return TriValue.UNKNOWN
    except (TypeError, ValueError):
        # 类型不匹配（如字符串与数值比较）视作无法判定。
        return TriValue.UNKNOWN
    return TriValue.TRUE if result else TriValue.FALSE


class ProductIndex:
    """商品倒排索引：等值倒排表 + 数值有序区间表。

    等值/包含类查询走倒排表（集合交并运算），数值范围查询走有序
    区间表的二分查找，把候选行筛选从 O(n) 全表扫描降为只访问相关
    行；最终结果仍由 evaluate 做三值精确校验兜底。
    """

    def __init__(self, products: list[Product]):
        """构建索引。

        Args:
            products: 待索引的商品列表；列表下标即内部候选行号。
        """

        self.products = products
        # 全体行号集合，作为 FULL（不做索引裁剪）及集合运算的起点。
        self.all_rows = set(range(len(products)))
        # postings: attr -> {值的 JSON 规范串 -> 包含该值的行号集合}
        self.postings: dict[str, dict[str, set[int]]] = {}
        # ranges: attr -> 按数值升序排列的 (数值, 行号) 列表，供二分查找。
        self.ranges: dict[str, list[tuple[float, int]]] = {}
        for row, product in enumerate(products):
            for attr, value in product.attrs.items():
                for item in _values(value):
                    # 用 JSON 规范串作为倒排键：ensure_ascii=False 保证中文
                    # 可读、sort_keys=True 保证键序稳定，等价值产生同一键。
                    self.postings.setdefault(attr, {}).setdefault(
                        json.dumps(item, ensure_ascii=False, sort_keys=True), set()
                    ).add(row)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    # 只有真正的数值（排除 bool，bool 是 int 的子类）才建区间索引。
                    self.ranges.setdefault(attr, []).append((value, row))
        for attr in self.ranges:
            # 按 (数值, 行号) 排序，使后续 bisect 二分查找可行。
            self.ranges[attr].sort()

    def lookup(self, item: dict[str, Any]) -> tuple[set[int], str]:
        """按单条叶约束利用索引裁剪候选行集合。

        Args:
            item: 叶约束节点字典（op/attr/value/value2）。

        Returns:
            (候选行号集合, 本次使用的索引操作描述字符串)。
            描述字符串用于 debug 输出，如 "EQ(price)"。
        """

        attr = item.get("attr")
        op = str(item.get("op", "")).upper()
        wanted = item.get("value")
        if op == "EXISTS":
            # EXISTS 无倒排表可用，直接扫描全部商品检查属性是否存在。
            return {i for i, p in enumerate(self.products) if attr in p.attrs}, "EXISTS(" + str(attr) + ")"
        if op in {"GE", "LE", "RANGE"} and attr in self.ranges:
            values = self.ranges[attr]
            numbers = [value for value, _ in values]
            low = wanted if op in {"GE", "RANGE"} else None
            high = item.get("value2") if op == "RANGE" else (wanted if op == "LE" else None)
            # bisect_left/bisect_right 在有序数值列上二分定位闭区间边界，
            # 再切出行号，实现 O(log n + k) 的范围筛选。
            left = bisect_left(numbers, low) if low is not None else 0
            right = bisect_right(numbers, high) if high is not None else len(values)
            return {row for _, row in values[left:right]}, op + "(" + str(attr) + ")"
        if op in {"EQ", "IN", "CONTAINS", "SUPERSET"}:
            posting = self.postings.get(attr, {})
            if op in {"EQ", "CONTAINS"}:
                keys = [wanted]
            elif op == "IN":
                keys = list(wanted or [])
            else:
                # SUPERSET：要求行同时覆盖 wanted 中所有值，故对每个值的
                # 倒排行集合做交集（从全集开始逐步收窄）。
                rows = self.all_rows.copy()
                for value in wanted or []:
                    rows &= posting.get(json.dumps(value, ensure_ascii=False, sort_keys=True), set())
                return rows, "SUPERSET(" + str(attr) + ")"
            rows: set[int] = set()
            # EQ/CONTAINS/IN：任一值命中即候选，故对倒排集合取并集。
            for value in keys:
                rows |= posting.get(json.dumps(value, ensure_ascii=False, sort_keys=True), set())
            return rows, op + "(" + str(attr) + ")"
        # NEQ, NOT_IN and compound negation must retain missing values.
        # 中文翻译：NEQ、NOT_IN 以及复合取反必须保留属性缺失的行
        #（缺失行可能经 NOT 语义成为满足项），因此直接返回全集。
        return self.all_rows.copy(), "FULL(" + str(attr) + ")"

    def candidates(self, item: dict[str, Any]) -> tuple[set[int], list[str]]:
        """递归计算约束树的索引候选行集合。

        Args:
            item: 约束节点（可为 AND/OR/NOT 组合节点或叶节点）。

        Returns:
            (候选行号集合, 各层级索引操作描述列表)。操作列表用于
            debug 输出，记录实际命中哪些索引路径。
        """

        op = str(item.get("op", "")).upper()
        if op in {"AND", "OR"}:
            child_sets = [self.candidates(child) for child in item.get("children", [])]
            if op == "AND":
                # AND：候选行需同时满足所有子约束，取交集。
                rows = self.all_rows.copy()
                for child_rows, _ in child_sets:
                    rows &= child_rows
            else:
                # OR：任一子约束命中即候选，取并集。
                rows = set()
                for child_rows, _ in child_sets:
                    rows |= child_rows
            return rows, [operation for _, operations in child_sets for operation in operations]
        if op == "NOT":
            # 取反节点的子约束无正向索引可裁剪，直接返回全集交给精确校验。
            return self.all_rows.copy(), ["FULL(NOT)"]
        rows, operation = self.lookup(item)
        return rows, [operation]


def build_index_payload(products: list[Product]) -> dict[str, Any]:
    """把商品集合构建为可 JSON 序列化的索引快照字典。

    供 import_products 等工具将索引持久化到 Runtime KV 存储
    （tcpr:current），使后续检索无需重复建索引。

    Args:
        products: 商品列表。

    Returns:
        形如 {"eq_postings": {attr: {值串: [行号...]}},
        "range_indexes": {attr: [[数值, 行号]...]}} 的索引快照。
    """

    index = ProductIndex(products)
    # set 无法 JSON 序列化，把行号集合转成升序列表。
    postings = {
        attr: {key: sorted(rows) for key, rows in values.items()}
        for attr, values in index.postings.items()
    }
    # 元组转列表同样是为 JSON 序列化服务。
    ranges = {attr: [[value, row] for value, row in values] for attr, values in index.ranges.items()}
    return {"eq_postings": postings, "range_indexes": ranges}


def _flatten_and(item: dict[str, Any]) -> list[dict[str, Any]]:
    """递归拍平 AND 子树，返回所有非 AND 叶节点。

    用于 _range_conflict 统一收集同一属性上的全部范围约束。

    Args:
        item: 约束节点。

    Returns:
        拍平后的叶节点列表（AND 节点自身被展开剔除）。
    """

    if item.get("op") == "AND":
        result: list[dict[str, Any]] = []
        for child in item.get("children", []):
            result.extend(_flatten_and(child))
        return result
    return [item]


def _range_conflict(hard: list[dict[str, Any]]) -> str | None:
    """检测硬约束中同一属性的范围条件是否互相矛盾（下界 > 上界）。

    例如 price>=100 且 price<=50 为空区间，任何商品都不可能满足，
    可直接判定 UNSAT 而无需建索引与逐行校验。

    Args:
        hard: 硬约束节点列表（允许嵌套 AND）。

    Returns:
        存在矛盾时返回对应属性名，否则返回 None。
    """

    bounds: dict[str, dict[str, float]] = {}
    # 先拍平 AND，把嵌套的多个范围条件摊到同一层统一检查。
    for item in _flatten_and({"op": "AND", "children": hard}):
        attr = item.get("attr")
        op = item.get("op")
        if op not in {"GE", "LE", "RANGE"}:
            continue
        pair = bounds.setdefault(attr, {})
        # 维护该属性当前最紧的下界：多个下界取最大值。
        if op in {"GE", "RANGE"} and isinstance(item.get("value"), (int, float)):
            pair["lo"] = max(pair.get("lo", item["value"]), item["value"])
        high = item.get("value2") if op == "RANGE" else item.get("value")
        # 维护该属性当前最紧的上界：多个上界取最小值。
        if op in {"LE", "RANGE"} and isinstance(high, (int, float)):
            pair["hi"] = min(pair.get("hi", high), high)
    for attr, pair in bounds.items():
        if "lo" in pair and "hi" in pair and pair["lo"] > pair["hi"]:
            # 下界大于上界 -> 空区间，约束不可满足。
            return attr
    return None


def search_products(products: list[Product], doc: dict[str, Any], top_k: int) -> dict[str, Any]:
    """对商品集合执行一次带硬/软约束的检索。

    流程：先检测硬约束范围矛盾（UNSAT 短路）-> 建索引裁剪候选行 ->
    对候选行逐条三值精确校验（hard 必须全部 TRUE）-> 以命中 soft
    条数打分排序 -> 截断 top_k 返回。

    Args:
        products: 商品全集。
        doc: 检索文档，含 "hard"（硬约束节点列表，必须全部满足）与
            "soft"（软约束节点列表，命中条数作为得分）两个字段。
        top_k: 期望返回条数；None/0 时按默认 20，最终夹在 [1, 100]
            区间内。

    Returns:
        结果字典：status（"OK"/"UNSAT"）、candidate_ids、
        candidates（含 soft_score 的商品字典列表）与 debug 统计。
    """

    hard = list(doc.get("hard", []))
    soft = list(doc.get("soft", []))
    conflict = _range_conflict(hard)
    if conflict:
        # 硬约束范围自身矛盾：无需建索引与逐行计算，直接判定不可满足。
        return {
            "status": "UNSAT",
            "candidate_ids": [],
            "candidates": [],
            "debug": {"reason": "empty hard range", "attribute": conflict},
        }
    index = ProductIndex(products)
    rows = index.all_rows
    operations: list[str] = []
    for item in hard:
        # 用索引对每条硬约束做候选裁剪，硬约束之间取交集。
        child_rows, child_ops = index.candidates(item)
        rows &= child_rows
        operations.extend(child_ops)
    hits: list[tuple[float, Product]] = []
    rejected = 0
    for row in sorted(rows):
        product = products[row]
        # 索引裁剪只是近似（如 FULL、NOT 场景不做裁剪），此处以三值
        # 精确校验兜底：硬约束必须全部为 TRUE 才算真正命中。
        if not all(evaluate(product, item) == TriValue.TRUE for item in hard):
            rejected += 1
            continue
        # 得分 = 满足的软约束条数（sum 中布尔 True 计 1）。
        score = float(sum(evaluate(product, item) == TriValue.TRUE for item in soft))
        hits.append((score, product))
    # 排序键：得分降序（-score），同分按 product_id 升序保证结果稳定。
    hits.sort(key=lambda pair: (-pair[0], pair[1].product_id))
    # 截断返回条数：top_k 缺省按 20，最终夹在 [1, 100] 区间内。
    hits = hits[:max(1, min(int(top_k or 20), 100))]
    candidates = [
        {"product_id": product.product_id, **product.attrs, "soft_score": score}
        for score, product in hits
    ]
    return {
        "status": "OK" if candidates else "UNSAT",
        "candidate_ids": [item["product_id"] for item in candidates],
        "candidates": candidates,
        "debug": {
            "candidate_count": len(rows),
            "verified_count": len(candidates),
            "rejected_after_verification": rejected,
            "index_operations": operations,
            "hard_unknown_policy": "UNKNOWN never satisfies Hard",
        },
    }
