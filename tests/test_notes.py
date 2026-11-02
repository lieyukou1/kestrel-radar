from __future__ import annotations

from pathlib import Path

from ai_radar.store import repo
from ai_radar.store.db import connect, init_db


def make_document(conn, title: str) -> int:
    return repo.insert_document(
        conn,
        canonical_id=f"test:{title}",
        kind="paper",
        title=title,
        abstract="abstract",
        url=f"https://example.com/{title}",
        published_at="2026-10-19T08:00:00+00:00",
        authority="A",
        primary_tag="1 模型与架构",
        tags=["1 模型与架构"],
    )


def test_add_and_list_notes(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = make_document(conn, "批注目标")

    repo.add_note(conn, document_id=document_id, content="第一条")
    repo.add_note(conn, document_id=document_id, content="第二条")

    notes = repo.list_notes(conn, document_id)
    assert [row["content"] for row in notes] == ["第二条", "第一条"]
    assert repo.count_notes(conn, [document_id]) == {document_id: 2}


def test_notes_are_append_only(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = make_document(conn, "只追加")

    repo.add_note(conn, document_id=document_id, content="A")
    repo.add_note(conn, document_id=document_id, content="B")

    assert len(repo.list_notes(conn, document_id)) == 2
    assert [row["content"] for row in repo.list_notes(conn, document_id, limit=1)] == ["B"]


def test_count_and_map_for_multiple_documents(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    first = make_document(conn, "甲")
    second = make_document(conn, "乙")

    repo.add_note(conn, document_id=first, content="一条")
    repo.add_note(conn, document_id=second, content="另一条")

    counts = repo.count_notes(conn, [first, second, 999])
    assert counts == {first: 1, second: 1}

    grouped = repo.notes_by_document(conn, [first, second])
    assert grouped[first] == ["一条"]


def test_empty_document_list_returns_empty(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    assert repo.count_notes(conn, []) == {}
    assert repo.notes_by_document(conn, []) == {}
