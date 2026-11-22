"""确定性布隆过滤器实现（本地模型，Python 3.8 兼容）。"""

import hashlib
from typing import Iterable

from .models import BloomFilter


def _hashes(key: str, num_hashes: int, size: int):
    digest1 = hashlib.sha1(key.encode("utf-8")).digest()
    digest2 = hashlib.md5(key.encode("utf-8")).digest()
    h1 = int.from_bytes(digest1[:8], "big")
    h2 = int.from_bytes(digest2[:8], "big") or 1
    for i in range(num_hashes):
        yield (h1 + i * h2) % size


def bitmap_checksum(bitmap: bytes) -> str:
    return hashlib.sha1(bitmap).hexdigest()


def build_filter(keys: Iterable[str], size: int, num_hashes: int,
                 version: int) -> BloomFilter:
    keys = tuple(keys)
    bitmap = bytearray((size + 7) // 8)
    for key in keys:
        for pos in _hashes(key, num_hashes, size):
            bitmap[pos // 8] |= 1 << (pos % 8)
    data = bytes(bitmap)
    return BloomFilter(
        size=size,
        num_hashes=num_hashes,
        bitmap=data,
        version=version,
        checksum=bitmap_checksum(data),
        source_keys=keys,
    )


def might_contain(bloom: BloomFilter, key: str) -> bool:
    for pos in _hashes(key, bloom.num_hashes, bloom.size):
        byte_index = pos // 8
        if byte_index >= len(bloom.bitmap):
            return False
        if not (bloom.bitmap[byte_index] >> (pos % 8)) & 1:
            return False
    return True
