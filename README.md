# storage-lsm-bloom-filter-consistency-validator
校验 LSM 布隆过滤器、SSTable 和删除标记一致性

> 说明：本项目中所有 SSTable、布隆过滤器、摘要索引与查询样本均为**本地模型**
> （纯内存数据结构，见 `bloomcheck/models.py`），不读写任何真实存储引擎文件。

## 功能

输入 SSTable 的键范围、摘要、布隆位图、版本、墓碑、层级和查询样本，
判断每个布隆过滤器能否安全用于负查询：

- **false negative 必须拒绝**：过滤器过期（版本不一致）、位图损坏、
  存活键或墓碑键未命中过滤器时，查询可能直接返回"不存在"或漏掉真实版本，
  该过滤器判定为不可用，必须重建。
- **false positive 可以容忍**：已删除的键仍留在过滤器中只会多一次读盘回退，
  过滤器仍判定为安全（记录为 `stale_positive_keys`）。
- 墓碑键同样必须命中过滤器，否则查询会跳过删除标记，从更低层级读到已删除的旧值。

## 输出

`validate()` 返回 `ValidationReport`：

- `invalid_filters`：失效过滤器及原因（`bitmap_corrupted` / `version_mismatch` /
  `false_negative` / `missing_filter`）与漏判键列表；
- `affected_key_ranges`：受影响键范围（相邻/重叠范围已合并）；
- `rebuild_order`：重建顺序——存在 false negative 的优先，其次按层级
  （新数据优先）与键范围排序；
- `query_verdicts`：每个查询样本的过滤器结论与是否发生 false negative。

## 用法

```python
from bloomcheck import Entry, QuerySample, SSTable, validate
from bloomcheck.bloom import build_filter

table = SSTable(sstable_id="t1", level=0, version=2,
                entries=[Entry("apple", "1"), Entry("banana", "2")],
                summary={"apple": 0, "banana": 1})
table.bloom = build_filter(table.all_keys(), size=4096, num_hashes=4, version=2)

report = validate([table], [QuerySample("banana", True)])
print(report.invalid_filters, report.affected_key_ranges, report.rebuild_order)
```

## 测试

兼容 Python 3.8.2+。测试覆盖位图损坏、版本不一致、墓碑与层级合并：

```
python -m unittest
```
