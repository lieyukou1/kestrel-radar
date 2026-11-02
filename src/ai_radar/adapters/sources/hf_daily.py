from __future__ import annotations

import httpx

from ..http import get_with_retry
from .base import RawItem

HF_ENDPOINTS = [
    "https://hf-mirror.com/api/daily_papers",
    "https://huggingface.co/api/daily_papers",
]


class HFDailyAdapter:
    def __init__(
        self,
        *,
        source_id: str = "hf_daily",
        proxy: str | None = None,
        timeout: float = 60.0,
        max_results: int = 50,
    ) -> None:
        self.source_id = source_id
        self.proxy = proxy
        self.timeout = timeout
        self.max_results = max_results

    def fetch(self) -> list[RawItem]:
        headers = {"User-Agent": "ai-radar/0.1"}
        last_error: Exception | None = None
        with httpx.Client(
            proxy=self.proxy, timeout=self.timeout, headers=headers, follow_redirects=True
        ) as client:
            for endpoint in HF_ENDPOINTS:
                try:
                    response = get_with_retry(client, endpoint, attempts=2, base_delay=3.0)
                    return self.parse(response.json())[: self.max_results]
                except (httpx.HTTPError, ValueError) as exc:
                    last_error = exc
        assert last_error is not None
        raise last_error

    def parse(self, data: list[dict]) -> list[RawItem]:
        items: list[RawItem] = []
        for entry in data or []:
            paper = entry.get("paper") or {}
            arxiv_id = str(paper.get("id") or "").strip()
            if not arxiv_id:
                continue
            title = (entry.get("title") or paper.get("title") or "").strip()
            if not title:
                continue
            authors = [
                author.get("name")
                for author in (paper.get("authors") or [])
                if isinstance(author, dict) and author.get("name")
            ]
            summary = entry.get("summary") or paper.get("summary") or ""
            items.append(
                RawItem(
                    source_id=self.source_id,
                    external_id=arxiv_id,
                    url=f"https://arxiv.org/abs/{arxiv_id}",
                    title=" ".join(title.split()),
                    kind="paper",
                    author=", ".join(authors) or None,
                    published_at=entry.get("publishedAt") or paper.get("publishedAt"),
                    summary=summary,
                    signals={
                        # HF 把点赞数挪到了 paper 里面，只读顶层会永远拿到 0
                        "points": entry.get("upvotes") or paper.get("upvotes") or 0,
                        "comments": entry.get("numComments") or 0,
                        "stars": paper.get("githubStars") or 0,
                    },
                    payload={
                        "arxiv_id": arxiv_id,
                        "summary": summary,
                        "upvotes": entry.get("upvotes") or paper.get("upvotes"),
                        "num_comments": entry.get("numComments"),
                        "github_repo": paper.get("githubRepo"),
                        "ai_summary": paper.get("ai_summary"),
                        "ai_keywords": paper.get("ai_keywords"),
                        "via": "hf_daily",
                    },
                )
            )
        return items
