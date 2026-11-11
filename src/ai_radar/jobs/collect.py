from __future__ import annotations

import os
import sqlite3

from ..adapters.sources.arxiv import ArxivAdapter
from ..adapters.sources.base import SourceAdapter
from ..adapters.sources.github import GitHubAdapter
from ..adapters.sources.hackernews import HackerNewsAdapter
from ..adapters.sources.hf_daily import HFDailyAdapter
from ..adapters.sources.lobsters import LobstersAdapter
from ..adapters.sources.rss import RSSAdapter
from ..config import AppConfig, SourceConfig
from ..pipeline.normalize import merge_item
from ..pipeline.preferences import paused_sources
from ..store import repo


def build_adapter(config: AppConfig, source: SourceConfig) -> SourceAdapter:
    proxy = config.proxy_for(source)
    timeout = config.network.timeout_seconds
    if source.kind == "arxiv":
        return ArxivAdapter(
            source_id=source.id,
            proxy=proxy,
            timeout=timeout,
            max_results=int(source.options.get("max_results", 50)),
            categories=list(source.options.get("categories") or []),
        )
    if source.kind == "hackernews":
        return HackerNewsAdapter(
            source_id=source.id,
            proxy=proxy,
            timeout=timeout,
            max_results=int(source.options.get("max_results", 50)),
            query=str(source.options.get("query", "AI")),
            min_points=int(source.options.get("min_points", 0)),
        )
    if source.kind == "hf_daily":
        return HFDailyAdapter(
            source_id=source.id,
            proxy=proxy,
            timeout=timeout,
            max_results=int(source.options.get("max_results", 50)),
        )
    if source.kind == "github":
        return GitHubAdapter(
            source_id=source.id,
            proxy=proxy,
            timeout=timeout,
            max_results=int(source.options.get("max_results", 20)),
            query=str(source.options.get("query", "topic:llm stars:>=200")),
            token=os.getenv("GITHUB_TOKEN"),
            created_days=int(source.options.get("created_days", 90)),
            min_stars=int(source.options.get("min_stars", 200)),
        )
    if source.kind == "rss":
        return RSSAdapter(
            source_id=source.id,
            proxy=proxy,
            timeout=timeout,
            feeds=list(source.options.get("feeds") or []),
            max_results=int(source.options.get("max_results", 30)),
            max_age_days=float(source.options.get("max_age_days", 0) or 0),
        )
    if source.kind == "lobsters":
        return LobstersAdapter(
            source_id=source.id,
            proxy=proxy,
            timeout=timeout,
            max_results=int(source.options.get("max_results", 30)),
            tags=list(source.options.get("tags") or ["ai", "ml"]),
            min_score=int(source.options.get("min_score", 3)),
            max_age_hours=float(source.options.get("max_age_hours", 24)),
        )
    raise ValueError(f"unsupported source kind: {source.kind}")


def run_collect(conn: sqlite3.Connection, config: AppConfig) -> dict:
    run_id = repo.start_run(conn, "collect")
    stats: dict = {"sources": {}, "items": 0, "errors": []}
    paused = paused_sources(conn)

    for source in config.sources:
        if not source.enabled:
            continue
        if source.id in paused:
            stats["sources"][source.id] = {"skipped": "paused"}
            continue
        repo.upsert_source(
            conn,
            source_id=source.id,
            kind=source.kind,
            name=source.name,
            authority=source.authority,
            region=source.region,
            config=source.options,
        )
        try:
            adapter = build_adapter(config, source)
            items = adapter.fetch()
            for item in items:
                merge_item(conn, item, item.authority or source.authority)
            stats["sources"][source.id] = {"fetched": len(items)}
            stats["items"] += len(items)
        except Exception as exc:  # 单源失败不影响其他来源
            message = f"{type(exc).__name__}: {exc}"
            stats["sources"][source.id] = {"error": message}
            stats["errors"].append(f"{source.id}: {message}")

    status = "failed" if stats["errors"] and stats["items"] == 0 else "success"
    repo.finish_run(conn, run_id, status=status, stats=stats, error="; ".join(stats["errors"]) or None)
    return stats
