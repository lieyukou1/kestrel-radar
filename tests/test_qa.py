from __future__ import annotations

from types import SimpleNamespace

import pytest

from ai_radar.llm.client import LLMError
from ai_radar.pipeline import qa
from ai_radar.store import repo
from ai_radar.store.db import connect, init_db


def _document(conn, canonical_id: str, title: str, *, deep: bool = False) -> int:
    document_id = repo.insert_document(
        conn,
        canonical_id=canonical_id,
        kind="paper",
        title=title,
        abstract="abstract",
        url=f"https://example.com/{canonical_id}",
        published_at="2026-10-19T08:00:00+00:00",
        authority="A",
        primary_tag="1 模型与架构",
        tags=["1 模型与架构"],
    )
    if deep:
        repo.save_kb_article(
            conn,
            document_id=document_id,
            problem="多模态问答如何归因",
            method="逻辑熵驱动",
            evidence=[],
            conclusions=["优于基线"],
            limitations=[],
            card={"problem": "p", "method": "m"},
        )
    else:
        repo.add_interaction(
            conn, document_id=document_id, digest_id=None, action="save"
        )
    return document_id


def _config():
    return SimpleNamespace(llm=None)


def test_empty_kb_is_unsupported(tmp_path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    result = qa.answer_question(conn, _config(), "RSI 的结论是什么")
    assert result["supported"] is False
    assert result["related"] == []


def test_question_without_anchor_finds_nothing(tmp_path, monkeypatch) -> None:
    """语义分不够强时不能硬当成依据，否则会拿无关文章编答案。"""
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    _document(conn, "test:edma", "EDMA: Entropy-Driven Multimodal Answering", deep=True)
    monkeypatch.setattr(qa, "_semantic_scores", lambda *args: {})

    assert qa.supported_hits(conn, "量子计算的表面码纠错阈值是多少") == []
    result = qa.answer_question(conn, _config(), "量子计算的表面码纠错阈值是多少")
    assert result["supported"] is False


def test_keyword_anchor_counts_as_evidence(tmp_path, monkeypatch) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = _document(
        conn, "test:edma", "EDMA: Entropy-Driven Multimodal Answering", deep=True
    )
    monkeypatch.setattr(qa, "_semantic_scores", lambda *args: {})

    hits = qa.supported_hits(conn, "EDMA 的结论是什么", keywords=["EDMA"])
    assert [hit.document_id for hit in hits] == [document_id]
    assert hits[0].reason == "检索词命中"


def test_strong_semantic_match_counts_as_evidence(tmp_path, monkeypatch) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = _document(conn, "test:agent", "Agents talking through cache", deep=True)
    monkeypatch.setattr(
        qa, "_semantic_scores", lambda *args: {document_id: qa.SEMANTIC_ONLY + 0.05}
    )

    hits = qa.supported_hits(conn, "智能体之间怎么互相通信的")
    assert [hit.document_id for hit in hits] == [document_id]
    assert hits[0].reason == "语义相近"


def test_saved_document_is_valid_evidence(tmp_path, monkeypatch) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = _document(conn, "test:saved", "只收藏没精读的 MoE 文章")
    monkeypatch.setattr(qa, "_semantic_scores", lambda *args: {})

    hits = qa.supported_hits(conn, "MoE 那篇讲了什么", keywords=["MoE"])
    assert [hit.document_id for hit in hits] == [document_id]
    assert hits[0].saved is True and hits[0].in_kb is False


def test_evidence_without_llm_raises(tmp_path, monkeypatch) -> None:
    """有依据但模型不可用时要报错，而不是静默返回空答案。"""
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    _document(conn, "test:edma", "EDMA: Entropy-Driven Multimodal Answering", deep=True)
    monkeypatch.setattr(qa, "_semantic_scores", lambda *args: {})

    with pytest.raises(LLMError):
        qa.answer_question(conn, _config(), "EDMA 的结论是什么", keywords=["EDMA"])


# ---------- 零命中兜底 ----------


def test_closest_returns_empty_without_kb(tmp_path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    assert qa._closest(conn, "任何问题") == []


def test_closest_returns_empty_without_scores(tmp_path, monkeypatch) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    _document(conn, "test:solo", "唯一一篇", deep=True)
    monkeypatch.setattr(qa, "_semantic_scores", lambda *args: {})
    assert qa._closest(conn, "任何问题") == []


def test_closest_filters_weak_similarity(tmp_path, monkeypatch) -> None:
    """0.35 以下不算「接近」，避免把无关条目当成线索端给用户。"""
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    weak = _document(conn, "test:weak", "弱相关", deep=True)
    strong = _document(conn, "test:strong", "强相关", deep=True)
    monkeypatch.setattr(qa, "_semantic_scores", lambda *args: {weak: 0.34, strong: 0.5})

    related = qa._closest(conn, "没有依据的问题")
    assert [item.document_id for item in related] == [strong]
    assert related[0].reason == "语义接近"


def test_unsupported_answer_returns_closest_entries(tmp_path, monkeypatch) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = _document(conn, "test:x", "接近的文章", deep=True)
    monkeypatch.setattr(qa, "supported_hits", lambda *args, **kwargs: [])
    monkeypatch.setattr(qa, "_semantic_scores", lambda *args: {document_id: 0.5})

    result = qa.answer_question(conn, _config(), "完全无关的问题")

    assert result["supported"] is False
    assert [item.document_id for item in result["related"]] == [document_id]


# ---------- 有依据时的回答 ----------


def test_supported_answer_uses_llm_and_sources(tmp_path, monkeypatch) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = _document(
        conn, "test:edma", "EDMA: Entropy-Driven Multimodal Answering", deep=True
    )
    monkeypatch.setattr(qa, "_semantic_scores", lambda *args: {})
    stub = SimpleNamespace(chat=lambda *args, **kwargs: ("  答案 [1]  ", None))
    monkeypatch.setattr(qa, "_llm_or_raise", lambda config: stub)

    result = qa.answer_question(conn, _config(), "EDMA 的结论", keywords=["EDMA"])

    assert result["supported"] is True
    assert result["answer"] == "答案 [1]"
    assert [source["document_id"] for source in result["sources"]] == [document_id]


def test_context_skips_missing_document_and_includes_claims(tmp_path) -> None:
    from ai_radar.pipeline.search import SearchHit

    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = _document(conn, "test:claims", "带要点的文章", deep=True)
    repo.insert_claims(
        conn,
        document_id=document_id,
        chunk_id=None,
        claims=[{"kind": "finding", "text": "要点甲"}],
    )

    missing = SearchHit(
        document_id=999, title="", url="", tag="", kind="", score=0.0, reason=""
    )
    present = SearchHit(
        document_id=document_id,
        title="带要点的文章",
        url="https://example.com/claims",
        tag="1 模型与架构",
        kind="paper",
        score=1.0,
        reason="检索词命中",
    )
    context, sources = qa._context(conn, [missing, present])

    assert "要点甲" in context
    assert [source["document_id"] for source in sources] == [document_id]


def test_document_text_of_missing_document_is_empty(tmp_path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    assert qa._document_text(conn, 999) == ""


def test_semantic_scores_fallbacks(tmp_path, monkeypatch) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = _document(conn, "test:v", "向量文章", deep=True)

    monkeypatch.setattr(qa.embedding, "ensure_vectors", lambda *args, **kwargs: {})
    assert qa._semantic_scores(conn, [document_id], "问题") == {}

    monkeypatch.setattr(
        qa.embedding, "ensure_vectors", lambda *args, **kwargs: {document_id: [1.0]}
    )
    monkeypatch.setattr(qa.embedding, "embed_texts", lambda texts: None)
    assert qa._semantic_scores(conn, [document_id], "问题") == {}

    monkeypatch.setattr(qa.embedding, "embed_texts", lambda texts: [[2.0]])
    monkeypatch.setattr(qa.embedding, "cosine", lambda left, right: 0.75)
    assert qa._semantic_scores(conn, [document_id], "问题") == {document_id: 0.75}


def test_answer_from_model_strips_text(tmp_path, monkeypatch) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    stub = SimpleNamespace(chat=lambda *args, **kwargs: ("  常识答案  ", None))
    monkeypatch.setattr(qa, "_llm_or_raise", lambda config: stub)

    assert qa.answer_from_model(conn, _config(), "问题") == "常识答案"


def test_llm_or_raise_builds_client() -> None:
    from ai_radar.llm.client import DeepSeekLLM

    assert isinstance(qa._llm_or_raise(SimpleNamespace(llm=SimpleNamespace())), DeepSeekLLM)


def test_plain_converts_stored_fields() -> None:
    assert qa._plain(None) == "无"
    assert qa._plain('["甲", "乙"]') == "甲；乙"
    assert qa._plain("[]") == "无"
    assert qa._plain("不是 JSON") == "不是 JSON"
    assert qa._plain('{"a": 1}') == '{"a": 1}'
