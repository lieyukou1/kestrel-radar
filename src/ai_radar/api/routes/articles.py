from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ...pipeline.interactions import record_dislike
from ...store import repo
from ..deps import ApiContext, Ctx
from ..serialize import document_dict, load_json

router = APIRouter(tags=["articles"])

SUPPORTED_ACTIONS = ("read", "save", "unsave", "dislike", "deep_read")


class ActionRequest(BaseModel):
    action: str = Field(description="read / save / unsave / dislike / deep_read")
    reason: str | None = Field(default=None, description="dislike 的原因")
    digest_date: str | None = Field(default=None, description="关联的日报日期")


def _digest_id(ctx: ApiContext, digest_date: str | None) -> int | None:
    if not digest_date:
        return None
    row = repo.get_digest(ctx.conn, digest_date)
    return int(row["id"]) if row else None


def _state(ctx: ApiContext, document_id: int) -> dict:
    document = repo.get_document(ctx.conn, document_id)
    return {
        "document": document_dict(document),
        "saved": repo.is_saved(ctx.conn, document_id),
        "read": bool(document["read_at"]),
        "status": document["status"],
    }


@router.get("/articles/{document_id}")
def get_article(document_id: int, ctx: Ctx) -> dict:
    row = repo.get_document(ctx.conn, document_id)
    if row is None:
        raise HTTPException(status_code=404, detail="文章不存在")

    kb = repo.get_kb_article(ctx.conn, document_id)
    sources = [
        {"source_id": item["source_id"], "name": item["name"], "kind": item["kind"]}
        for item in ctx.conn.execute(
            """
            SELECT DISTINCT r.source_id, s.name, s.kind
            FROM document_sources ds
            JOIN raw_items r ON r.id = ds.raw_item_id
            JOIN sources s ON s.id = r.source_id
            WHERE ds.document_id = ?
            ORDER BY s.name
            """,
            (document_id,),
        ).fetchall()
    ]
    appearances = [
        {
            "digest_date": item["digest_date"],
            "kind": item["kind"],
            "label": item["label"],
            "section": item["section"],
        }
        for item in ctx.conn.execute(
            """
            SELECT g.digest_date, g.kind, t.label, di.section
            FROM digest_items di
            JOIN digests g ON g.id = di.digest_id
            LEFT JOIN topic_items ti ON ti.document_id = di.document_id
            LEFT JOIN topics t ON t.id = ti.topic_id
            WHERE di.document_id = ?
            ORDER BY g.digest_date DESC, di.id DESC
            LIMIT 20
            """,
            (document_id,),
        ).fetchall()
    ]
    return {
        "document": document_dict(row),
        "summary": repo.find_latest_summary(ctx.conn, document_id),
        "kb": load_json(kb["card_json"], {}) if kb is not None else None,
        "claims": [
            {"kind": item["kind"], "text": item["text"]}
            for item in repo.list_claims(ctx.conn, document_id)
        ],
        "notes": [
            {"content": item["content"], "created_at": item["created_at"]}
            for item in repo.list_notes(ctx.conn, document_id)
        ],
        "sources": sources,
        "appearances": appearances,
        "saved": repo.is_saved(ctx.conn, document_id),
        "read": bool(row["read_at"]),
    }


@router.post("/articles/{document_id}/actions")
def act(
    document_id: int,
    payload: ActionRequest,
    ctx: Ctx,
) -> dict:
    if repo.get_document(ctx.conn, document_id) is None:
        raise HTTPException(status_code=404, detail="文章不存在")
    action = payload.action
    if action not in SUPPORTED_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的动作：{action}，可选 {', '.join(SUPPORTED_ACTIONS)}",
        )

    digest_id = _digest_id(ctx, payload.digest_date)
    if action == "read":
        repo.add_interaction(
            ctx.conn, document_id=document_id, digest_id=digest_id, action="read"
        )
        repo.mark_read(ctx.conn, document_id)
    elif action == "save":
        repo.add_interaction(
            ctx.conn, document_id=document_id, digest_id=digest_id, action="save"
        )
    elif action == "unsave":
        repo.remove_interaction(ctx.conn, document_id=document_id, action="save")
    elif action == "dislike":
        record_dislike(
            ctx.conn,
            document_id=document_id,
            reason=payload.reason or "其他",
            digest_date=payload.digest_date,
        )
    elif action == "deep_read":
        repo.add_interaction(
            ctx.conn, document_id=document_id, digest_id=digest_id, action="deep_read"
        )
        repo.mark_status(ctx.conn, [document_id], "deep_reading")

    return {"ok": True, "action": action, **_state(ctx, document_id)}
