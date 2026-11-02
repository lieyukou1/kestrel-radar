"""事件时间的外网核对：用 Google News RSS 找该事件最早的报道。

项目没有独立搜索 API key，Bing RSS 结果不可用、DuckDuckGo 有反爬，
Google News RSS 稳定且带 pubDate，足够回答「这件事最早什么时候被报道」。
失败一律静默返回空，绝不影响日报投递。
"""

from __future__ import annotations

import time

import feedparser
import httpx

NEWS_RSS = "https://news.google.com/rss/search"
TIMEOUT = 8.0
MAX_RESULTS = 8


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def search_news(
    query: str,
    *,
    proxy: str | None = None,
    limit: int = MAX_RESULTS,
) -> list[dict]:
    """检索新闻，返回 [{title, published, source}]，按 RSS 原顺序。"""
    query = str(query or "").strip()
    if not query:
        return []
    cjk = _has_cjk(query)
    params = {
        "q": query,
        "hl": "zh-CN" if cjk else "en-US",
        "gl": "CN" if cjk else "US",
        "ceid": "CN:zh-Hans" if cjk else "US:en",
    }
    # 代理可能没开：先按配置走，失败再直连，仍失败就放弃。
    attempts = [proxy] if proxy else []
    attempts.append(None)
    for attempt_proxy in attempts:
        try:
            with httpx.Client(
                proxy=attempt_proxy,
                timeout=TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": "ai-radar/0.1 (personal research; contact: local)"},
            ) as client:
                response = client.get(NEWS_RSS, params=params)
            if response.status_code >= 400:
                continue
            feed = feedparser.parse(response.text)
            items: list[dict] = []
            for entry in feed.entries[:limit]:
                published = ""
                parsed = getattr(entry, "published_parsed", None)
                if parsed:
                    published = time.strftime("%Y-%m-%d", parsed)
                title = str(getattr(entry, "title", "") or "").strip()
                if not title:
                    continue
                source = ""
                detail = getattr(entry, "source", None)
                if detail is not None:
                    source = str(getattr(detail, "title", "") or "").strip()
                items.append({"title": title, "published": published, "source": source})
            if items:
                return items
        except Exception:
            continue
    return []
