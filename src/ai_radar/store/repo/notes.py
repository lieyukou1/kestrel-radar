from __future__ import annotations

import sqlite3
from typing import Iterable

from ...util import iso, utcnow


def add_note(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    content: str,
    created_at: str | None = None,
) -> int:
    """追加一条用户批注；只追加不覆盖，返回新记录 id。"""
    cur = conn.execute(
        "INSERT INTO notes(document_id, content, created_at) VALUES(?, ?, ?)",
        (document_id, content, created_at or iso(utcnow())),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def list_notes(
    conn: sqlite3.Connection, document_id: int, *, limit: int = 20
) -> list[sqlite3.Row]:
    """按时间倒序取某篇的批注，最多 limit 条。"""
    return conn.execute(
        """
        SELECT * FROM notes
        WHERE document_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (document_id, limit),
    ).fetchall()


def count_notes(conn: sqlite3.Connection, document_ids: Iterable[int]) -> dict[int, int]:
    """批量取批注条数，供知识库列表展示，避免逐篇查询。"""
    ids = [int(value) for value in dict.fromkeys(document_ids)]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT document_id, COUNT(*) AS total
        FROM notes
        WHERE document_id IN ({placeholders})
        GROUP BY document_id
        """,
        ids,
    ).fetchall()
    return {int(row["document_id"]): int(row["total"]) for row in rows}


def notes_by_document(
    conn: sqlite3.Connection, document_ids: Iterable[int], *, limit: int = 3
) -> dict[int, list[str]]:
    """问答与卡片用：每篇取最近几条批注正文。"""
    result: dict[int, list[str]] = {}
    for document_id in dict.fromkeys(int(value) for value in document_ids):
        rows = list_notes(conn, document_id, limit=limit)
        if rows:
            result[document_id] = [str(row["content"]) for row in rows]
    return result
