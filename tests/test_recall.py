from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from ai_radar.pipeline.recall import ENTER_AFTER_DAYS, MAX_RECALLS, MIN_GAP_DAYS, pick_recall
from ai_radar.store import repo
from ai_radar.store.db import connect, init_db
from ai_radar.util import iso

NOW = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)


def make_conn(tmp_path: Path):
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    return conn


def add_doc(conn, key: str, *, tag: str = "模型与架构", status: str = "observing") -> int:
    document_id = repo.insert_document(
        conn,
        canonical_id=key,
        kind="article",
        title=f"文档 {key}",
        abstract="摘要",
        url=f"https://example.com/{key}",
        published_at=None,
        authority="rss",
        primary_tag=tag,
        tags=[tag],
    )
    if status != "observing":
        repo.mark_status(conn, [document_id], status)
    return document_id


def add_action(conn, document_id: int, action: str, days_ago: float) -> None:
    conn.execute(
        "INSERT INTO interactions(document_id, digest_id, action, reason, created_at)"
        " VALUES(?, NULL, ?, NULL, ?)",
        (document_id, action, iso(NOW - timedelta(days=days_ago))),
    )
    conn.commit()


def test_recent_save_is_not_a_candidate(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    document_id = add_doc(conn, "fresh")
    add_action(conn, document_id, "save", ENTER_AFTER_DAYS - 1)
    assert pick_recall(conn, NOW) is None


def test_save_after_thirty_days_becomes_candidate(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    document_id = add_doc(conn, "old")
    add_action(conn, document_id, "save", ENTER_AFTER_DAYS + 1)

    picked = pick_recall(conn, NOW)
    assert picked is not None
    assert picked.document_id == document_id
    assert picked.source_action == "save"
    assert "收藏过" in picked.reason


def test_recall_within_gap_is_blocked(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    document_id = add_doc(conn, "recent-recall")
    add_action(conn, document_id, "save", 100)
    add_action(conn, document_id, "recall", MIN_GAP_DAYS - 5)
    assert pick_recall(conn, NOW) is None


def test_recall_after_gap_is_allowed(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    document_id = add_doc(conn, "gap-ok")
    add_action(conn, document_id, "save", 200)
    add_action(conn, document_id, "recall", MIN_GAP_DAYS + 1)
    assert pick_recall(conn, NOW).document_id == document_id


def test_max_two_recalls(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    document_id = add_doc(conn, "twice")
    add_action(conn, document_id, "save", 300)
    add_action(conn, document_id, "recall", 200)
    add_action(conn, document_id, "recall", 100)
    assert repo.action_count(conn, document_id, "recall") == MAX_RECALLS
    assert pick_recall(conn, NOW) is None


def test_unread_favorite_outranks_read_one(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    unread = add_doc(conn, "unread")
    add_action(conn, unread, "save", 40)
    read = add_doc(conn, "read")
    add_action(conn, read, "save", 90)
    add_action(conn, read, "deep_read", 35)

    assert pick_recall(conn, NOW).document_id == unread


def test_within_same_tier_newest_action_wins(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    newer = add_doc(conn, "newer")
    add_action(conn, newer, "save", 40)
    older = add_doc(conn, "older")
    add_action(conn, older, "save", 90)

    assert pick_recall(conn, NOW).document_id == newer


def test_same_tag_as_recent_recall_is_skipped(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    same_tag = add_doc(conn, "same-tag", tag="推理加速")
    add_action(conn, same_tag, "save", 40)
    other_tag = add_doc(conn, "other-tag", tag="多模态与生成")
    add_action(conn, other_tag, "save", 40)
    # 昨天刚回看过「推理加速」，今天不再重复同一主分类
    previous = add_doc(conn, "previous", tag="推理加速")
    add_action(conn, previous, "save", 120)
    add_action(conn, previous, "recall", 1)

    assert pick_recall(conn, NOW).document_id == other_tag


def test_recall_off_is_permanent(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    document_id = add_doc(conn, "muted")
    add_action(conn, document_id, "save", 40)
    repo.set_recall_off(conn, document_id)
    assert pick_recall(conn, NOW) is None


def test_disliked_document_is_excluded(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    document_id = add_doc(conn, "hated", status="disliked")
    add_action(conn, document_id, "save", 40)
    assert pick_recall(conn, NOW) is None


def test_empty_pool_returns_none(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    assert pick_recall(conn, NOW) is None


def digest_config(tmp_path: Path):
    from types import SimpleNamespace

    return SimpleNamespace(
        llm=None,
        network=SimpleNamespace(proxy=None, mode="overseas-only"),
        weights={},
        outbox_dir=tmp_path / "outbox",
    )


def test_build_digest_appends_recall_and_records_after_delivery(
    tmp_path: Path, monkeypatch
) -> None:
    from ai_radar.adapters.notify.fake import FakeNotifier
    from ai_radar.pipeline import digest as digest_mod
    from ai_radar.pipeline.composer import Composition

    conn = make_conn(tmp_path)
    document_id = add_doc(conn, "recall-me")
    add_action(conn, document_id, "save", ENTER_AFTER_DAYS + 1)
    monkeypatch.setattr(digest_mod, "collect_candidates", lambda *a, **k: [])
    monkeypatch.setattr(
        digest_mod.composer,
        "compose_daily",
        lambda *a, **k: Composition(kind="daily", blocks=[], theme=None),
    )
    config = digest_config(tmp_path)

    result = digest_mod.build_digest(
        conn,
        config,
        notifier=FakeNotifier(config.outbox_dir),
        digest_date="2026-10-30",
        kind="daily",
        now=NOW,
    )

    assert result["count"] == 1
    assert repo.action_count(conn, document_id, "recall") == 1
    row = conn.execute(
        "SELECT section FROM digest_items WHERE document_id = ?", (document_id,)
    ).fetchone()
    assert row["section"] == "recall"


def test_preview_does_not_spend_recall_quota(tmp_path: Path, monkeypatch) -> None:
    from ai_radar.adapters.notify.fake import FakeNotifier
    from ai_radar.pipeline import digest as digest_mod
    from ai_radar.pipeline.composer import Composition

    conn = make_conn(tmp_path)
    document_id = add_doc(conn, "preview-only")
    add_action(conn, document_id, "save", ENTER_AFTER_DAYS + 1)
    monkeypatch.setattr(digest_mod, "collect_candidates", lambda *a, **k: [])
    monkeypatch.setattr(
        digest_mod.composer,
        "compose_daily",
        lambda *a, **k: Composition(kind="daily", blocks=[], theme=None),
    )
    config = digest_config(tmp_path)

    result = digest_mod.build_digest(
        conn,
        config,
        notifier=FakeNotifier(config.outbox_dir),
        digest_date="2026-10-30",
        kind="daily",
        now=NOW,
        preview=True,
    )

    assert result["count"] == 0
    assert repo.action_count(conn, document_id, "recall") == 0
