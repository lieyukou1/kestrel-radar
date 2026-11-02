from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone

import feedparser
import httpx

from ..http import get_with_retry
from .base import RawItem
from ...util import utcnow

TAG_RE = re.compile(r"<[^>]+>")


def strip_html(value: str | None) -> str:
    text = TAG_RE.sub(" ", value or "")
    return " ".join(html.unescape(text).split())


def match_keywords(text: str, keywords: list[str]) -> bool:
    """判断文本是否命中任一关键词。

    英文关键词按词边界匹配，避免 "AI" 命中 "AirPods"；中文关键词直接子串匹配。
    """
    lowered = text.lower()
    for keyword in keywords:
        word = str(keyword).lower().strip()
        if not word:
            continue
        if word.isascii():
            if re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", lowered):
                return True
        elif word in lowered:
            return True
    return False


class RSSAdapter:
    def __init__(
        self,
        *,
        source_id: str = "rss",
        proxy: str | None = None,
        timeout: float = 30.0,
        feeds: list[dict] | None = None,
        max_results: int = 30,
        max_age_days: float = 0,
    ) -> None:
        self.source_id = source_id
        self.proxy = proxy
        self.timeout = timeout
        self.feeds = feeds or []
        self.max_results = max_results
        # 公众号类 feed 常把几个月前的历史文章一起吐出来，日报用不上，按天过滤掉。
        self.max_age_days = max_age_days

    def fetch(self) -> list[RawItem]:
        headers = {"User-Agent": "ai-radar/0.1 (+personal research)"}
        items: list[RawItem] = []
        errors: list[str] = []
        with httpx.Client(proxy=self.proxy, timeout=self.timeout, headers=headers) as client:
            for feed in self.feeds:
                url = feed.get("url")
                if not url:
                    continue
                try:
                    response = get_with_retry(client, url, attempts=2)
                except httpx.HTTPError as exc:
                    errors.append(f"{feed.get('name') or url}: {type(exc).__name__}: {exc}")
                    continue
                items.extend(self.parse_feed(response.text, feed)[: self.max_results])
        # 单个 feed 失败不影响其他 feed；全部失败要暴露出来，否则采集会「静默零条」。
        if not items and errors:
            raise RuntimeError("; ".join(errors))
        return items

    def parse_feed(self, xml_text: str, feed: dict) -> list[RawItem]:
        parsed = feedparser.parse(xml_text)
        items: list[RawItem] = []
        feed_name = str(feed.get("name") or self.source_id)
        authority = str(feed.get("authority") or "B")
        # 有的站点（如 InfoQ 中文）把北京时间标成 GMT，需要按配置回拨时区。
        tz_offset = float(feed.get("published_tz_offset_hours") or 0)
        # 泛科技源（极客公园 / 雷锋网 / 掘金）混有大量非 AI 内容，按关键词过滤后再入库。
        keywords = [str(word) for word in (feed.get("keywords") or [])]
        exclude_keywords = [str(word) for word in (feed.get("exclude_keywords") or [])]
        now = utcnow()
        for entry in parsed.entries:
            link = str(entry.get("link") or "").strip()
            title = strip_html(entry.get("title"))
            if not link or not title:
                continue
            published_at = None
            if entry.get("published_parsed"):
                published = datetime(
                    *entry.published_parsed[:6], tzinfo=timezone.utc
                ) - timedelta(hours=tz_offset)
                # 时间戳落在未来会让成熟窗口永远不满足，兜底按当前时间算。
                if published > now:
                    published = now
                if self.max_age_days and (now - published).days >= self.max_age_days:
                    continue
                published_at = published.isoformat(timespec="seconds")
            summary = strip_html(entry.get("summary") or entry.get("description"))
            # 只看标题：泛科技源的「早报」正文常顺带提到 AI，按正文匹配会把整条非 AI 新闻放进来。
            if keywords and not match_keywords(title, keywords):
                continue
            # 「A；B；C」式的多话题聚合条目信息密度低，按配置整条丢弃。
            if exclude_keywords and any(word in title for word in exclude_keywords):
                continue
            items.append(
                RawItem(
                    source_id=self.source_id,
                    external_id=link,
                    url=link,
                    title=title,
                    kind="blog",
                    author=feed_name,
                    published_at=published_at,
                    summary=summary,
                    authority=authority,
                    payload={
                        "feed": feed_name,
                        "feed_url": feed.get("url"),
                        "summary": summary,
                        "via": "rss",
                    },
                )
            )
        return items
