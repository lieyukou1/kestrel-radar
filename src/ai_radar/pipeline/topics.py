"""把候选文章聚成「话题」，再按话题分排序。

选择单位从单篇改成话题，是这次重塑的核心：
- 同一件事被论文、公众号、HN 同时提到，才算真热点；
- 只有一篇文章的话题不成立（宁可判定「今日无主线」）；
- 话题分不是简单相加，跨来源会加权（同一话题出现在 3 个来源更可信）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import entities
from .embedding import cosine

# 只共享一个实体时，语义也要接近才合并；否则「都提到同一个词」会被当成同一件事。
ENTITY_SIMILARITY = 0.55

# 「N 篇论文」「合集」「盘点」这类是集合名，不是一件事。
BUCKET_LABEL = re.compile(
    r"(?:\d+\s*篇|[一二三四五六七八九十两几多]+\s*篇|合集|汇总|盘点|一览)"
)


@dataclass(slots=True)
class Topic:
    key: str
    label: str
    items: list[dict] = field(default_factory=list)
    entities: set[str] = field(default_factory=set)
    score: float = 0.0

    @property
    def kinds(self) -> set[str]:
        return {str(item["doc"]["kind"]) for item in self.items}

    @property
    def sources(self) -> int:
        return len(self.kinds)


def _doc_id(entry: dict) -> int:
    try:
        return int(entry["doc"]["id"])
    except (KeyError, IndexError, TypeError, ValueError):
        return 0


def _best_similarity(vectors: dict[int, list[float]], doc_id: int, topic: Topic) -> float | None:
    """文档与话题内最相似成员的余弦；没有向量时返回 None（离线环境不做语义判断）。"""
    vector = vectors.get(doc_id)
    if not vector:
        return None
    best: float | None = None
    for member in topic.items:
        other = vectors.get(_doc_id(member))
        if not other:
            continue
        similarity = cosine(vector, other)
        best = similarity if best is None else max(best, similarity)
    return best


def is_bucket_label(label: str) -> bool:
    return bool(BUCKET_LABEL.search(str(label or "")))


def build_topics(
    entries: list[dict],
    vectors: dict[int, list[float]],
    *,
    min_members: int = 2,
    similarity: float = 0.82,
    max_docs: int = 12,
    entity_similarity: float = ENTITY_SIMILARITY,
) -> list[Topic]:
    """把打分后的候选聚成话题，返回按话题分倒序的列表。"""
    if not entries:
        return []

    raw = {_doc_id(entry): entities.extract(str(entry.get("title") or "")) for entry in entries}
    tagged = entities.filter_by_frequency(raw, min_docs=min_members, max_docs=max_docs)

    clusters: list[Topic] = []
    # 分数从高到低处理，高分条目先成为话题的「代表」
    for entry in sorted(entries, key=lambda item: item["score"], reverse=True):
        doc_id = _doc_id(entry)
        own = tagged.get(doc_id) or set()

        best: Topic | None = None
        best_overlap = 0
        for topic in clusters:
            overlap = len(own & topic.entities)
            if overlap <= best_overlap:
                continue
            # 共享两个以上实体算是强证据；只共享一个实体时，语义也要接近。
            if overlap >= 2:
                best, best_overlap = topic, overlap
                continue
            semantic = _best_similarity(vectors, doc_id, topic)
            if semantic is None or semantic >= entity_similarity:
                best, best_overlap = topic, overlap

        if best is None and vectors:
            # 没有共同实体时，退回向量相似度（同一事件的不同命名）
            vector = vectors.get(doc_id)
            if vector:
                best_sim = 0.0
                for topic in clusters:
                    for member in topic.items:
                        other = vectors.get(_doc_id(member))
                        if not other:
                            continue
                        sim = cosine(vector, other)
                        if sim >= similarity and sim > best_sim:
                            best, best_sim = topic, sim

        if best is None:
            topic = Topic(
                key=f"topic-{doc_id}",
                label=str(entry.get("title") or "")[:40],
                entities=set(own),
            )
            topic.items.append(entry)
            clusters.append(topic)
            continue

        best.items.append(entry)
        best.entities |= own

    result: list[Topic] = []
    for topic in clusters:
        if len(topic.items) < min_members:
            continue
        # 只靠向量粘起来的簇必须有跨类型证据，否则宁可不成题
        if not topic.entities and len(topic.kinds) < 2:
            continue
        total = sum(float(item["score"]) for item in topic.items)
        topic.score = total * (1.0 + 0.12 * (len(topic.kinds) - 1))
        label = _topic_label(topic)
        if label:
            topic.label = label
        result.append(topic)

    result.sort(key=lambda item: item.score, reverse=True)
    return result


def _topic_label(topic: Topic) -> str:
    if topic.entities:
        return sorted(topic.entities, key=len, reverse=True)[0]
    return str(topic.items[0].get("title") or "")[:30]


def pick_theme_items(topic: Topic, *, max_items: int = 3) -> list[dict]:
    """在话题内部按「角度多样」挑条目：先每类各取一条，再按分数补足。"""
    ordered = sorted(topic.items, key=lambda item: float(item["score"]), reverse=True)
    picked: list[dict] = []
    used_kinds: set[str] = set()
    for entry in ordered:
        kind = str(entry["doc"]["kind"])
        if kind in used_kinds:
            continue
        picked.append(entry)
        used_kinds.add(kind)
        if len(picked) >= max_items:
            return picked
    for entry in ordered:
        if len(picked) >= max_items:
            break
        if any(entry is chosen for chosen in picked):
            continue
        picked.append(entry)
    return picked
