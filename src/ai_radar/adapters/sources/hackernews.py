from __future__ import annotations

import html as html_lib
import re

import httpx

from ..http import get_with_retry
from .base import RawItem

HN_API = "https://hn.algolia.com/api/v1/search_by_date"
HN_ITEM_API = "https://hn.algolia.com/api/v1/items"
TAG_RE = re.compile(r"<[^>]+>")


def _comment_text(raw: str | None) -> str:
    """HN 评论是 HTML 片段，去掉标签、反转义实体。"""
    if not raw:
        return ""
    return " ".join(html_lib.unescape(TAG_RE.sub(" ", raw)).split())


def parse_comments(data: dict, *, limit: int = 8, max_chars: int = 600) -> list[str]:
    """从 Algolia item 树里取顶层评论；顺序沿用 HN 展示顺序。"""
    comments: list[str] = []
    for child in data.get("children") or []:
        text = _comment_text(child.get("text"))
        if len(text) < 20:
            continue
        comments.append(text[:max_chars])
        if len(comments) >= limit:
            break
    return comments


def fetch_comments(
    object_id: str,
    *,
    proxy: str | None = None,
    timeout: float = 20.0,
    limit: int = 8,
) -> list[str]:
    """取某条 HN 帖子的评论正文，用于精读时的「社区怎么看」。"""
    headers = {"User-Agent": "ai-radar/0.1 (+personal research)"}
    with httpx.Client(proxy=proxy, timeout=timeout, headers=headers) as client:
        response = get_with_retry(client, f"{HN_ITEM_API}/{object_id}")
    return parse_comments(response.json(), limit=limit)


class HackerNewsAdapter:
    def __init__(
        self,
        *,
        source_id: str = "hackernews",
        proxy: str | None = None,
        timeout: float = 30.0,
        max_results: int = 50,
        query: str = "AI",
    ) -> None:
        self.source_id = source_id
        self.proxy = proxy
        self.timeout = timeout
        self.max_results = max_results
        self.query = query

    def fetch(self) -> list[RawItem]:
        params = {
            "tags": "story",
            "query": self.query,
            "hitsPerPage": self.max_results,
        }
        headers = {"User-Agent": "ai-radar/0.1 (+personal research)"}
        with httpx.Client(proxy=self.proxy, timeout=self.timeout, headers=headers) as client:
            response = get_with_retry(client, HN_API, params=params)
        return self.parse(response.json())

    def parse(self, data: dict) -> list[RawItem]:
        items: list[RawItem] = []
        for hit in data.get("hits") or []:
            title = (hit.get("title") or "").strip()
            object_id = hit.get("objectID")
            if not title or not object_id:
                continue
            url = hit.get("url") or f"https://news.ycombinator.com/item?id={object_id}"
            items.append(
                RawItem(
                    source_id=self.source_id,
                    external_id=str(object_id),
                    url=url,
                    title=" ".join(title.split()),
                    kind="forum",
                    author=hit.get("author"),
                    published_at=hit.get("created_at"),
                    payload={
                        "object_id": str(object_id),
                        "points": hit.get("points"),
                        "comments": hit.get("num_comments"),
                        "hn_url": f"https://news.ycombinator.com/item?id={object_id}",
                    },
                    signals={
                        "points": hit.get("points") or 0,
                        "comments": hit.get("num_comments") or 0,
                    },
                )
            )
        return items
