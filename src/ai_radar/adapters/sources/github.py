from __future__ import annotations

from datetime import timedelta

import httpx

from ..http import get_with_retry
from .base import RawItem
from ...util import utcnow

GITHUB_SEARCH = "https://api.github.com/search/repositories"


class GitHubAdapter:
    def __init__(
        self,
        *,
        source_id: str = "github",
        proxy: str | None = None,
        timeout: float = 30.0,
        max_results: int = 20,
        query: str = "topic:llm stars:>=200",
        token: str | None = None,
        created_days: int = 90,
        min_stars: int = 200,
    ) -> None:
        self.source_id = source_id
        self.proxy = proxy
        self.timeout = timeout
        self.max_results = max_results
        self.query = query
        self.token = token
        self.created_days = created_days
        self.min_stars = min_stars

    def fetch(self) -> list[RawItem]:
        since = (utcnow() - timedelta(days=self.created_days)).date().isoformat()
        params = {
            "q": f"{self.query} created:>={since}",
            "sort": "stars",
            "order": "desc",
            "per_page": self.max_results,
        }
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "ai-radar/0.1",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        with httpx.Client(proxy=self.proxy, timeout=self.timeout, headers=headers) as client:
            response = get_with_retry(client, GITHUB_SEARCH, params=params, attempts=2)
        return self.parse(response.json())

    def parse(self, data: dict) -> list[RawItem]:
        items: list[RawItem] = []
        for repo in data.get("items") or []:
            full_name = str(repo.get("full_name") or "").strip()
            if not full_name:
                continue
            stars = repo.get("stargazers_count") or 0
            # 搜索查询里已经带了 stars 条件，这里再兜一层，避免参数被改掉后放进刷榜仓库。
            if stars < self.min_stars:
                continue
            items.append(
                RawItem(
                    source_id=self.source_id,
                    external_id=full_name,
                    url=str(repo.get("html_url") or f"https://github.com/{full_name}"),
                    title=full_name,
                    kind="repo",
                    author=(repo.get("owner") or {}).get("login"),
                    published_at=repo.get("created_at"),
                    summary=repo.get("description"),
                    signals={"stars": stars},
                    payload={
                        "full_name": full_name,
                        "description": repo.get("description"),
                        "stars": stars,
                        "forks": repo.get("forks_count"),
                        "topics": repo.get("topics"),
                        "language": repo.get("language"),
                        "via": "github_search",
                    },
                )
            )
        return items
