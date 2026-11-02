from __future__ import annotations

import sqlite3

from ...util import utcnow
from .usage import daily_usage


def stats(conn: sqlite3.Connection) -> dict:
    by_status = {
        row["status"]: row["n"]
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM documents GROUP BY status"
        ).fetchall()
    }
    today = utcnow().date().isoformat()
    digest = conn.execute(
        "SELECT * FROM digests WHERE digest_date = ?", (today,)
    ).fetchone()
    runs = conn.execute(
        "SELECT job, status, started_at, finished_at, error FROM runs ORDER BY id DESC LIMIT 5"
    ).fetchall()
    return {
        "documents_by_status": by_status,
        "today_digest": dict(digest) if digest else None,
        "today_llm": daily_usage(conn, today),
        "recent_runs": [dict(row) for row in runs],
    }
