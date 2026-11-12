from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..domain.profile import DomainProfile


def load_json(value: Any, default: Any = None) -> Any:
    """数据库里的 JSON 字段统一走这里；坏数据返回默认值，不让接口 500。"""
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _string_list(value: Any) -> list[str]:
    data = load_json(value, [])
    if isinstance(data, list):
        return [str(item) for item in data]
    return []


def document_dict(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "kind": row["kind"],
        "title": row["title"],
        "abstract": row["abstract"],
        "url": row["url"],
        "published_at": row["published_at"],
        "first_seen_at": row["first_seen_at"],
        "authority": row["authority"],
        "status": row["status"],
        "tag": row["primary_tag"],
        "tags": _string_list(row["tags_json"]),
        "read": bool(row["read_at"]),
        "read_at": row["read_at"],
        "read_count": int(row["read_count"] or 0),
        "local_path": row["local_path"],
    }


def digest_dict(row: sqlite3.Row) -> dict:
    stats = load_json(row["stats_json"], {}) or {}
    return {
        "date": row["digest_date"],
        "kind": row["kind"],
        "status": row["status"],
        "created_at": row["created_at"],
        "sent_at": row["sent_at"],
        "count": stats.get("count"),
        "events_count": stats.get("events_count"),
        "theme": stats.get("theme", ""),
        "stats": stats,
    }


def topic_dict(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "label": row["label"] or "",
        "summary": row["summary"] or "",
        "entity": row["entity"] or "",
        "dimension": row["dimension"],
        "score": float(row["score"] or 0),
        "item_count": int(row["item_count"] or 0),
        "event_date": row["event_date"],
        "event_key": row["event_key"],
        "first_date": row["first_date"],
        "day_index": int(row["day_index"] or 1),
        "new_count": int(row["new_count"] or 0),
    }


def profile_dict(profile: DomainProfile) -> dict:
    return {
        "id": profile.id,
        "name": profile.name,
        "taxonomy": [
            {"name": item.name, "description": item.description}
            for item in profile.taxonomy
        ],
        "defaults": profile.defaults,
    }
