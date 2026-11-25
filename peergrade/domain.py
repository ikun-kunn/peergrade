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


AI_SCORE_DIMENSIONS = ("specificity", "evidence_grounding", "actionability")
AI_ISSUE_CODES = ("VAGUE_FEEDBACK", "INSUFFICIENT_EVIDENCE", "LACK_OF_ACTIONABILITY", "OTHER")
_LEGACY_ISSUE_CODES = {
    "VAGUE": "VAGUE_FEEDBACK", "LACK_OF_SPECIFICITY": "VAGUE_FEEDBACK",
    "空泛": "VAGUE_FEEDBACK", "评价空泛": "VAGUE_FEEDBACK", "评语空泛": "VAGUE_FEEDBACK",
    "具体性不足": "VAGUE_FEEDBACK", "缺乏具体性": "VAGUE_FEEDBACK", "不够具体": "VAGUE_FEEDBACK",
    "依据不足": "INSUFFICIENT_EVIDENCE", "缺少依据": "INSUFFICIENT_EVIDENCE",
    "证据不充分": "INSUFFICIENT_EVIDENCE", "证据不足": "INSUFFICIENT_EVIDENCE",
    "缺少证据": "INSUFFICIENT_EVIDENCE", "缺乏依据": "INSUFFICIENT_EVIDENCE",
    "IRRELEVANT_EVIDENCE": "INSUFFICIENT_EVIDENCE", "证据不相关": "INSUFFICIENT_EVIDENCE",
    "NOT_ACTIONABLE": "LACK_OF_ACTIONABILITY", "可操作性不足": "LACK_OF_ACTIONABILITY",
    "缺乏可操作性": "LACK_OF_ACTIONABILITY", "建议不具体": "LACK_OF_ACTIONABILITY",
    "建议不明确": "LACK_OF_ACTIONABILITY", "缺少改进方向": "LACK_OF_ACTIONABILITY",
}


def _quality_scores(result):
    if not isinstance(result, dict):
        raise ValueError("AI 返回格式无效")
    scores = [result.get(name) for name in AI_SCORE_DIMENSIONS]
    return {**dict(zip(AI_SCORE_DIMENSIONS, scores)), "quality_level": quality_level(scores)}


def _valid_issue(issue):
    return (isinstance(issue, dict) and isinstance(issue.get("type"), str)
            and isinstance(issue.get("message"), str) and len(issue["message"]) <= 500)


def _valid_suggestion(suggestion):
    return isinstance(suggestion, str) and len(suggestion) <= 500


def _canonical_issue(issue):
    label = issue["type"].strip().upper()
    code = label if label in AI_ISSUE_CODES else _LEGACY_ISSUE_CODES.get(label, "OTHER")
    return {"type": code, "message": issue["message"]}


def diagnostic_candidates(result):
    candidates = result.get('diagnostics') if isinstance(result, dict) else None
    if not isinstance(candidates, list):
        return []
    clean = []
    for item in candidates[:3]:
        if not isinstance(item, dict) or item.get('field') not in ('strengths', 'weaknesses', 'suggestions'):
            continue
        strings = ('type', 'review_quote', 'source_quote', 'reason', 'guidance')
        if any(not isinstance(item.get(key), str) for key in strings):
            continue
        if (len(item['review_quote']) > 200 or len(item['source_quote']) > 200
                or not item['reason'].strip() or not item['guidance'].strip()
                or len(item['reason']) > 300 or len(item['guidance']) > 300):
            continue
        index = item.get('evidence_index')
        if index is not None and type(index) is not int:
            continue
        label = _canonical_issue({'type': item['type'], 'message': ''})['type']
        clean.append({'type': label, 'field': item['field'], 'review_quote': item['review_quote'],
                      'source_quote': item['source_quote'], 'evidence_index': index,
                      'reason': item['reason'], 'guidance': item['guidance']})
    return clean


def unique_span(text, quote):
    if not isinstance(text, str) or not isinstance(quote, str) or not quote.strip():
        return None
    start = text.find(quote)
    if start < 0 or text.find(quote, start + 1) >= 0:
        return None
    return {'start': start, 'end': start + len(quote), 'quote': quote}


def ground_diagnostics(result, payload, original_text=None):
    """Recompute locations from exact quotes; absent or ambiguous references stay unlocated."""
    if 'diagnostics' not in result:
        return result
    payload = payload if isinstance(payload, dict) else {}
    review = payload.get('review') if isinstance(payload.get('review'), dict) else {}
    anchors = payload.get('evidence_anchors')
    quotes = payload.get('evidence_quotes')
    anchors_verified = False
    if isinstance(original_text, str) and isinstance(anchors, list) and isinstance(quotes, list):
        try:
            validate_anchors(original_text, anchors)
            anchors_verified = quotes == [anchor['quote'] for anchor in anchors]
        except (ValueError, TypeError, AttributeError, KeyError):
            pass
    grounded = []
    for item in diagnostic_candidates(result):
        review_location = unique_span(review.get(item['field']), item['review_quote'])
        source_location = None
        index, quote = item['evidence_index'], item['source_quote']
        if anchors_verified and type(index) is int and 0 <= index < len(anchors):
            anchor = anchors[index]
            match = unique_span(anchor['quote'], quote)
            if match:
                source_location = {'start': anchor['start'] + match['start'], 'end': anchor['start'] + match['end'],
                                   'quote': quote, 'kind': 'SELECTED_EVIDENCE', 'content_hash': anchor['content_hash']}
        elif index is None and isinstance(original_text, str) and unique_span(payload.get('submission_excerpt'), quote):
            match = unique_span(original_text, quote)
            if match:
                source_location = {**match, 'kind': 'EXCERPT', 'content_hash': content_hash(original_text)}
        grounded.append({**item, 'review_location': review_location, 'source_location': source_location,
                         'location_status': 'LOCATED' if review_location and source_location else 'UNCERTAIN'})
    return {**result, 'diagnostics': grounded}


def validate_ai_result(result: dict) -> dict:
    """Validate model output and normalize every issue to a stable code."""
    summary = _quality_scores(result)
    issues, suggestions = result.get("issues"), result.get("suggestions")
    if not isinstance(issues, list) or len(issues) > 10 or not isinstance(suggestions, list) or len(suggestions) > 10:
        raise ValueError("AI 返回的问题或建议格式无效")
    if not all(map(_valid_issue, issues)):
        raise ValueError("AI 返回的问题格式无效")
    if not all(map(_valid_suggestion, suggestions)):
        raise ValueError("AI 返回的建议格式无效")
    cleaned = {**summary, "issues": [_canonical_issue(i) for i in issues], "suggestions": suggestions}
    if 'diagnostics' in result:
        cleaned['diagnostics'] = diagnostic_candidates(result)
    return cleaned


def historical_ai_result(result, payload=None, original_text=None) -> dict | None:
    """Read legacy results without trusting their level or optional display fields."""
    try:
        summary = _quality_scores(result)
    except ValueError:
        return None
    issues, suggestions = result.get("issues"), result.get("suggestions")
    cleaned = {**summary,
            "issues": [_canonical_issue(i) for i in issues if _valid_issue(i)][:10] if isinstance(issues, list) else [],
            "suggestions": [s for s in suggestions if _valid_suggestion(s)][:10] if isinstance(suggestions, list) else []}
    if 'diagnostics' in result:
        cleaned['diagnostics'] = diagnostic_candidates(result)
        return ground_diagnostics(cleaned, payload, original_text)
    return cleaned


def ai_dimension_means(results):
    samples = list(results)
    return {key: (sum(result[key] for result in samples) / len(samples) if samples else None)
            for key in AI_SCORE_DIMENSIONS}


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
