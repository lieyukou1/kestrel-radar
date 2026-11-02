from __future__ import annotations

from pathlib import Path

from ai_radar.pipeline.interactions import (
    format_favorites,
    handle_action,
    record_dislike,
)
from ai_radar.store import repo
from ai_radar.store.db import connect, init_db


def make_document(conn, title: str = "Test paper") -> int:
    return repo.insert_document(
        conn,
        canonical_id=f"test:{title}",
        kind="paper",
        title=title,
        abstract="abstract",
        url="https://example.com/paper",
        published_at="2026-10-19T08:00:00+00:00",
        authority="A",
        primary_tag="1 模型与架构",
        tags=["1 模型与架构"],
    )


def test_read_updates_document(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = make_document(conn)

    result = handle_action(conn, {"action": "read", "document_id": document_id})
    assert result["type"] == "text"

    row = repo.get_document(conn, document_id)
    assert row["read_count"] == 1
    assert row["read_at"] is not None
    assert row["status"] == "read"


def test_save_and_unsave(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = make_document(conn)

    handle_action(conn, {"action": "save", "document_id": document_id})
    assert "Test paper" in format_favorites(conn)

    handle_action(conn, {"action": "unsave", "document_id": document_id})
    assert "收藏夹为空" in format_favorites(conn)


def test_save_puts_document_into_kb_view(tmp_path: Path) -> None:
    """收藏即入库：不必精读，也要出现在知识库列表里。"""
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = make_document(conn, "只收藏没精读")

    result = handle_action(conn, {"action": "save", "document_id": document_id})
    assert "知识库" in result["text"]

    entries = repo.kb_entries(conn)
    assert [row["title"] for row in entries] == ["只收藏没精读"]
    assert entries[0]["deep_read"] == 0
    assert repo.kb_counts(conn) == {"deep": 0, "only_saved": 1, "total": 1}


def test_kb_lists_deep_read_and_saved_together(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    saved = make_document(conn, "收藏的")
    deep = make_document(conn, "精读的")
    repo.save_kb_article(
        conn,
        document_id=deep,
        problem="p",
        method="m",
        evidence=[],
        conclusions=[],
        limitations=[],
        card={"problem": "p", "method": "m"},
    )
    handle_action(conn, {"action": "save", "document_id": saved})

    entries = {row["title"]: row for row in repo.kb_entries(conn)}
    assert entries["收藏的"]["deep_read"] == 0
    assert entries["精读的"]["deep_read"] == 1
    assert repo.kb_counts(conn) == {"deep": 1, "only_saved": 1, "total": 2}


def test_kb_action_requests_card(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    assert handle_action(conn, {"action": "kb"}) == {
        "type": "kb_card",
        "scope": "kb",
        "filters": {},
        "page": 0,
    }
    favorites = handle_action(
        conn, {"action": "favorites", "filters": {"kind": "paper"}, "page": 2}
    )
    assert favorites == {
        "type": "favorites_card",
        "scope": "favorites",
        "filters": {"kind": "paper"},
        "page": 2,
    }


def test_favorites_view_only_lists_saved(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    saved = make_document(conn, "收藏的")
    deep_only = make_document(conn, "只精读的")
    repo.save_kb_article(
        conn,
        document_id=deep_only,
        problem="p",
        method="m",
        evidence=[],
        conclusions=[],
        limitations=[],
        card={"problem": "p", "method": "m"},
    )
    handle_action(conn, {"action": "save", "document_id": saved})

    entries = repo.kb_entries(conn, only_saved=True)
    assert [row["title"] for row in entries] == ["收藏的"]
    assert repo.favorite_counts(conn) == {"total": 1, "deep": 0, "only_saved": 1}


def test_kb_filters_and_pagination(tmp_path: Path) -> None:
    from datetime import timedelta

    from ai_radar.util import iso, utcnow

    conn = connect(tmp_path / "test.db")
    init_db(conn)
    paper_a = make_document(conn, "论文甲")
    paper_b = make_document(conn, "论文乙")
    blog = make_document(conn, "博客丙")
    for document_id in (paper_a, paper_b):
        conn.execute("UPDATE documents SET kind = 'paper' WHERE id = ?", (document_id,))
    conn.execute("UPDATE documents SET kind = 'blog' WHERE id = ?", (blog,))
    repo.save_kb_article(
        conn,
        document_id=paper_a,
        problem="p",
        method="m",
        evidence=[],
        conclusions=[],
        limitations=[],
        card={"problem": "p", "method": "m"},
    )
    handle_action(conn, {"action": "save", "document_id": paper_b})
    handle_action(conn, {"action": "save", "document_id": blog})

    assert {row["title"] for row in repo.kb_entries(conn, kind="paper")} == {"论文甲", "论文乙"}
    assert {row["title"] for row in repo.kb_entries(conn, status="saved")} == {"博客丙", "论文乙"}
    assert repo.kb_counts(conn, kind="paper") == {"deep": 1, "only_saved": 1, "total": 2}
    assert repo.kb_facets(conn)["kinds"] == ["blog", "paper"]

    first_page = repo.kb_entries(conn, limit=1, offset=0)
    second_page = repo.kb_entries(conn, limit=1, offset=1)
    assert len(first_page) == 1 and len(second_page) == 1
    assert first_page[0]["id"] != second_page[0]["id"]

    future = iso(utcnow() + timedelta(days=1))
    assert repo.kb_entries(conn, since=future) == []
    assert repo.kb_counts(conn, since=future)["total"] == 0


def test_kb_remove_saved_only(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = make_document(conn)
    handle_action(conn, {"action": "save", "document_id": document_id})

    result = handle_action(conn, {"action": "kb_remove", "document_id": document_id})
    assert result == {"type": "text", "text": "已移出知识库。"}
    assert repo.is_saved(conn, document_id) is False
    assert repo.kb_entries(conn) == []


def test_kb_remove_deep_read_requires_confirm(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = make_document(conn)
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
    repo.insert_claims(
        conn, document_id=document_id, chunk_id=None, claims=[{"kind": "finding", "text": "c"}]
    )
    handle_action(conn, {"action": "save", "document_id": document_id})

    confirm = handle_action(conn, {"action": "kb_remove", "document_id": document_id})
    assert confirm == {"type": "remove_confirm_card", "document_id": document_id}

    handle_action(conn, {"action": "kb_remove_confirm", "document_id": document_id})
    assert repo.get_kb_article(conn, document_id) is None
    assert repo.list_claims(conn, document_id) == []
    assert repo.kb_entries(conn) == []
    assert repo.is_saved(conn, document_id) is False
    assert repo.get_document(conn, document_id)["status"] == "read"


def test_kb_remove_cancel_keeps_article(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = make_document(conn)
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

    result = handle_action(conn, {"action": "kb_remove_cancel", "document_id": document_id})
    assert "仍在知识库" in result["text"]
    assert repo.get_kb_article(conn, document_id) is not None


def test_dislike_updates_preferences(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = make_document(conn)

    record_dislike(conn, document_id=document_id, reason="维度不相关")
    prefs = repo.get_preference_weights(conn)
    assert prefs["dimension:1 模型与架构"] == 0.9

    row = repo.get_document(conn, document_id)
    assert row["status"] == "disliked"


def test_dislike_returns_reason_card_request(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = make_document(conn)

    result = handle_action(conn, {"action": "dislike", "document_id": document_id})
    assert result["type"] == "reason_card"
    assert result["document_id"] == document_id


def test_qa_common_keeps_question(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    result = handle_action(conn, {"action": "qa_common", "question": " 什么是 MoE "})
    assert result == {"type": "qa_common", "question": "什么是 MoE"}
    assert handle_action(conn, {"action": "qa_common", "question": ""})["type"] == "text"
