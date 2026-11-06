"""手动链接精读：把用户发来的文章链接变成一篇待精读文档。

只负责抓元信息、落库、入队；真正的切块与总结仍由 deep_read worker 完成。
"""

from __future__ import annotations

import re
import sqlite3
from urllib.parse import urlsplit

import trafilatura

from ..config import AppConfig
from ..store import repo
from .cleaning import CleanError, clean_html
from .deep_read import fetch_html
from .normalize import canonical_from_url

# 中文里链接后面经常紧跟「）】」或句号，匹配后统一剥掉。
URL_RE = re.compile(r"https?://[^\s<>\"'）)】\]]+")
MAX_ABSTRACT = 280


class LinkIngestError(RuntimeError):
    """失败原因可以直接展示给用户。"""


def extract_url(text: str) -> str | None:
    """从一条消息里取出第一个链接；没有就返回 None。"""
    match = URL_RE.search(text or "")
    if match is None:
        return None
    return match.group(0).rstrip(".,;:，。；：、")


def _metadata(html: str, url: str) -> tuple[str, str | None]:
    meta = trafilatura.extract_metadata(html, default_url=url)
    if meta is None:
        return "", None
    title = str(getattr(meta, "title", "") or "").strip()
    date = str(getattr(meta, "date", "") or "").strip()
    return title, (date or None)


def _is_wechat_verify_page(html: str, url: str) -> bool:
    if "mp.weixin.qq.com" not in url:
        return False
    return any(marker in html for marker in ("环境异常", "完成验证", "去验证"))


def _fetch_error(exc: Exception) -> str:
    detail = str(exc)
    if "blocked non-public url" in detail:
        return "只支持公开的 http(s) 链接。"
    if "403" in detail or "402" in detail:
        return "原文返回 403/402，通常是反爬或付费墙，服务器拿不到正文。"
    if "404" in detail:
        return "链接 404，文章可能已删除或需要登录。"
    if "timeout" in detail.lower():
        return "抓取原文超时，可以稍后重试。"
    return "原文抓取失败，可以稍后重试。"


def ingest_link(conn: sqlite3.Connection, config: AppConfig, url: str) -> dict:
    """抓取链接并加入精读队列。

    返回 {"document_id", "title", "state"}，state 取值：
    queued（新建入队）/ requeued（已存在重新入队）/ queued_existing（已在队列）/
    ready（已精读）。
    """
    canonical = canonical_from_url(url)
    existing = repo.find_document_by_canonical(conn, canonical)
    if existing is not None:
        document_id = int(existing["id"])
        if repo.get_kb_article(conn, document_id) is not None:
            return {"document_id": document_id, "title": existing["title"], "state": "ready"}
        if existing["status"] == "deep_reading":
            return {
                "document_id": document_id,
                "title": existing["title"],
                "state": "queued_existing",
            }
        repo.mark_status(conn, [document_id], "deep_reading")
        repo.add_interaction(conn, document_id=document_id, digest_id=None, action="deep_read")
        return {"document_id": document_id, "title": existing["title"], "state": "requeued"}

    proxy = config.network.proxy if config.network.mode == "overseas-only" else None
    try:
        html, final_url = fetch_html(url, proxy)
    except CleanError as exc:
        raise LinkIngestError(_fetch_error(exc)) from exc

    if _is_wechat_verify_page(html, final_url):
        raise LinkIngestError(
            "微信返回了「环境异常」验证页，服务器抓不到正文。"
            "可以稍后重试，或把正文粘贴给我。"
        )

    title, published_at = _metadata(html, final_url)
    try:
        text = clean_html(html, final_url, kind="blog")
    except CleanError as exc:
        raise LinkIngestError(
            "页面抓到了，但正文太少（可能是图片流、需要登录或反爬），没法精读。"
        ) from exc

    if not title:
        host = urlsplit(final_url).hostname or "链接"
        title = f"{host} 的文章"
    abstract = " ".join(text.split())[:MAX_ABSTRACT]
    document_id = repo.insert_document(
        conn,
        canonical_id=canonical,
        kind="blog",
        title=title[:200],
        abstract=abstract,
        url=final_url,
        published_at=published_at,
        authority="B",
        primary_tag=None,
        tags=[],
    )
    repo.mark_status(conn, [document_id], "deep_reading")
    repo.add_interaction(conn, document_id=document_id, digest_id=None, action="deep_read")
    return {"document_id": document_id, "title": title, "state": "queued"}
