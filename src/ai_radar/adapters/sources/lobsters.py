from __future__ import annotations

import re
from datetime import timedelta

import httpx

from ..http import get_with_retry
from .base import RawItem
from ...util import parse_iso, utcnow

# Lobsters 的 tag 列表接口直接给出带分数与评论数的帖子，按 tag 抓比全站热榜更聚焦。
LOBSTERS_TAG_API = "https://lobste.rs/t/{tag}.json"
LOBSTERS_STORY_API = "https://lobste.rs/s/{short_id}.json"
SPACE_RE = re.compile(r"\s+")


def _clean(text: str | None) -> str:
    return SPACE_RE.sub(" ", str(text or "")).strip()


def parse_comments(data: dict, *, limit: int = 8, max_chars: int = 600) -> list[str]:
    """取高赞评论，忽略已删除/被折叠的楼层，只留两层以内。"""
    rows = [
        comment
        for comment in data.get("comments") or []
        if isinstance(comment, dict)
        and not comment.get("is_deleted")
        and not comment.get("is_moderated")
        and int(comment.get("depth") or 0) <= 2
    ]
    rows.sort(key=lambda comment: int(comment.get("score") or 0), reverse=True)
    comments: list[str] = []
    for comment in rows:
        text = _clean(comment.get("comment_plain"))
        if len(text) < 20:
            continue
        comments.append(text[:max_chars])
        if len(comments) >= limit:
            break
    return comments


def fetch_comments(
    short_id: str,
    *,
    proxy: str | None = None,
    timeout: float = 20.0,
    limit: int = 8,
) -> list[str]:
    """取某条 Lobsters 帖子的评论正文，用于「社区怎么看」。"""
    headers = {"User-Agent": "ai-radar/0.1 (+personal research)"}
    with httpx.Client(proxy=proxy, timeout=timeout, headers=headers) as client:
        response = get_with_retry(client, LOBSTERS_STORY_API.format(short_id=short_id))
    return parse_comments(response.json(), limit=limit)


class LobstersAdapter:
    def __init__(
        self,
        *,
        source_id: str = "lobsters",
        proxy: str | None = None,
        timeout: float = 30.0,
        max_results: int = 30,
        tags: list[str] | None = None,
        min_score: int = 3,
        max_age_hours: float = 24.0,
    ) -> None:
        self.source_id = source_id
        self.proxy = proxy
        self.timeout = timeout
        self.max_results = max_results
        self.tags = list(tags or ["ai", "ml"])
        self.min_score = min_score
        self.max_age_hours = max_age_hours

    def fetch(self) -> list[RawItem]:
        headers = {"User-Agent": "ai-radar/0.1 (+personal research)"}
        items: list[RawItem] = []
        with httpx.Client(proxy=self.proxy, timeout=self.timeout, headers=headers) as client:
            for tag in self.tags:
                try:
                    response = get_with_retry(
                        client, LOBSTERS_TAG_API.format(tag=tag), attempts=2
                    )
                except httpx.HTTPError:
                    # 单个 tag 不存在/被限流不影响其它 tag
                    continue
                items.extend(self.parse(response.json()))

        seen: set[str] = set()
        picked: list[RawItem] = []
        for item in items:
            if item.external_id in seen:
                continue
            seen.add(item.external_id)
            picked.append(item)
        return picked[: self.max_results]

    def parse(self, data: list[dict], *, now=None) -> list[RawItem]:
        now = now or utcnow()
        items: list[RawItem] = []
        for story in data or []:
            short_id = str(story.get("short_id") or "").strip()
            title = " ".join(str(story.get("title") or "").split())
            if not short_id or not title:
                continue
            try:
                score = int(story.get("score") or 0)
            except (TypeError, ValueError):
                score = 0
            if score < self.min_score:
                continue
            created = parse_iso(story.get("created_at"))
            if created is not None and now - created > timedelta(hours=self.max_age_hours):
                continue
            comments_url = str(
                story.get("comments_url") or f"https://lobste.rs/s/{short_id}"
            )
            # 外链帖用原文 URL（便于和 RSS / HN 跨源合并），纯文本帖退回讨论页。
            url = str(story.get("url") or comments_url)
            items.append(
                RawItem(
                    source_id=self.source_id,
                    external_id=short_id,
                    url=url,
                    title=title,
                    kind="forum",
                    author=story.get("submitter_user"),
                    published_at=story.get("created_at"),
                    summary=_clean(story.get("description_plain")),
                    signals={
                        "points": score,
                        "comments": int(story.get("comment_count") or 0),
                    },
                    payload={
                        "short_id": short_id,
                        "tags": list(story.get("tags") or []),
                        "comments_url": comments_url,
                        "score": score,
                    },
                )
            )
        return items
