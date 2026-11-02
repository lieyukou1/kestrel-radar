from __future__ import annotations

import math
import sqlite3

from ..store import repo
from ..util import parse_iso

SOURCE_LABEL = {
    "hackernews": "HN",
    "hf_daily": "HF Daily",
    "arxiv": "arXiv",
    "github": "GitHub",
    "lobsters": "Lobsters",
}

# 每个类型「一天涨多少」算满分动量。repo 用对数增速：0.2 ≈ 每天涨 22%。
DEFAULT_GROWTH_TARGET_PER_DAY = {"repo": 0.2, "forum": 1.0, "paper": 0.3}
BASE_WEIGHT = 0.65
MOMENTUM_WEIGHT = 0.35
MIN_MOMENTUM_SPAN_HOURS = 1.0


def _signal_value(row: sqlite3.Row) -> float:
    points = row["points"] or 0
    comments = row["comments"] or 0
    stars = row["stars"] or 0
    citations = row["citations"] or 0
    return math.log1p(points + 2 * comments + 3 * stars + citations)


def _momentum(rows: list[sqlite3.Row], target_per_day: float) -> float | None:
    """按首尾快照算「每天涨多少」，观测窗口不足时返回 None。"""
    if len(rows) < 2 or target_per_day <= 0:
        return None
    newest, oldest = rows[0], rows[-1]
    first, last = parse_iso(oldest["captured_at"]), parse_iso(newest["captured_at"])
    if first is None or last is None:
        return None
    hours = (last - first).total_seconds() / 3600
    if hours < MIN_MOMENTUM_SPAN_HOURS:
        return None
    per_day = (_signal_value(newest) - _signal_value(oldest)) / (hours / 24)
    return max(0.0, min(1.0, per_day / target_per_day))


def heat_score(
    conn: sqlite3.Connection,
    document_id: int,
    kind: str | None = None,
    weights: dict | None = None,
    *,
    neutral: float = 0.5,
) -> float:
    """热度 = 绝对信号（静态）+ 增速（动态），按来源分别计算后取最高。

    合并文档（例如 arXiv 论文同时被 HN 和 HF Daily 收录）里多个来源的信号量纲不同，
    混在一条序列里算增速会把「来源切换」误当成「热度暴涨」，所以按来源拆开。

    neutral 是「完全没有信号」时的取值：日报按中性分处理，周报要选「真的热过」的，
    传 0 更合适——否则没被收录的论文会白拿 0.5，压在「被收录但偏冷门」的前面。
    """
    grouped = _series_by_source(conn, document_id)
    if not grouped:
        # 中文媒体等来源没有热度信号，按中性分处理，不能等同于「没人看」。
        return neutral

    config = (weights or {}).get("heat") or {}
    targets = config.get("targets_per_day") or DEFAULT_GROWTH_TARGET_PER_DAY
    target = float(targets.get(kind or "", 1.0))
    return max(_source_heat(rows, target, config) for rows in grouped.values())


def _series_by_source(
    conn: sqlite3.Connection, document_id: int
) -> dict[str, list[sqlite3.Row]]:
    """按来源分组，每组保持时间倒序。"""
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in repo.signal_series(conn, document_id):
        grouped.setdefault(str(row["source_id"]), []).append(row)
    return grouped


def _source_heat(rows: list[sqlite3.Row], target: float, config: dict) -> float:
    base = min(1.0, _signal_value(rows[0]) / 7.0)
    momentum = _momentum(rows, target)
    if momentum is None:
        return base
    base_weight = float(config.get("base_weight", BASE_WEIGHT))
    momentum_weight = float(config.get("momentum_weight", MOMENTUM_WEIGHT))
    return min(1.0, base_weight * base + momentum_weight * momentum)


def heat_label(conn: sqlite3.Connection, document_id: int) -> str:
    """一行热度摘要，例如「HN 118 分 · 123 评论」；没有信号时返回空串。"""
    rows = repo.latest_signals(conn, document_id)
    if not rows:
        return ""
    row = rows[0]
    source = str(row["source_id"])
    parts: list[str] = []
    if row["points"]:
        # HF Daily 的 points 是点赞数，HN 的 points 是得分。
        parts.append(f"{row['points']} 赞" if source == "hf_daily" else f"{row['points']} 分")
    if row["comments"]:
        parts.append(f"{row['comments']} 评论")
    if row["stars"]:
        if source == "hf_daily":
            # HF Daily 的 stars 是论文关联仓库的星数，不是论文自己的热度，别显示成裸「★」。
            parts.append(f"关联仓库 {row['stars']:,}★")
        else:
            parts.append(f"{row['stars']:,}★")
    if not parts:
        return ""
    return f"{SOURCE_LABEL.get(source, source)} " + " · ".join(parts)


def repo_trend(conn: sqlite3.Connection, document_id: int) -> dict:
    """GitHub 仓库的观测趋势：快照数、观测时长、日增星与日增百分比。

    只看 github 来源，避免与同一文档上的 HN / HF 信号混在一起。
    """
    rows = [row for row in repo.signal_series(conn, document_id) if row["source_id"] == "github"]
    if not rows:
        return {
            "observations": 0,
            "span_hours": 0.0,
            "stars": 0,
            "stars_per_day": 0.0,
            "pct_per_day": 0.0,
        }
    newest, oldest = rows[0], rows[-1]
    first, last = parse_iso(oldest["captured_at"]), parse_iso(newest["captured_at"])
    hours = 0.0
    if first is not None and last is not None:
        hours = max(0.0, (last - first).total_seconds() / 3600)
    days = hours / 24 if hours > 0 else 0.0
    stars_new = newest["stars"] or 0
    stars_old = oldest["stars"] or 0
    delta = stars_new - stars_old
    return {
        "observations": len(rows),
        "span_hours": round(hours, 2),
        "stars": stars_new,
        "stars_delta": delta,
        "stars_per_day": round(delta / days, 1) if days > 0 else 0.0,
        "pct_per_day": (
            round(100 * delta / max(1, stars_old) / days, 2) if days > 0 else 0.0
        ),
    }


def repo_gate_ok(
    conn: sqlite3.Connection, document_id: int, gate: dict | None
) -> bool:
    """GitHub 双层校验：星数达标 + 多次观测下的增速达标。"""
    gate = gate or {}
    trend = repo_trend(conn, document_id)
    if trend["observations"] < int(gate.get("min_observations", 3)):
        return False
    if trend["span_hours"] < float(gate.get("min_span_hours", 1.5)):
        return False
    if trend["stars"] < int(gate.get("min_stars", 200)):
        return False
    if trend["stars_per_day"] < float(gate.get("min_stars_per_day", 20)):
        return False
    if trend["pct_per_day"] < float(gate.get("min_pct_per_day", 2)):
        return False
    return True
