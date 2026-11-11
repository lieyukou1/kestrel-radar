from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

SECTION_TITLES = {
    "theme": "今日主线",
    "progress": "其他进展",
    "breaking": "突发",
    "brief": "快讯",
    "recall": "值得回看",
    "weekly_paper": "本周论文",
    "weekly_repo": "本周仓库",
}


@dataclass(slots=True)
class DigestItemView:
    rank: int
    document_id: int
    title: str
    url: str
    tag: str
    source: str
    reason: str
    score: float
    discussion_url: str | None = None
    # 讨论来源标签（HN / Lobsters），与 discussion_url 配套
    discussion_label: str = ""
    section: str = "brief"
    # 回看块专用：形如「你 9 月 12 日收藏过」
    note: str = ""
    # 命中长期关注实体时置为 True，用于卡片上打「关注」标记
    followed: bool = False
    # 原文发布日期：论文/讨论/媒体必须展示，否则旧内容会被当成新闻
    published_at: str | None = None


def parse_published(value) -> date | None:
    """published_at 可能是 ISO 时间串或缺字段，统一截取日期部分。"""
    text = str(value or "").strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def age_label(published: date, reference: date | None) -> str:
    """相对推送日的年龄，用于提示「这是旧内容」。"""
    if reference is None:
        return ""
    days = (reference - published).days
    if days <= 0:
        return ""
    if days == 1:
        return "昨天"
    if days < 30:
        return f"{days} 天前"
    if days < 365:
        return f"{days // 30} 个月前"
    return f"{days // 365} 年前"


def published_label(published_at, kind: str | None, reference: date | None) -> str:
    """材料的发布时间，和事件本身发生的时间是两件事，必须写清楚。

    仓库没有「发布」概念（仓库可以存在很多年），不展示时间。
    """
    if (kind or "") == "repo":
        return ""
    published = parse_published(published_at)
    if published is None:
        return ""
    text = f"发布于 {published.isoformat()}"
    age = age_label(published, reference)
    return f"{text}（{age}）" if age else text


class Notifier(Protocol):
    def send_digest(
        self,
        digest_date: str,
        items: list[DigestItemView],
        *,
        kind: str = "daily",
        theme_label: str = "",
        theme_reason: str = "",
        theme_query: str = "",
        theme_when: str = "",
        theme_when_note: str = "",
        preview: bool = False,
    ) -> str: ...

    def send_text(self, text: str) -> str: ...

    def send_card(self, card: dict) -> str: ...
