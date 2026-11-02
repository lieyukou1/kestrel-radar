"""维度轮转：让每天的主线在 8 个维度之间转起来。

口径（PRD §10.4）：
- 赤字按「席位」算：`(期望席位 - 实际席位) / 期望席位`，夹在 [-1, 1]；
- 主线只在头名 85% 分带内轮换，避免为了补维度去推明显冷门的话题；
- 互动只做 ±20% 的微调，并用 Beta 平滑，避免只推过一次、点开一次就是 100%；
- 前 N 次日报只记录、不生效（冷启动），期间统计照常累积。

本模块只负责算账，不参与选择；`composer` 拿 `RotationState` 加偏置，
`preferences` 拿 `format_rotation()` 展示。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..config import AppConfig
from ..util import iso, utcnow
from .preferences import paused_dimensions
from .tagging import TAGS

UNKNOWN_TAG = "未分类"


@dataclass(slots=True)
class DimensionStat:
    tag: str
    exposure: int = 0
    reads: int = 0
    saves: int = 0
    deep_reads: int = 0
    dislikes: int = 0
    candidates: int = 0
    rate: float = 0.0
    item_deficit: float = 0.0
    theme_deficit: float = 0.0
    weight: float = 1.0
    theme_weight: float = 1.0
    paused: bool = False


@dataclass(slots=True)
class RotationState:
    active: bool
    recorded_pushes: int
    required_pushes: int
    stats: dict[str, DimensionStat] = field(default_factory=dict)

    def item_weight(self, tag: str | None) -> float:
        stat = self.stats.get(str(tag or ""))
        return stat.weight if stat is not None else 1.0

    def theme_weight(self, tag: str | None) -> float:
        stat = self.stats.get(str(tag or ""))
        return stat.theme_weight if stat is not None else 1.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _item_counts(conn: sqlite3.Connection, window: int) -> dict[str, int]:
    """最近 window 条推送按维度计数；周报也算曝光，但不参与主线轮转。"""
    rows = conn.execute(
        """
        SELECT tag, COUNT(1) AS n FROM (
          SELECT COALESCE(d.primary_tag, ?) AS tag
          FROM digest_items i
          JOIN digests g ON g.id = i.digest_id
          JOIN documents d ON d.id = i.document_id
          WHERE g.sent_at IS NOT NULL
          ORDER BY g.sent_at DESC, i.rank ASC
          LIMIT ?
        )
        GROUP BY tag
        """,
        (UNKNOWN_TAG, window),
    ).fetchall()
    return {str(row["tag"]): int(row["n"]) for row in rows}


def _theme_counts(conn: sqlite3.Connection, window: int) -> dict[str, int]:
    """最近 window 次日报的主线维度；主线才是「今天讲哪个方向」的单位。"""
    rows = conn.execute(
        """
        SELECT dimension AS tag, COUNT(1) AS n FROM (
          SELECT t.dimension AS dimension
          FROM topics t
          JOIN digests g ON g.id = t.digest_id
          WHERE g.sent_at IS NOT NULL
            AND t.kind = 'daily'
            AND t.dimension IS NOT NULL
            AND t.dimension != ''
          ORDER BY t.topic_date DESC, t.id DESC
          LIMIT ?
        )
        GROUP BY tag
        """,
        (window,),
    ).fetchall()
    return {str(row["tag"]): int(row["n"]) for row in rows}


def _engagement(
    conn: sqlite3.Connection, since_iso: str
) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    exposure_rows = conn.execute(
        """
        SELECT COALESCE(d.primary_tag, ?) AS tag, COUNT(1) AS n
        FROM digest_items i
        JOIN digests g ON g.id = i.digest_id
        JOIN documents d ON d.id = i.document_id
        WHERE g.sent_at IS NOT NULL AND g.sent_at >= ?
        GROUP BY tag
        """,
        (UNKNOWN_TAG, since_iso),
    ).fetchall()
    exposure = {str(row["tag"]): int(row["n"]) for row in exposure_rows}

    action_rows = conn.execute(
        """
        SELECT COALESCE(d.primary_tag, ?) AS tag, i.action AS action, COUNT(1) AS n
        FROM interactions i
        JOIN documents d ON d.id = i.document_id
        WHERE i.created_at >= ?
        GROUP BY tag, action
        """,
        (UNKNOWN_TAG, since_iso),
    ).fetchall()
    actions: dict[str, dict[str, int]] = {}
    for row in action_rows:
        actions.setdefault(str(row["tag"]), {})[str(row["action"])] = int(row["n"])
    return exposure, actions


def _candidate_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT COALESCE(primary_tag, ?) AS tag, COUNT(1) AS n
        FROM documents
        WHERE merged_into IS NULL AND status IN ('observing', 'eligible')
        GROUP BY tag
        """,
        (UNKNOWN_TAG,),
    ).fetchall()
    return {str(row["tag"]): int(row["n"]) for row in rows}


def _recorded_pushes(conn: sqlite3.Connection) -> int:
    """冷启动按「已推送的日报次数」算，而不是自然日：周末不推不该算数。"""
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT digest_date) AS n
        FROM digests
        WHERE sent_at IS NOT NULL AND kind = 'daily'
        """
    ).fetchone()
    return int(row["n"] or 0)


def _deficits(counts: dict[str, int], active: list[str]) -> dict[str, float]:
    """期望席位按启用维度平分；暂停的维度不参与，免得永远补不上。"""
    total = sum(counts.get(tag, 0) for tag in active)
    expected = total / len(active) if active else 0.0
    if expected <= 0:
        return {tag: 0.0 for tag in active}
    return {
        tag: _clamp((expected - counts.get(tag, 0)) / expected, -1.0, 1.0)
        for tag in active
    }


def _engagement_score(actions: dict[str, int]) -> float:
    """精读与收藏比打开更能说明「这篇真有用」。"""
    return (
        actions.get("read", 0)
        + 2 * actions.get("deep_read", 0)
        + 2 * actions.get("save", 0)
        - actions.get("dislike", 0)
    )


def rotation_state(
    conn: sqlite3.Connection,
    config: AppConfig | None = None,
    *,
    now: datetime | None = None,
) -> RotationState:
    now = now or utcnow()
    weights = (config.weights if config is not None else {}) or {}
    cfg = weights.get("rotation") or {}
    item_window = int(cfg.get("item_window", 30))
    theme_window = int(cfg.get("theme_window", 10))
    learning_days = int(cfg.get("learning_days", 14))
    max_adjust = float(cfg.get("max_adjust", 0.2))

    paused = paused_dimensions(conn)
    active = [tag for tag in TAGS if tag not in paused]

    item_counts = _item_counts(conn, item_window)
    theme_counts = _theme_counts(conn, theme_window)
    exposure, actions = _engagement(conn, iso(now - timedelta(days=learning_days)))
    candidates = _candidate_counts(conn)
    recorded = _recorded_pushes(conn)

    item_deficit = _deficits(item_counts, active)
    theme_deficit = _deficits(theme_counts, active)

    total_exposure = sum(exposure.get(tag, 0) for tag in active)
    total_score = sum(_engagement_score(actions.get(tag, {})) for tag in active)
    global_rate = total_score / total_exposure if total_exposure > 0 else 0.0

    enabled = bool(cfg.get("enabled", True))
    active_now = enabled and recorded >= learning_days

    state = RotationState(
        active=active_now,
        recorded_pushes=recorded,
        required_pushes=learning_days,
    )
    for tag in TAGS:
        stat = DimensionStat(
            tag=tag,
            exposure=int(exposure.get(tag, 0)),
            reads=int((actions.get(tag) or {}).get("read", 0)),
            saves=int((actions.get(tag) or {}).get("save", 0)),
            deep_reads=int((actions.get(tag) or {}).get("deep_read", 0)),
            dislikes=int((actions.get(tag) or {}).get("dislike", 0)),
            candidates=int(candidates.get(tag, 0)),
            paused=tag in paused,
            item_deficit=round(item_deficit.get(tag, 0.0), 3),
            theme_deficit=round(theme_deficit.get(tag, 0.0), 3),
        )

        if stat.paused:
            state.stats[tag] = stat
            continue

        raw = _engagement_score(actions.get(tag, {}))
        # Beta 平滑：先验取全库均值，样本少时乘子不会乱跳。
        stat.rate = (
            (raw + 2 * global_rate) / (stat.exposure + 2)
            if stat.exposure or raw
            else global_rate
        )

        if not active_now:
            state.stats[tag] = stat
            continue

        item_factor = 1.0 + max_adjust * stat.item_deficit
        engagement_factor = 1.0
        if global_rate > 0 and stat.exposure > 0:
            ratio = _clamp(stat.rate / global_rate - 1.0, -1.0, 1.0)
            engagement_factor = 1.0 + max_adjust * ratio
        stat.weight = round(
            _clamp(item_factor * engagement_factor, 1 - max_adjust, 1 + max_adjust), 3
        )
        # 主线只看赤字：互动信号回答「想不想多看」，不回答「今天该讲哪个方向」。
        stat.theme_weight = round(
            _clamp(1.0 + max_adjust * stat.theme_deficit, 1 - max_adjust, 1 + max_adjust),
            3,
        )
        state.stats[tag] = stat

    return state


def format_rotation(state: RotationState) -> str:
    if state.active:
        header = f"维度轮转（已生效，样本：最近 {state.recorded_pushes} 次日报）"
    else:
        header = (
            f"维度轮转（只记录，已 {state.recorded_pushes}/{state.required_pushes} 次日报）"
        )
    lines = [header]
    for tag in TAGS:
        stat = state.stats.get(tag)
        if stat is None:
            continue
        parts = [
            f"曝光 {stat.exposure}",
            f"打开 {stat.reads}",
            f"精读 {stat.deep_reads}",
            f"收藏 {stat.saves}",
            f"候选 {stat.candidates}",
            f"赤字 {stat.item_deficit:+.2f}",
            f"主线 {stat.theme_deficit:+.2f}",
            f"乘子 {stat.weight:.2f}",
        ]
        line = f"{tag}｜" + " · ".join(parts)
        if stat.paused:
            line += "（已暂停）"
        lines.append(line)
    return "\n".join(lines)
