"""产品数据文件解析与规范化模块（ingest）。

该模块是 TCPR 插件数据入库（import_products）的第一步，承担“读取 + 清洗”
两个职责：
1. read_product_rows 读取上传的 CSV 或 XLSX 商品文件，校验文件大小、
   行数、单元格长度，并要求表头与生产环境标准 14 列（schema.SOURCE_COLUMNS）
   完全一致；
2. normalize_product_rows 把每行原始数据规范化为带类型的属性字典，
   供后续 staging 快照建索引使用（见 tcpr_core 中索引构建逻辑）。

设计边界：
- 不打包 Excel、不嵌入真实商品记录，商品行在运行时从上传文件读取；
- 本地测试使用 InMemoryStorage 时同样依赖本模块解析输入文件；
- 硬性错误（编码、表头、重复产品编号等）直接抛异常整批拒绝，
  字段级类型化失败则降级为结构化 warning，避免带病数据破坏索引快照。
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Iterable
from typing import Any
from xml.etree import ElementTree as ET

from .schema import FIELD_BY_NAME, SOURCE_COLUMNS, normalize_value, resolve_attr

# 输入文件大小硬上限：64 MiB（64 * 1024 * 1024 字节）。
# 与 manifest.yaml 中申请的总存储配额 256 MiB 保持安全余量，
# 防止超大文件在解析时耗尽插件内存。
MAX_INPUT_BYTES = 64 * 1024 * 1024
# 数据行数上限：CSV 严格限制 100,000 行；
# XLSX 允许 100,000 行数据外加 1 行表头（见 _xlsx_rows 中 MAX_ROWS + 1）。
MAX_ROWS = 100_000
# 单个单元格字符数上限：20,000 字符，
# 防止异常超长单元格（如误将整段说明文字粘进一格）拖垮解析。
MAX_CELL_CHARS = 20_000
# “关键属性”列中文标签到插件内部标准属性名（schema.Field.name）的映射。
# 解析关键属性字符串时（_split_key_attributes）先按标签查本表得到目标
# 字段名，再交给 schema.normalize_value 做类型化解析与校验。
KEY_ATTRIBUTE_MAP = {
    "型号": "model", "规格型号": "model",
    "材质": "material", "绝缘材质": "material",
    "屏蔽方式": "shielding_method", "屏蔽材质": "shielding_material",
    "颜色": "color", "纤芯规格": "fiber_spec",
    "成卷米数": "roll_length_m", "截面": "cross_section_mm2",
    "芯数": "core_count", "光纤芯数": "fiber_core_count",
    "长度": "length_m", "像素": "pixels", "端口数": "port_count",
}


def _cell_column(ref: str) -> int:
    """把 Excel 单元格引用（如 "A1"、"BC12"）中的列字母换算成 0 基列索引。

    参数：
        ref: 单元格引用字符串（XLSX 中 c 元素的 r 属性），可能为空串。

    返回：
        int —— 0 基列索引：A -> 0、B -> 1、...、Z -> 25、AA -> 26。
        引用中不含大写字母时返回 0。

    说明：
        列号是 26 进制“无零位”计数（A=1..Z=26）：
        index = index * 26 + (ord(char) - 64)，
        其中 ord('A') = 65，故 ord(char) - 64 得到 1..26 的位值，
        展开后再减 1 转为 0 基索引。
    """
    # 提取单元格引用开头的连续大写字母部分（如 "BC12" -> "BC"）
    letters = re.match(r"[A-Z]+", ref or "")
    if not letters:
        return 0
    index = 0
    # 26 进制展开：每读一位字母，先乘 26（进一位）再加当前位值
    for char in letters.group(0):
        index = index * 26 + ord(char) - 64
    return index - 1


def _text_from_element(element: ET.Element, tag: str) -> str:
    """递归收集元素内所有指定标签（tag）的文本并拼接。

    参数：
        element: 待搜索的 XML 元素（作为搜索根）。
        tag: 要匹配的完整限定标签名（如 "{命名空间}t"）。

    返回：
        str —— 所有匹配子元素的文本按文档顺序拼接的结果；
        文本节点为 None（空标签）时按空串处理，不会抛异常。
    """
    return "".join(item.text or "" for item in element.iter(tag))


def _xlsx_rows(data: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """解析 XLSX 文件字节流，返回表头列表与数据行字典列表。

    XLSX 本质是 ZIP 包，本函数直接读取包内 XML：
    - xl/sharedStrings.xml：共享字符串表（t="s" 的单元格只存其下标）；
    - xl/workbook.xml：工作簿结构，从中取第一个工作表；
    - xl/_rels/workbook.xml.rels：关系文件，把工作表的 rId 映射成实际路径；
    - 第一个工作表的 sheetData：逐行读取单元格文本。

    参数：
        data: XLSX 文件的完整字节内容。

    返回：
        (headers, rows_out) —— headers 为表头字符串列表；
        rows_out 为每行一个字典（表头 -> 单元格文本），
        行内缺失的单元格用空串补齐，保证每行长度与表头一致。

    异常：
        ValueError —— 不是合法 XLSX 工作簿、没有工作表、
                       行数超过 MAX_ROWS + 1、单元格超过 MAX_CELL_CHARS、
                       或文件为空。
    """
    # OpenXML 常用命名空间：m 主文档、r 文档关系、p 包关系
    ns = {
        "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        "p": "http://schemas.openxmlformats.org/package/2006/relationships",
    }
    # XLSX 即 ZIP：用 BytesIO 包一层，让 zipfile 直接读内存中的字节
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        if "xl/workbook.xml" not in names:
            raise ValueError("not a valid XLSX workbook")
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            # 共享字符串表：每个 <si> 条目内全部 <t> 文本拼成一个字符串
            shared = [
                _text_from_element(item, "{%s}t" % ns["m"])
                for item in root.findall("m:si", ns)
            ]
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        # 关系表：Id -> Target（包内相对路径），用于定位工作表文件
        relation_map = {
            item.attrib["Id"]: item.attrib["Target"]
            for item in relationships
        }
        sheets = workbook.find("m:sheets", ns)
        if sheets is None or not list(sheets):
            raise ValueError("XLSX has no worksheet")
        # 取第一个工作表的 r:id（r 命名空间属性），再借关系表得到文件路径
        relation_id = list(sheets)[0].attrib["{%s}id" % ns["r"]]
        target = relation_map[relation_id].lstrip("/")
        # Target 可能带前导斜杠（如 "/xl/..."），统一成包内相对路径
        sheet_path = target if target.startswith("xl/") else "xl/" + target
        root = ET.fromstring(archive.read(sheet_path))
        # sheetData 下的全部 row；+1 是因为首行是表头，限制的是数据行数
        rows = root.findall(".//m:sheetData/m:row", ns)
        if len(rows) > MAX_ROWS + 1:
            raise ValueError("row limit exceeded")
        parsed: list[list[str]] = []
        for row in rows:
            # 稀疏存储：列索引 -> 单元格文本，缺失列不占条目
            values: dict[int, str] = {}
            for cell in row.findall("m:c", ns):
                value = ""
                node = cell.find("m:v", ns)
                if node is not None:
                    value = node.text or ""
                    # t="s" 表示共享字符串：v 里存的是 shared 列表的下标
                    if cell.attrib.get("t") == "s" and value:
                        value = shared[int(value)]
                elif cell.attrib.get("t") == "inlineStr":
                    # 内联字符串：文本直接写在 <is><t> 里，不经过共享表
                    value = _text_from_element(cell, "{%s}t" % ns["m"])
                if len(value) > MAX_CELL_CHARS:
                    raise ValueError("cell character limit exceeded")
                # 用列字母换算 0 基列号；无 r 属性的单元格按第 0 列处理
                values[_cell_column(cell.attrib.get("r", ""))] = value
            # 行宽 = 最大列号 + 1；整行为空（values 为空）时宽度为 0
            width = max(values, default=-1) + 1
            # 把稀疏行铺平成定长列表，缺失列位置填空串
            parsed.append([values.get(index, "") for index in range(width)])
    if not parsed:
        raise ValueError("XLSX is empty")
    headers = parsed[0]
    # 首行作表头；数据行与表头 zip 成字典，行内缺列用空串补齐
    # （row + [""] * n 保证 zip 时键都能配上值）
    rows_out = [dict(zip(headers, row + [""] * (len(headers) - len(row)))) for row in parsed[1:]]
    return headers, rows_out


def _csv_rows(data: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """解析 CSV 文件字节流，返回表头列表与数据行字典列表。

    编码按 utf-8-sig（带 BOM 的 UTF-8）、utf-8、gb18030 顺序尝试解码，
    覆盖常见中文 CSV 场景（Windows Excel 导出的中文 CSV 多为 GB 系）。

    参数：
        data: CSV 文件的完整字节内容。

    返回：
        (headers, rows) —— headers 为表头字符串列表；
        rows 为每行一个字典（表头 -> 字段文本）。

    异常：
        ValueError —— 三种候选编码均无法解码（即编码不是
                       UTF-8/GB18030），或数据行数超过 MAX_ROWS。
    """
    text = None
    # 依次尝试候选编码，任一成功即跳出循环；全部失败则 text 保持 None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("CSV encoding must be UTF-8 or GB18030")
    # DictReader 自动把首行当表头，其后每行按表头转成 dict
    reader = csv.DictReader(io.StringIO(text))
    headers = list(reader.fieldnames or [])
    rows = [dict(row) for row in reader]
    if len(rows) > MAX_ROWS:
        raise ValueError("row limit exceeded")
    return headers, rows


def read_product_rows(data: bytes, filename: str = "") -> tuple[list[str], list[dict[str, str]]]:
    """读取上传的商品文件（CSV 或 XLSX）并做基础校验。

    文件类型判定规则：
    - 文件名以 .csv 结尾 -> 按 CSV 解析；
    - 文件名不以 .xlsx 结尾、且内容不是 ZIP 魔数（b"PK"）-> 按 CSV 解析
      （容忍不带扩展名或扩展名写错的 CSV）；
    - 其余情况 -> 按 XLSX 解析（.xlsx 扩展名，或内容确实是 ZIP 包）。

    解析成功后校验表头必须与生产环境标准 14 列（schema.SOURCE_COLUMNS）
    完全一致，且至少存在一行数据。

    参数：
        data: 上传文件的完整字节内容。
        filename: 原始文件名，仅用于扩展名判定，可为空串。

    返回：
        (headers, rows) —— headers 为标准表头列表；
        rows 为每行一个字典（列名 -> 单元格文本）。

    异常：
        TypeError —— data 不是 bytes/bytearray。
        ValueError —— 文件超过 64 MiB、表头与标准 14 列不一致、
                       没有数据行，或底层解析器（_csv_rows/_xlsx_rows）
                       抛出的各类校验错误。
    """
    # 只接受字节流，避免调用方误传已解码的 str
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("file input must be bytes")
    # 64 MiB 硬上限，防止超大文件拖垮插件内存
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError("input file exceeds 64 MiB limit")
    lower = filename.lower()
    # ZIP 魔数为 b"PK"：没有 .xlsx 扩展名但内容确是 ZIP 包的，
    # 交给 XLSX 解析器；反之非 ZIP 内容一律走 CSV 路径
    if lower.endswith(".csv") or (not lower.endswith(".xlsx") and data[:2] != b"PK"):
        headers, rows = _csv_rows(bytes(data))
    else:
        headers, rows = _xlsx_rows(bytes(data))
    # 表头必须与生产 Schema 的 14 列完全一致（顺序也一致），否则整批拒绝
    if headers != SOURCE_COLUMNS:
        raise ValueError("unexpected product headers; expected the 14 production columns exactly")
    if not rows:
        raise ValueError("product file has no data rows")
    return headers, rows


def _split_key_attributes(value: str) -> Iterable[tuple[str, str]]:
    """把“关键属性”单元格文本拆成 (标签, 值) 二元组序列。

    支持的分隔符为 &、;、换行、竖线（|）；每个片段须形如
    “标签: 值”或“标签：值”（冒号兼容中英文半全角），
    无法匹配该格式的片段会被静默跳过（不报错）。

    参数：
        value: 关键属性单元格原文，如 "型号:RVS;芯数:2"。

    返回：
        Iterable[tuple[str, str]] —— 逐个产出
        (去首尾空格后的标签, 去首尾空格后的值)。
    """
    # 先按分隔符切成片段：& ; 换行 |，连续分隔符按一个处理（+ 量词）
    for part in re.split(r"[&;\n|]+", value or ""):
        part = part.strip()
        if not part:
            continue
        # 片段须形如“标签: 值”：标签不含冒号（[^:：]+），
        # 冒号中英文均可，值部分非贪婪匹配到行尾（.*?）
        match = re.match(r"^\s*([^:：]+)\s*[:：]\s*(.*?)\s*$", part)
        if match:
            yield match.group(1).strip(), match.group(2).strip()


def _warn(warnings: list[dict[str, Any]], row_number: int, field: str, message: str) -> None:
    """向警告列表追加一条结构化警告记录（原地修改列表）。

    参数：
        warnings: 用于收集警告的列表，直接 append。
        row_number: 出现问题的原始文件行号（表头为第 1 行，数据从 2 起），
                    便于用户按行号定位问题。
        field: 出问题的字段名（或关键属性标签）。
        message: 具体错误描述，通常为 normalize_value 抛出的异常文本。

    返回：
        None —— 仅通过传入的列表输出结果。
    """
    warnings.append({"row": row_number, "field": field, "message": message})


def normalize_product_rows(rows: list[dict[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把解析后的商品行规范化成带类型的属性字典。

    对每一行依次处理：
    1. 校验“产品编号”非空且整批内不重复（违反直接抛异常，整批拒绝）；
    2. 直接映射的文本列（产品名称、三级分类、关键词、包装清单等）
       去首尾空格后原样写入 attrs，空值跳过不写入；
    3. 三列类型化字段（起订量/是否支持账期/是否成卷）经
       schema.normalize_value 做类型化解析，失败降级为警告；
    4. “关键属性”列按 _split_key_attributes 拆分后，经 KEY_ATTRIBUTE_MAP
       落到标准字段并做类型化解析；未知标签保留在原始 key_attributes
       文本中并记警告；解析失败同样记警告。

    参数：
        rows: read_product_rows 返回的数据行字典列表。

    返回：
        (products, warnings) ——
        products 为规范化结果列表，每项形如
        {"product_id": ..., "attrs": {...}, "raw": {...}}；
        attrs 是类型化后的标准属性字典，raw 是整行原文快照
        （供索引构建时回填未映射的原始文本）。
        warnings 为结构化的 {row, field, message} 警告列表，
        记录字段级类型化失败但不阻断导入。

    异常：
        ValueError —— 某行产品编号为空，或与前面的行重复。
    """
    products: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    # 已见产品编号集合，用于整批查重
    seen: set[str] = set()
    # start=2：文件第 1 行是表头，数据行号从 2 开始，便于用户按行号定位
    for row_number, row in enumerate(rows, start=2):
        product_id = str(row.get("产品编号", "") or "").strip()
        if not product_id:
            raise ValueError("row %d has empty 产品编号" % row_number)
        if product_id in seen:
            raise ValueError("duplicate 产品编号: " + product_id)
        seen.add(product_id)
        attrs: dict[str, Any] = {"product_id": product_id}
        # 直接透传的文本列：内部标准属性名 -> Excel 列名
        direct = {
            "product_name": "产品名称", "product_subtitle": "产品副标题",
            "category_level1": "一级分类", "category_level2": "二级分类",
            "category_level3": "三级分类", "key_attributes": "关键属性",
            "common_attributes": "普通属性", "keywords": "关键词",
            "package_list": "包装清单", "raw_material_basis": "是否原材料浮动产品",
        }
        for attr, column in direct.items():
            # 去首尾空格；空值不写入 attrs，保持 attrs 精简
            value = str(row.get(column, "") or "").strip()
            if value:
                attrs[attr] = value
        # 需要类型化解析的三列：解析失败只记警告，不中断整批
        for attr, column in {
            "min_order_qty": "起订量",
            "is_credit_supported": "是否支持账期",
            "is_coiled": "是否成卷",
        }.items():
            value = str(row.get(column, "") or "").strip()
            if not value:
                continue
            try:
                # normalize_value 按 schema 中字段类型把字符串转成数值/布尔等
                attrs[attr] = normalize_value(attr, value)
            except ValueError as exc:
                _warn(warnings, row_number, column, str(exc))
        # 关键属性：先按分隔符拆成 (标签, 值)，再经映射表落到标准字段
        for label, value in _split_key_attributes(str(row.get("关键属性", "") or "")):
            attr = KEY_ATTRIBUTE_MAP.get(label)
            if attr is None:
                # 未知标签：保留在原始 key_attributes 文本里，仅记警告
                _warn(warnings, row_number, label, "dynamic key attribute retained in key_attributes")
                continue
            try:
                attrs[attr] = normalize_value(attr, value)
            except ValueError as exc:
                _warn(warnings, row_number, label, str(exc))
        # raw 保存整行原文快照，便于后续索引构建回填未映射的原始文本
        products.append({"product_id": product_id, "attrs": attrs, "raw": dict(row)})
    return products, warnings
