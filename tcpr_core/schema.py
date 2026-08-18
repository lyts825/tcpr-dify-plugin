"""TCPR 插件的生产 Schema 定义模块。

本模块是整个插件的“类型约束核心”：它用代码固定了生产环境上架产品基础数据.xlsx
的 14 列原始字段，并在此基础上扩展出可检索的派生字段（型号、材质、成卷米数等），
统一描述每个字段的类型（string/enum/numeric/boolean/text）、来源列、合法单位与别名。

模块对外提供的能力：
- resolve_attr：把用户输入的属性别名（中文名或英文名）解析为规范的字段名；
- parse_numeric：解析带单位的数值（如 "100米"、"2.5mm2"），校验单位是否合法；
- normalize_value：按字段类型规范化约束中的单个值；
- normalize_constraint_doc：递归规范化整份约束文档（hard/soft/unparsed 三层结构）；
- schema_payload：生成供 get_schema 工具返回的 Schema 描述。

该模块只做“类型与约束的静态校验”，不涉及检索与索引，因此可被其他模块安全复用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Field:
    """单个可检索字段的静态定义。

    Attributes:
        name: 字段的规范英文名（约束文档与 Schema 输出中使用的键名）。
        kind: 字段类型，取值之一：string / enum / numeric / boolean / text。
        source: 该字段在生产数据源中的来源说明（原始列名或“关键属性:xxx”派生名）。
        units: 合法单位元组（空元组表示该字段不携带单位）。
        aliases: 用户可输入的别名元组，至少包含中文名与英文名本身。
    """

    name: str
    kind: str
    source: str
    units: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()


# 全字段清单：前 14 个字段一一对应 SOURCE_COLUMNS 中生产 Excel 的原始列；
# 从 "model" 开始是由“关键属性”列解析出的派生枚举/数值字段，由 import_products 提取。
FIELDS = [
    Field("product_id", "string", "产品编号", aliases=("产品编号", "id", "product_id")),
    Field("product_name", "string", "产品名称", aliases=("产品名称", "product_name")),
    Field("product_subtitle", "string", "产品副标题", aliases=("产品副标题", "product_subtitle")),
    Field("category_level1", "enum", "一级分类", aliases=("一级分类", "category_level1")),
    Field("category_level2", "enum", "二级分类", aliases=("二级分类", "category_level2")),
    Field("category_level3", "enum", "三级分类", aliases=("三级分类", "category_level3")),
    Field("key_attributes", "text", "关键属性", aliases=("关键属性", "key_attributes")),
    Field("common_attributes", "text", "普通属性", aliases=("普通属性", "common_attributes")),
    Field("min_order_qty", "numeric", "起订量", ("", "个", "件"), ("起订量", "min_order_qty")),
    Field("raw_material_basis", "enum", "是否原材料浮动产品", aliases=("是否原材料浮动产品", "raw_material_basis")),
    Field("is_credit_supported", "boolean", "是否支持账期", aliases=("是否支持账期", "is_credit_supported")),
    Field("is_coiled", "boolean", "是否成卷", aliases=("是否成卷", "is_coiled")),
    Field("keywords", "text", "关键词", aliases=("关键词", "keywords")),
    Field("package_list", "text", "包装清单", aliases=("包装清单", "package_list")),
    Field("model", "enum", "关键属性:型号", aliases=("型号", "model")),
    Field("material", "enum", "关键属性:材质", aliases=("材质", "material")),
    Field("shielding_method", "enum", "关键属性:屏蔽方式", aliases=("屏蔽方式", "shielding_method")),
    Field("shielding_material", "enum", "关键属性:屏蔽材质", aliases=("屏蔽材质", "shielding_material")),
    Field("color", "enum", "关键属性:颜色", aliases=("颜色", "color")),
    Field("fiber_spec", "enum", "关键属性:纤芯规格", aliases=("纤芯规格", "fiber_spec")),
    Field("roll_length_m", "numeric", "关键属性:成卷米数", ("", "m", "米"), ("成卷米数", "roll_length_m")),
    Field("cross_section_mm2", "numeric", "关键属性:截面", ("", "mm2", "平方毫米"), ("截面", "cross_section_mm2")),
    Field("core_count", "numeric", "关键属性:芯数", ("", "芯"), ("芯数", "core_count")),
    Field("fiber_core_count", "numeric", "关键属性:光纤芯数", ("", "芯"), ("光纤芯数", "fiber_core_count")),
    Field("length_m", "numeric", "关键属性:长度", ("", "m", "米"), ("长度", "length_m")),
    Field("pixels", "numeric", "关键属性:像素", ("", "px", "像素"), ("像素", "pixels")),
    Field("port_count", "numeric", "关键属性:端口数", ("", "个", "端口"), ("端口数", "port_count")),
]

# 生产 Excel 的 14 个原始列名，顺序必须与 FIELDS 前 14 项一致（get_schema 会原样返回）。
SOURCE_COLUMNS = [
    "产品编号", "产品名称", "产品副标题", "一级分类", "二级分类", "三级分类",
    "关键属性", "普通属性", "起订量", "是否原材料浮动产品", "是否支持账期",
    "是否成卷", "关键词", "包装清单",
]
# 规范字段名 -> Field 对象的查找表。
FIELD_BY_NAME = {field.name: field for field in FIELDS}
# 别名（统一转小写）-> 规范字段名；用于 resolve_attr 的 O(1) 解析。
ALIAS_TO_NAME = {alias.lower(): field.name for field in FIELDS for alias in field.aliases}
# 支持范围查询（RANGE/GE/LE）的数值字段名集合。
RANGE_FIELDS = {field.name for field in FIELDS if field.kind == "numeric"}
# 布尔字段名集合。
BOOLEAN_FIELDS = {field.name for field in FIELDS if field.kind == "boolean"}
# 约束文档中允许出现的全部操作符；AND/OR/NOT 是逻辑组合节点，其余是叶子比较操作符。
KNOWN_OPS = {"EQ", "NEQ", "IN", "NOT_IN", "RANGE", "GE", "LE", "EXISTS", "CONTAINS", "SUPERSET", "AND", "OR", "NOT"}


def resolve_attr(value: str) -> str:
    """把用户提供的属性名（中文名或英文别名）解析为规范字段名。

    Args:
        value: 用户输入的属性名，如 "起订量"、"min_order_qty" 或 "成卷米数"。

    Returns:
        对应的规范字段名（Field.name），如 "min_order_qty"。

    Raises:
        ValueError: 当 value 无法匹配任何已注册别名时（即未知生产属性）。
    """
    # 先字符串化再去除首尾空白并统一小写，保证大小写与多余空格不影响匹配。
    key = str(value).strip().lower()
    if key not in ALIAS_TO_NAME:
        raise ValueError("unknown production attribute: " + str(value))
    return ALIAS_TO_NAME[key]


def parse_numeric(value: Any, field_name: str) -> int | float:
    """解析并校验数值字段的约束值，剥离并验证其单位。

    Args:
        value: 待解析的数值，可以是 int/float、带单位字符串（如 "100米"）、
            或 {"value": ..., "unit": ...} 字典形式。
        field_name: 规范字段名，用于查找该字段允许的单位集合。

    Returns:
        规范化后的数值；整数（如 100.0）会返回 int，非整数返回 float。

    Raises:
        ValueError: 当 value 为布尔值、字符串无法解析为“数值+可选单位”、
            或单位不在该字段的 units 白名单中时。
    """
    field = FIELD_BY_NAME[field_name]
    if isinstance(value, bool):
        # Python 中 bool 是 int 的子类，必须先排除，避免 True 被当成 1。
        raise ValueError(field_name + " cannot be Boolean")
    unit = ""
    if isinstance(value, dict):
        # 字典形式：{"unit": "米", "value": 100}，分别取出单位与数值。
        unit, value = value.get("unit", ""), value.get("value")
    if isinstance(value, str):
        # 正则解析“数字 + 可选单位”的整串匹配：
        # [-+]?\d+(?:\.\d+)? 匹配整数或小数（可带正负号）；
        # 单位部分允许字母数字及 ² ^ . - 等符号（如 mm2、kg/卷），
        # 或中文单位：元/米/个/件/芯/端口/像素/平方毫米；两端允许空白。
        match = re.fullmatch(r"\s*([-+]?\d+(?:\.\d+)?)\s*([A-Za-z0-9²^.-]+|元|米|个|件|芯|端口|像素|平方毫米)?\s*", value)
        if not match:
            # 匹配失败说明字符串含多余字符（如 "10米以上"），视为歧义值。
            raise ValueError(field_name + " numeric value is ambiguous")
        value, parsed_unit = match.groups()
        # 字典里的 unit 优先于字符串中解析出的单位；都没有则为 ""（无单位）。
        unit = unit or parsed_unit or ""
    if unit not in field.units:
        # 单位白名单校验：例如 min_order_qty 只允许 ""、"个"、"件"。
        raise ValueError(field_name + " unsupported unit: " + str(unit))
    result = float(value)
    # 整数化处理：100.0 -> 100，保留非整数的浮点精度（如 2.5）。
    return int(result) if result.is_integer() else result


def normalize_value(attr: str, value: Any) -> Any:
    """按字段类型规范化约束文档中的单个值。

    Args:
        attr: 规范字段名（必须存在于 FIELD_BY_NAME）。
        value: 原始约束值，类型取决于字段 kind 与输入来源。

    Returns:
        规范化后的值：numeric 返回 int/float，boolean 返回 bool，
        enum/string/text 返回去除首尾空白后的字符串（None 原样返回）。

    Raises:
        ValueError: 当布尔字段的值无法识别、或 enum 字段误传了布尔值时。
    """
    field = FIELD_BY_NAME[attr]
    if field.kind == "numeric":
        # 数值字段委托 parse_numeric 完成“数值+单位”的解析与校验。
        return parse_numeric(value, attr)
    if field.kind == "boolean":
        # 布尔字段接受原生 bool，或一组约定的中文/英文真值词。
        if isinstance(value, bool):
            return value
        key = str(value).strip().lower()
        if key in {"是", "yes", "true", "支持"}:
            return True
        if key in {"否", "no", "false", "不支持"}:
            return False
        raise ValueError(attr + " requires 是/否")
    if field.kind == "enum" and isinstance(value, bool):
        # bool 是 int 的子类，枚举字段收到布尔值属于类型混淆，必须显式拒绝。
        raise ValueError(attr + " is enum, not Boolean")
    # 其余类型统一转为去除首尾空白的字符串；None 保持原样（如 EXISTS 缺值场景）。
    return str(value).strip() if value is not None else value


def normalize_constraint_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """递归规范化整份约束文档，返回结构固定、类型安全的约束树。

    约束文档的顶层包含三个列表：
    - hard：必须满足的硬约束；
    - soft：尽量满足的软约束（不满足只降权不淘汰）；
    - unparsed：无法解析的原生文本片段（原样保留，供后续告警展示）。
    每个约束节点是 {"op": ..., "attr": ..., "value": ...} 字典；
    AND/OR/NOT 为逻辑组合节点，其子节点放在 "children" 列表中递归处理。

    Args:
        doc: 原始约束文档字典，可能来自 LLM 输出或用户手工构造。

    Returns:
        规范化后的约束文档：所有别名已解析为规范字段名，所有值已按字段
        类型规范化，逻辑节点被递归重写为 {op, children} 形态。

    Raises:
        ValueError: 顶层不是字典、节点不是字典、操作符不支持、NOT 子节点
            数量不为 1、列表类操作符的值不是列表、字段不存在或值不合法时。
    """
    if not isinstance(doc, dict):
        raise ValueError("constraint document must be object")
    allowed_top_level = {"hard", "soft", "unparsed"}
    unknown_top_level = set(doc) - allowed_top_level
    if unknown_top_level:
        raise ValueError("unknown constraint document fields: " + ", ".join(sorted(unknown_top_level)))
    hard = doc.get("hard", [])
    soft = doc.get("soft", [])
    unparsed = doc.get("unparsed", [])
    if not isinstance(hard, list) or not isinstance(soft, list) or not isinstance(unparsed, list):
        raise ValueError("hard, soft and unparsed must be arrays")
    if not hard and not soft:
        raise ValueError("at least one hard or soft constraint is required")

    def atom(item: dict[str, Any]) -> dict[str, Any]:
        """规范化单个约束节点（递归处理逻辑组合节点的子节点）。

        Args:
            item: 单个原始约束节点字典。

        Returns:
            规范化后的节点字典；叶子节点形如 {"attr": ..., "op": ..., "value": ...}，
            RANGE 节点额外携带 "value2"，逻辑节点形如 {"op": ..., "children": [...]}。

        Raises:
            ValueError: 节点结构、操作符、属性名或值不符合 Schema 约束时。
        """
        if not isinstance(item, dict):
            raise ValueError("constraint must be object")
        op = str(item.get("op", "")).upper()
        if op in {"AND", "OR", "NOT"}:
            # 逻辑组合节点：递归规范化 children；NOT 是单目运算，只允许 1 个子节点。
            children = item.get("children")
            if (not isinstance(children, list) or not children or
                    (op == "NOT" and len(children) != 1)):
                raise ValueError(op + " children invalid")
            return {"op": op, "children": [atom(child) for child in children]}
        if op not in KNOWN_OPS:
            raise ValueError("unsupported operator: " + op)
        # 叶子节点：把中文/英文别名解析为规范字段名。
        attr = resolve_attr(item.get("attr", ""))
        # 值字段名做兼容处理：优先取 "value"，缺省时回退到 "values"（列表型）。
        value = item.get("value", item.get("values"))
        result = {"attr": attr, "op": op}
        if op != "EXISTS":
            if value is None:
                raise ValueError(attr + " missing value")
            # EXISTS 只判断字段是否存在，不携带值。
            if op in {"IN", "NOT_IN", "SUPERSET"}:
                # 列表型操作符要求值是列表，并逐元素按字段类型规范化。
                if not isinstance(value, list) or not value:
                    raise ValueError(op + " requires a non-empty list")
                result["value"] = [normalize_value(attr, x) for x in value]
            else:
                # 标量型操作符（EQ/NEQ/RANGE/GE/LE/CONTAINS）直接规范化单个值。
                result["value"] = normalize_value(attr, value)
            if op == "RANGE":
                # RANGE 是双边界操作符，额外规范化上界 value2。
                if item.get("value2") is None:
                    raise ValueError("RANGE requires value2")
                result["value2"] = normalize_value(attr, item.get("value2"))
        return result

    return {
        "hard": [atom(x) for x in hard],
        "soft": [atom(x) for x in soft],
        "unparsed": list(unparsed),
    }


def schema_payload(record_count: int = 0) -> dict[str, Any]:
    """生成 get_schema 工具返回的 Schema 描述字典。

    Args:
        record_count: 当前索引中的商品记录数，默认 0（未索引状态）。

    Returns:
        Schema 描述字典，包含：
        - schema_version：Schema 版本号（变更字段结构时需同步升级）；
        - source：数据源信息（14 个原始列名 + 记录数）；
        - fields：每个可检索字段的类型、来源、单位、是否支持范围查询
          以及索引方式（数值字段为 eq/in/range，其余为 eq/in）；
        - dynamic_long_tail：长尾文本字段的保留策略说明，声明只保留原文与
          告警/未解析片段、不做类型推断。
    """
    return {
        "schema_version": "tcpr-production-v1",
        "source": {"columns": SOURCE_COLUMNS, "record_count": record_count},
        "fields": {
            field.name: {
                "kind": field.kind,
                "source": field.source,
                "units": list(field.units),
                "range_supported": field.kind == "numeric",
                "index": "eq/in" if field.kind != "numeric" else "eq/in/range",
            }
            for field in FIELDS
        },
        "dynamic_long_tail": {
            "fields": ["key_attributes", "common_attributes", "keywords", "package_list"],
            "policy": "retain text and warnings/unparsed; do not infer types",
        },
    }
