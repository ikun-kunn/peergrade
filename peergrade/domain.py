"""Pure, independently testable PeerGrade business rules."""
from __future__ import annotations

import hashlib
import random
from collections import Counter


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


def validate_allocation(owners: list[str], pairs: list[tuple[str, str]]) -> None:
    population = set(owners)
    if len(population) != len(owners) or len(pairs) != 2 * len(owners):
        raise ValueError("分配数量不正确")
    if len(set(pairs)) != len(pairs) or any(a == b or a not in population or b not in population for a, b in pairs):
        raise ValueError("分配包含自评、重复或无效参与者")
    if Counter(a for a, _ in pairs) != Counter({a: 2 for a in owners}) or Counter(b for _, b in pairs) != Counter({a: 2 for a in owners}):
        raise ValueError("分配不均衡")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def score_review(scores: list[int]) -> int:
    if len(scores) != 3 or any(type(x) is not int or not 0 <= x <= 5 for x in scores):
        raise ValueError("三个评分必须是 0 至 5 的整数")
    return sum(scores)


def quality_level(scores: list[int]) -> str:
    if len(scores) != 3 or any(type(x) is not int or not 0 <= x <= 2 for x in scores):
        raise ValueError("质量评分必须是 0 至 2 的整数")
    total = sum(scores)
    return "LOW" if total <= 2 else "MEDIUM" if total <= 4 else "HIGH"


def validate_ai_result(result: dict) -> dict:
    """Validate untrusted model output; never accept the model's own level."""
    if not isinstance(result, dict):
        raise ValueError("AI 返回格式无效")
    names = ("specificity", "evidence_grounding", "actionability")
    scores = [result.get(name) for name in names]
    level = quality_level(scores)
    issues, suggestions = result.get("issues"), result.get("suggestions")
    if not isinstance(issues, list) or len(issues) > 10 or not isinstance(suggestions, list) or len(suggestions) > 10:
        raise ValueError("AI 返回的问题或建议格式无效")
    if any(not isinstance(i, dict) or not isinstance(i.get("type"), str) or not isinstance(i.get("message"), str) or len(i["message"]) > 500 for i in issues):
        raise ValueError("AI 返回的问题格式无效")
    if any(not isinstance(s, str) or len(s) > 500 for s in suggestions):
        raise ValueError("AI 返回的建议格式无效")
    return {**dict(zip(names, scores)), "quality_level": level,
            "issues": [{"type": i["type"][:100], "message": i["message"]} for i in issues],
            "suggestions": suggestions}


def feedback_summary(reviews: list[list[int]]) -> dict:
    """Average available reviews; flag incomplete and high disagreement."""
    if len(reviews) > 2:
        raise ValueError("一份作业最多有两条互评")
    totals = [score_review(scores) for scores in reviews]
    return {
        "count": len(reviews),
        "incomplete": len(reviews) < 2,
        "average_total": sum(totals) / len(totals) if totals else None,
        "dimension_averages": [sum(r[i] for r in reviews) / len(reviews) for i in range(3)] if reviews else [],
        "high_disagreement": len(totals) == 2 and abs(totals[0] - totals[1]) >= 5,
    }
