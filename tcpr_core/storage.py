"""TCPR 插件的持久化存储层。

本模块在 Dify 运行时 KV 存储与插件核心逻辑之间建立边界：
- StorageAdapter 协议抽象存储后端（生产环境为 RuntimeKVStorageAdapter，
  本地测试为 InMemoryStorage）；
- _pack/_unpack 提供 JSON 序列化 + gzip 压缩 + base64 编码的打包/解包，
  压缩体积以适配 Dify 256 MiB 的存储配额；
- GenerationStore 以「代际（generation）」为单位管理商品数据快照，
  写入采用「先 staging、数据全部落盘后原子切换指针（tcpr:current）」的顺序，
  保证索引重建失败不会破坏旧快照。
"""
from __future__ import annotations

import base64
import gzip
import json
import time
import uuid
from collections.abc import Mapping
from typing import Any, Protocol


class StorageNotConfigured(RuntimeError):
    """存储未配置或不可用异常。

    当 Dify 运行时存储后端缺失、接口方法不存在或返回值类型非法时抛出；
    调用方可通过捕获该异常区分「存储故障」与普通业务错误。
    """

    pass


class StorageAdapter(Protocol):
    """存储适配器协议。

    定义 GenerationStore 依赖的最小存储接口；任何实现了 get/put 两个方法的
    对象都可用作存储后端（结构化类型，无需显式继承本协议）。
    """

    # 读取键对应的值（bytes 或 str）；键不存在时返回 None
    def get(self, key: str) -> bytes | str | None: ...
    # 写入字节串值
    def put(self, key: str, value: bytes) -> None: ...


class InMemoryStorage:
    """Deterministic local test double; never represents Dify persistence.
    确定性的本地测试替身（test double）；绝不代表 Dify 的持久化存储。
    仅用于本地单元测试，行为可完全复现。
    """

    def __init__(self):
        """初始化空的键值字典作为内存存储空间。"""
        self.values: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        """按 key 读取字节串。

        参数：
            key：存储键。
        返回：
            对应的 bytes 值；键不存在时返回 None。
        """
        return self.values.get(key)

    def put(self, key: str, value: bytes) -> None:
        """写入字节串到内存字典。

        参数：
            key：存储键。
            value：待写入的字节串。
        异常：
            TypeError：value 不是 bytes 时抛出（模拟 Dify 存储的类型约束）。
        """
        if not isinstance(value, bytes):
            raise TypeError("in-memory storage values must be bytes")
        self.values[key] = value


class RuntimeKVStorageAdapter:
    """Small adapter boundary; SDK storage method names are not guessed.
    轻量适配器边界；不臆测 SDK 的存储方法名（而是在运行时动态探测）。
    生产环境中将 Dify 插件 runtime 暴露的 storage 后端适配为 StorageAdapter 协议。
    """

    def __init__(self, runtime: Any):
        """保存 Dify 插件运行时对象引用。

        参数：
            runtime：Dify 插件 SDK 注入的 runtime 对象，期望其暴露 storage 属性。
        """
        self.runtime = runtime

    def _backend(self) -> Any:
        """获取 runtime 的 storage 后端对象。

        返回：
            runtime.storage 属性值。
        异常：
            StorageNotConfigured：runtime 未提供 storage 属性（值为 None）时抛出，
            此时不假设任何本地持久化兜底。
        """
        backend = getattr(self.runtime, "storage", None)
        if backend is None:
            raise StorageNotConfigured(
                "Dify runtime storage is unavailable; no local persistence is assumed"
            )
        return backend

    def get(self, key: str) -> bytes | None:
        """从 Dify 运行时存储读取值并归一化为 bytes。

        参数：
            key：存储键。
        返回：
            bytes 值；后端返回 None 时原样返回 None。
        异常：
            StorageNotConfigured：后端没有可调用的 get 方法，或返回值既不是
                None/bytes 也不是 str 时抛出。
        """
        backend = self._backend()
        getter = getattr(backend, "get", None)
        if not callable(getter):
            raise StorageNotConfigured("Dify storage adapter requires a documented get method")
        value = getter(key)
        if value is None or isinstance(value, bytes):
            return value
        if isinstance(value, str):
            # 后端若返回 str，统一按 UTF-8 编码为 bytes，保证上层类型一致
            return value.encode("utf-8")
        raise StorageNotConfigured("Dify storage get must return bytes or str")

    def put(self, key: str, value: bytes) -> None:
        """向 Dify 运行时存储写入 bytes 值。

        参数：
            key：存储键。
            value：待写入的字节串。
        异常：
            TypeError：value 不是 bytes 时抛出。
            StorageNotConfigured：后端没有可调用的 put/set 方法时抛出。
        """
        if not isinstance(value, bytes):
            raise TypeError("Dify storage values must be bytes")
        backend = self._backend()
        # 兼容不同 SDK 版本的写入方法名：优先探测 set，回退到 put
        setter = getattr(backend, "set", None) or getattr(backend, "put", None)
        if not callable(setter):
            raise StorageNotConfigured("Dify storage adapter requires a documented put/set method")
        setter(key, value)


def _pack(value: Any) -> bytes:
    """将任意 JSON 可序列化对象打包为紧凑字节串。

    处理流程：JSON 序列化（UTF-8、紧凑分隔符）→ gzip 压缩（级别 6）→
    base64 编码。压缩可显著缩小商品数据的存储体积，以适配 Dify 256 MiB
    的存储配额。

    参数：
        value：待打包对象；遇到 set 等 JSON 原生不可序列化的类型时，
            由 default=list 兜底转换为列表。
    返回：
        编码后的 bytes。
    """
    # 紧凑 JSON：ensure_ascii=False 保留非 ASCII 字符原文（体积更小），
    # separators 去除分隔符两侧冗余空白
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=list).encode("utf-8")
    # compresslevel=6 是 gzip 的默认压缩级别，在压缩率与 CPU 开销之间取平衡
    return base64.b64encode(gzip.compress(raw, compresslevel=6))


def _unpack(value: bytes | str) -> Any:
    """解包 _pack 生成的字节串，还原为原始 Python 对象。

    参数：
        value：bytes 或 str（str 会先按 UTF-8 编码为 bytes）。
    返回：
        反序列化后的对象。
    """
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    return json.loads(gzip.decompress(base64.b64decode(encoded)).decode("utf-8"))


class GenerationStore:
    """代际商品数据存储。

    以「generation」（一代快照）为单位组织商品、索引、schema 与告警数据；
    每个代际的所有键统一使用 "tcpr:<generation>:<kind>" 前缀，当前生效的
    代际由指针键 tcpr:current 指向。写入采用「先 staging、数据全部落盘后
    原子切换指针」的顺序，确保重建索引失败时旧快照仍可读取。
    """

    CURRENT_KEY = "tcpr:current"  # 指向当前生效代际的指针键
    CHUNK_SIZE = 500  # 商品列表分块落盘时每块的最大条数

    def __init__(self, storage: StorageAdapter):
        """绑定底层存储适配器。

        参数：
            storage：实现 StorageAdapter 协议的后端（生产为
                RuntimeKVStorageAdapter，本地测试为 InMemoryStorage）。
        """
        self.storage = storage

    def current_generation(self) -> str | None:
        """读取当前生效的代际 ID。

        返回：
            指针键 tcpr:current 指向的代际 ID；尚未初始化（键不存在）时
            返回 None。
        """
        raw = self.storage.get(self.CURRENT_KEY)
        if raw is None:
            return None
        # 后端可能返回 str 或 bytes，统一归一化为 str 供上层使用
        return raw.decode("utf-8") if isinstance(raw, bytes) else raw

    def _put_json(self, key: str, value: Any) -> None:
        """将对象打包（JSON → gzip → base64 → bytes）后写入存储。"""
        self.storage.put(key, _pack(value))

    def _get_json(self, key: str) -> Any:
        """从存储读取并解包对象。

        参数：
            key：存储键。
        返回：
            解包后的对象。
        异常：
            StorageNotConfigured：键不存在（读取到 None）时抛出。
        """
        raw = self.storage.get(key)
        if raw is None:
            raise StorageNotConfigured("missing generation key: " + key)
        return _unpack(raw)

    def write_generation(
        self,
        products: list[dict[str, Any]],
        index_payload: dict[str, Any],
        schema_payload: dict[str, Any],
        warnings: list[dict[str, Any]],
    ) -> str:
        """写入一代新的商品快照（先 staging 后原子切换指针）。

        流程：生成唯一代际 ID → 写入 manifest（状态 staged）→ 落盘
        schema、index、warnings → 按 CHUNK_SIZE 分块落盘 products →
        将 manifest 复写为 ready → 最后切换 tcpr:current 指针。指针是
        最后一步写入，此前任何异常都会让旧代际保持生效。

        参数：
            products：商品行列表（每行是列名到值的字典）。
            index_payload：检索索引载荷。
            schema_payload：schema 载荷（须包含 "schema_version" 键）。
            warnings：导入过程中产生的告警列表。
        返回：
            新写入的代际 ID（形如 gen-YYYYmmddHHMMSS-<10 位随机 hex>）。
        """
        # 代际 ID = 时间戳（精确到秒）+ uuid 前 10 位 hex，既可按字典序排序又全局唯一
        generation = "gen-" + time.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:10]
        manifest = {
            "generation_id": generation,
            "status": "staged",  # 初始状态为 staged（写入中），全部落盘后才改为 ready
            "product_count": len(products),
            "chunk_size": self.CHUNK_SIZE,
            # 向上取整计算分块数量：如 25,803 条商品按 500/块 → 52 块
            "chunk_count": (len(products) + self.CHUNK_SIZE - 1) // self.CHUNK_SIZE,
            "warning_count": len(warnings),
            "warning_sample": warnings[:100],  # manifest 只保留前 100 条告警样本，避免单键过大
            "schema_version": schema_payload["schema_version"],
            "created_at_epoch": time.time(),
        }
        # 同一代际的所有数据键共享前缀，便于按代际整体定位与（未来）清理
        prefix = "tcpr:" + generation + ":"
        self._put_json(prefix + "manifest", manifest)
        self._put_json(prefix + "schema", schema_payload)
        self._put_json(prefix + "index", index_payload)
        self._put_json(prefix + "warnings", warnings)
        # 商品按 CHUNK_SIZE 分块存储（键 tcpr:<gen>:products:<块号>），
        # 避免单键值过大超出 KV 存储限制
        for start in range(0, len(products), self.CHUNK_SIZE):
            chunk_no = start // self.CHUNK_SIZE
            self._put_json(prefix + "products:" + str(chunk_no), products[start:start + self.CHUNK_SIZE])
        manifest["status"] = "ready"
        self._put_json(prefix + "manifest", manifest)
        # The pointer is the final write. Any prior exception leaves the old current intact.
        # 指针是最后一步写入：此前任何异常都不会破坏旧的 current 指向。
        self.storage.put(self.CURRENT_KEY, generation.encode("utf-8"))
        return generation

    def load_generation(self, generation: str | None = None) -> dict[str, Any]:
        """读取并组装指定代际的完整快照。

        参数：
            generation：代际 ID；缺省（None）时自动取当前生效代际。
        返回：
            包含 generation_id、manifest、schema、index、warnings、
            products 的字典。
        异常：
            StorageNotConfigured：没有生效代际、代际数据缺失或 manifest
                状态不是 ready 时抛出。
        """
        generation = generation or self.current_generation()
        if not generation:
            raise StorageNotConfigured("no active TCPR generation")
        prefix = "tcpr:" + generation + ":"
        manifest = self._get_json(prefix + "manifest")
        # 仅接受 ready 状态的代际；staged 表示写入未完成，视为不可用
        if manifest.get("status") != "ready":
            raise StorageNotConfigured("generation is not ready: " + generation)
        products: list[dict[str, Any]] = []
        # 按块号顺序重组完整商品列表，保持与写入时一致的顺序
        for chunk_no in range(int(manifest["chunk_count"])):
            products.extend(self._get_json(prefix + "products:" + str(chunk_no)))
        return {
            "generation_id": generation,
            "manifest": manifest,
            "schema": self._get_json(prefix + "schema"),
            "index": self._get_json(prefix + "index"),
            "warnings": self._get_json(prefix + "warnings"),
            "products": products,
        }
