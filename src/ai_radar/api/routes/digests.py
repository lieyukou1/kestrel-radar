from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException, Query

from ...store import repo
from ..deps import Ctx
from ..serialize import digest_dict, document_dict, load_json, topic_dict

router = APIRouter(tags=["digests"])


def _summary(conn: sqlite3.Connection, digest_id: int, document_id: int) -> dict | None:
    if digest_id <= 0:
        return None
    row = repo.get_digest_item(conn, digest_id, document_id)
    if row is None:
        return None
    value = load_json(row["summary_json"], {})
    return value if isinstance(value, dict) and value else None


def _event(conn: sqlite3.Connection, digest_id: int, topic: sqlite3.Row) -> dict:
    items = []
    for row in repo.topic_items(conn, int(topic["id"])):
        document_id = int(row["id"])
        item = document_dict(row)
        item["angle"] = row["angle"]
        item["item_rank"] = int(row["item_rank"] or 0)
        item["summary"] = _summary(conn, digest_id, document_id)
        item["saved"] = repo.is_saved(conn, document_id)
        items.append(item)
    payload = topic_dict(topic)
    payload["items"] = items
    return payload


def _topics(conn: sqlite3.Connection, digest_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM topics WHERE digest_id = ? ORDER BY score DESC, id",
        (digest_id,),
    ).fetchall()


@router.get("/digests")
def list_digests(
    ctx: Ctx,
    limit: int = Query(30, ge=1, le=200),
    kind: str | None = None,
) -> list[dict]:
    sql = "SELECT * FROM digests"
    params: list = []
    if kind:
        sql += " WHERE kind = ?"
        params.append(kind)
    sql += " ORDER BY digest_date DESC, id DESC LIMIT ?"
    params.append(limit)
    return [digest_dict(row) for row in ctx.conn.execute(sql, params).fetchall()]


@router.get("/digests/{date}")
def get_digest(
    date: str,
    ctx: Ctx,
    kind: str | None = None,
) -> dict:
    row = repo.get_digest(ctx.conn, date, kind)
    if row is None:
        raise HTTPException(status_code=404, detail=f"没有 {date} 的日报")

    digest_id = int(row["id"])
    events = [_event(ctx.conn, digest_id, topic) for topic in _topics(ctx.conn, digest_id)]
    # 周报的论文/仓库分组不属于事件，单独返回，网页端按 section 渲染。
    briefs = [
        {**document_dict(item), "section": item["section"], "reason": item["reason"]}
        for item in ctx.conn.execute(
            """
            SELECT di.section, di.reason, d.*
            FROM digest_items di
            JOIN documents d ON d.id = di.document_id
            WHERE di.digest_id = ?
              AND di.section IN ('weekly_paper', 'weekly_repo')
            ORDER BY di.rank
            """,
            (digest_id,),
        ).fetchall()
    ]
    return {"digest": digest_dict(row), "events": events, "briefs": briefs}


@router.get("/events")
def list_events(
    ctx: Ctx,
    date: str | None = None,
    kind: str | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    if date is None:
        row = ctx.conn.execute("SELECT MAX(topic_date) AS day FROM topics").fetchone()
        date = row["day"] if row else None
    if not date:
        return {"date": None, "events": []}

    sql = "SELECT * FROM topics WHERE topic_date = ?"
    params: list = [date]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY score DESC, id LIMIT ?"
    params.append(limit)
    topics = ctx.conn.execute(sql, params).fetchall()

    events = []
    for topic in topics:
        digest_id = int(topic["digest_id"]) if topic["digest_id"] else 0
        events.append(_event(ctx.conn, digest_id, topic))
    return {"date": date, "events": events}


@router.get("/events/{topic_id}")
def get_event(topic_id: int, ctx: Ctx) -> dict:
    topic = repo.get_topic(ctx.conn, topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="事件不存在")
    digest_id = int(topic["digest_id"]) if topic["digest_id"] else 0
    return _event(ctx.conn, digest_id, topic)
