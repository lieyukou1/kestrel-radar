"""精读产物与检索单元。

阅读层和检索层共用同一套结构：章节、主张、证据、主张关系、检索单元。
不再为检索另建一套文档摘要。
"""

from __future__ import annotations

import json
import sqlite3

from ...util import iso, utcnow
from ._common import _dumps


def _loads(value, default):
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


# ---------- 阅读结构 ----------


def replace_sections(
    conn: sqlite3.Connection, document_id: int, sections: list[dict]
) -> list[int]:
    conn.execute("DELETE FROM sections WHERE document_id = ?", (document_id,))
    ids: list[int] = []
    for index, item in enumerate(sections, start=1):
        cur = conn.execute(
            """
            INSERT INTO sections(document_id, ord, heading, role, summary, anchor)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                int(item.get("ord") or index),
                str(item.get("heading") or ""),
                str(item.get("role") or "other"),
                str(item.get("summary") or ""),
                str(item.get("anchor") or ""),
            ),
        )
        ids.append(int(cur.lastrowid))
    conn.commit()
    return ids


def replace_claims(
    conn: sqlite3.Connection, document_id: int, claims: list[dict]
) -> list[int]:
    """重建主张；连带清掉旧证据与主张关系，保证重读幂等。"""
    conn.execute("DELETE FROM evidence WHERE document_id = ?", (document_id,))
    conn.execute(
        "DELETE FROM claim_relations WHERE from_claim_id IN "
        "(SELECT id FROM claims WHERE document_id = ?) "
        "OR to_claim_id IN (SELECT id FROM claims WHERE document_id = ?)",
        (document_id, document_id),
    )
    conn.execute("DELETE FROM claims WHERE document_id = ?", (document_id,))
    ids: list[int] = []
    for item in claims:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        cur = conn.execute(
            """
            INSERT INTO claims(
                document_id, chunk_id, section_id, kind, text, claim_type,
                confidence, quote, anchor, question, status, retrieval_text
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                item.get("chunk_id"),
                item.get("section_id"),
                str(item.get("kind") or item.get("claim_type") or "finding"),
                text,
                str(item.get("claim_type") or "sub"),
                str(item.get("confidence") or "source"),
                str(item.get("quote") or ""),
                str(item.get("anchor") or ""),
                str(item.get("question") or ""),
                str(item.get("status") or "active"),
                str(item.get("retrieval_text") or text),
            ),
        )
        ids.append(int(cur.lastrowid))
    conn.commit()
    return ids


def insert_evidence(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    claim_id: int,
    items: list[dict],
) -> int:
    count = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        quote = str(item.get("quote") or "").strip()
        if not quote and not item.get("value"):
            continue
        conn.execute(
            """
            INSERT INTO evidence(
                claim_id, document_id, kind, quote, anchor, metric, value,
                unit, baseline, condition, sample
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                claim_id,
                document_id,
                str(item.get("kind") or "reasoning"),
                quote,
                str(item.get("anchor") or ""),
                str(item.get("metric") or ""),
                str(item.get("value") or ""),
                str(item.get("unit") or ""),
                str(item.get("baseline") or ""),
                str(item.get("condition") or ""),
                str(item.get("sample") or ""),
            ),
        )
        count += 1
    conn.commit()
    return count


def replace_claim_relations(
    conn: sqlite3.Connection, document_id: int, relations: list[dict]
) -> int:
    conn.execute(
        "DELETE FROM claim_relations WHERE from_claim_id IN "
        "(SELECT id FROM claims WHERE document_id = ?) "
        "OR to_claim_id IN (SELECT id FROM claims WHERE document_id = ?)",
        (document_id, document_id),
    )
    count = 0
    for item in relations:
        source = item.get("from_claim_id")
        target = item.get("to_claim_id")
        if not source or not target or int(source) == int(target):
            continue
        conn.execute(
            """
            INSERT INTO claim_relations(from_claim_id, to_claim_id, relation, note)
            VALUES(?, ?, ?, ?)
            """,
            (
                int(source),
                int(target),
                str(item.get("relation") or "supports"),
                str(item.get("note") or ""),
            ),
        )
        count += 1
    conn.commit()
    return count


def save_reading(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    reading: dict,
    problem: str,
    method: str,
    evidence: list[str],
    conclusions: list[str],
    limitations: list[str],
) -> None:
    """写 kb_articles；旧字段保留，供既有页面和问答降级使用。"""
    conn.execute(
        """
        INSERT INTO kb_articles(
            document_id, problem, method, evidence, conclusions, limitations,
            card_json, deep_read_at, thesis, narrative, outline_json,
            questions_json, critique_json, reading_json
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(document_id) DO UPDATE SET
            problem = excluded.problem,
            method = excluded.method,
            evidence = excluded.evidence,
            conclusions = excluded.conclusions,
            limitations = excluded.limitations,
            card_json = excluded.card_json,
            deep_read_at = excluded.deep_read_at,
            thesis = excluded.thesis,
            narrative = excluded.narrative,
            outline_json = excluded.outline_json,
            questions_json = excluded.questions_json,
            critique_json = excluded.critique_json,
            reading_json = excluded.reading_json
        """,
        (
            document_id,
            problem,
            method,
            _dumps(evidence),
            _dumps(conclusions),
            _dumps(limitations),
            _dumps(reading),
            iso(utcnow()),
            str(reading.get("thesis") or ""),
            str(reading.get("narrative") or ""),
            _dumps(reading.get("outline") or []),
            _dumps(reading.get("questions") or []),
            _dumps(reading.get("critique") or {}),
            _dumps(reading),
        ),
    )
    conn.commit()


def list_sections(conn: sqlite3.Connection, document_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sections WHERE document_id = ? ORDER BY ord",
        (document_id,),
    ).fetchall()


def list_evidence(conn: sqlite3.Connection, document_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM evidence WHERE document_id = ? ORDER BY id",
        (document_id,),
    ).fetchall()


def list_claim_relations(
    conn: sqlite3.Connection, document_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT r.* FROM claim_relations r
        JOIN claims c ON c.id = r.from_claim_id
        WHERE c.document_id = ?
        ORDER BY r.id
        """,
        (document_id,),
    ).fetchall()


# ---------- 检索单元 ----------


def replace_search_units(
    conn: sqlite3.Connection, document_id: int, units: list[dict]
) -> list[int]:
    conn.execute(
        "DELETE FROM unit_vectors WHERE unit_id IN "
        "(SELECT id FROM search_units WHERE document_id = ?)",
        (document_id,),
    )
    conn.execute("DELETE FROM search_units WHERE document_id = ?", (document_id,))
    ids: list[int] = []
    now = iso(utcnow())
    for item in units:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        cur = conn.execute(
            """
            INSERT INTO search_units(
                document_id, unit_type, unit_id, text, anchor, metadata_json, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                str(item.get("unit_type") or "doc"),
                int(item.get("unit_id") or document_id),
                text,
                str(item.get("anchor") or ""),
                _dumps(item.get("metadata") or {}),
                now,
            ),
        )
        ids.append(int(cur.lastrowid))
    conn.commit()
    return ids


def list_search_units(
    conn: sqlite3.Connection,
    *,
    unit_types: tuple[str, ...] | None = None,
    document_ids: list[int] | None = None,
    tag: str | None = None,
    kind: str | None = None,
    limit: int = 5000,
) -> list[sqlite3.Row]:
    clauses = ["d.merged_into IS NULL", "d.status != 'disliked'"]
    params: list = []
    if unit_types:
        clauses.append(f"u.unit_type IN ({','.join('?' for _ in unit_types)})")
        params.extend(unit_types)
    if document_ids:
        clauses.append(f"u.document_id IN ({','.join('?' for _ in document_ids)})")
        params.extend(document_ids)
    if tag:
        clauses.append("d.primary_tag = ?")
        params.append(tag)
    if kind:
        clauses.append("d.kind = ?")
        params.append(kind)
    params.append(limit)
    return conn.execute(
        f"""
        SELECT u.*, d.title, d.url, d.primary_tag, d.kind, d.authority,
               d.published_at, d.first_seen_at
        FROM search_units u
        JOIN documents d ON d.id = u.document_id
        WHERE {' AND '.join(clauses)}
        ORDER BY u.id
        LIMIT ?
        """,
        params,
    ).fetchall()


def save_unit_vector(
    conn: sqlite3.Connection,
    *,
    unit_id: int,
    model: str,
    vector: list[float],
) -> None:
    conn.execute(
        """
        INSERT INTO unit_vectors(unit_id, model, dim, vector_json, created_at)
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(unit_id, model) DO UPDATE SET
            dim = excluded.dim,
            vector_json = excluded.vector_json,
            created_at = excluded.created_at
        """,
        (unit_id, model, len(vector), _dumps(vector), iso(utcnow())),
    )
    conn.commit()


def list_unit_vectors(
    conn: sqlite3.Connection, model: str, unit_ids: list[int] | None = None
) -> dict[int, list[float]]:
    clauses = ["model = ?"]
    params: list = [model]
    if unit_ids:
        clauses.append(f"unit_id IN ({','.join('?' for _ in unit_ids)})")
        params.extend(unit_ids)
    rows = conn.execute(
        f"SELECT unit_id, vector_json FROM unit_vectors WHERE {' AND '.join(clauses)}",
        params,
    ).fetchall()
    result: dict[int, list[float]] = {}
    for row in rows:
        try:
            value = json.loads(row["vector_json"])
        except (TypeError, ValueError):
            continue
        if isinstance(value, list) and value:
            result[int(row["unit_id"])] = value
    return result


def units_missing_vectors(
    conn: sqlite3.Connection, model: str, *, limit: int = 200
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT u.* FROM search_units u
        LEFT JOIN unit_vectors v ON v.unit_id = u.id AND v.model = ?
        WHERE v.unit_id IS NULL
        ORDER BY u.id
        LIMIT ?
        """,
        (model, limit),
    ).fetchall()


def unit_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT unit_type, COUNT(*) AS n FROM search_units GROUP BY unit_type"
    ).fetchall()
    return {str(row["unit_type"]): int(row["n"]) for row in rows}


def read_card(conn: sqlite3.Connection, document_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM kb_articles WHERE document_id = ?", (document_id,)
    ).fetchone()
    if row is None:
        return {}
    return _loads(row["reading_json"], {}) or {}
