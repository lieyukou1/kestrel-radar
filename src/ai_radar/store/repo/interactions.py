from __future__ import annotations

import sqlite3

from ...util import iso, utcnow


def add_interaction(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    digest_id: int | None,
    action: str,
    reason: str | None = None,
) -> bool:
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO interactions(document_id, digest_id, action, reason, created_at)
        VALUES(?, ?, ?, ?, ?)
        """,
        (document_id, digest_id, action, reason, iso(utcnow())),
    )
    conn.commit()
    return cur.rowcount > 0

def remove_interaction(
    conn: sqlite3.Connection, *, document_id: int, action: str
) -> None:
    conn.execute(
        "DELETE FROM interactions WHERE document_id = ? AND action = ?",
        (document_id, action),
    )
    conn.commit()

def list_favorites(conn: sqlite3.Connection, *, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT d.*, MAX(i.created_at) AS saved_at
        FROM interactions i
        JOIN documents d ON d.id = i.document_id
        WHERE i.action = 'save'
        GROUP BY d.id
        ORDER BY saved_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def has_action(conn: sqlite3.Connection, document_id: int, action: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM interactions WHERE document_id = ? AND action = ? LIMIT 1",
        (document_id, action),
    ).fetchone()
    return row is not None


def action_count(conn: sqlite3.Connection, document_id: int, action: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM interactions WHERE document_id = ? AND action = ?",
        (document_id, action),
    ).fetchone()
    return int(row["n"]) if row is not None else 0


def last_action_at(
    conn: sqlite3.Connection, document_id: int, action: str
) -> str | None:
    row = conn.execute(
        """
        SELECT created_at FROM interactions
        WHERE document_id = ? AND action = ?
        ORDER BY id DESC LIMIT 1
        """,
        (document_id, action),
    ).fetchone()
    return str(row["created_at"]) if row is not None else None


def recall_pool(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """回看候选的基础集合：仍在知识库里的已收藏 / 已精读文档。

    已被移出的文档（kb_articles 被删、只剩 deep_read 记录）不在此集合内；
    负反馈过的文档也不进候选，避免把讨厌过的内容再推一次。
    """
    return conn.execute(
        """
        SELECT d.*,
               MAX(CASE WHEN i.action = 'save' THEN i.created_at END) AS saved_at,
               MAX(CASE WHEN i.action = 'deep_read' THEN i.created_at END) AS deep_read_at,
               MAX(k.deep_read_at) AS kb_read_at
        FROM documents d
        LEFT JOIN interactions i
          ON i.document_id = d.id AND i.action IN ('save', 'deep_read')
        LEFT JOIN kb_articles k ON k.document_id = d.id
        WHERE d.merged_into IS NULL AND d.status != 'disliked'
        GROUP BY d.id
        HAVING MAX(k.deep_read_at) IS NOT NULL
            OR MAX(CASE WHEN i.action = 'save' THEN i.created_at END) IS NOT NULL
        """,
    ).fetchall()


def set_recall_off(conn: sqlite3.Connection, document_id: int) -> None:
    """「不再提醒」：digest_id 留空，唯一键对 NULL 不生效，可安全重复写入。"""
    add_interaction(
        conn, document_id=document_id, digest_id=None, action="recall_off"
    )


def is_recall_off(conn: sqlite3.Connection, document_id: int) -> bool:
    return has_action(conn, document_id, "recall_off")


def recent_recall_tags(
    conn: sqlite3.Connection, *, limit: int = 2, since: str | None = None
) -> list[str]:
    """最近几次回看过的主分类，用于「同一维度连续两天不重复」。

    since 限定时间窗：不传就是全部历史。判断「连续两天」时必须传，
    否则两个月前回看过的分类会永远把同分类挡在门外。
    """
    sql = """
        SELECT d.primary_tag AS tag
        FROM interactions i
        JOIN documents d ON d.id = i.document_id
        WHERE i.action = 'recall' AND d.primary_tag IS NOT NULL
    """
    params: list[object] = []
    if since is not None:
        sql += " AND i.created_at >= ?"
        params.append(since)
    sql += " ORDER BY i.id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [str(row["tag"]) for row in rows]
