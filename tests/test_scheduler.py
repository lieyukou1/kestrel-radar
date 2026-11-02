from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from ai_radar.adapters.notify.fake import FakeNotifier
from ai_radar.config import AppConfig, load_config
from ai_radar.jobs import scheduler as scheduler_mod
from ai_radar.jobs.scheduler import Scheduler
from ai_radar.pipeline.digest import build_digest
from ai_radar.store import repo
from ai_radar.store.db import connect, init_db
from ai_radar.util import iso, utcnow


class FailingNotifier:
    def send_digest(self, digest_date: str, items, **kwargs) -> str:
        raise RuntimeError("feishu down")


def make_config(tmp_path: Path) -> AppConfig:
    base = load_config()
    return AppConfig(
        root=base.root,
        network=base.network,
        sources=base.sources,
        weights=base.weights,
        db_path=tmp_path / "test.db",
        outbox_dir=tmp_path / "outbox",
        llm=None,
    )


def seed_document(conn) -> int:
    published = iso(utcnow() - timedelta(days=3))
    repo.upsert_source(
        conn,
        source_id="test",
        kind="arxiv",
        name="test",
        authority="A",
        region="overseas",
        config={},
    )
    return repo.insert_document(
        conn,
        canonical_id="test:digest-retry",
        kind="blog",
        title="Retryable blog",
        abstract="abstract",
        url="https://example.com/retry",
        published_at=published,
        authority="A",
        primary_tag="1 模型与架构",
        tags=["1 模型与架构"],
    )


def test_failed_digest_is_rebuilt_on_retry(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    seed_document(conn)

    try:
        build_digest(conn, config, notifier=FailingNotifier(), digest_date="2026-10-21")
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected send failure")

    row = repo.get_digest(conn, "2026-10-21")
    assert row["status"] == "failed"

    result = build_digest(
        conn,
        config,
        notifier=FakeNotifier(config.outbox_dir),
        digest_date="2026-10-21",
    )
    assert result["status"] == "sent"
    assert result["count"] == 1

    again = build_digest(
        conn,
        config,
        notifier=FakeNotifier(config.outbox_dir),
        digest_date="2026-10-21",
    )
    assert again["skipped"] is True


def test_monday_pushes_weekly_then_daily() -> None:
    """周一先周报后日报，其余工作日只有日报，周末不发。"""
    assert Scheduler._digest_kinds(datetime(2026, 11, 2, 10, 0)) == ["weekly", "daily"]
    assert Scheduler._digest_kinds(datetime(2026, 11, 3, 10, 0)) == ["daily"]
    assert Scheduler._digest_kinds(datetime(2026, 10, 31, 10, 0)) == []


def test_same_date_can_hold_weekly_and_daily(tmp_path: Path) -> None:
    """同一天两份 digest 各自有记录，互不覆盖（每周一的固定形态）。"""
    conn = connect(tmp_path / "test.db")
    init_db(conn)

    weekly_id = repo.create_digest(conn, "2026-11-02", kind="weekly")
    daily_id = repo.create_digest(conn, "2026-11-02", kind="daily")
    assert weekly_id != daily_id
    assert repo.get_digest(conn, "2026-11-02", "weekly")["id"] == weekly_id
    assert repo.get_digest(conn, "2026-11-02", "daily")["id"] == daily_id
    # 不指定 kind 时给当天最新一份
    assert repo.get_digest(conn, "2026-11-02")["id"] == daily_id


def test_old_schema_is_migrated_to_two_kinds_per_day(tmp_path: Path) -> None:
    """老库是「日期唯一」，启动时要自动重建表，否则周一存不下第二份。"""
    conn = connect(tmp_path / "old.db")
    conn.executescript(
        """
        CREATE TABLE digests (
          id          INTEGER PRIMARY KEY AUTOINCREMENT,
          digest_date TEXT NOT NULL UNIQUE,
          created_at  TEXT NOT NULL,
          sent_at     TEXT,
          status      TEXT NOT NULL,
          stats_json  TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO digests(digest_date, created_at, status) VALUES('2026-11-02', 'x', 'sent')"
    )
    conn.commit()

    init_db(conn)

    rows = conn.execute("SELECT id, kind FROM digests").fetchall()
    assert len(rows) == 1 and rows[0]["kind"] == "daily" and rows[0]["id"] == 1
    conn.execute(
        "INSERT INTO digests(digest_date, created_at, status, kind)"
        " VALUES('2026-11-02', 'y', 'building', 'weekly')"
    )
    conn.commit()
    assert len(conn.execute("SELECT * FROM digests").fetchall()) == 2


def test_scheduler_catch_up_collects_when_no_recent_run(tmp_path: Path, monkeypatch) -> None:
    config = make_config(tmp_path)
    conn = connect(config.db_path)
    init_db(conn)

    collected: list[int] = []
    monkeypatch.setattr(
        scheduler_mod,
        "run_collect",
        lambda conn, cfg: collected.append(1) or {"items": 0, "errors": []},
    )
    monkeypatch.setattr(scheduler_mod, "build_digest", lambda *a, **k: {"count": 0})

    scheduler = Scheduler(config, FakeNotifier(config.outbox_dir))
    scheduler._catch_up(conn)
    assert collected == [1]

    # 刚刚尝试过采集，下一次 tick 不应重复触发。
    scheduler._tick(conn)
    assert collected == [1]
