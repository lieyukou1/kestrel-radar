"""反刍选品（FR-16）：把收藏过/精读过的内容，在遗忘曲线上重新送回来一次。

只做选择，不写库、不发消息：
写 interactions(action='recall') 必须在真正投递成功之后（T-06），否则预览、失败重试
都会污染「60 天间隔」与「累计 2 次」这两个状态。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..store import repo
from ..util import iso, parse_iso

LOCAL_TZ = timezone(timedelta(hours=8))
# 动作发生过满 30 天才进候选；同一条两次回看间隔 ≥60 天；每条累计最多 2 次
ENTER_AFTER_DAYS = 30
MIN_GAP_DAYS = 60
MAX_RECALLS = 2
# 「同一主分类连续两天不重复」只看最近两天；不加窗口会把两个月前的分类也挡掉
TAG_REPEAT_WINDOW_DAYS = 2


@dataclass(slots=True)
class RecallCandidate:
    document_id: int
    title: str
    url: str
    primary_tag: str | None
    reason: str
    source_action: str


def pick_recall(conn: sqlite3.Connection, now: datetime) -> RecallCandidate | None:
    """挑一条值得回看的文档；没有合格候选时返回 None，调用方据此不占位。"""
    entered_before = now - timedelta(days=ENTER_AFTER_DAYS)
    gap_cutoff = now - timedelta(days=MIN_GAP_DAYS)
    # 最近两次回看过的主分类都不再选：比「只看昨天」更严，避免连着三天都在同一维度
    recent_tags = set(repo.recent_recall_tags(
        conn, limit=2, since=iso(now - timedelta(days=TAG_REPEAT_WINDOW_DAYS))
    ))

    ranked: list[tuple[int, float, int, RecallCandidate]] = []
    for row in repo.recall_pool(conn):
        document_id = int(row["id"])
        if repo.is_recall_off(conn, document_id):
            continue
        if repo.action_count(conn, document_id, "recall") >= MAX_RECALLS:
            continue
        last_recall = parse_iso(repo.last_action_at(conn, document_id, "recall"))
        if last_recall is not None and last_recall > gap_cutoff:
            continue

        saved_at = parse_iso(row["saved_at"])
        read_at = parse_iso(row["deep_read_at"]) or parse_iso(row["kb_read_at"])
        actions = [moment for moment in (saved_at, read_at) if moment is not None]
        if not actions:
            continue
        last_action = max(actions)
        if last_action > entered_before:
            continue
        if row["primary_tag"] in recent_tags:
            continue

        # 收藏且未精读优先；其余（已精读）排在后面
        tier = 0 if (saved_at is not None and read_at is None) else 1
        source = "save" if saved_at is not None else "deep_read"
        ranked.append((
            tier,
            -last_action.timestamp(),
            document_id,
            RecallCandidate(
                document_id=document_id,
                title=str(row["title"]),
                url=str(row["url"]),
                primary_tag=row["primary_tag"],
                reason=_reason(saved_at or read_at, source),
                source_action=source,
            ),
        ))

    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    return ranked[0][3]


def _reason(when: datetime | None, source: str) -> str:
    if when is None:
        return "你曾经收藏过"
    local = when.astimezone(LOCAL_TZ)
    verb = "收藏" if source == "save" else "精读"
    return f"你 {local.month} 月 {local.day} 日{verb}过"
