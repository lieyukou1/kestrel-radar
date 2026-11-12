from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Query

from ...pipeline.search import search_documents
from ...store import repo
from ..deps import Ctx
from ..serialize import document_dict

router = APIRouter(tags=["query"])


@router.get("/archive")
def archive(
    ctx: Ctx,
    q: str | None = None,
    tag: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    saved: bool | None = None,
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    clauses = ["d.merged_into IS NULL"]
    params: list = []
    if q:
        like = f"%{q.strip()}%"
        clauses.append("(d.title LIKE ? OR d.abstract LIKE ?)")
        params.extend([like, like])
    if tag:
        clauses.append("d.primary_tag = ?")
        params.append(tag)
    if kind:
        clauses.append("d.kind = ?")
        params.append(kind)
    if status:
        clauses.append("d.status = ?")
        params.append(status)
    if saved is not None:
        exists = (
            "EXISTS (SELECT 1 FROM interactions i "
            "WHERE i.document_id = d.id AND i.action = 'save')"
        )
        clauses.append(exists if saved else f"NOT {exists}")
    if date_from:
        clauses.append("COALESCE(d.published_at, d.first_seen_at) >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("COALESCE(d.published_at, d.first_seen_at) <= ?")
        params.append(f"{date_to}T23:59:59")

    where = " AND ".join(clauses)
    total = int(
        ctx.conn.execute(
            f"SELECT COUNT(*) AS n FROM documents d WHERE {where}", params
        ).fetchone()["n"]
    )
    rows = ctx.conn.execute(
        f"""
        SELECT d.* FROM documents d
        WHERE {where}
        ORDER BY COALESCE(d.published_at, d.first_seen_at) DESC, d.id DESC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [document_dict(row) for row in rows],
    }


@router.get("/kb")
def kb(
    ctx: Ctx,
    dimension: str | None = None,
    kind: str | None = None,
    saved: bool = False,
    since: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """知识库视图：已精读 ∪ 已收藏，按最近精读/收藏时间倒序。"""
    counts = repo.kb_counts(
        ctx.conn,
        only_saved=saved,
        dimension=dimension,
        kind=kind,
        since=since,
    )
    facets = repo.kb_facets(ctx.conn, only_saved=saved)
    rows = repo.kb_entries(
        ctx.conn,
        limit=limit,
        offset=offset,
        only_saved=saved,
        dimension=dimension,
        kind=kind,
        since=since,
    )
    items = []
    for row in rows:
        item = document_dict(row)
        item.update(
            {
                "deep_read_at": row["deep_read_at"],
                "saved_at": row["saved_at"],
                "deep_read": int(row["deep_read"] or 0),
            }
        )
        items.append(item)
    return {"counts": counts, "facets": facets, "items": items}


@router.get("/search")
def search(
    ctx: Ctx,
    q: str = Query(min_length=1),
    limit: int = Query(default=5, ge=1, le=20),
    external: bool = True,
) -> dict:
    return asdict(
        search_documents(ctx.conn, ctx.config, q, limit=limit, external=external)
    )
