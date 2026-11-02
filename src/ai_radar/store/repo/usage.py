from __future__ import annotations

import sqlite3

from ...util import iso, utcnow


def record_llm_usage(
    conn: sqlite3.Connection,
    *,
    day: str,
    stage: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_cny: float,
    document_id: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO llm_usage(
            day, stage, model, input_tokens, output_tokens, cost_cny, document_id, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            day,
            stage,
            model,
            input_tokens,
            output_tokens,
            cost_cny,
            document_id,
            iso(utcnow()),
        ),
    )
    conn.commit()

def daily_usage(conn: sqlite3.Connection, day: str) -> dict:
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(cost_cny), 0) AS cost_cny,
            COALESCE(SUM(input_tokens), 0) AS input_tokens,
            COALESCE(SUM(output_tokens), 0) AS output_tokens,
            COUNT(*) AS calls
        FROM llm_usage WHERE day = ?
        """,
        (day,),
    ).fetchone()
    return dict(row)
