from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping


class SchemaError(ValueError):
    pass


@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: str
    aliases: tuple[str, ...] = ()
    value_aliases: Mapping[str, Any] = field(default_factory=dict)
    unit_multipliers: Mapping[str, int] = field(default_factory=dict)
    enum_order: tuple[str, ...] = ()

    def canonical_value(self, value: Any, unit: str | None = None) -> Any:
        if self.kind == "numeric":
            if isinstance(value, bool):
                raise SchemaError(f"{self.name}: boolean is not numeric")
            if isinstance(value, dict):
                unit = value.get("unit")
                value = value.get("value")
            if isinstance(value, str):
                import re
                # Capture the unit as a token and let the declared unit map
                # decide whether it is supported. This keeps manual index
                # definitions usable for Chinese units such as 厘米/公斤.
                match = re.match(r"^\s*([-+]?\d+(?:\.\d+)?)\s*(\S*)\s*$", value)
                if match:
                    value, parsed_unit = match.groups()
                    unit = unit if unit is not None else (parsed_unit or "")
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise SchemaError(f"{self.name}: invalid number {value!r}") from exc
            multiplier = self.unit_multipliers.get(unit or "", 1)
            if unit not in (None, "") and unit not in self.unit_multipliers:
                raise SchemaError(f"{self.name}: unsupported unit {unit!r}")
            result = number * multiplier
            return int(result) if result.is_integer() else result
        if self.kind in {"string", "text"}:
            if value is None:
                return None
            return str(value).strip()
        if self.kind == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.strip().lower() in {"true", "yes", "是", "有"}:
                return True
            if isinstance(value, str) and value.strip().lower() in {"false", "no", "否", "无"}:
                return False
            raise SchemaError(f"{self.name}: invalid boolean {value!r}")
        if self.kind in {"enum", "ordered_enum"}:
            key = str(value).strip().lower()
            aliases = {str(k).lower(): v for k, v in self.value_aliases.items()}
            result = aliases.get(key, value)
            if self.enum_order and result not in self.enum_order:
                raise SchemaError(f"{self.name}: invalid enum value {value!r}")
            return result
        if self.kind == "multi":
            values = value if isinstance(value, (list, tuple, set, frozenset)) else [value]
            return frozenset(self._canonical_multi_value(v) for v in values)
        return value

    def _canonical_multi_value(self, value: Any) -> Any:
        key = str(value).strip().lower()
        aliases = {str(k).lower(): v for k, v in self.value_aliases.items()}
        return aliases.get(key, value)

    def rank(self, value: Any) -> int:
        if self.kind != "ordered_enum" or value not in self.enum_order:
            raise SchemaError(f"{self.name} is not an ordered enum value")
        return self.enum_order.index(value)


class Schema:
    def __init__(self, fields: Iterable[FieldSpec]):
        self.fields = {f.name: f for f in fields}
        self.aliases = {a: f.name for f in self.fields.values() for a in (f.name, *f.aliases)}

    def resolve_attr(self, name: str) -> str:
        key = str(name).strip().lower()
        for alias, canonical in self.aliases.items():
            if alias.lower() == key:
                return canonical
        raise SchemaError(f"unknown attribute: {name}")

    def field(self, name: str) -> FieldSpec:
        return self.fields[self.resolve_attr(name)]

    def normalize_value(self, attr: str, value: Any, unit: str | None = None) -> Any:
        return self.field(attr).canonical_value(value, unit)

    def validate(self, attrs: Mapping[str, Any]) -> None:
        for key, value in attrs.items():
            self.normalize_value(key, value)

    def to_dict(self) -> dict[str, Any]:
        return {name: {"kind": f.kind, "aliases": list(f.aliases),
                        "value_aliases": dict(f.value_aliases),
                        "units": dict(f.unit_multipliers),
                        "enum_order": list(f.enum_order)}
                for name, f in self.fields.items()}


def default_schema() -> Schema:
    return Schema([
        FieldSpec("price_cny", "numeric", ("price", "价格", "售价"),
                  unit_multipliers={"元": 1, "cny": 1, "": 1}),
        FieldSpec("ram_mb", "numeric", ("ram", "内存", "运行内存"),
                  unit_multipliers={"MB": 1, "GB": 1024, "mb": 1, "gb": 1024,
                                    "G": 1024, "g": 1024, "": 1}),
        FieldSpec("storage_gb", "numeric", ("storage", "硬盘", "存储"),
                  unit_multipliers={"GB": 1, "TB": 1024, "gb": 1, "tb": 1024, "": 1}),
        FieldSpec("gpu", "enum", ("显卡", "graphics"),
                  value_aliases={"4060": "rtx4060", "rtx 4060": "rtx4060", "rtx4060": "rtx4060",
                                 "4070": "rtx4070", "rtx 4070": "rtx4070", "rtx4070": "rtx4070",
                                 "集成显卡": "integrated", "integrated graphics": "integrated"}),
        FieldSpec("brand", "enum", ("品牌",), value_aliases={"联想": "lenovo", "lenovo": "lenovo", "戴尔": "dell", "dell": "dell"}),
        FieldSpec("color", "enum", ("颜色",), value_aliases={"黑色": "black", "白色": "white"}),
        FieldSpec("features", "multi", ("特性", "features"), value_aliases={"雷电": "thunderbolt", "指纹": "fingerprint", "背光键盘": "backlit_keyboard"}),
        FieldSpec("os", "enum", ("系统",), value_aliases={"windows": "windows", "windows 11": "windows", "linux": "linux"}),
    ])
