from __future__ import annotations

import json
import sqlite3

from ...util import iso, utcnow
from ._common import _dumps


def save_vector(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    model: str,
    vector: list[float],
) -> None:
    conn.execute(
        """
        INSERT INTO kb_vectors(document_id, model, dim, vector_json, created_at)
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(document_id) DO UPDATE SET
            model = excluded.model,
            dim = excluded.dim,
            vector_json = excluded.vector_json,
            created_at = excluded.created_at
        """,
        (document_id, model, len(vector), _dumps(vector), iso(utcnow())),
    )
    conn.commit()

def get_vector(
    conn: sqlite3.Connection, document_id: int, model: str
) -> list[float] | None:
    row = conn.execute(
        "SELECT vector_json FROM kb_vectors WHERE document_id = ? AND model = ?",
        (document_id, model),
    ).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row["vector_json"])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, list) and value else None

def list_vectors(
    conn: sqlite3.Connection, model: str
) -> dict[int, list[float]]:
    rows = conn.execute(
        "SELECT document_id, vector_json FROM kb_vectors WHERE model = ?",
        (model,),
    ).fetchall()
    result: dict[int, list[float]] = {}
    for row in rows:
        try:
            value = json.loads(row["vector_json"])
        except json.JSONDecodeError:
            continue
        if isinstance(value, list) and value:
            result[int(row["document_id"])] = value
    return result

def documents_missing_vector(
    conn: sqlite3.Connection, model: str
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT d.* FROM documents d
        JOIN kb_articles k ON k.document_id = d.id
        LEFT JOIN kb_vectors v ON v.document_id = d.id AND v.model = ?
        WHERE v.document_id IS NULL AND d.merged_into IS NULL
        ORDER BY k.deep_read_at
        """,
        (model,),
    ).fetchall()
