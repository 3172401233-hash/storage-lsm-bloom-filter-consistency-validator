"""SSTable、布隆过滤器与查询样本的本地数据模型。"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class Entry:
    """SSTable 中的一条记录。tombstone=True 表示删除标记。"""

    key: str
    value: Optional[str] = None
    tombstone: bool = False


@dataclass
class BloomFilter:
    """布隆过滤器的本地模型。

    bitmap: 位图字节；checksum: 构建时位图的 SHA1，用于检测损坏；
    version: 构建过滤器所依据的 SSTable 数据版本；
    source_keys: 构建时使用的键集合（本地模型中保留以便审计）。
    """

    size: int
    num_hashes: int
    bitmap: bytes
    version: int
    checksum: str = ""
    source_keys: Tuple[str, ...] = ()


@dataclass
class SSTable:
    """SSTable 的本地模型。

    level: 所在层级（0 为最新）；version: 数据版本号；
    summary: 稀疏索引（键 -> 块偏移的本地模拟）；
    entries: 按键排序的记录；key_range 由 entries 推导。
    """

    sstable_id: str
    level: int
    version: int
    entries: List[Entry] = field(default_factory=list)
    summary: Dict[str, int] = field(default_factory=dict)
    bloom: Optional[BloomFilter] = None

    @property
    def key_range(self) -> Tuple[str, str]:
        keys = [entry.key for entry in self.entries]
        if not keys:
            return ("", "")
        return (min(keys), max(keys))

    def live_keys(self) -> List[str]:
        return [entry.key for entry in self.entries if not entry.tombstone]

    def tombstone_keys(self) -> List[str]:
        return [entry.key for entry in self.entries if entry.tombstone]

    def all_keys(self) -> List[str]:
        return [entry.key for entry in self.entries]


@dataclass
class QuerySample:
    """一次负查询样本：查询键与期望结论（True 表示应判为可能存在）。"""

    key: str
    expected_may_exist: bool
