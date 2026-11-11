from __future__ import annotations

import json
import sqlite3

from ...util import iso, utcnow
from ._common import _dumps


def get_digest(
    conn: sqlite3.Connection, digest_date: str, kind: str | None = None
) -> sqlite3.Row | None:
    """同一天可能有周报和日报两份；不指定 kind 时返回当天最新的一份。"""
    if kind is None:
        return conn.execute(
            "SELECT * FROM digests WHERE digest_date = ? ORDER BY id DESC LIMIT 1",
            (digest_date,),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM digests WHERE digest_date = ? AND kind = ?",
        (digest_date, kind),
    ).fetchone()

def create_digest(conn: sqlite3.Connection, digest_date: str, *, kind: str = "daily") -> int:
    conn.execute(
        "INSERT OR IGNORE INTO digests(digest_date, created_at, status, kind)"
        " VALUES(?, ?, 'building', ?)",
        (digest_date, iso(utcnow()), kind),
    )
    conn.commit()
    row = get_digest(conn, digest_date, kind)
    return int(row["id"])

def add_digest_item(
    conn: sqlite3.Connection,
    *,
    digest_id: int,
    document_id: int,
    rank: int,
    score: float,
    reason: str,
    summary: dict | None = None,
    section: str = "brief",
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO digest_items(
            digest_id, document_id, rank, score, reason, section, summary_json
        ) VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (digest_id, document_id, rank, score, reason, section, _dumps(summary or {})),
    )
    conn.commit()

def mark_digest_sent(conn: sqlite3.Connection, digest_id: int, stats: dict) -> None:
    conn.execute(
        "UPDATE digests SET status = 'sent', sent_at = ?, stats_json = ? WHERE id = ?",
        (iso(utcnow()), _dumps(stats), digest_id),
    )
    conn.commit()

def mark_digest_failed(conn: sqlite3.Connection, digest_id: int, error: str) -> None:
    conn.execute(
        "UPDATE digests SET status = 'failed', stats_json = ? WHERE id = ?",
        (_dumps({"error": error}), digest_id),
    )
    conn.commit()

def reset_digest(conn: sqlite3.Connection, digest_id: int) -> None:
    """删除失败的 digest 及其条目，允许次日任务重建。

    事件优先后，digest 会连带写入 topics / topic_items（即使当天只有单篇事件），
    不一起清掉会撞上 topics.digest_id 的外键。
    """
    conn.execute("DELETE FROM digest_items WHERE digest_id = ?", (digest_id,))
    conn.execute(
        "DELETE FROM topic_items WHERE topic_id IN (SELECT id FROM topics WHERE digest_id = ?)",
        (digest_id,),
    )
    conn.execute("DELETE FROM topics WHERE digest_id = ?", (digest_id,))
    conn.execute("DELETE FROM digests WHERE id = ?", (digest_id,))
    conn.commit()

def find_latest_summary(conn: sqlite3.Connection, document_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT summary_json FROM digest_items
        WHERE document_id = ? AND summary_json IS NOT NULL AND summary_json != '{}'
        ORDER BY id DESC LIMIT 1
        """,
        (document_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row["summary_json"])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) and value else None

def get_digest_item(
    conn: sqlite3.Connection, digest_id: int, document_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM digest_items WHERE digest_id = ? AND document_id = ?",
        (digest_id, document_id),
    ).fetchone()
