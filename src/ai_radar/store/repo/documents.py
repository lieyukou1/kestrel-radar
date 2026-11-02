from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable

from rapidfuzz import fuzz

from ...util import iso, utcnow
from ._common import _dumps


def find_document_by_canonical(conn: sqlite3.Connection, canonical_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM documents WHERE canonical_id = ?",
        (canonical_id,),
    ).fetchone()

def get_document(conn: sqlite3.Connection, document_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM documents WHERE id = ?",
        (document_id,),
    ).fetchone()

def update_document_authority(
    conn: sqlite3.Connection, document_id: int, authority: str
) -> None:
    conn.execute(
        "UPDATE documents SET authority = ? WHERE id = ?",
        (authority, document_id),
    )
    conn.commit()

def list_recent_titles(conn: sqlite3.Connection, since_iso: str) -> list[str]:
    rows = conn.execute(
        "SELECT title FROM documents WHERE merged_into IS NULL AND first_seen_at >= ?",
        (since_iso,),
    ).fetchall()
    return [row["title"] for row in rows if row["title"]]

def find_similar_document(
    conn: sqlite3.Connection,
    title: str,
    since_iso: str,
    *,
    threshold: float = 90.0,
    conflict: Callable[[str, str], bool] | None = None,
) -> int | None:
    rows = conn.execute(
        "SELECT id, title FROM documents WHERE merged_into IS NULL AND first_seen_at >= ?",
        (since_iso,),
    ).fetchall()
    best_id: int | None = None
    best_score = 0.0
    for row in rows:
        if not row["title"]:
            continue
        # 标题相似但实体互斥（同产品不同事件/不同版本）：跳过这条，继续看后面的候选。
        if conflict is not None and conflict(title, str(row["title"])):
            continue
        score = fuzz.token_set_ratio(title, row["title"])
        if score > best_score:
            best_score = score
            best_id = int(row["id"])
    if best_score >= threshold:
        return best_id
    return None

def insert_document(
    conn: sqlite3.Connection,
    *,
    canonical_id: str,
    kind: str,
    title: str,
    abstract: str | None,
    url: str,
    published_at: str | None,
    authority: str,
    primary_tag: str | None,
    tags: list[str],
) -> int:
    now = iso(utcnow())
    cur = conn.execute(
        """
        INSERT INTO documents(
            canonical_id, kind, title, abstract, url, published_at,
            first_seen_at, authority, status, primary_tag, tags_json
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'observing', ?, ?)
        """,
        (
            canonical_id,
            kind,
            title,
            abstract,
            url,
            published_at,
            now,
            authority,
            primary_tag,
            _dumps(tags),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)

def link_document_source(
    conn: sqlite3.Connection, document_id: int, raw_item_id: int
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO document_sources(document_id, raw_item_id) VALUES(?, ?)",
        (document_id, raw_item_id),
    )
    conn.commit()

def insert_signal(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    source_id: str,
    points: int | None = None,
    comments: int | None = None,
    stars: int | None = None,
    citations: int | None = None,
    extra: dict | None = None,
) -> None:
    if all(value is None for value in (points, comments, stars, citations)):
        return
    conn.execute(
        """
        INSERT INTO signals(
            document_id, source_id, captured_at, points, comments, stars, citations, extra_json
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            document_id,
            source_id,
            iso(utcnow()),
            points,
            comments,
            stars,
            citations,
            _dumps(extra or {}),
        ),
    )
    conn.commit()

def list_candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM documents
        WHERE status IN ('observing', 'eligible')
          AND read_at IS NULL
          AND merged_into IS NULL
        ORDER BY published_at DESC
        """
    ).fetchall()

def latest_signals(conn: sqlite3.Connection, document_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM signals WHERE document_id = ? ORDER BY captured_at DESC LIMIT 2",
        (document_id,),
    ).fetchall()

def document_sources_map(conn: sqlite3.Connection) -> dict[int, set[str]]:
    """documents.id → 命中的来源 id 集合。"""
    rows = conn.execute(
        """
        SELECT ds.document_id, r.source_id
        FROM document_sources ds
        JOIN raw_items r ON r.id = ds.raw_item_id
        """
    ).fetchall()
    result: dict[int, set[str]] = {}
    for row in rows:
        result.setdefault(int(row["document_id"]), set()).add(str(row["source_id"]))
    return result

def document_regions_map(conn: sqlite3.Connection) -> dict[int, set[str]]:
    """documents.id → 命中的来源地区集合（overseas / domestic）。"""
    rows = conn.execute(
        """
        SELECT ds.document_id, s.region
        FROM document_sources ds
        JOIN raw_items r ON r.id = ds.raw_item_id
        JOIN sources s ON s.id = r.source_id
        """
    ).fetchall()
    result: dict[int, set[str]] = {}
    for row in rows:
        if row["region"]:
            result.setdefault(int(row["document_id"]), set()).add(str(row["region"]))
    return result

def source_external_id(
    conn: sqlite3.Connection, document_id: int, source_id: str
) -> str | None:
    """该文档在指定来源里的原始 id；没有这个来源时返回 None。"""
    row = conn.execute(
        """
        SELECT r.external_id
        FROM document_sources ds
        JOIN raw_items r ON r.id = ds.raw_item_id
        WHERE ds.document_id = ? AND r.source_id = ?
        LIMIT 1
        """,
        (document_id, source_id),
    ).fetchone()
    if row is None or not row["external_id"]:
        return None
    return str(row["external_id"])

def hn_object_id(conn: sqlite3.Connection, document_id: int) -> str | None:
    """该文档对应的 HN item id；不是 HN 来源则返回 None。"""
    return source_external_id(conn, document_id, "hackernews")


def hn_discussion_url(conn: sqlite3.Connection, document_id: int) -> str | None:
    """该文档在 Hacker News 上的讨论页；不是 HN 来源则返回 None。"""
    object_id = hn_object_id(conn, document_id)
    if object_id is None:
        return None
    return f"https://news.ycombinator.com/item?id={object_id}"

def discussion_target(
    conn: sqlite3.Connection, document_id: int
) -> tuple[str, str] | None:
    """该文档的社区讨论页与来源名：HN 优先，其次 Lobsters。"""
    object_id = hn_object_id(conn, document_id)
    if object_id is not None:
        return (f"https://news.ycombinator.com/item?id={object_id}", "HN")
    short_id = source_external_id(conn, document_id, "lobsters")
    if short_id is not None:
        return (f"https://lobste.rs/s/{short_id}", "Lobsters")
    return None

def signal_series(
    conn: sqlite3.Connection, document_id: int, *, limit: int = 200
) -> list[sqlite3.Row]:
    """完整信号序列，按时间倒序；用于计算增速。"""
    return conn.execute(
        "SELECT * FROM signals WHERE document_id = ? ORDER BY captured_at DESC LIMIT ?",
        (document_id, limit),
    ).fetchall()

def recent_pushed_titles(conn: sqlite3.Connection, since_iso: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT d.title
        FROM digest_items i
        JOIN digests g ON g.id = i.digest_id
        JOIN documents d ON d.id = i.document_id
        WHERE g.sent_at IS NOT NULL AND g.sent_at >= ?
        """,
        (since_iso,),
    ).fetchall()
    return [row["title"] for row in rows if row["title"]]

def pushed_document_ids(conn: sqlite3.Connection, since_iso: str) -> set[int]:
    """近一段时间确实推送过的文档 id，供主动查询避让。"""
    rows = conn.execute(
        """
        SELECT DISTINCT i.document_id
        FROM digest_items i
        JOIN digests g ON g.id = i.digest_id
        WHERE g.sent_at IS NOT NULL AND g.sent_at >= ?
        """,
        (since_iso,),
    ).fetchall()
    return {int(row["document_id"]) for row in rows}

def searchable_documents(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """主动查询的本地语料：全时间范围，排除已合并与明确不感兴趣的。"""
    return conn.execute(
        """
        SELECT * FROM documents
        WHERE merged_into IS NULL AND status != 'disliked'
        ORDER BY published_at DESC
        """
    ).fetchall()

def mark_status(
    conn: sqlite3.Connection, document_ids: Iterable[int], status: str
) -> None:
    ids = list(document_ids)
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"UPDATE documents SET status = ? WHERE id IN ({placeholders})",
        [status, *ids],
    )
    conn.commit()

def mark_read(conn: sqlite3.Connection, document_id: int) -> None:
    conn.execute(
        """
        UPDATE documents
        SET read_at = ?, read_count = read_count + 1, status = 'read'
        WHERE id = ?
        """,
        (iso(utcnow()), document_id),
    )
    conn.commit()

def set_local_path(conn: sqlite3.Connection, document_id: int, path: str) -> None:
    conn.execute(
        "UPDATE documents SET local_path = ? WHERE id = ?",
        (path, document_id),
    )
    conn.commit()

def list_deep_read_queue(
    conn: sqlite3.Connection, *, limit: int = 5
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM documents
        WHERE status = 'deep_reading' AND merged_into IS NULL
        ORDER BY published_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
