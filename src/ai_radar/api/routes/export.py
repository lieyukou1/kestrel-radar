"""导出与备份。

日报导出直接复用 `load_digest` 的取材结果，内容与网页端看到的一致；
数据库备份走 SQLite 的在线备份接口，不在文件层面直接拷贝 WAL 库。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from starlette.background import BackgroundTask

from ...util import utcnow
from ..deps import Ctx
from .digests import load_digest

router = APIRouter(tags=["export"])

SECTION_TITLES = {
    "weekly_paper": "本周论文",
    "weekly_repo": "本周仓库",
}


def _item_markdown(item: dict, heading: str) -> list[str]:
    lines = [f"#### {heading}", ""]
    if item.get("tag"):
        lines.append(f"- 维度：{item['tag']}")
    if item.get("published_at"):
        lines.append(f"- 发布：{str(item['published_at'])[:10]}")
    summary = item.get("summary") or {}
    if summary.get("why"):
        lines.append(f"- 为什么值得关注：{summary['why']}")
    if summary.get("problem"):
        lines.append(f"- 问题：{summary['problem']}")
    if summary.get("method"):
        lines.append(f"- 方法：{summary['method']}")
    for point in summary.get("conclusions") or []:
        lines.append(f"- 结论：{point}")
    for point in summary.get("points") or []:
        lines.append(f"- 社区观点：{point}")
    if not summary and item.get("abstract"):
        lines.append(f"- 摘要：{item['abstract']}")
    lines.append(f"- 链接：{item['url']}")
    lines.append("")
    return lines


def digest_markdown(payload: dict) -> str:
    digest = payload["digest"]
    events = payload["events"]
    briefs = payload["briefs"]
    title = "Kestrel Radar 周报" if digest.get("kind") == "weekly" else "Kestrel Radar"
    lines = [f"# {title} · {digest.get('date', '')}", ""]

    meta = [
        f"{digest.get('events_count') or 0} 个事件",
        f"{digest.get('count') or 0} 篇内容",
    ]
    if digest.get("theme"):
        meta.append(f"主线：{digest['theme']}")
    lines.extend([" · ".join(meta), ""])

    if not events and not briefs:
        lines.extend(["这一天没有内容。", ""])

    for index, event in enumerate(events, start=1):
        lines.extend([f"## {index}. {event.get('label') or '未命名事件'}", ""])
        if event.get("summary"):
            lines.extend([str(event["summary"]), ""])
        extras = [f"{event.get('item_count') or 0} 篇"]
        if event.get("event_date"):
            extras.append(f"事件时间：{event['event_date']}")
        if int(event.get("day_index") or 1) > 1:
            extras.append(
                f"第 {event['day_index']} 天 · 新增 {event.get('new_count') or 0} 条"
            )
        lines.extend([f"- {' · '.join(extras)}", ""])
        for item_index, item in enumerate(event.get("items") or [], start=1):
            lines.extend(
                _item_markdown(item, f"{index}.{item_index} {item.get('title', '')}")
            )

    if briefs:
        lines.extend(["## 周报分组", ""])
        section = ""
        for item in briefs:
            current = item.get("section") or ""
            if current != section:
                section = current
                lines.extend([f"### {SECTION_TITLES.get(section, section)}", ""])
            lines.extend(_item_markdown(item, str(item.get("title") or "")))

    return "\n".join(lines).rstrip() + "\n"


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


@router.get("/export/digest/{date}")
def export_digest(
    date: str,
    ctx: Ctx,
    format: str = Query("markdown", pattern="^(markdown|json)$"),
    kind: str | None = None,
):
    payload = load_digest(ctx.conn, date, kind)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"没有 {date} 的日报")
    if format == "json":
        return payload

    digest_kind = payload["digest"].get("kind") or "daily"
    filename = f"kestrel-{ctx.domain_id}-{date}-{digest_kind}.md"
    return PlainTextResponse(
        digest_markdown(payload),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/backup")
def export_backup(ctx: Ctx) -> FileResponse:
    fd, path = tempfile.mkstemp(prefix="kestrel-", suffix=".db")
    os.close(fd)
    dest = sqlite3.connect(path)
    try:
        ctx.conn.backup(dest)
    except sqlite3.Error as exc:
        dest.close()
        _remove(path)
        raise HTTPException(status_code=500, detail=f"备份失败：{exc}") from exc
    else:
        dest.close()

    filename = f"kestrel-{ctx.domain_id}-{utcnow().date().isoformat()}.db"
    return FileResponse(
        path,
        filename=filename,
        media_type="application/octet-stream",
        background=BackgroundTask(_remove, path),
    )
