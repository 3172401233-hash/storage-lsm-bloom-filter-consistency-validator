import unittest

from bloomcheck.bloom import build_filter
from bloomcheck.models import Entry, QuerySample, SSTable
from bloomcheck.validator import (
    REASON_BITMAP_CORRUPT,
    REASON_FALSE_NEGATIVE,
    REASON_MISSING_FILTER,
    REASON_VERSION_MISMATCH,
    validate,
)

SIZE = 4096
HASHES = 4


def make_table(sstable_id, level, version, entries, with_filter=True,
               filter_version=None, filter_keys=None):
    table = SSTable(
        sstable_id=sstable_id,
        level=level,
        version=version,
        entries=list(entries),
        summary={entry.key: i for i, entry in enumerate(entries)},
    )
    if with_filter:
        keys = filter_keys if filter_keys is not None else table.all_keys()
        table.bloom = build_filter(keys, SIZE, HASHES,
                                   filter_version if filter_version is not None
                                   else version)
    return table


class HealthyFilterTest(unittest.TestCase):
    def test_fresh_filter_is_safe(self):
        table = make_table("t1", 0, 3, [
            Entry("apple", "1"), Entry("banana", "2"), Entry("cherry", "3"),
        ])
        report = validate([table], [QuerySample("apple", True),
                                    QuerySample("durian", False)])
        self.assertEqual(report.invalid_filters, [])
        self.assertEqual(report.rebuild_order, [])
        self.assertEqual(report.affected_key_ranges, [])
        self.assertTrue(all(not v.false_negative
                            for v in report.query_verdicts))

    def test_stale_positive_is_safe_and_falls_back_to_disk(self):
        # 已删除键 "fig" 仍留在过滤器中：false positive，可回退读盘，仍安全。
        table = make_table("t1", 0, 2, [Entry("apple", "1")],
                           filter_keys=["apple", "fig"])
        report = validate([table])
        self.assertEqual(report.invalid_filters, [])
        issue = report.issues[0]
        self.assertEqual(issue.stale_positive_keys, ["fig"])
        self.assertTrue(issue.safe_for_negative_queries)


class BitmapCorruptionTest(unittest.TestCase):
    def test_corrupted_bitmap_rejected(self):
        table = make_table("t1", 0, 1, [Entry("apple", "1"),
                                        Entry("banana", "2")])
        damaged = bytearray(table.bloom.bitmap)
        damaged[0] ^= 0xFF
        table.bloom.bitmap = bytes(damaged)
        report = validate([table])
        self.assertEqual(len(report.invalid_filters), 1)
        issue = report.invalid_filters[0]
        self.assertIn(REASON_BITMAP_CORRUPT, issue.reasons)
        self.assertFalse(issue.safe_for_negative_queries)
        self.assertEqual(report.rebuild_order, ["t1"])

    def test_truncated_bitmap_causes_false_negative(self):
        table = make_table("t1", 0, 1, [Entry("apple", "1")])
        table.bloom.bitmap = table.bloom.bitmap[:1]
        table.bloom.checksum = ""  # 跳过校验和，单独验证位图内容
        report = validate([table])
        self.assertIn(REASON_FALSE_NEGATIVE, report.invalid_filters[0].reasons)


class VersionMismatchTest(unittest.TestCase):
    def test_stale_version_filter_rejected(self):
        # SSTable 已写入新键（version=2），过滤器仍来自 version=1。
        table = make_table("t1", 0, 2,
                           [Entry("apple", "1"), Entry("banana", "2")],
                           filter_version=1, filter_keys=["apple"])
        report = validate([table], [QuerySample("banana", True)])
        issue = report.invalid_filters[0]
        self.assertIn(REASON_VERSION_MISMATCH, issue.reasons)
        self.assertIn(REASON_FALSE_NEGATIVE, issue.reasons)
        self.assertEqual(issue.false_negative_keys, ["banana"])
        self.assertTrue(report.query_verdicts[0].false_negative)

    def test_missing_filter_rejected(self):
        table = make_table("t1", 0, 1, [Entry("apple", "1")],
                           with_filter=False)
        report = validate([table])
        self.assertIn(REASON_MISSING_FILTER,
                      report.invalid_filters[0].reasons)


class TombstoneTest(unittest.TestCase):
    def test_tombstone_key_must_stay_in_filter(self):
        # 墓碑键被过滤器漏掉时，查询会跳过删除标记读到旧值：false negative。
        table = make_table("t1", 0, 2,
                           [Entry("apple", None, tombstone=True)],
                           filter_keys=[])
        report = validate([table], [QuerySample("apple", True)])
        issue = report.invalid_filters[0]
        self.assertEqual(issue.false_negative_keys, ["apple"])
        self.assertFalse(issue.safe_for_negative_queries)

    def test_tombstone_present_in_filter_is_safe(self):
        table = make_table("t1", 0, 2,
                           [Entry("apple", None, tombstone=True)])
        report = validate([table], [QuerySample("apple", True)])
        self.assertEqual(report.invalid_filters, [])


class LevelMergeTest(unittest.TestCase):
    def test_compaction_invalidates_old_filter(self):
        # 合并后新 SSTable（L1, v5）沿用了旧过滤器（v3）：必须拒绝。
        merged = make_table("L1-merged", 1, 5,
                            [Entry("apple", "9"), Entry("banana", "8"),
                             Entry("cherry", "7")],
                            filter_version=3,
                            filter_keys=["apple", "banana"])
        report = validate([merged], [QuerySample("cherry", True)])
        issue = report.invalid_filters[0]
        self.assertIn(REASON_VERSION_MISMATCH, issue.reasons)
        self.assertEqual(issue.false_negative_keys, ["cherry"])
        self.assertEqual(report.affected_key_ranges, [("apple", "cherry")])

    def test_query_resolves_newest_level(self):
        # L0 有新值，L1 有旧值；查询应命中 L0 的过滤器。
        upper = make_table("L0", 0, 4, [Entry("apple", "new")])
        lower = make_table("L1", 1, 1, [Entry("apple", "old")],
                           filter_keys=[])  # L1 过滤器缺失该键
        report = validate([upper, lower], [QuerySample("apple", True)])
        verdict = report.query_verdicts[0]
        self.assertTrue(verdict.filter_may_exist)
        self.assertFalse(verdict.false_negative)

    def test_rebuild_order_prioritizes_false_negatives_and_level(self):
        stale_positive_only = make_table(
            "t-stale-pos", 0, 2, [Entry("mango", "1")],
            filter_version=1, filter_keys=["mango", "papaya"])
        false_neg_l1 = make_table(
            "t-fn-l1", 1, 3, [Entry("apple", "1"), Entry("apricot", "2")],
            filter_version=2, filter_keys=["apple"])
        false_neg_l0 = make_table(
            "t-fn-l0", 0, 3, [Entry("banana", "1"), Entry("cherry", "2")],
            filter_version=2, filter_keys=["banana"])
        report = validate([stale_positive_only, false_neg_l1, false_neg_l0])
        self.assertEqual(report.rebuild_order,
                         ["t-fn-l0", "t-fn-l1", "t-stale-pos"])
        self.assertEqual(report.affected_key_ranges,
                         [("apple", "apricot"), ("banana", "cherry"),
                          ("mango", "mango")])


if __name__ == "__main__":
    unittest.main()
