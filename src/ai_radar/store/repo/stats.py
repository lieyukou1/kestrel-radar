from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

from ...util import iso, utcnow
from .usage import daily_usage


def _json(value) -> dict:
    try:
        data = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def observability(conn: sqlite3.Connection, *, days: int = 7) -> dict:
    """最近 N 天的运行状况：哪个源哑了、钱花在哪、推了几次。

    只读查询，供 `status` 使用；空库也要能正常返回空结构。
    """
    now = utcnow()
    since = iso(now - timedelta(days=days))
    since_day = (now - timedelta(days=days)).date().isoformat()

    sources = []
    for row in conn.execute(
        "SELECT id, kind, name, enabled FROM sources ORDER BY id"
    ).fetchall():
        latest = conn.execute(
            "SELECT MAX(fetched_at) AS at FROM raw_items WHERE source_id = ?",
            (row["id"],),
        ).fetchone()["at"]
        recent = conn.execute(
            "SELECT COUNT(*) AS n FROM raw_items WHERE source_id = ? AND fetched_at >= ?",
            (row["id"], since),
        ).fetchone()["n"]
        sources.append(
            {
                "source_id": row["id"],
                "kind": row["kind"],
                "name": row["name"],
                "enabled": bool(row["enabled"]),
                "last_fetched_at": latest,
                "items_in_window": recent,
            }
        )

    llm_by_stage = [
        {
            "stage": row["stage"],
            "calls": row["calls"],
            "cost_cny": round(float(row["cost_cny"] or 0), 4),
        }
        for row in conn.execute(
            """
            SELECT stage, COUNT(*) AS calls, COALESCE(SUM(cost_cny), 0) AS cost_cny
            FROM llm_usage WHERE day >= ? GROUP BY stage ORDER BY cost_cny DESC
            """,
            (since_day,),
        ).fetchall()
    ]

    digests = []
    for row in conn.execute(
        """
        SELECT digest_date, kind, status, sent_at, stats_json
        FROM digests WHERE digest_date >= ? ORDER BY digest_date DESC, kind
        """,
        (since_day,),
    ).fetchall():
        stats = _json(row["stats_json"])
        digests.append(
            {
                "date": row["digest_date"],
                "kind": row["kind"],
                "status": row["status"],
                "sent_at": row["sent_at"],
                "count": stats.get("count"),
            }
        )

    failed_runs = [
        {
            "job": row["job"],
            "started_at": row["started_at"],
            "status": row["status"],
            "error": row["error"],
        }
        for row in conn.execute(
            "SELECT job, started_at, status, error FROM runs "
            "WHERE started_at >= ? AND status NOT IN ('success', 'running') "
            "ORDER BY id DESC LIMIT 10",
            (since,),
        ).fetchall()
    ]

    return {
        "window_days": days,
        "llm_total_cny": round(sum(item["cost_cny"] for item in llm_by_stage), 4),
        "llm_calls": sum(item["calls"] for item in llm_by_stage),
        "sources": sources,
        "llm_by_stage": llm_by_stage,
        "digests": digests,
        "failed_runs": failed_runs,
    }


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
        "last_7_days": observability(conn),
    }
