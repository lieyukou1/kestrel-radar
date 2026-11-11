from __future__ import annotations

import sqlite3

from ...util import iso, utcnow


def add_topic(
    conn: sqlite3.Connection,
    *,
    digest_id: int | None,
    topic_date: str,
    kind: str,
    label: str,
    summary: str,
    entity: str,
    score: float,
    item_count: int,
    dimension: str | None = None,
    event_date: str = "",
    event_key: str = "",
    first_date: str = "",
    day_index: int = 1,
    new_count: int = 0,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO topics(
            digest_id, topic_date, kind, label, summary, entity, dimension,
            score, item_count, event_date, event_key, first_date, day_index,
            new_count, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            digest_id,
            topic_date,
            kind,
            label,
            summary,
            entity,
            dimension,
            score,
            item_count,
            event_date,
            event_key,
            first_date or topic_date,
            day_index,
            new_count,
            iso(utcnow()),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)

def add_topic_item(
    conn: sqlite3.Connection,
    *,
    topic_id: int,
    document_id: int,
    angle: str,
    rank: int,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO topic_items(topic_id, document_id, angle, rank) VALUES(?, ?, ?, ?)",
        (topic_id, document_id, angle, rank),
    )
    conn.commit()

def recent_topics(conn: sqlite3.Connection, since_date: str, *, limit: int = 30) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM topics
        WHERE topic_date >= ? AND label IS NOT NULL AND label <> ''
        ORDER BY topic_date DESC, score DESC LIMIT ?
        """,
        (since_date, limit),
    ).fetchall()

def last_topic_for_entity(
    conn: sqlite3.Connection, entity: str, before_date: str
) -> sqlite3.Row | None:
    """同一实体上一次开主线是在哪天、当时的事件时间是什么。

    用来判断这次的报道是「事件的新进展」还是「同一件事的媒体回声」。
    """
    entity = str(entity or "").strip()
    if not entity:
        return None
    return conn.execute(
        """
        SELECT * FROM topics
        WHERE entity = ? AND topic_date < ? AND label IS NOT NULL AND label <> ''
        ORDER BY topic_date DESC LIMIT 1
        """,
        (entity, before_date),
    ).fetchone()


def last_topic_by_key(
    conn: sqlite3.Connection, event_key: str, since_date: str, before_date: str
) -> sqlite3.Row | None:
    """同一事件键在时间窗内的上一次推送，用于跨天跟踪与「新增内容」判定。"""
    key = str(event_key or "").strip()
    if not key:
        return None
    return conn.execute(
        """
        SELECT * FROM topics
        WHERE event_key = ? AND topic_date >= ? AND topic_date < ?
        ORDER BY topic_date DESC LIMIT 1
        """,
        (key, since_date, before_date),
    ).fetchone()


def topic_document_ids(conn: sqlite3.Connection, topic_id: int) -> set[int]:
    rows = conn.execute(
        "SELECT document_id FROM topic_items WHERE topic_id = ?", (topic_id,)
    ).fetchall()
    return {int(row["document_id"]) for row in rows}
