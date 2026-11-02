from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

from ...util import iso, parse_iso, utcnow
from ._common import _dumps


THEME_KEY = "theme:active"
MIN_WEIGHT = 0.1
DEFAULT_HALF_LIFE_DAYS = 7.0


def _clamp_weight(value: float) -> float:
    return max(MIN_WEIGHT, min(1.0, value))


def _read_number(conn: sqlite3.Connection, key: str) -> float | None:
    row = conn.execute(
        "SELECT value_json FROM preferences WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    try:
        return float(json.loads(row["value_json"]))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def get_decayed(
    conn: sqlite3.Connection,
    key: str,
    *,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> float:
    """按半衰期把权重拉回 1.0：越久没被触发，惩罚越淡。

    存储值始终是「当前权重」，衰减只在读取时计算，不写库。
    """
    value = _read_number(conn, key)
    if value is None:
        return 1.0
    row = conn.execute(
        "SELECT updated_at FROM preferences WHERE key = ?", (key,)
    ).fetchone()
    updated = parse_iso(row["updated_at"]) if row is not None else None
    if updated is None:
        return _clamp_weight(value)
    age_days = max(0.0, (utcnow() - updated).total_seconds() / 86400)
    remaining = 0.5 ** (age_days / max(half_life_days, 0.1))
    return _clamp_weight(1.0 - (1.0 - value) * remaining)


def bump(
    conn: sqlite3.Connection,
    key: str,
    factor: float,
    *,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> float:
    """先按衰减后的现值乘上 factor 再落库，返回新权重。"""
    value = _clamp_weight(get_decayed(conn, key, half_life_days=half_life_days) * factor)
    set_preference(conn, key, value)
    return value


def get_counter(conn: sqlite3.Connection, key: str) -> int:
    value = _read_number(conn, key)
    return max(0, int(value)) if value is not None else 0


def increment_counter(conn: sqlite3.Connection, key: str) -> int:
    value = get_counter(conn, key) + 1
    set_preference(conn, key, float(value))
    return value


def reset_counter(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM preferences WHERE key = ?", (key,))
    conn.commit()


def get_preference_weights(conn: sqlite3.Connection) -> dict[str, float]:
    rows = conn.execute("SELECT key, value_json FROM preferences").fetchall()
    result: dict[str, float] = {}
    for row in rows:
        try:
            value = json.loads(row["value_json"])
        except json.JSONDecodeError:
            continue
        if isinstance(value, (int, float)):
            result[row["key"]] = float(value)
    return result

def set_preference(conn: sqlite3.Connection, key: str, value: float) -> None:
    conn.execute(
        """
        INSERT INTO preferences(key, value_json, updated_at) VALUES(?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value_json = excluded.value_json,
            updated_at = excluded.updated_at
        """,
        (key, _dumps(value), iso(utcnow())),
    )
    conn.commit()

def get_text_preference(conn: sqlite3.Connection, key: str) -> str | None:
    """字符串型偏好（如飞书单聊会话 id），与数值权重共用 preferences 表。"""
    row = conn.execute(
        "SELECT value_json FROM preferences WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row["value_json"])
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, str) else None

def set_text_preference(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO preferences(key, value_json, updated_at) VALUES(?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value_json = excluded.value_json,
            updated_at = excluded.updated_at
        """,
        (key, _dumps(value), iso(utcnow())),
    )
    conn.commit()

def update_preference(conn: sqlite3.Connection, key: str, factor: float) -> None:
    current = conn.execute(
        "SELECT value_json FROM preferences WHERE key = ?", (key,)
    ).fetchone()
    value = 1.0
    if current is not None:
        try:
            value = float(json.loads(current["value_json"]))
        except (json.JSONDecodeError, TypeError, ValueError):
            value = 1.0
    value = max(0.1, min(1.0, value * factor))
    conn.execute(
        """
        INSERT INTO preferences(key, value_json, updated_at) VALUES(?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,
                                       updated_at = excluded.updated_at
        """,
        (key, _dumps(value), iso(utcnow())),
    )
    conn.commit()

def recent_dislikes(
    conn: sqlite3.Connection, *, limit: int = 10
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT i.document_id, i.reason, i.created_at, d.title, d.primary_tag
        FROM interactions i
        JOIN documents d ON d.id = i.document_id
        WHERE i.action = 'dislike'
        ORDER BY i.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

def set_theme_preference(
    conn: sqlite3.Connection,
    *,
    keywords: list[str],
    dimension: str | None,
    days: int = 7,
    raw: str = "",
) -> dict:
    """记录用户用自然语言设定的临时推送主题。"""
    payload = {
        "keywords": [word for word in keywords if word.strip()],
        "dimension": dimension or None,
        "expires_at": iso(utcnow() + timedelta(days=max(1, days))),
        "raw": raw,
    }
    conn.execute(
        """
        INSERT INTO preferences(key, value_json, updated_at) VALUES(?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,
                                       updated_at = excluded.updated_at
        """,
        (THEME_KEY, _dumps(payload), iso(utcnow())),
    )
    conn.commit()
    return payload

def get_theme_preference(conn: sqlite3.Connection) -> dict | None:
    """当前生效的临时主题；过期自动清除。"""
    row = conn.execute(
        "SELECT value_json FROM preferences WHERE key = ?", (THEME_KEY,)
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["value_json"])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    expires = parse_iso(payload.get("expires_at"))
    if expires is not None and expires <= utcnow():
        clear_theme_preference(conn)
        return None
    return payload

def clear_theme_preference(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM preferences WHERE key = ?", (THEME_KEY,))
    conn.commit()
