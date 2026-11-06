from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..adapters.sources.base import RawItem
from ..store import repo
from ..util import iso, utcnow
from .entity_conflict import titles_conflict
from .tagging import classify

ARXIV_ID = re.compile(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})", re.IGNORECASE)
ARXIV_EXTERNAL = re.compile(r"^[0-9]{4}\.[0-9]{4,5}$")
GITHUB_REPO = re.compile(r"github\.com/([^/]+)/([^/#?]+)", re.IGNORECASE)

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "source", "fbclid", "gclid", "mc_cid", "mc_eid",
}

AUTHORITY_RANK = {"S": 4, "A": 3, "B": 2, "C": 1}


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    path = parts.path.rstrip("/")
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    ]
    return urlunsplit((parts.scheme.lower(), host, path, urlencode(query), ""))


def arxiv_id_from(item: RawItem) -> str | None:
    match = ARXIV_ID.search(item.url)
    if match:
        return match.group(1)
    if item.kind == "paper" and ARXIV_EXTERNAL.match(item.external_id):
        return item.external_id
    return None


def canonical_from_url(url: str) -> str:
    """只凭 URL 推导 canonical_id：arXiv / GitHub 用稳定标识，其余用归一化哈希。

    手动发链接（link_ingest）与采集侧必须共用这一份推导，否则同一条 arXiv
    论文会以 `arxiv:xxx` 和 `url:<sha1>` 两个身份各入库一次。
    """
    arxiv = ARXIV_ID.search(url or "")
    if arxiv:
        return f"arxiv:{arxiv.group(1)}"

    github = GITHUB_REPO.search(url or "")
    if github:
        return f"github:{github.group(1).lower()}/{github.group(2).lower().removesuffix('.git')}"

    digest = hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()
    return f"url:{digest}"


def canonical_id(item: RawItem) -> str:
    # 采集侧的 external_id 也可能带 arXiv 编号，这一路 URL 里看不出来。
    arxiv_id = arxiv_id_from(item)
    if arxiv_id:
        return f"arxiv:{arxiv_id}"
    return canonical_from_url(item.url)


def merge_item(conn: sqlite3.Connection, item: RawItem, authority: str) -> int:
    raw_id = repo.insert_raw_item(
        conn,
        source_id=item.source_id,
        external_id=item.external_id,
        url=item.url,
        title=item.title,
        author=item.author,
        published_at=item.published_at,
        payload=item.payload,
    )

    cid = canonical_id(item)
    document = repo.find_document_by_canonical(conn, cid)

    if document is None:
        since = iso(utcnow() - timedelta(days=30))
        similar_id = repo.find_similar_document(
            conn, item.title, since, conflict=titles_conflict
        )
        if similar_id is not None:
            document = repo.get_document(conn, similar_id)

    if document is not None:
        document_id = int(document["id"])
        repo.link_document_source(conn, document_id, raw_id)
        repo.insert_signal(
            conn,
            document_id=document_id,
            source_id=item.source_id,
            points=item.signals.get("points"),
            comments=item.signals.get("comments"),
            stars=item.signals.get("stars"),
            citations=item.signals.get("citations"),
            extra=item.signals,
        )
        if AUTHORITY_RANK.get(authority, 0) > AUTHORITY_RANK.get(document["authority"], 0):
            repo.update_document_authority(conn, document_id, authority)
        return document_id

    primary_tag, tags = classify(item)
    document_id = repo.insert_document(
        conn,
        canonical_id=cid,
        kind=item.kind,
        title=item.title,
        abstract=item.summary,
        url=item.url,
        published_at=item.published_at,
        authority=authority,
        primary_tag=primary_tag,
        tags=tags,
    )
    repo.link_document_source(conn, document_id, raw_id)
    repo.insert_signal(
        conn,
        document_id=document_id,
        source_id=item.source_id,
        points=item.signals.get("points"),
        comments=item.signals.get("comments"),
        stars=item.signals.get("stars"),
        citations=item.signals.get("citations"),
        extra=item.signals,
    )
    return document_id
