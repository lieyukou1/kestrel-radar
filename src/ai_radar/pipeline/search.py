"""主动查询：`/topic 关键词` 与卡片上的「围绕此话题找」。

两段式检索：
1. **本地语料优先**：向量语义 + 关键词命中，时间不限，覆盖「收进来了但没推过」的内容；
2. **站外补位**：本地不足 3 条时，并发查 arXiv / Hacker News / GitHub，
   总预算 10 秒，哪家慢就放弃哪家，不拖住飞书回调。

与日报的差别：这里不按新鲜度压分，也不做话题聚类，只回答「和这个词有关的都拿出来」。
唯一沿用日报的约束是：近 30 天推过的内容排在后面，避免把用户刚看过的又端上来。
"""

from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import timedelta

import httpx

from ..adapters.http import get_with_retry
from ..adapters.sources.arxiv import ArxivAdapter
from ..adapters.sources.github import GitHubAdapter
from ..config import AppConfig
from ..store import repo
from ..util import iso, utcnow
from . import embedding

LOCAL_LIMIT = 5
EXTERNAL_LIMIT = 3
BUDGET_SECONDS = 9.0
PUSHED_COOLDOWN_DAYS = 30
EXTERNAL_TIMEOUT = 6.0

ARXIV_SEARCH = "https://export.arxiv.org/api/query"
HN_SEARCH = "https://hn.algolia.com/api/v1/search"
GITHUB_SEARCH = "https://api.github.com/search/repositories"

AUTHORITY_RANK = {"S": 4, "A": 3, "B": 2, "C": 1}


@dataclass(slots=True)
class SearchHit:
    document_id: int
    title: str
    url: str
    tag: str
    kind: str
    score: float
    reason: str
    published_at: str | None = None
    pushed_recently: bool = False
    discussion_url: str | None = None
    in_kb: bool = False
    saved: bool = False


@dataclass(slots=True)
class ExternalHit:
    source: str
    title: str
    url: str
    meta: str = ""


@dataclass(slots=True)
class SearchResult:
    query: str
    local: list[SearchHit] = field(default_factory=list)
    external: list[ExternalHit] = field(default_factory=list)
    elapsed_ms: int = 0
    note: str = ""


def _tokens(query: str) -> list[str]:
    parts = query.replace(",", " ").replace("，", " ").replace("、", " ").split()
    return [part for part in parts if part.strip()]


def _local_hits(
    conn: sqlite3.Connection, query: str, *, limit: int
) -> tuple[list[SearchHit], str]:
    tokens = _tokens(query)
    if not tokens:
        return [], ""

    since = iso(utcnow() - timedelta(days=PUSHED_COOLDOWN_DAYS))
    pushed = repo.pushed_document_ids(conn, since)
    kb_ids = repo.kb_document_ids(conn)
    saved_ids = repo.saved_document_ids(conn)
    vectors = repo.list_vectors(conn, embedding.model_name())
    query_vector = None
    embedded = embedding.embed_texts([query])
    if embedded:
        query_vector = embedded[0]

    hits: list[SearchHit] = []
    authority_rank: dict[int, int] = {}
    for document in repo.searchable_documents(conn):
        document_id = int(document["id"])
        title = str(document["title"] or "")
        abstract = str(document["abstract"] or "")
        title_folded = title.casefold()
        abstract_folded = abstract.casefold()

        keyword = 0.0
        title_hits = 0
        covered = 0
        for token in tokens:
            key = token.casefold()
            if key in title_folded:
                keyword += 1.0
                title_hits += 1
                covered += 1
            elif key in abstract_folded:
                keyword += 0.5
                covered += 1
        keyword /= len(tokens)

        vector = vectors.get(document_id)
        semantic: float | None = None
        if query_vector and vector:
            semantic = max(0.0, embedding.cosine(query_vector, vector))

        # 多词查询要求每个词都出现，或语义足够强：
        # 否则「Boltzmann machine」会被一堆只含「machine」的论文灌满。
        if len(tokens) > 1 and covered < len(tokens):
            if semantic is None or semantic < 0.62:
                continue

        if semantic is None:
            # 没有向量时只靠关键词，命中不足就别硬塞。
            if keyword <= 0:
                continue
            relevance = keyword
        else:
            if keyword <= 0 and semantic < 0.55:
                continue
            # 两种证据取较高者，同时命中再加一点分；标题精确命中仍排最前。
            relevance = min(1.0, max(keyword, semantic) + 0.15 * min(keyword, semantic))
        if relevance < 0.2:
            continue

        reasons: list[str] = []
        if title_hits:
            reasons.append(f"标题命中 {title_hits} 个词")
        elif keyword > 0:
            reasons.append("摘要命中")
        if semantic is not None and semantic >= 0.5:
            reasons.append(f"语义相近 {int(semantic * 100)}%")

        authority_rank[document_id] = AUTHORITY_RANK.get(
            str(document["authority"] or ""), 0
        )
        hits.append(
            SearchHit(
                document_id=document_id,
                title=title,
                url=str(document["url"] or ""),
                tag=str(document["primary_tag"] or "未分类"),
                kind=str(document["kind"] or ""),
                score=round(relevance, 3),
                reason=" · ".join(reasons) or "关键词相近",
                published_at=document["published_at"],
                pushed_recently=document_id in pushed,
                discussion_url=repo.hn_discussion_url(conn, document_id),
                in_kb=document_id in kb_ids,
                saved=document_id in saved_ids,
            )
        )

    # 近 30 天推过的排在后面，但不当成淘汰：主动查询是用户点名要的。
    # 分数相同时用权威级别兜底，避免同分条目按入库顺序随机排。
    hits.sort(
        key=lambda hit: (
            not hit.pushed_recently,
            hit.score,
            authority_rank.get(hit.document_id, 0),
        ),
        reverse=True,
    )
    fresh = [hit for hit in hits if not hit.pushed_recently]
    recent = [hit for hit in hits if hit.pushed_recently]
    selected = fresh[:limit]
    note = ""
    if len(selected) < 3 and recent:
        selected.extend(recent[: limit - len(selected)])
        note = "本地结果不足 3 条，补充了近期推过的内容。"
    return selected, note


def _arxiv_hits(config: AppConfig, query: str, limit: int) -> list[ExternalHit]:
    adapter = ArxivAdapter(
        proxy=config.network.proxy, timeout=EXTERNAL_TIMEOUT, max_results=limit
    )
    params = {
        "search_query": f'all:"{query}"',
        "start": 0,
        "max_results": limit,
        "sortBy": "relevance",
        "sortOrder": "descending",
    }
    headers = {"User-Agent": "ai-radar/0.1 (personal research; contact: local)"}
    with httpx.Client(
        proxy=config.network.proxy,
        timeout=EXTERNAL_TIMEOUT,
        headers=headers,
        follow_redirects=True,
    ) as client:
        response = get_with_retry(client, ARXIV_SEARCH, params=params, attempts=1)
    items = adapter.parse(response.text)
    return [
        ExternalHit(source="arXiv", title=item.title, url=item.url, meta="论文")
        for item in items[:limit]
    ]


def _hn_hits(config: AppConfig, query: str, limit: int) -> list[ExternalHit]:
    params = {"query": query, "tags": "story", "hitsPerPage": limit}
    with httpx.Client(proxy=config.network.proxy, timeout=EXTERNAL_TIMEOUT) as client:
        response = get_with_retry(client, HN_SEARCH, params=params, attempts=1)
    hits: list[ExternalHit] = []
    for hit in (response.json() or {}).get("hits") or []:
        title = str(hit.get("title") or hit.get("story_title") or "").strip()
        if not title:
            continue
        url = str(hit.get("url") or "").strip() or (
            f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
        )
        points = int(hit.get("points") or 0)
        comments = int(hit.get("num_comments") or 0)
        hits.append(
            ExternalHit(
                source="Hacker News",
                title=title,
                url=url,
                meta=f"{points} 分 · {comments} 评论",
            )
        )
    return hits[:limit]


def _github_hits(config: AppConfig, query: str, limit: int) -> list[ExternalHit]:
    adapter = GitHubAdapter(
        proxy=config.network.proxy, timeout=EXTERNAL_TIMEOUT, max_results=limit
    )
    params = {
        "q": f"{query} stars:>=200",
        "sort": "stars",
        "order": "desc",
        "per_page": limit,
    }
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ai-radar/0.1",
    }
    with httpx.Client(
        proxy=config.network.proxy, timeout=EXTERNAL_TIMEOUT, headers=headers
    ) as client:
        response = get_with_retry(client, GITHUB_SEARCH, params=params, attempts=1)
    items = adapter.parse(response.json())
    return [
        ExternalHit(
            source="GitHub",
            title=item.title,
            url=item.url,
            meta=f"{item.signals.get('stars', 0)}★",
        )
        for item in items[:limit]
    ]


def _external_hits(
    config: AppConfig, query: str, *, limit: int, budget: float
) -> list[ExternalHit]:
    """三家并发，超预算就放弃，飞书回调不能等太久。"""
    if budget <= 1.0:
        return []
    pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ai-radar-topic")
    try:
        futures = [
            pool.submit(fetcher, config, query, 2)
            for fetcher in (_arxiv_hits, _hn_hits, _github_hits)
        ]
        done, pending = wait(futures, timeout=budget)
        for future in pending:
            future.cancel()
        found: list[ExternalHit] = []
        for future in done:
            try:
                found.extend(future.result() or [])
            except Exception:
                continue
        return found[:limit]
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def search_documents(
    conn: sqlite3.Connection,
    config: AppConfig,
    query: str,
    *,
    limit: int = LOCAL_LIMIT,
    external: bool = True,
) -> SearchResult:
    started = time.monotonic()
    query = " ".join(query.split())
    result = SearchResult(query=query)
    if not query:
        return result

    result.local, result.note = _local_hits(conn, query, limit=limit)
    if external and len(result.local) < 3:
        budget = BUDGET_SECONDS - (time.monotonic() - started)
        found = _external_hits(
            config, query, limit=EXTERNAL_LIMIT, budget=budget
        )
        seen = {hit.url.rstrip("/") for hit in result.local}
        for hit in found:
            url = hit.url.rstrip("/")
            if url in seen:
                continue
            seen.add(url)
            result.external.append(hit)
    result.elapsed_ms = int((time.monotonic() - started) * 1000)
    return result
