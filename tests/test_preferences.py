from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from ai_radar.store import repo
from ai_radar.store.db import connect, init_db
from ai_radar.util import iso, utcnow


def _age(conn, key: str, days: float) -> None:
    """把偏好记录的更新时间往前拨，模拟时间流逝。"""
    conn.execute(
        "UPDATE preferences SET updated_at = ? WHERE key = ?",
        (iso(utcnow() - timedelta(days=days)), key),
    )
    conn.commit()


def test_missing_key_is_neutral(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    assert repo.get_decayed(conn, "entity:不存在") == 1.0


def test_decay_returns_halfway_after_one_half_life(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    repo.set_preference(conn, "dimension:3 推理与加速", 0.5)

    # 衰减用真实时钟计算，写入到读取之间的毫秒差会让结果略高于 0.5。
    assert repo.get_decayed(conn, "dimension:3 推理与加速") == pytest.approx(0.5, abs=1e-3)
    _age(conn, "dimension:3 推理与加速", 7)
    assert repo.get_decayed(conn, "dimension:3 推理与加速") == pytest.approx(0.75, abs=1e-3)


def test_decay_approaches_neutral_over_time(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    repo.set_preference(conn, "source:hackernews", 0.5)
    _age(conn, "source:hackernews", 70)
    assert repo.get_decayed(conn, "source:hackernews") > 0.99


def test_bump_uses_decayed_value_and_clamps(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    repo.set_preference(conn, "source:量子位", 0.5)
    _age(conn, "source:量子位", 7)

    value = repo.bump(conn, "source:量子位", 0.85)
    assert value == pytest.approx(0.6375, abs=1e-3)
    assert repo.get_preference_weights(conn)["source:量子位"] == pytest.approx(0.6375, abs=1e-3)

    for _ in range(20):
        repo.bump(conn, "source:量子位", 0.5)
    assert repo.get_decayed(conn, "source:量子位") >= repo.MIN_WEIGHT


def test_counters(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    key = "less:count:source:量子位"

    assert repo.get_counter(conn, key) == 0
    assert repo.increment_counter(conn, key) == 1
    assert repo.increment_counter(conn, key) == 2
    assert repo.get_counter(conn, key) == 2

    repo.reset_counter(conn, key)
    assert repo.get_counter(conn, key) == 0
