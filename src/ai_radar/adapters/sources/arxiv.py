from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import timezone
from email.utils import parsedate_to_datetime

import httpx

from ..http import get_with_retry
from .base import RawItem

ARXIV_ENDPOINTS = [
    "https://arxiv.org/api/query",
    "http://export.arxiv.org/api/query",
]
ATOM = {"a": "http://www.w3.org/2005/Atom"}
ARXIV_ID = re.compile(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})", re.IGNORECASE)


class ArxivAdapter:
    def __init__(
        self,
        *,
        source_id: str = "arxiv",
        proxy: str | None = None,
        timeout: float = 30.0,
        max_results: int = 50,
        categories: list[str] | None = None,
    ) -> None:
        self.source_id = source_id
        self.proxy = proxy
        self.timeout = timeout
        self.max_results = max_results
        self.categories = categories or ["cs.AI", "cs.LG", "cs.CL", "cs.CV"]

    def fetch(self) -> list[RawItem]:
        """优先走 API；被限流时回退到 arXiv 官方 RSS。"""
        try:
            return self.fetch_api()
        except httpx.HTTPError:
            return self.fetch_rss()

    def fetch_api(self) -> list[RawItem]:
        query = " OR ".join(f"cat:{category}" for category in self.categories)
        params = {
            "search_query": query,
            "start": 0,
            "max_results": self.max_results,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        headers = {"User-Agent": "ai-radar/0.1 (personal research; contact: local)"}
        last_error: Exception | None = None
        with httpx.Client(
            proxy=self.proxy, timeout=self.timeout, headers=headers, follow_redirects=True
        ) as client:
            for endpoint in ARXIV_ENDPOINTS:
                try:
                    response = get_with_retry(
                        client, endpoint, params=params, attempts=2, base_delay=3.0
                    )
                    return self.parse(response.text)
                except httpx.HTTPError as exc:
                    last_error = exc
        assert last_error is not None
        raise last_error

    def fetch_rss(self) -> list[RawItem]:
        headers = {"User-Agent": "ai-radar/0.1 (personal research; contact: local)"}
        items: list[RawItem] = []
        seen: set[str] = set()
        with httpx.Client(
            proxy=self.proxy, timeout=self.timeout, headers=headers, follow_redirects=True
        ) as client:
            for category in self.categories:
                response = get_with_retry(
                    client,
                    f"https://rss.arxiv.org/rss/{category}",
                    attempts=2,
                    base_delay=3.0,
                )
                for item in self.parse_rss(response.text):
                    if item.external_id in seen:
                        continue
                    seen.add(item.external_id)
                    items.append(item)
        return items[: self.max_results]

    def parse(self, xml_text: str) -> list[RawItem]:
        root = ET.fromstring(xml_text)
        items: list[RawItem] = []
        for entry in root.findall("a:entry", ATOM):
            raw_id = (entry.findtext("a:id", default="", namespaces=ATOM) or "").strip()
            match = ARXIV_ID.search(raw_id)
            if not match:
                continue
            arxiv_id = match.group(1)
            title = _clean(entry.findtext("a:title", default="", namespaces=ATOM))
            if not title:
                continue
            summary = _clean(entry.findtext("a:summary", default="", namespaces=ATOM))
            published = (entry.findtext("a:published", default="", namespaces=ATOM) or "").strip()
            authors = [
                (node.findtext("a:name", default="", namespaces=ATOM) or "").strip()
                for node in entry.findall("a:author", ATOM)
            ]
            categories = [
                node.attrib["term"]
                for node in entry.findall("a:category", ATOM)
                if node.attrib.get("term")
            ]
            items.append(
                RawItem(
                    source_id=self.source_id,
                    external_id=arxiv_id,
                    url=f"https://arxiv.org/abs/{arxiv_id}",
                    title=title,
                    kind="paper",
                    author=", ".join(authors) or None,
                    published_at=published or None,
                    summary=summary or None,
                    payload={
                        "arxiv_id": arxiv_id,
                        "summary": summary,
                        "categories": categories,
                        "authors": authors,
                    },
                )
            )
        return items


    def parse_rss(self, xml_text: str) -> list[RawItem]:
        root = ET.fromstring(xml_text)
        items: list[RawItem] = []
        for node in root.findall("./channel/item"):
            title = _clean(node.findtext("title"))
            link = (node.findtext("link") or "").strip()
            description = node.findtext("description") or ""

            match = re.search(r"arXiv:([0-9]{4}\.[0-9]{4,5})", description)
            if match:
                arxiv_id = match.group(1)
            else:
                link_match = ARXIV_ID.search(link)
                if not link_match:
                    continue
                arxiv_id = link_match.group(1)

            abstract = (
                description.split("Abstract:", 1)[1].strip()
                if "Abstract:" in description
                else description.strip()
            )
            published = _rfc822_to_iso(node.findtext("pubDate"))
            creator = node.findtext("{http://purl.org/dc/elements/1.1/}creator")
            categories = [child.text for child in node.findall("category") if child.text]

            items.append(
                RawItem(
                    source_id=self.source_id,
                    external_id=arxiv_id,
                    url=f"https://arxiv.org/abs/{arxiv_id}",
                    title=title,
                    kind="paper",
                    author=creator,
                    published_at=published,
                    summary=abstract or None,
                    payload={
                        "arxiv_id": arxiv_id,
                        "summary": abstract,
                        "categories": categories,
                        "via": "rss",
                    },
                )
            )
        return items


def _rfc822_to_iso(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())
