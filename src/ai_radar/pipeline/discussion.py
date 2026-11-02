"""把社区评论提炼成卡片上的「社区怎么看」。

只在内容已经入选 digest 后执行，来源仅 HN / Lobsters；最多 8 条评论、3 条结论。
结果写回 summary_json（discussion + discussion_at），72 小时内同一文档直接复用。
失败一律静默：抓不到评论、模型超预算、JSON 格式坏，都不能影响推送。
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from ..adapters.sources import hackernews, lobsters
from ..llm.client import BudgetExceeded, LLMError, parse_json_object
from ..llm.prompts import DISCUSSION_SYSTEM, DISCUSSION_TEMPLATE
from ..store import repo
from ..util import iso, parse_iso, utcnow

CACHE_TTL_HOURS = 72
MIN_COMMENTS = 3
MAX_COMMENTS = 8
MAX_POINTS = 3


def _cached(conn: sqlite3.Connection, document_id: int) -> list[str]:
    """上次提炼的社区观点还在有效期内就直接用，省一次抓取和一次模型调用。"""
    previous = repo.find_latest_summary(conn, document_id) or {}
    points = previous.get("discussion") or []
    created = parse_iso(previous.get("discussion_at"))
    if not points or created is None:
        return []
    if utcnow() - created > timedelta(hours=CACHE_TTL_HOURS):
        return []
    return [str(point).strip() for point in points if str(point).strip()][:MAX_POINTS]


def collect_comments(
    conn: sqlite3.Connection, document, *, proxy: str | None = None
) -> list[str]:
    document_id = int(document["id"])
    object_id = repo.source_external_id(conn, document_id, "hackernews")
    if object_id is not None:
        comments = hackernews.fetch_comments(object_id, proxy=proxy, limit=MAX_COMMENTS)
    else:
        short_id = repo.source_external_id(conn, document_id, "lobsters")
        if short_id is None:
            return []
        comments = lobsters.fetch_comments(short_id, proxy=proxy, limit=MAX_COMMENTS)
    return comments if len(comments) >= MIN_COMMENTS else []


def _summarize(llm, conn: sqlite3.Connection, document, comments: list[str]) -> list[str]:
    user = DISCUSSION_TEMPLATE.format(
        title=document["title"],
        comments="\n".join(f"- {text}" for text in comments),
    )
    text, _usage = llm.chat(
        conn,
        stage="discussion",
        system=DISCUSSION_SYSTEM,
        user=user,
        max_tokens=500,
        document_id=int(document["id"]),
    )
    payload = parse_json_object(text)
    points = [
        str(item).strip()[:120] for item in payload.get("points") or [] if str(item).strip()
    ]
    return points[:MAX_POINTS]


def enrich(
    conn: sqlite3.Connection,
    document,
    summary: dict,
    *,
    llm,
    proxy: str | None = None,
) -> dict:
    """给论坛类条目的摘要补上 discussion；拿不到就原样返回。"""
    if (document["kind"] or "") != "forum":
        return summary

    cached = _cached(conn, int(document["id"]))
    if cached:
        summary["discussion"] = cached
        return summary

    try:
        comments = collect_comments(conn, document, proxy=proxy)
    except Exception:  # 抓评论是可选增强，网络问题不该影响推送
        return summary
    if not comments:
        return summary

    try:
        points = _summarize(llm, conn, document, comments)
    except (BudgetExceeded, LLMError):
        return summary
    if points:
        summary["discussion"] = points
        summary["discussion_at"] = iso(utcnow())
    return summary
