from __future__ import annotations

import json
import sqlite3

from ..store import repo

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


def handle_action(conn: sqlite3.Connection, value: dict) -> dict:
    """处理飞书卡片/命令动作，返回给渠道层的指令。"""
    action = value.get("action")
    document_id = int(value.get("document_id") or 0)
    digest_date = value.get("digest_date")

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
