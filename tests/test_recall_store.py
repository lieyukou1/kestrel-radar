from __future__ import annotations

from pathlib import Path

from ai_radar.adapters.notify.base import DigestItemView
from ai_radar.store import repo
from ai_radar.store.db import connect, init_db


def make_document(conn, title: str, *, tag: str = "1 模型与架构") -> int:
    return repo.insert_document(
        conn,
        canonical_id=f"test:{title}",
        kind="paper",
        title=title,
        abstract="abstract",
        url=f"https://example.com/{title}",
        published_at="2026-10-19T08:00:00+00:00",
        authority="A",
        primary_tag=tag,
        tags=[tag],
    )


def save_kb(conn, document_id: int) -> None:
    repo.save_kb_article(
        conn,
        document_id=document_id,
        problem="p",
        method="m",
        evidence=[],
        conclusions=[],
        limitations=[],
        card={"problem": "p", "method": "m"},
    )


def test_pool_includes_saved_and_deep_read(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    saved = make_document(conn, "只收藏")
    deep = make_document(conn, "已精读")
    repo.add_interaction(conn, document_id=saved, digest_id=None, action="save")
    repo.add_interaction(conn, document_id=deep, digest_id=None, action="deep_read")
    save_kb(conn, deep)

    pool = {row["id"]: row for row in repo.recall_pool(conn)}
    assert set(pool) == {saved, deep}
    assert pool[saved]["saved_at"] is not None
    assert pool[deep]["kb_read_at"] is not None


def test_pool_includes_kb_article_without_interaction(tmp_path: Path) -> None:
    """CLI 精读只写 kb_articles，不写 deep_read 交互，也应进候选。"""
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = make_document(conn, "仅精读无交互")
    save_kb(conn, document_id)

    pool = {row["id"]: row for row in repo.recall_pool(conn)}
    assert set(pool) == {document_id}
    assert pool[document_id]["kb_read_at"] is not None
    assert pool[document_id]["saved_at"] is None


def test_pool_excludes_removed_and_disliked(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    removed = make_document(conn, "已移出")
    disliked = make_document(conn, "讨厌的")
    repo.add_interaction(conn, document_id=removed, digest_id=None, action="deep_read")
    save_kb(conn, removed)
    repo.remove_kb_article(conn, removed)

    repo.add_interaction(conn, document_id=disliked, digest_id=None, action="save")
    repo.mark_status(conn, [disliked], "disliked")

    assert repo.recall_pool(conn) == []


def test_pool_excludes_merged_documents(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = make_document(conn, "被合并")
    repo.add_interaction(conn, document_id=document_id, digest_id=None, action="save")
    conn.execute("UPDATE documents SET merged_into = 1 WHERE id = ?", (document_id,))
    conn.commit()

    assert repo.recall_pool(conn) == []


def test_recall_off_is_idempotent(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = make_document(conn, "不再提醒")

    assert repo.is_recall_off(conn, document_id) is False
    repo.set_recall_off(conn, document_id)
    repo.set_recall_off(conn, document_id)
    assert repo.is_recall_off(conn, document_id) is True


def test_action_helpers(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = make_document(conn, "计数")

    assert repo.action_count(conn, document_id, "recall") == 0
    repo.add_interaction(conn, document_id=document_id, digest_id=None, action="recall")
    assert repo.has_action(conn, document_id, "recall") is True
    assert repo.action_count(conn, document_id, "recall") == 1
    assert repo.last_action_at(conn, document_id, "recall") is not None


def test_recent_recall_tags_follows_latest_order(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    first = make_document(conn, "甲", tag="3 推理与加速")
    second = make_document(conn, "乙", tag="5 多模态与生成")
    repo.add_interaction(conn, document_id=first, digest_id=None, action="recall")
    repo.add_interaction(conn, document_id=second, digest_id=None, action="recall")

    assert repo.recent_recall_tags(conn, limit=2) == ["5 多模态与生成", "3 推理与加速"]


def test_digest_item_view_has_recall_fields() -> None:
    item = DigestItemView(
        rank=1,
        document_id=1,
        title="t",
        url="u",
        tag="tag",
        source="paper",
        reason="r",
        score=1.0,
        section="recall",
        note="你 9 月 12 日收藏过",
        followed=True,
    )
    assert item.note and item.followed
    assert DigestItemView(1, 1, "t", "u", "tag", "paper", "r", 1.0).note == ""
