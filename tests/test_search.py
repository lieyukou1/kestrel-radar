from __future__ import annotations

from pathlib import Path

from ai_radar.adapters.notify.feishu import build_digest_card, build_topic_card
from ai_radar.config import AppConfig, load_config
from ai_radar.pipeline import search as search_mod
from ai_radar.pipeline.interactions import handle_action
from ai_radar.pipeline.search import (
    ExternalHit,
    SearchHit,
    SearchResult,
    search_documents,
)
from ai_radar.store import repo
from ai_radar.store.db import connect, init_db


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


def add_document(
    conn,
    *,
    title: str,
    abstract: str = "abstract",
    tag: str = "1 模型与架构",
    kind: str = "blog",
    status: str = "observing",
) -> int:
    document_id = repo.insert_document(
        conn,
        canonical_id=f"test:{title}",
        kind=kind,
        title=title,
        abstract=abstract,
        url=f"https://example.com/{title}",
        published_at="2026-10-01T00:00:00+00:00",
        authority="A",
        primary_tag=tag,
        tags=[tag],
    )
    if status != "observing":
        repo.mark_status(conn, [document_id], status)
    return document_id


def card_text(card: dict) -> str:
    return "\n".join(
        element["text"]["content"]
        for element in card["elements"]
        if element.get("tag") == "div"
    )


def test_search_matches_keyword_without_vectors(tmp_path, monkeypatch) -> None:
    config = make_config(tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    add_document(conn, title="Claude 形式化验证挖出数百个 Bug")
    add_document(conn, title="完全无关的向量数据库对比")

    monkeypatch.setattr("ai_radar.pipeline.search.embedding.embed_texts", lambda texts: None)
    result = search_documents(conn, config, "Claude", external=False)

    assert [hit.title for hit in result.local] == ["Claude 形式化验证挖出数百个 Bug"]
    assert result.local[0].score > 0
    assert result.external == []


def test_search_skips_disliked_documents(tmp_path, monkeypatch) -> None:
    config = make_config(tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    add_document(conn, title="Claude 被不感兴趣", status="disliked")

    monkeypatch.setattr("ai_radar.pipeline.search.embedding.embed_texts", lambda texts: None)
    result = search_documents(conn, config, "Claude", external=False)
    assert result.local == []


def test_search_deprioritizes_recently_pushed(tmp_path, monkeypatch) -> None:
    config = make_config(tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    fresh = add_document(conn, title="Claude 新论文")
    pushed = add_document(conn, title="Claude 旧讨论")

    digest_id = repo.create_digest(conn, "2026-10-20")
    repo.add_digest_item(
        conn,
        digest_id=digest_id,
        document_id=pushed,
        rank=1,
        score=0.9,
        reason="",
        summary={},
        section="brief",
    )
    repo.mark_digest_sent(conn, digest_id, {"count": 1})

    monkeypatch.setattr("ai_radar.pipeline.search.embedding.embed_texts", lambda texts: None)
    result = search_documents(conn, config, "Claude", external=False)

    assert [hit.document_id for hit in result.local] == [fresh, pushed]
    assert result.local[0].pushed_recently is False
    assert result.local[1].pushed_recently is True
    assert "近期推过" in result.note


def test_search_marks_documents_already_in_kb(tmp_path, monkeypatch) -> None:
    config = make_config(tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    saved = add_document(conn, title="Claude 收藏的")
    deep = add_document(conn, title="Claude 精读的")
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

    monkeypatch.setattr("ai_radar.pipeline.search.embedding.embed_texts", lambda texts: None)
    result = search_documents(conn, config, "Claude", external=False)

    flags = {hit.document_id: (hit.in_kb, hit.saved) for hit in result.local}
    assert flags[saved] == (False, True)
    assert flags[deep] == (True, False)


def test_handle_action_topic_search() -> None:
    assert handle_action({}, {"action": "topic_search", "query": " "})["type"] == "text"
    assert handle_action({}, {"action": "topic_search", "query": " 强化学习 "}) == {
        "type": "topic_card",
        "query": "强化学习",
    }


def test_topic_card_renders_local_and_external() -> None:
    result = SearchResult(
        query="Claude",
        local=[
            SearchHit(
                document_id=7,
                title="Claude 形式化验证",
                url="https://example.com/claude",
                tag="7 理论与可解释性",
                kind="blog",
                score=0.9,
                reason="标题命中 1 个词",
                published_at="2026-10-24T10:00:00+00:00",
                pushed_recently=True,
            )
        ],
        external=[
            ExternalHit(source="arXiv", title="Formal verification with LLMs", url="https://arxiv.org/abs/1234.5678", meta="论文")
        ],
        elapsed_ms=1234,
        note="本地结果不足 3 条，补充了近期推过的内容。",
    )
    card = build_topic_card("Claude", result)
    text = card_text(card)

    assert "理论" in text and "1 模型与架构" not in text
    assert "近 30 天推过" in text
    assert "站外补充" in text and "arxiv.org/abs/1234.5678" in text
    assert "用时 1.2s" in text and "近期推过" in text

    values = [
        action["value"]
        for element in card["elements"]
        if element.get("tag") == "action"
        for action in element["actions"]
    ]
    assert {"action": "detail", "document_id": 7} in values


def test_topic_card_handles_no_results() -> None:
    card = build_topic_card("不存在的东西", SearchResult(query="不存在的东西"))
    assert "没有找到相关内容" in card_text(card)


def test_digest_card_has_topic_button_only_with_theme() -> None:
    with_theme = build_digest_card(
        "2026-10-28",
        [],
        theme_label="Claude 形式化验证",
        theme_reason="跨来源讨论",
        theme_query="claude",
    )
    values = [
        action["value"]
        for element in with_theme["elements"]
        if element.get("tag") == "action"
        for action in element["actions"]
    ]
    assert {"action": "topic_search", "query": "claude"} in values

    without_theme = build_digest_card("2026-10-28", [])
    plain_values = [
        action["value"]
        for element in without_theme["elements"]
        if element.get("tag") == "action"
        for action in element["actions"]
    ]
    assert all(value.get("action") != "topic_search" for value in plain_values)


# ---------- 零命中与站外补位 ----------


def test_search_empty_query_returns_empty_result(tmp_path, monkeypatch) -> None:
    config = make_config(tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    called: list[int] = []
    monkeypatch.setattr(
        search_mod, "_external_hits", lambda *args, **kwargs: called.append(1) or []
    )

    result = search_documents(conn, config, "   ", external=True)

    assert (result.query, result.local, result.external) == ("", [], [])
    assert called == []


def test_search_with_only_separators_has_no_tokens(tmp_path, monkeypatch) -> None:
    config = make_config(tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    add_document(conn, title="Claude 论文")
    monkeypatch.setattr("ai_radar.pipeline.search.embedding.embed_texts", lambda texts: None)

    result = search_documents(conn, config, "，、,", external=False)
    assert result.local == []


def test_multi_token_query_requires_every_token(tmp_path, monkeypatch) -> None:
    """「Boltzmann machine」不能被只含 machine 的文章灌满。"""
    config = make_config(tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    add_document(conn, title="Boltzmann machine 原理")
    add_document(conn, title="machine learning 综述")
    monkeypatch.setattr("ai_radar.pipeline.search.embedding.embed_texts", lambda texts: None)

    result = search_documents(conn, config, "Boltzmann machine", external=False)
    assert [hit.title for hit in result.local] == ["Boltzmann machine 原理"]


def test_partial_tokens_pass_with_strong_semantics(tmp_path, monkeypatch) -> None:
    config = make_config(tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    document_id = add_document(conn, title="machine learning 综述")

    monkeypatch.setattr(
        "ai_radar.pipeline.search.embedding.embed_texts", lambda texts: [[1.0]]
    )
    monkeypatch.setattr(
        "ai_radar.pipeline.search.embedding.cosine", lambda left, right: 0.7
    )
    monkeypatch.setattr(
        "ai_radar.pipeline.search.repo.list_vectors",
        lambda conn, name: {document_id: [1.0]},
    )

    result = search_documents(conn, config, "quantum machine", external=False)

    assert [hit.document_id for hit in result.local] == [document_id]
    assert "语义相近 70%" in result.local[0].reason


def test_weak_semantic_without_keyword_is_dropped(tmp_path, monkeypatch) -> None:
    config = make_config(tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    document_id = add_document(conn, title="machine learning 综述")

    monkeypatch.setattr(
        "ai_radar.pipeline.search.embedding.embed_texts", lambda texts: [[1.0]]
    )
    monkeypatch.setattr(
        "ai_radar.pipeline.search.embedding.cosine", lambda left, right: 0.4
    )
    monkeypatch.setattr(
        "ai_radar.pipeline.search.repo.list_vectors",
        lambda conn, name: {document_id: [1.0]},
    )

    assert search_documents(conn, config, "quantum", external=False).local == []


def test_external_results_are_deduped_against_local(tmp_path, monkeypatch) -> None:
    config = make_config(tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    add_document(conn, title="Claude 论文")
    monkeypatch.setattr("ai_radar.pipeline.search.embedding.embed_texts", lambda texts: None)
    monkeypatch.setattr(
        search_mod,
        "_external_hits",
        lambda *args, **kwargs: [
            ExternalHit("arXiv", "重复的", "https://example.com/Claude 论文/"),
            ExternalHit("Hacker News", "新的", "https://news.example.com/1"),
        ],
    )

    result = search_documents(conn, config, "Claude", external=True)

    assert [hit.title for hit in result.external] == ["新的"]


def test_external_search_skipped_when_budget_too_small(tmp_path) -> None:
    config = make_config(tmp_path)
    assert search_mod._external_hits(config, "q", limit=3, budget=0.5) == []


def test_external_search_swallows_source_failures(tmp_path, monkeypatch) -> None:
    config = make_config(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("source down")

    monkeypatch.setattr(search_mod, "_arxiv_hits", boom)
    monkeypatch.setattr(search_mod, "_github_hits", boom)
    monkeypatch.setattr(
        search_mod,
        "_hn_hits",
        lambda *args, **kwargs: [
            ExternalHit("Hacker News", "幸存", "https://hn.example/1")
        ],
    )

    found = search_mod._external_hits(config, "q", limit=3, budget=5)

    assert [hit.title for hit in found] == ["幸存"]
