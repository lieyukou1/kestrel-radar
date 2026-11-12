from __future__ import annotations

from dataclasses import asdict, is_dataclass

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ...llm.client import BudgetExceeded, LLMError
from ...pipeline.preferences import (
    pause_dimension,
    pause_source,
    paused_dimensions,
    paused_sources,
    resume_dimension,
    resume_source,
)
from ...pipeline.qa import answer_question
from ...store import repo
from ..deps import ApiContext, Ctx
from ..serialize import profile_dict

router = APIRouter(tags=["assistant"])


class QARequest(BaseModel):
    question: str = Field(min_length=2)
    keywords: list[str] | None = None


class SettingsRequest(BaseModel):
    paused_dimensions: list[str] | None = None
    paused_sources: list[str] | None = None


def _settings_payload(ctx: ApiContext) -> dict:
    profile = ctx.config.domain
    return {
        "domain": profile_dict(profile) if profile else None,
        "paused_dimensions": sorted(paused_dimensions(ctx.conn)),
        "paused_sources": sorted(paused_sources(ctx.conn)),
        "preferences": repo.get_preference_weights(ctx.conn),
        "theme": repo.get_theme_preference(ctx.conn),
    }


@router.post("/qa")
def ask(payload: QARequest, ctx: Ctx) -> dict:
    try:
        result = answer_question(
            ctx.conn, ctx.config, payload.question, keywords=payload.keywords
        )
    except (LLMError, BudgetExceeded) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    related = result.get("related")
    if related:
        result["related"] = [
            asdict(item) if is_dataclass(item) else item for item in related
        ]
    return result


@router.get("/settings")
def read_settings(ctx: Ctx) -> dict:
    return _settings_payload(ctx)


@router.put("/settings")
def update_settings(payload: SettingsRequest, ctx: Ctx) -> dict:
    if payload.paused_dimensions is not None:
        known = (
            {item.name for item in ctx.config.domain.taxonomy}
            if ctx.config.domain
            else set()
        )
        wanted = set(payload.paused_dimensions)
        if known:
            unknown = wanted - known
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail=f"未知分类：{', '.join(sorted(unknown))}",
                )
        current = paused_dimensions(ctx.conn)
        for tag in current - wanted:
            resume_dimension(ctx.conn, tag)
        for tag in wanted - current:
            pause_dimension(ctx.conn, tag)

    if payload.paused_sources is not None:
        wanted_sources = set(payload.paused_sources)
        current_sources = paused_sources(ctx.conn)
        for source_id in current_sources - wanted_sources:
            resume_source(ctx.conn, source_id)
        for source_id in wanted_sources - current_sources:
            pause_source(ctx.conn, source_id)

    return _settings_payload(ctx)
