from __future__ import annotations

import json
import sqlite3
import unicodedata

from ..store import repo
from ..util import parse_iso, utcnow

DISLIKE_REASONS = [
    "维度不相关",
    "太偏理论",
    "太偏工程",
    "质量低",
    "已读过",
    "来源不信任",
    "其他",
]


def _digest_id(conn: sqlite3.Connection, digest_date: str | None) -> int | None:
    if not digest_date:
        return None
    row = repo.get_digest(conn, digest_date)
    return int(row["id"]) if row else None


def record_dislike(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    reason: str,
    digest_date: str | None = None,
) -> None:
    digest_id = _digest_id(conn, digest_date)
    repo.add_interaction(
        conn,
        document_id=document_id,
        digest_id=digest_id,
        action="dislike",
        reason=reason,
    )

    document = repo.get_document(conn, document_id)
    if document is None:
        return

    if reason == "维度不相关" and document["primary_tag"]:
        repo.update_preference(conn, f"dimension:{document['primary_tag']}", 0.90)
    elif reason in ("太偏理论", "太偏工程"):
        repo.update_preference(conn, f"type:{document['kind']}", 0.97)
    elif reason == "质量低":
        repo.update_preference(conn, f"type:{document['kind']}", 0.95)
    elif reason == "来源不信任":
        repo.update_preference(conn, f"authority:{document['authority']}", 0.90)

    repo.mark_status(conn, [document_id], "disliked")


def format_favorites(conn: sqlite3.Connection, *, limit: int = 20) -> str:
    rows = repo.list_favorites(conn, limit=limit)
    if not rows:
        return "收藏夹为空。"
    lines = [f"收藏夹（{len(rows)}）"]
    for index, row in enumerate(rows, start=1):
        lines.append(f"{index}. {row['title']}")
        lines.append(f"   {row['primary_tag'] or '未分类'} · {row['url']}")
    return "\n".join(lines)


def format_status(conn: sqlite3.Connection) -> str:
    return json.dumps(repo.stats(conn), ensure_ascii=False, indent=2, default=str)


def _display_width(text: str) -> int:
    """中文按两列宽算，否则表格会错位。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [_display_width(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], _display_width(cell))

    def render(cells: list[str]) -> str:
        padded = [
            cell + " " * (widths[index] - _display_width(cell))
            for index, cell in enumerate(cells)
        ]
        return "  ".join(padded).rstrip()

    lines = [render(headers), "  ".join("-" * width for width in widths)]
    lines.extend(render([str(cell) for cell in row]) for row in rows)
    return "\n".join(lines)


def _short_time(value: str | None) -> str:
    at = parse_iso(value)
    return at.strftime("%m-%d %H:%M") if at else "—"


def _source_state(last_fetched_at: str | None, enabled: bool, now) -> str:
    if not enabled:
        return "已停用"
    at = parse_iso(last_fetched_at)
    if at is None:
        return "未采集"
    hours = (now - at).total_seconds() / 3600
    if hours >= 48:
        return "停滞"
    if hours >= 24:
        return "偏慢"
    return "正常"


def _truncate(text: str, width: int = 48) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def format_status_table(stats: dict) -> str:
    """把 stats 渲染成给人看的表格；空库也要能正常输出。"""
    now = utcnow()
    lines: list[str] = []

    lines.append("== 文档状态 ==")
    by_status = stats.get("documents_by_status") or {}
    rows = [[str(key), str(value)] for key, value in sorted(by_status.items())]
    lines.append(_table(["状态", "数量"], rows or [["（空）", "0"]]))

    lines.append("")
    lines.append("== 今日 ==")
    digest = stats.get("today_digest") or {}
    if digest:
        lines.append(
            f"推送：{digest.get('kind', '-')} · {digest.get('status', '-')} · "
            f"{_json(digest.get('stats_json')).get('count', '-')} 条"
        )
    else:
        lines.append("推送：今天还没有 digest")
    today_llm = stats.get("today_llm") or {}
    lines.append(
        f"模型：{today_llm.get('calls', 0)} 次调用，"
        f"¥{float(today_llm.get('cost_cny') or 0):.4f}"
    )

    window = stats.get("last_7_days") or {}
    days = window.get("window_days", 7)
    lines.append("")
    lines.append(f"== 最近 {days} 天 · 信源 ==")
    sources = window.get("sources") or []
    source_rows = [
        [
            str(item.get("source_id", "")),
            _truncate(item.get("name", ""), 16),
            str(item.get("kind", "")),
            str(item.get("items_in_window", 0)),
            _short_time(item.get("last_fetched_at")),
            _source_state(item.get("last_fetched_at"), item.get("enabled", True), now),
        ]
        for item in sources
    ]
    lines.append(
        _table(
            ["source_id", "名称", "kind", "7日新增", "最近采集(UTC)", "状态"],
            source_rows or [["（空）", "", "", "0", "—", "未采集"]],
        )
    )

    lines.append("")
    lines.append(f"== 最近 {days} 天 · 模型 ==")
    stages = window.get("llm_by_stage") or []
    stage_rows = [
        [
            str(item.get("stage", "")),
            str(item.get("calls", 0)),
            f"¥{float(item.get('cost_cny') or 0):.4f}",
        ]
        for item in stages
    ]
    lines.append(_table(["stage", "调用", "花费"], stage_rows or [["（无）", "0", "¥0.0000"]]))
    lines.append(
        f"合计：{window.get('llm_calls', 0)} 次，"
        f"¥{float(window.get('llm_total_cny') or 0):.4f}"
    )

    lines.append("")
    lines.append(f"== 最近 {days} 天 · 推送 ==")
    digests = window.get("digests") or []
    digest_rows = [
        [
            str(item.get("date", "")),
            str(item.get("kind", "")),
            str(item.get("status", "")),
            str(item.get("count") if item.get("count") is not None else "-"),
        ]
        for item in digests
    ]
    lines.append(_table(["日期", "类型", "状态", "条数"], digest_rows or [["（无）", "", "", "-"]]))

    lines.append("")
    lines.append(f"== 最近 {days} 天 · 失败任务 ==")
    failures = window.get("failed_runs") or []
    failure_rows = [
        [
            str(item.get("job", "")),
            _short_time(item.get("started_at")),
            _truncate(item.get("error", "")),
        ]
        for item in failures
    ]
    lines.append(_table(["job", "开始(UTC)", "错误"], failure_rows or [["（无）", "—", ""]]))

    return "\n".join(lines)


def _json(value) -> dict:
    try:
        data = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def handle_action(conn: sqlite3.Connection, value: dict) -> dict:
    """处理飞书卡片/命令动作，返回给渠道层的指令。"""
    action = value.get("action")
    document_id = int(value.get("document_id") or 0)
    digest_date = value.get("digest_date")

    if action == "event_detail":
        event_id = int(value.get("event_id") or 0)
        if event_id <= 0 or repo.get_topic(conn, event_id) is None:
            return {"type": "text", "text": "事件不存在或已过期。"}
        return {
            "type": "event_card",
            "event_id": event_id,
            "digest_date": str(digest_date or ""),
        }

    if action == "topic_search":
        query = " ".join(str(value.get("query") or "").split())
        if len(query) < 2:
            return {"type": "text", "text": "用法：/topic <关键词>，例如 /topic 强化学习"}
        return {"type": "topic_card", "query": query}

    if action == "compare":
        left_id = int(value.get("left_id") or 0)
        right_id = int(value.get("right_id") or 0)
        if (
            left_id <= 0
            or right_id <= 0
            or repo.get_document(conn, left_id) is None
            or repo.get_document(conn, right_id) is None
        ):
            return {"type": "text", "text": "对比的文章不存在。"}
        return {"type": "compare_card", "left_id": left_id, "right_id": right_id}

    if action in (
        "read",
        "save",
        "unsave",
        "dislike",
        "dislike_reason",
        "detail",
        "deep_read",
        "related",
    ):
        if document_id <= 0 or repo.get_document(conn, document_id) is None:
            return {"type": "text", "text": "这是测试卡片，未关联真实文章。"}

    if action == "related" and document_id:
        return {"type": "related_card", "document_id": document_id}

    if action == "deep_read" and document_id:
        if repo.get_kb_article(conn, document_id) is not None:
            return {
                "type": "text",
                "text": "这篇已经精读过了，可以用「相关」查看同主题文章。",
            }
        digest_id = _digest_id(conn, digest_date)
        repo.add_interaction(
            conn,
            document_id=document_id,
            digest_id=digest_id,
            action="deep_read",
        )
        repo.mark_status(conn, [document_id], "deep_reading")
        return {
            "type": "text",
            "text": "已加入精读队列，完成后会推送深度讲解。",
        }

    if action == "detail" and document_id:
        digest_id = _digest_id(conn, digest_date)
        if repo.add_interaction(
            conn,
            document_id=document_id,
            digest_id=digest_id,
            action="read",
        ):
            repo.mark_read(conn, document_id)
        return {
            "type": "detail_card",
            "document_id": document_id,
            "digest_date": digest_date,
        }

    if action == "read" and document_id:
        digest_id = _digest_id(conn, digest_date)
        if repo.add_interaction(
            conn,
            document_id=document_id,
            digest_id=digest_id,
            action="read",
        ):
            repo.mark_read(conn, document_id)
        return {"type": "text", "text": "已标记为已读。"}

    if action == "save" and document_id:
        digest_id = _digest_id(conn, digest_date)
        created = repo.add_interaction(
            conn,
            document_id=document_id,
            digest_id=digest_id,
            action="save",
        )
        return {
            "type": "text",
            "text": "已加入知识库，可用 /kb 查看。" if created else "已经在知识库里了。",
        }

    if action == "unsave" and document_id:
        repo.remove_interaction(conn, document_id=document_id, action="save")
        return {"type": "text", "text": "已取消收藏。"}

    if action == "kb_remove" and document_id:
        if repo.get_kb_article(conn, document_id) is not None:
            # 已精读的要二次确认，避免手滑删掉卡片。
            return {"type": "remove_confirm_card", "document_id": document_id}
        if repo.is_saved(conn, document_id):
            repo.remove_interaction(conn, document_id=document_id, action="save")
            return {"type": "text", "text": "已移出知识库。"}
        return {"type": "text", "text": "这篇不在知识库里。"}

    if action == "kb_remove_confirm" and document_id:
        repo.remove_kb_article(conn, document_id)
        return {"type": "text", "text": "已移出知识库，原文文件保留。"}

    if action == "kb_remove_cancel":
        return {"type": "text", "text": "已取消，文章仍在知识库里。"}

    if action == "dislike" and document_id:
        return {
            "type": "reason_card",
            "document_id": document_id,
            "digest_date": digest_date,
        }

    if action == "dislike_reason" and document_id:
        reason = str(value.get("reason") or "其他")
        record_dislike(
            conn,
            document_id=document_id,
            reason=reason,
            digest_date=digest_date,
        )
        return {"type": "text", "text": f"已记录：不感兴趣（{reason}），后续会降低同类内容权重。"}

    if action == "qa_common":
        question = str(value.get("question") or "").strip()
        if not question:
            return {"type": "text", "text": "问题为空。"}
        return {"type": "qa_common", "question": question}

    if action in ("favorites", "kb"):
        payload = {
            "scope": "favorites" if action == "favorites" else "kb",
            "filters": value.get("filters") or {},
            "page": int(value.get("page") or 0),
        }
        return {
            "type": "favorites_card" if action == "favorites" else "kb_card",
            **payload,
        }

    if action == "status":
        return {"type": "text", "text": format_status(conn)}

    return {"type": "text", "text": "未识别的操作。"}
