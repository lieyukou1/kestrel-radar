"""话题的数据结构与主线内挑条目的规则。

真正的「是不是一件事」已经交给 `events.py`（实体粗分组 + 模型切分）判断，
这里只保留被编排与投递共用的形状与工具函数。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

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


def is_bucket_label(label: str) -> bool:
    return bool(BUCKET_LABEL.search(str(label or "")))


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
