from __future__ import annotations

import sqlite3

from ...util import iso, utcnow
from ._common import _dumps


def save_kb_article(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    problem: str,
    method: str,
    evidence: list[str],
    conclusions: list[str],
    limitations: list[str],
    card: dict,
) -> None:
    conn.execute(
        """
        INSERT INTO kb_articles(
            document_id, problem, method, evidence, conclusions, limitations,
            card_json, deep_read_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(document_id) DO UPDATE SET
            problem = excluded.problem,
            method = excluded.method,
            evidence = excluded.evidence,
            conclusions = excluded.conclusions,
            limitations = excluded.limitations,
            card_json = excluded.card_json,
            deep_read_at = excluded.deep_read_at
        """,
        (
            document_id,
            problem,
            method,
            _dumps(evidence),
            _dumps(conclusions),
            _dumps(limitations),
            _dumps(card),
            iso(utcnow()),
        ),
    )
    conn.commit()

def replace_chunks(
    conn: sqlite3.Connection, document_id: int, chunks: list[dict]
) -> list[int]:
    """重建某篇文章的切块，返回与入参顺序一致的 chunk id。"""
    conn.execute("DELETE FROM claims WHERE document_id = ?", (document_id,))
    conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
    ids: list[int] = []
    for chunk in chunks:
        cur = conn.execute(
            "INSERT INTO chunks(document_id, section, ord, text, anchor) VALUES(?, ?, ?, ?, ?)",
            (
                document_id,
                chunk.get("section"),
                int(chunk["ord"]),
                chunk["text"],
                chunk.get("anchor"),
            ),
        )
        ids.append(int(cur.lastrowid))
    conn.commit()
    return ids

def insert_claims(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    chunk_id: int | None,
    claims: list[dict],
) -> int:
    count = 0
    for claim in claims:
        text = str(claim.get("text") or "").strip()
        if not text:
            continue
        conn.execute(
            "INSERT INTO claims(document_id, chunk_id, kind, text) VALUES(?, ?, ?, ?)",
            (document_id, chunk_id, str(claim.get("kind") or "finding"), text),
        )
        count += 1
    conn.commit()
    return count

def get_kb_article(
    conn: sqlite3.Connection, document_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM kb_articles WHERE document_id = ?",
        (document_id,),
    ).fetchone()

def list_chunks(conn: sqlite3.Connection, document_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM chunks WHERE document_id = ? ORDER BY ord",
        (document_id,),
    ).fetchall()

def list_claims(conn: sqlite3.Connection, document_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM claims WHERE document_id = ? ORDER BY id",
        (document_id,),
    ).fetchall()

def _kb_filter_sql(
    *,
    only_saved: bool,
    dimension: str | None,
    kind: str | None,
    status: str | None,
) -> tuple[str, list]:
    clauses = [
        "d.merged_into IS NULL",
        "d.status != 'disliked'",
        "(k.document_id IS NOT NULL OR i.id IS NOT NULL)",
    ]
    if only_saved or status == "saved":
        clauses.append("i.id IS NOT NULL")
    if status == "deep":
        clauses.append("k.document_id IS NOT NULL")
    params: list = []
    if dimension:
        clauses.append("d.primary_tag = ?")
        params.append(dimension)
    if kind:
        clauses.append("d.kind = ?")
        params.append(kind)
    return " AND ".join(clauses), params

def kb_entries(
    conn: sqlite3.Connection,
    *,
    limit: int = 10,
    offset: int = 0,
    only_saved: bool = False,
    dimension: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    since: str | None = None,
) -> list[sqlite3.Row]:
    """知识库视图：已精读 ∪ 已收藏；only_saved 时只看收藏。"""
    where, params = _kb_filter_sql(
        only_saved=only_saved, dimension=dimension, kind=kind, status=status
    )
    having = ""
    if since:
        having = (
            "HAVING MAX(COALESCE(k.deep_read_at, ''), COALESCE(MAX(i.created_at), '')) >= ?"
        )
        params.append(since)
    params.extend([limit, offset])
    return conn.execute(
        f"""
        SELECT d.*,
               k.deep_read_at AS deep_read_at,
               MAX(i.created_at) AS saved_at,
               CASE WHEN k.document_id IS NULL THEN 0 ELSE 1 END AS deep_read
        FROM documents d
        LEFT JOIN kb_articles k ON k.document_id = d.id
        LEFT JOIN interactions i ON i.document_id = d.id AND i.action = 'save'
        WHERE {where}
        GROUP BY d.id
        {having}
        ORDER BY MAX(COALESCE(k.deep_read_at, ''), COALESCE(MAX(i.created_at), '')) DESC
        LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()

def kb_counts(
    conn: sqlite3.Connection,
    *,
    only_saved: bool = False,
    dimension: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    since: str | None = None,
) -> dict[str, int]:
    """知识库规模：精读数、仅收藏数、总数（都受筛选条件影响）。"""
    where, params = _kb_filter_sql(
        only_saved=only_saved, dimension=dimension, kind=kind, status=status
    )
    having = ""
    if since:
        having = (
            "HAVING MAX(COALESCE(k.deep_read_at, ''), COALESCE(MAX(i.created_at), '')) >= ?"
        )
        params.append(since)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS total, COALESCE(SUM(deep_read), 0) AS deep
        FROM (
          SELECT d.id AS id,
                 CASE WHEN k.document_id IS NULL THEN 0 ELSE 1 END AS deep_read
          FROM documents d
          LEFT JOIN kb_articles k ON k.document_id = d.id
          LEFT JOIN interactions i ON i.document_id = d.id AND i.action = 'save'
          WHERE {where}
          GROUP BY d.id
          {having}
        )
        """,
        params,
    ).fetchone()
    total = int(row["total"])
    deep = int(row["deep"])
    return {"deep": deep, "only_saved": total - deep, "total": total}

def kb_facets(conn: sqlite3.Connection, *, only_saved: bool = False) -> dict[str, list[str]]:
    """当前知识库里实际出现过的维度与类型，用于生成筛选按钮。"""
    where, params = _kb_filter_sql(
        only_saved=only_saved, dimension=None, kind=None, status=None
    )
    rows = conn.execute(
        f"""
        SELECT d.primary_tag AS tag, d.kind AS kind
        FROM documents d
        LEFT JOIN kb_articles k ON k.document_id = d.id
        LEFT JOIN interactions i ON i.document_id = d.id AND i.action = 'save'
        WHERE {where}
        GROUP BY d.id
        """,
        params,
    ).fetchall()
    return {
        "dimensions": sorted({row["tag"] for row in rows if row["tag"]}),
        "kinds": sorted({row["kind"] for row in rows if row["kind"]}),
    }

def favorite_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """收藏夹规模：收藏总数，其中已精读多少。"""
    return kb_counts(conn, only_saved=True)

def kb_document_ids(conn: sqlite3.Connection) -> set[int]:
    return {
        int(row["document_id"])
        for row in conn.execute("SELECT document_id FROM kb_articles")
    }

def saved_document_ids(conn: sqlite3.Connection) -> set[int]:
    return {
        int(row["document_id"])
        for row in conn.execute(
            "SELECT DISTINCT document_id FROM interactions WHERE action = 'save'"
        )
    }

def is_saved(conn: sqlite3.Connection, document_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM interactions WHERE document_id = ? AND action = 'save' LIMIT 1",
        (document_id,),
    ).fetchone()
    return row is not None

def remove_kb_article(conn: sqlite3.Connection, document_id: int) -> None:
    """软删除：删知识卡片及其派生数据，保留原文文件，并标记已读不再推送。"""
    now = iso(utcnow())
    for table in ("claims", "chunks", "kb_vectors", "kb_articles"):
        conn.execute(f"DELETE FROM {table} WHERE document_id = ?", (document_id,))
    conn.execute(
        "DELETE FROM relations WHERE from_doc = ? OR to_doc = ?",
        (document_id, document_id),
    )
    conn.execute(
        "DELETE FROM interactions WHERE document_id = ? AND action = 'save'",
        (document_id,),
    )
    conn.execute(
        """
        UPDATE documents
        SET status = 'read', read_at = COALESCE(read_at, ?)
        WHERE id = ?
        """,
        (now, document_id),
    )
    conn.commit()
