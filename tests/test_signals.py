from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from ai_radar.pipeline.signals import heat_label, heat_score, repo_gate_ok, repo_trend
from ai_radar.store import repo
from ai_radar.store.db import connect, init_db
from ai_radar.util import iso, utcnow

GATE = {
    "min_stars": 200,
    "min_observations": 3,
    "min_span_hours": 2,
    "min_stars_per_day": 20,
    "min_pct_per_day": 1.0,
}


def make_repo(tmp_path: Path, *, stars: int) -> tuple:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = repo.insert_document(
        conn,
        canonical_id=f"github:owner/repo-{stars}",
        kind="repo",
        title="owner/repo",
        abstract="desc",
        url=f"https://github.com/owner/repo-{stars}",
        published_at=iso(utcnow() - timedelta(days=10)),
        authority="B",
        primary_tag="1 模型与架构",
        tags=["1 模型与架构", "repo"],
    )
    return conn, document_id


def add_star_history(conn, document_id: int, values: list[int], step_hours: float) -> None:
    start = utcnow() - timedelta(hours=step_hours * (len(values) - 1))
    for index, stars in enumerate(values):
        captured = iso(start + timedelta(hours=step_hours * index))
        conn.execute(
            "INSERT INTO signals(document_id, source_id, captured_at, stars, extra_json)"
            " VALUES(?, 'github', ?, ?, '{}')",
            (document_id, captured, stars),
        )


def test_heat_does_not_collapse_when_signals_are_flat(tmp_path: Path) -> None:
    conn, document_id = make_repo(tmp_path, stars=1000)
    add_star_history(conn, document_id, [1000, 1000, 1000], step_hours=1)
    heat = heat_score(conn, document_id, "repo")
    assert heat > 0.5


def test_heat_does_not_mix_sources(tmp_path: Path) -> None:
    conn, document_id = make_repo(tmp_path, stars=1)
    add_star_history(conn, document_id, [1, 1], step_hours=1)
    # 同一条目先有 HN 信号、后有 GitHub 信号：来源切换不能被当成热度暴涨。
    conn.execute(
        "INSERT INTO signals(document_id, source_id, captured_at, points, extra_json)"
        " VALUES(?, 'hackernews', ?, 1, '{}')",
        (document_id, iso(utcnow() - timedelta(hours=1))),
    )
    assert heat_score(conn, document_id, "repo") < 0.45


def test_heat_neutral_score_is_configurable(tmp_path: Path) -> None:
    """周报要选真的热过的内容：没有信号时按 0 算，不能白拿 0.5 压过冷门内容。"""
    conn, document_id = make_repo(tmp_path, stars=100)
    assert heat_score(conn, document_id, "repo") == 0.5
    assert heat_score(conn, document_id, "repo", neutral=0.0) == 0.0


def test_heat_label_marks_hf_repo_stars(tmp_path: Path) -> None:
    """HF Daily 的 stars 是论文关联仓库的星数，不能显示成论文自己的 ★。"""
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    paper_id = repo.insert_document(
        conn,
        canonical_id="arxiv:2601.00002",
        kind="paper",
        title="A Paper",
        abstract="abstract",
        url="https://arxiv.org/abs/2601.00002",
        published_at=iso(utcnow()),
        authority="A",
        primary_tag="1 模型与架构",
        tags=["1 模型与架构"],
    )
    conn.execute(
        "INSERT INTO signals(document_id, source_id, captured_at, points, comments, stars, extra_json)"
        " VALUES(?, 'hf_daily', ?, 12, 3, 563, '{}')",
        (paper_id, iso(utcnow())),
    )
    conn.commit()

    label = heat_label(conn, paper_id)
    assert "12 赞" in label
    assert "关联仓库 563★" in label


def test_repo_gate_needs_enough_observations(tmp_path: Path) -> None:
    conn, document_id = make_repo(tmp_path, stars=500)
    add_star_history(conn, document_id, [500, 600], step_hours=1)
    assert repo_gate_ok(conn, document_id, GATE) is False


def test_repo_gate_accepts_fast_growth(tmp_path: Path) -> None:
    conn, document_id = make_repo(tmp_path, stars=500)
    add_star_history(conn, document_id, [500, 520, 545, 570], step_hours=1)
    trend = repo_trend(conn, document_id)
    assert trend["observations"] == 4
    assert trend["stars_per_day"] >= 20
    assert repo_gate_ok(conn, document_id, GATE) is True


def test_repo_gate_rejects_slow_growth(tmp_path: Path) -> None:
    conn, document_id = make_repo(tmp_path, stars=5000)
    add_star_history(conn, document_id, [5000, 5000, 5001, 5001], step_hours=1)
    assert repo_gate_ok(conn, document_id, GATE) is False
