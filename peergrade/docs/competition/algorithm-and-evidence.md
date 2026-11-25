# 匿名双评算法与原文证据锚定实现

## 一 匿名双评分配

入口：`app.py` 的 `start_review`。服务端锁定教师所属任务，验证提交已截止且互评未截止；筛选实际提交者（按 student_id 排序），至少三位。生成 128 位随机种子并与分配同一事务保存。`domain.py` 洗牌后以环形偏移 1、2 构成 (作者, 评审者) 关系。

```python
def allocate(submission_owners: list[str], seed: int | None = None) -> list[tuple[str, str]]:
    """Return (owner, reviewer) pairs with two balanced reviews per person."""
    if len(submission_owners) < 3 or len(set(submission_owners)) != len(submission_owners):
        raise ValueError("至少需要三名不同的有效提交者")
    ring = list(submission_owners)
    random.Random(seed).shuffle(ring)
    pairs = [(owner, ring[(i + offset) % len(ring)])
             for i, owner in enumerate(ring) for offset in (1, 2)]
    validate_allocation(submission_owners, pairs)
    return pairs
```

以洗牌后的序列 A、B、C、D、E 为示例（不是固定分配结果）：

| 作者作业 | 评审者一 | 评审者二 |
| --- | --- | --- |
| A | B | C |
| B | C | D |
| C | D | E |
| D | E | A |
| E | A | B |

对于 n≥3 的互不重复作者，偏移 1 与 2 在模 n 意义下非零且互异，因此不自评、不重复；两个偏移分别是一个置换，所以每位参与者恰好获得两名评审并评价两份作业。环形分配生成 2n 条关系，核心洗牌、构造和计数验证耗时与空间均为 O(n)；数据库按 ID 排序和插入的实际耗时另计。

启动互评时状态更新与所有分配同一事务提交，SQLite 写预留、PostgreSQL 任务行锁以及 `(submission_id, reviewer_id)` 唯一约束避免重复分配；重复启动已处于 REVIEWING 的任务不会再次插入。固定输入排序、保存种子与实际分配便于复核。

## 二 证据锚定

浏览器将 DOM 中 UTF-16 选择换算为 Unicode 码点（`Array.from`），记录 `start` 含起点、`end` 不含终点、原样引文以及当前作业文本的 UTF-8 SHA-256。举例：文本为 Python 字符串 `\U0001F600证据支持观点。`（开头为一个表情码点），选择“证据”：

```json
{
  "start": 1,
  "end": 3,
  "quote": "证据",
  "content_hash": "bdc44e4cdc19650af73d659500e11a8f75f657b88f39bb701b656425e1ca33c0"
}
```

实际服务端规则：

```python
def validate_anchors(text: str, anchors: list[dict]) -> None:
    if len(anchors) > 3:
        raise ValueError("最多关联三处证据")
    digest = content_hash(text)
    for anchor in anchors:
        start, end = anchor.get("start"), anchor.get("end")
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
            raise ValueError("证据位置无效")
        if anchor.get("content_hash") != digest or anchor.get("quote") != text[start:end]:
            raise ValueError("证据与当前作业不一致")
```

草稿、AI 检测和正式提交均验证对应作业、摘要、引文与范围；每条评价最多关联三处证据。同一句引文出现多次时，用户选区的区间仍能区分不同位置。AI 给出的引文则不能由模型自报偏移：必须依据真实输入及证据重算；不唯一或不匹配时位置为空，状态 UNCERTAIN。原作业互评开始后冻结，发布后修订新建独立记录并保存当时采纳快照，旧证据不失效。

## 三 可追溯验收

对应 `test_allocation_is_balanced_for_required_sizes`、`test_concurrent_start_creates_no_duplicate_tasks`、`test_unicode_code_point_offsets_work_end_to_end`、`test_tampered_anchor_offsets_quote_and_hash_rejected`、`test_feedback_adoption_and_immutable_revision_snapshots` 及完整浏览器流程。
