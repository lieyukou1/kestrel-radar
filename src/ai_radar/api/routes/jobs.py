from __future__ import annotations

import threading
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from pydantic import BaseModel

from ...config import load_config
from ...jobs.collect import run_collect
from ...notify_factory import CHANNELS, build_notifier
from ...pipeline.digest import build_digest
from ...store import repo
from ...store.db import connect, init_db
from ..deps import Ctx
from ..serialize import load_json

router = APIRouter(tags=["jobs"])

# 同一时间只跑一个采集/生成任务：两个任务同时写库会互相抢锁，
# 网页上重复点击也容易把同一天的日报重建两次。
_JOB_LOCK = threading.Lock()


class DigestJobRequest(BaseModel):
    date: str | None = None
    kind: str = "daily"
    channel: str = "fake"
    preview: bool = True


def _run_collect_job(root: Path, domain: str) -> None:
    config = load_config(root, domain=domain)
    conn = connect(config.db_path)
    init_db(conn)
    try:
        run_collect(conn, config)
    finally:
        conn.close()


def _run_digest_job(
    root: Path, domain: str, *, date: str | None, kind: str, channel: str, preview: bool
) -> None:
    config = load_config(root, domain=domain)
    conn = connect(config.db_path)
    init_db(conn)
    try:
        build_digest(
            conn,
            config,
            notifier=build_notifier(config, channel),
            digest_date=date,
            kind=kind,
            preview=preview,
        )
    finally:
        conn.close()


def _run_in_background(background: BackgroundTasks, job) -> None:
    def runner() -> None:
        try:
            job()
        finally:
            _JOB_LOCK.release()

    background.add_task(runner)


@router.get("/jobs")
def list_jobs(
    ctx: Ctx,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict]:
    rows = ctx.conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    payload = []
    for row in rows:
        item = dict(row)
        item["stats"] = load_json(item.pop("stats_json", None), {})
        payload.append(item)
    return payload


@router.post("/jobs/collect", status_code=202)
def trigger_collect(
    background: BackgroundTasks,
    request: Request,
    ctx: Ctx,
) -> dict:
    if not _JOB_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有任务在运行，请稍后再试")
    root = Path(request.app.state.root)
    domain = ctx.domain_id
    _run_in_background(background, lambda: _run_collect_job(root, domain))
    return {"status": "started", "job": "collect", "domain": domain}


@router.post("/jobs/digest", status_code=202)
def trigger_digest(
    payload: DigestJobRequest,
    background: BackgroundTasks,
    request: Request,
    ctx: Ctx,
) -> dict:
    if payload.channel not in CHANNELS:
        raise HTTPException(
            status_code=400,
            detail=f"未知渠道：{payload.channel}（可选 {', '.join(CHANNELS)}）",
        )
    if payload.kind not in ("daily", "weekly"):
        raise HTTPException(status_code=400, detail="kind 只能是 daily 或 weekly")
    if not _JOB_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有任务在运行，请稍后再试")
    root = Path(request.app.state.root)
    domain = ctx.domain_id
    _run_in_background(
        background,
        lambda: _run_digest_job(
            root,
            domain,
            date=payload.date,
            kind=payload.kind,
            channel=payload.channel,
            preview=payload.preview,
        ),
    )
    return {
        "status": "started",
        "job": "digest",
        "domain": domain,
        "channel": payload.channel,
        "preview": payload.preview,
    }


@router.get("/status")
def status(ctx: Ctx) -> dict:
    return repo.stats(ctx.conn)
