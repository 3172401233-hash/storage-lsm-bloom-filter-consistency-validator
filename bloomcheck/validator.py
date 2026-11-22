"""校验布隆过滤器能否安全用于负查询。

判定原则：
- 过滤器过期/损坏导致 false negative（真实存在的键被判为不存在）时必须拒绝使用；
- false positive（已删除的键仍留在过滤器中）只会多一次读盘回退，过滤器仍可用。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .bloom import bitmap_checksum, might_contain
from .models import QuerySample, SSTable

REASON_MISSING_FILTER = "missing_filter"
REASON_BITMAP_CORRUPT = "bitmap_corrupted"
REASON_VERSION_MISMATCH = "version_mismatch"
REASON_FALSE_NEGATIVE = "false_negative"


@dataclass
class FilterIssue:
    sstable_id: str
    level: int
    key_range: Tuple[str, str]
    reasons: List[str] = field(default_factory=list)
    false_negative_keys: List[str] = field(default_factory=list)
    stale_positive_keys: List[str] = field(default_factory=list)
    safe_for_negative_queries: bool = True


@dataclass
class QueryVerdict:
    key: str
    expected_may_exist: bool
    filter_may_exist: bool
    false_negative: bool


@dataclass
class ValidationReport:
    issues: List[FilterIssue] = field(default_factory=list)
    query_verdicts: List[QueryVerdict] = field(default_factory=list)

    @property
    def invalid_filters(self) -> List[FilterIssue]:
        return [issue for issue in self.issues
                if not issue.safe_for_negative_queries]

    @property
    def affected_key_ranges(self) -> List[Tuple[str, str]]:
        ranges = sorted(issue.key_range for issue in self.invalid_filters
                        if issue.key_range != ("", ""))
        merged: List[List[str]] = []
        for start, end in ranges:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return [(start, end) for start, end in merged]

    @property
    def rebuild_order(self) -> List[str]:
        """重建顺序：存在 false negative 的优先，其次按层级（新数据优先）与键范围。"""
        invalid = self.invalid_filters
        invalid.sort(key=lambda issue: (
            0 if issue.false_negative_keys else 1,
            issue.level,
            issue.key_range,
        ))
        return [issue.sstable_id for issue in invalid]


def _check_sstable(table: SSTable) -> FilterIssue:
    issue = FilterIssue(
        sstable_id=table.sstable_id,
        level=table.level,
        key_range=table.key_range,
    )
    bloom = table.bloom
    if bloom is None:
        issue.reasons.append(REASON_MISSING_FILTER)
        issue.safe_for_negative_queries = False
        return issue

    if bloom.checksum and bitmap_checksum(bloom.bitmap) != bloom.checksum:
        issue.reasons.append(REASON_BITMAP_CORRUPT)

    if bloom.version != table.version:
        issue.reasons.append(REASON_VERSION_MISMATCH)

    # 存活键与墓碑键都必须可命中：漏掉墓碑会让查询跳过删除标记，
    # 从更低层级读到已删除的旧值，同样属于 false negative。
    for key in table.all_keys():
        if not might_contain(bloom, key):
            issue.false_negative_keys.append(key)
    if issue.false_negative_keys:
        issue.reasons.append(REASON_FALSE_NEGATIVE)

    current_keys = set(table.all_keys())
    issue.stale_positive_keys = sorted(
        key for key in bloom.source_keys if key not in current_keys)

    if (issue.false_negative_keys or REASON_BITMAP_CORRUPT in issue.reasons
            or REASON_VERSION_MISMATCH in issue.reasons):
        issue.safe_for_negative_queries = False
    return issue


def _resolve_entry(tables: List[SSTable], key: str):
    """按 LSM 语义解析键的最新记录：层级低者优先，同层版本高者优先。"""
    best_rank: Optional[Tuple[int, int]] = None
    best_table: Optional[SSTable] = None
    for table in tables:
        low, high = table.key_range
        if not (low <= key <= high):
            continue
        if not any(entry.key == key for entry in table.entries):
            continue
        rank = (table.level, -table.version)
        if best_rank is None or rank < best_rank:
            best_rank = rank
            best_table = table
    return best_table


def validate(tables: List[SSTable],
             queries: Optional[List[QuerySample]] = None) -> ValidationReport:
    report = ValidationReport()
    by_id: Dict[str, FilterIssue] = {}
    for table in tables:
        issue = _check_sstable(table)
        report.issues.append(issue)
        by_id[table.sstable_id] = issue

    for sample in queries or []:
        owner = _resolve_entry(tables, sample.key)
        if owner is None or owner.bloom is None:
            filter_may_exist = False
        else:
            filter_may_exist = might_contain(owner.bloom, sample.key)
        report.query_verdicts.append(QueryVerdict(
            key=sample.key,
            expected_may_exist=sample.expected_may_exist,
            filter_may_exist=filter_may_exist,
            false_negative=sample.expected_may_exist and not filter_may_exist,
        ))
    return report
