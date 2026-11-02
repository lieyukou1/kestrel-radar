from __future__ import annotations

import math
import sqlite3
from datetime import datetime

from rapidfuzz import fuzz

from ..util import parse_iso


def authority_score(authority: str, weights: dict) -> float:
    return float((weights.get("authority") or {}).get(authority, 0.3))


def recency_score(document: sqlite3.Row, weights: dict, now: datetime) -> float:
    # GitHub 仓库的 created_at 可能是几个月前，用它算时效会让所有成熟仓库归零；
    # 仓库看的是「我们发现它多久了」，其余类型仍看发布时间。
    if document["kind"] == "repo":
        published = parse_iso(document["first_seen_at"]) or parse_iso(
            document["published_at"]
        )
    else:
        published = parse_iso(document["published_at"]) or parse_iso(
            document["first_seen_at"]
        )
    if published is None:
        return 0.5
    half_life = float(
        (weights.get("half_life_hours") or {}).get(document["kind"], 48)
    )
    age_hours = max(0.0, (now - published).total_seconds() / 3600)
    return math.exp(-age_hours / half_life)


def novelty_score(title: str, recent_titles: list[str]) -> float:
    if not recent_titles:
        return 1.0
    max_similarity = max(
        fuzz.token_set_ratio(title, other) / 100.0 for other in recent_titles
    )
    return max(0.0, 1.0 - max_similarity)


def preference_score(
    document: sqlite3.Row, preferences: dict[str, float]
) -> float:
    values = [
        preferences.get(f"dimension:{document['primary_tag']}", 1.0),
        preferences.get(f"type:{document['kind']}", 1.0),
        preferences.get(f"authority:{document['authority']}", 1.0),
    ]
    score = 1.0
    for value in values:
        score *= max(0.0, min(1.0, value))
    return max(0.0, min(1.0, score))


def theme_matches(document: sqlite3.Row, theme: dict | None) -> bool:
    """用户设定的临时主题：命中维度或任一关键词即可。"""
    if not theme:
        return False
    dimension = theme.get("dimension")
    if dimension and document["primary_tag"] == dimension:
        return True
    text = f"{document['title'] or ''} {document['abstract'] or ''}".casefold()
    return any(
        str(word).casefold() in text
        for word in theme.get("keywords") or []
        if str(word).strip()
    )


def score_document(
    document: sqlite3.Row,
    *,
    heat: float,
    recent_titles: list[str],
    weights: dict,
    preferences: dict[str, float],
    now: datetime,
    theme: dict | None = None,
) -> dict:
    factors = weights.get("scoring") or {}
    score = (
        float(factors.get("authority", 0.3)) * authority_score(document["authority"], weights)
        + float(factors.get("heat", 0.3)) * heat
        + float(factors.get("recency", 0.15)) * recency_score(document, weights, now)
        + float(factors.get("novelty", 0.15))
        * novelty_score(document["title"], recent_titles)
        + float(factors.get("preference", 0.10))
        * preference_score(document, preferences)
    )
    # 用户点了名要看的东西，给一点加成；不做大幅改写，避免一条偏好压过全部信号。
    if theme_matches(document, theme):
        score = min(1.0, score * 1.15)
    return {
        "doc": document,
        "score": round(score, 6),
        "tag": document["primary_tag"] or "未分类",
        "title": document["title"],
    }


def _kind_of(item: dict) -> str | None:
    try:
        return item["doc"]["kind"]
    except (KeyError, IndexError, TypeError):
        return None


def _select(
    pool: list[dict],
    *,
    size: int,
    max_per_tag: int,
    mmr_lambda: float,
    base: list[dict],
    kind_caps: dict[str, int] | None = None,
    ignore_tag_cap: bool = False,
    tag_weights: dict[str, float] | None = None,
) -> list[dict]:
    """从 pool 里挑最多 size 条，标签配额与 MMR 都把 base 里已选中的算进去。

    `tag_weights` 是维度轮转给出的乘子：只影响排序与 MMR，不改写条目本身的分数。
    """
    selected: list[dict] = []
    if size <= 0:
        return selected
    # base 里已经选过的不能再进候选池，否则补位轮会把它重复选一次。
    pool = [item for item in pool if all(item is not picked for picked in base)]
    caps = kind_caps or {}
    weights = tag_weights or {}

    def weighted(item: dict) -> float:
        return float(item["score"]) * float(weights.get(item["tag"], 1.0))

    def kind_count(kind: str) -> int:
        return sum(1 for item in base + selected if _kind_of(item) == kind)

    def tag_count(tag: str) -> int:
        return sum(1 for item in base + selected if item["tag"] == tag)

    def can_add(item: dict) -> bool:
        # 保底配额（kind_min）不受维度上限约束，否则会被同维度的论文/HN 挤掉。
        if not ignore_tag_cap and tag_count(item["tag"]) >= max_per_tag:
            return False
        kind = _kind_of(item)
        return kind not in caps or kind_count(kind) < caps[kind]

    # 第一轮：每个维度保底一篇
    by_tag: dict[str, list[dict]] = {}
    for item in sorted(pool, key=weighted, reverse=True):
        by_tag.setdefault(item["tag"], []).append(item)

    for items in sorted(by_tag.values(), key=lambda group: weighted(group[0]), reverse=True):
        if len(selected) >= size:
            break
        if not can_add(items[0]):
            continue
        selected.append(items[0])

    # 第二轮：MMR 补足剩余名额
    remaining = [item for item in pool if all(item is not picked for picked in selected)]
    while len(selected) < size and remaining:
        best: dict | None = None
        best_mmr = -1.0
        for item in remaining:
            if not can_add(item):
                continue
            similarity = max(
                (
                    fuzz.token_set_ratio(item["title"], other["title"]) / 100.0
                    for other in base + selected
                ),
                default=0.0,
            )
            mmr = mmr_lambda * weighted(item) - (1 - mmr_lambda) * similarity
            if mmr > best_mmr:
                best_mmr = mmr
                best = item
        if best is None:
            break
        selected.append(best)
        remaining = [item for item in remaining if item is not best]

    return selected

