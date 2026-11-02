from __future__ import annotations

import sqlite3

from ..config import AppConfig
from ..store import repo

PAUSE_PREFIX = "paused:"
PAUSE_THRESHOLD = 0.5


def paused_keys(conn: sqlite3.Connection) -> set[str]:
    values = repo.get_preference_weights(conn)
    return {
        key
        for key, value in values.items()
        if key.startswith(PAUSE_PREFIX) and value >= PAUSE_THRESHOLD
    }


def paused_dimensions(conn: sqlite3.Connection) -> set[str]:
    prefix = f"{PAUSE_PREFIX}dimension:"
    return {key[len(prefix):] for key in paused_keys(conn) if key.startswith(prefix)}


def paused_sources(conn: sqlite3.Connection) -> set[str]:
    prefix = f"{PAUSE_PREFIX}source:"
    return {key[len(prefix):] for key in paused_keys(conn) if key.startswith(prefix)}


def pause_source(conn: sqlite3.Connection, source_id: str) -> None:
    repo.set_preference(conn, f"{PAUSE_PREFIX}source:{source_id}", 1.0)


def resume_source(conn: sqlite3.Connection, source_id: str) -> None:
    repo.set_preference(conn, f"{PAUSE_PREFIX}source:{source_id}", 0.0)


def pause_dimension(conn: sqlite3.Connection, tag: str) -> None:
    repo.set_preference(conn, f"{PAUSE_PREFIX}dimension:{tag}", 1.0)


def resume_dimension(conn: sqlite3.Connection, tag: str) -> None:
    repo.set_preference(conn, f"{PAUSE_PREFIX}dimension:{tag}", 0.0)


def resolve_dimension(conn: sqlite3.Connection, token: str) -> str | None:
    """支持完整标签或编号前缀，例如 "4" 或 "4 Agent 与应用"。"""
    token = token.strip()
    if not token:
        return None
    tags = [
        row["primary_tag"]
        for row in conn.execute(
            "SELECT DISTINCT primary_tag FROM documents WHERE primary_tag IS NOT NULL"
        )
    ]
    if token in tags:
        return token
    matches = [tag for tag in tags if tag.split()[0] == token]
    if len(matches) == 1:
        return matches[0]
    matches = [tag for tag in tags if tag.startswith(token)]
    return matches[0] if len(matches) == 1 else None


def format_preferences(
    conn: sqlite3.Connection,
    config: AppConfig | None = None,
    *,
    limit: int = 8,
) -> str:
    values = repo.get_preference_weights(conn)
    dimension = sorted(
        (key[len("dimension:"):], value)
        for key, value in values.items()
        if key.startswith("dimension:") and abs(value - 1.0) > 1e-9
    )
    type_weights = sorted(
        (key[len("type:"):], value)
        for key, value in values.items()
        if key.startswith("type:") and abs(value - 1.0) > 1e-9
    )
    authority = sorted(
        (key[len("authority:"):], value)
        for key, value in values.items()
        if key.startswith("authority:") and abs(value - 1.0) > 1e-9
    )

    lines = ["偏好设置"]
    lines.append(
        "维度权重：" + ("、".join(f"{tag} ×{value:.2f}" for tag, value in dimension) or "暂无调整")
    )
    lines.append(
        "类型权重：" + ("、".join(f"{tag} ×{value:.2f}" for tag, value in type_weights) or "暂无调整")
    )
    lines.append(
        "权威权重：" + ("、".join(f"{tag} ×{value:.2f}" for tag, value in authority) or "暂无调整")
    )

    dims = sorted(paused_dimensions(conn))
    sources = sorted(paused_sources(conn))
    lines.append("暂停维度：" + ("、".join(dims) or "无"))
    lines.append("暂停来源：" + ("、".join(sources) or "无"))

    theme_pref = repo.get_theme_preference(conn)
    if theme_pref:
        keywords = "、".join(theme_pref.get("keywords") or []) or "无"
        dimension_hint = theme_pref.get("dimension") or "不限维度"
        expires = str(theme_pref.get("expires_at") or "")[:10]
        lines.append(f"临时主题：{keywords}（{dimension_hint}，{expires} 到期）")

    dislikes = repo.recent_dislikes(conn, limit=limit)
    if dislikes:
        lines.append("最近负反馈：")
        for row in dislikes:
            day = str(row["created_at"])[:10]
            lines.append(f"- {day} {row['reason'] or '未填原因'} · {row['title'][:40]}")
    else:
        lines.append("最近负反馈：无")

    # 延迟导入：rotation 依赖本模块的 paused_dimensions，模块级导入会循环。
    from .rotation import format_rotation, rotation_state

    lines.append("")
    lines.append(format_rotation(rotation_state(conn, config)))

    lines.append("调整：/pause dimension 4；/resume dimension 4；/pause source hackernews")
    return "\n".join(lines)
