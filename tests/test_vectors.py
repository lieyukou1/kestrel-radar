from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from ai_radar.adapters.notify.feishu import build_l3_card
from ai_radar.pipeline import embedding
from ai_radar.pipeline.compare import find_related
from ai_radar.store import repo
from ai_radar.store.db import connect, init_db
from ai_radar.util import iso, utcnow


def add_document(
    conn,
    *,
    title: str,
    tag: str,
    tags: list[str] | None = None,
    problem: str = "problem",
) -> int:
    document_id = repo.insert_document(
        conn,
        canonical_id=f"test:{title}",
        kind="paper",
        title=title,
        abstract="abstract",
        url=f"https://example.com/{title}",
        published_at=iso(utcnow() - timedelta(days=2)),
        authority="A",
        primary_tag=tag,
        tags=tags or [tag],
    )
    repo.mark_status(conn, [document_id], "kb_ready")
    repo.save_kb_article(
        conn,
        document_id=document_id,
        problem=problem,
        method="method",
        evidence=["evidence"],
        conclusions=["conclusion"],
        limitations=["limitation"],
        card={
            "problem": problem,
            "method": "method",
            "evidence": ["evidence"],
            "conclusions": ["conclusion"],
            "limitations": ["limitation"],
            "l3": "l3",
        },
    )
    return document_id


def keyword_vectors(texts: list[str]) -> list[list[float]]:
    """用关键词造二维向量，模拟语义模型对主题的区分。"""
    vectors = []
    for text in texts:
        lowered = text.lower()
        if "routing" in lowered or "expert" in lowered:
            vectors.append([1.0, 0.0])
        elif "storage" in lowered:
            vectors.append([0.0, 1.0])
        else:
            vectors.append([0.6, 0.8])
    return vectors


def test_ensure_vectors_persists_and_reuses(tmp_path: Path, monkeypatch) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    left = add_document(conn, title="Routing paper", tag="1 模型与架构", problem="expert routing")
    right = add_document(conn, title="Storage paper", tag="6 系统与基础设施", problem="storage engine")

    monkeypatch.setattr(embedding, "embed_texts", keyword_vectors)
    vectors = embedding.ensure_vectors(conn, [left, right])
    assert set(vectors) == {left, right}
    assert vectors[left] == [1.0, 0.0]
    assert repo.get_vector(conn, left, embedding.model_name()) == [1.0, 0.0]

    calls: list[list[str]] = []

    def counting(texts: list[str]) -> list[list[float]]:
        calls.append(texts)
        return keyword_vectors(texts)

    monkeypatch.setattr(embedding, "embed_texts", counting)
    again = embedding.ensure_vectors(conn, [left])
    assert again[left] == [1.0, 0.0]
    assert calls == []


def test_find_related_prefers_semantic_over_same_dimension(
    tmp_path: Path, monkeypatch
) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    base = add_document(
        conn, title="MoE routing", tag="1 模型与架构", tags=["1 模型与架构"], problem="expert routing"
    )
    same_dim = add_document(
        conn, title="Storage engine", tag="1 模型与架构", tags=["1 模型与架构"], problem="storage engine"
    )
    cross_dim = add_document(
        conn, title="Router design", tag="6 系统与基础设施", tags=["6 系统与基础设施"], problem="expert routing"
    )

    monkeypatch.setattr(embedding, "embed_texts", keyword_vectors)
    related = find_related(conn, base)
    assert related
    assert related[0]["document_id"] == cross_dim
    assert related[0]["semantic"] is not None
    assert related[0]["semantic"] > related[1]["semantic"]
    assert {item["document_id"] for item in related} == {same_dim, cross_dim}


def test_find_related_falls_back_without_model(tmp_path: Path, monkeypatch) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    base = add_document(conn, title="MoE routing", tag="1 模型与架构", problem="expert routing")
    same_dim = add_document(
        conn, title="Routing efficiency", tag="1 模型与架构", problem="expert routing efficiency"
    )
    add_document(conn, title="Storage", tag="6 系统与基础设施", problem="storage engine")

    monkeypatch.setattr(embedding, "embed_texts", lambda texts: None)
    related = find_related(conn, base)
    assert related
    assert related[0]["document_id"] == same_dim
    assert related[0]["semantic"] is None
    assert "同维度" in related[0]["reason"]


def test_l3_card_has_no_compare_with_previous_button(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    current = add_document(conn, title="Current", tag="1 模型与架构")

    card = build_l3_card(repo.get_document(conn, current), {"problem": "p", "method": "m"})
    buttons = [
        action
        for element in card["elements"]
        if element.get("tag") == "action"
        for action in element["actions"]
    ]
    assert [button["text"]["content"] for button in buttons] == ["相关", "收藏", "不感兴趣"]
