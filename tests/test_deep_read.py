from __future__ import annotations

import json
from pathlib import Path

from ai_radar.adapters.notify.feishu import build_l3_card
from ai_radar.config import AppConfig, LLMConfig, load_config
from ai_radar.llm.client import Usage
from ai_radar.pipeline import deep_read as deep_read_mod
from ai_radar.pipeline.cleaning import clean_html
from ai_radar.pipeline.deep_read import chunk_markdown, deep_read_document
from ai_radar.pipeline.interactions import handle_action
from ai_radar.store import repo
from ai_radar.store.db import connect, init_db


def make_config(tmp_path: Path, *, with_llm: bool = True) -> AppConfig:
    base = load_config()
    return AppConfig(
        root=tmp_path,
        network=base.network,
        sources=base.sources,
        weights=base.weights,
        db_path=tmp_path / "test.db",
        outbox_dir=tmp_path / "outbox",
        llm=LLMConfig(api_key="test") if with_llm else None,
    )


def seed_document(conn) -> int:
    return repo.insert_document(
        conn,
        canonical_id="arxiv:2610.00001",
        kind="paper",
        title="Deep paper",
        abstract="abstract",
        url="https://arxiv.org/abs/2610.00001",
        published_at="2026-10-19T08:00:00+00:00",
        authority="A",
        primary_tag="1 模型与架构",
        tags=["1 模型与架构"],
    )


def seed_hn_document(conn) -> int:
    repo.upsert_source(
        conn,
        source_id="hackernews",
        kind="forum",
        name="Hacker News",
        authority="B",
        region="overseas",
        config={},
    )
    raw_id = repo.insert_raw_item(
        conn,
        source_id="hackernews",
        external_id="4444",
        url="https://example.com/story",
        title="Story",
        author=None,
        published_at=None,
        payload={"object_id": "4444"},
    )
    document_id = repo.insert_document(
        conn,
        canonical_id="https://example.com/story",
        kind="forum",
        title="Story",
        abstract=None,
        url="https://example.com/story",
        published_at=None,
        authority="B",
        primary_tag="4 Agent 与应用",
        tags=["4 Agent 与应用"],
    )
    repo.link_document_source(conn, document_id, raw_id)
    return document_id


def test_chunk_markdown_splits_by_heading_and_size() -> None:
    text = "# Intro\n\n" + ("alpha " * 1500) + "\n\n## Method\n\nshort body\n"
    chunks = chunk_markdown(text, max_chars=5000)
    assert len(chunks) >= 2
    assert chunks[0]["section"] == "Intro"
    assert any(chunk["section"] == "Method" for chunk in chunks)
    assert all(len(chunk["text"]) <= 5000 for chunk in chunks)
    assert [chunk["ord"] for chunk in chunks] == list(range(1, len(chunks) + 1))


def test_clean_html_strips_nav_and_keeps_figcaption() -> None:
    paragraphs = "".join(
        f"<p>Paragraph {i} discusses mixture of experts routing and evaluation details.</p>"
        for i in range(25)
    )
    html = (
        "<html><body><nav>Home Login Subscribe</nav><article>"
        f"<h1>Paper</h1>{paragraphs}"
        "<figure><img src='x.png'><figcaption>Figure 1: Routing accuracy across scales.</figcaption></figure>"
        "</article><footer>All rights reserved</footer></body></html>"
    )
    text = clean_html(html, "https://example.com/paper", kind="paper")
    assert "mixture of experts" in text
    assert "图表说明" in text
    assert "Routing accuracy across scales" in text
    assert "Login" not in text


def test_deep_read_persists_kb_and_chunks(tmp_path: Path, monkeypatch) -> None:
    config = make_config(tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    document_id = seed_document(conn)

    paragraphs = "".join(
        f"<p>Section text {i} about routing, capacity and evaluation.</p>" for i in range(40)
    )
    html = (
        f"<html><body><article><h1>Deep</h1><h2>Abstract</h2>{paragraphs}</article></body></html>"
    )
    monkeypatch.setattr(
        deep_read_mod, "fetch_html", lambda url, proxy, timeout=60.0: (html, url)
    )

    def fake_chat(self, conn, *, stage, system, user, max_tokens=800, document_id=None):
        if "片段内容" in user:
            return json.dumps(
                {
                    "problem": "p",
                    "method": "m",
                    "evidence": ["e1"],
                    "conclusions": ["c1"],
                    "limitations": ["l1"],
                    "claims": [{"kind": "result", "text": "claim one"}],
                },
                ensure_ascii=False,
            ), Usage(1, 1, 0.0)
        return json.dumps(
            {
                "problem": "merged problem",
                "method": "merged method",
                "evidence": ["e1", "e2"],
                "conclusions": ["c1"],
                "limitations": ["l1"],
                "l3": "deep explanation",
            },
            ensure_ascii=False,
        ), Usage(1, 1, 0.0)

    monkeypatch.setattr(deep_read_mod.DeepSeekLLM, "chat", fake_chat)

    document = repo.get_document(conn, document_id)
    result = deep_read_document(conn, config, document)

    assert result.chunks >= 1
    assert result.claims == result.chunks
    assert Path(result.local_path).exists()
    assert (tmp_path / "data" / "articles" / "arxiv_2610.00001" / "raw" / "article.html").exists()

    kb = repo.get_kb_article(conn, document_id)
    assert kb["problem"] == "merged problem"
    assert json.loads(kb["evidence"]) == ["e1", "e2"]
    assert repo.get_document(conn, document_id)["status"] == "kb_ready"
    assert len(repo.list_chunks(conn, document_id)) == result.chunks
    assert len(repo.list_claims(conn, document_id)) == result.claims


def test_arxiv_pdf_url() -> None:
    from ai_radar.pipeline.cleaning import arxiv_pdf_url

    assert arxiv_pdf_url("https://arxiv.org/abs/2610.00001") == "https://arxiv.org/pdf/2610.00001"
    assert arxiv_pdf_url("https://arxiv.org/html/2610.00001v2") == "https://arxiv.org/pdf/2610.00001v2"
    assert arxiv_pdf_url("https://example.com/post") == "https://example.com/post"


def test_deep_read_falls_back_to_pdf(tmp_path: Path, monkeypatch) -> None:
    config = make_config(tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    document_id = seed_document(conn)

    shell_html = (
        "<html><body><article>"
        + "<p>Body text about routing and capacity.</p>" * 40
        + "</article></body></html>"
    )
    monkeypatch.setattr(
        deep_read_mod, "fetch_html", lambda url, proxy, timeout=60.0: (shell_html, url)
    )
    monkeypatch.setattr(deep_read_mod, "fetch_pdf", lambda url, proxy, timeout=120.0: b"%PDF-1.4")
    monkeypatch.setattr(
        deep_read_mod,
        "pdf_to_text",
        lambda data: ("PDF extracted body about routing. " * 60),
    )

    def fake_chat(self, conn, *, stage, system, user, max_tokens=800, document_id=None):
        if "片段内容" in user:
            return (
                '{"problem": "p", "method": "m", "evidence": [], "conclusions": [], '
                '"limitations": [], "claims": [{"kind": "finding", "text": "c"}]}',
                Usage(1, 1, 0.0),
            )
        return (
            '{"problem": "p2", "method": "m2", "evidence": [], "conclusions": [], '
            '"limitations": [], "l3": "x"}',
            Usage(1, 1, 0.0),
        )

    monkeypatch.setattr(deep_read_mod.DeepSeekLLM, "chat", fake_chat)

    result = deep_read_document(conn, config, repo.get_document(conn, document_id))
    assert result.source_format == "pdf"
    assert (tmp_path / "data" / "articles" / "arxiv_2610.00001" / "raw" / "article.pdf").exists()
    assert repo.get_kb_article(conn, document_id) is not None


def test_deep_read_action_marks_queue(tmp_path: Path) -> None:
    config = make_config(tmp_path, with_llm=False)
    conn = connect(config.db_path)
    init_db(conn)
    document_id = seed_document(conn)

    result = handle_action(conn, {"action": "deep_read", "document_id": document_id})
    assert result["type"] == "text"
    assert "精读队列" in result["text"]
    assert repo.get_document(conn, document_id)["status"] == "deep_reading"
    assert repo.list_deep_read_queue(conn)[0]["id"] == document_id


def test_l3_card_renders_sections(tmp_path: Path) -> None:
    config = make_config(tmp_path, with_llm=False)
    conn = connect(config.db_path)
    init_db(conn)
    document = repo.get_document(conn, seed_document(conn))

    card = build_l3_card(
        document,
        {
            "problem": "问题",
            "method": "方法",
            "evidence": ["证据"],
            "conclusions": ["结论"],
            "limitations": ["局限"],
            "l3": "深度讲解内容",
        },
    )
    content = str(card)
    for expected in ("问题", "方法", "证据", "结论", "局限", "深度讲解内容"):
        assert expected in content
    assert card["header"]["template"] == "purple"

def test_parse_json_object_raises_llm_json_error() -> None:
    import pytest

    from ai_radar.llm.client import LLMJSONError, parse_json_object

    with pytest.raises(LLMJSONError):
        parse_json_object('{"problem": "a" "method": "b"}')


def test_parse_comments_strips_html_and_skips_empty() -> None:
    from ai_radar.adapters.sources.hackernews import parse_comments

    data = {
        "children": [
            {"text": "Great point about <i>routing</i> &amp; capacity."},
            {"text": None},
            {"text": "<p>Short</p>"},
            {"text": "<p>Second meaningful comment that is long enough.</p>"},
        ]
    }
    comments = parse_comments(data, limit=5)
    assert comments == [
        "Great point about routing & capacity.",
        "Second meaningful comment that is long enough.",
    ]


def test_deep_read_attaches_hn_community(tmp_path: Path, monkeypatch) -> None:
    """HN 条目在原文之外补一层「社区怎么看」，并随 kb 卡片落库。"""
    config = make_config(tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    document_id = seed_hn_document(conn)

    html = (
        "<html><body><article><h1>Story</h1>"
        + "".join(
            f"<p>Paragraph {i} about AI displacing workers and retraining programs.</p>"
            for i in range(40)
        )
        + "</article></body></html>"
    )
    monkeypatch.setattr(
        deep_read_mod, "fetch_html", lambda url, proxy, timeout=60.0: (html, url)
    )
    monkeypatch.setattr(
        deep_read_mod,
        "fetch_comments",
        lambda object_id, proxy=None, limit=8: ["c1", "c2", "c3"],
    )

    def fake_chat(self, conn, *, stage, system, user, max_tokens=800, document_id=None):
        if "评论" in user and "片段内容" not in user:
            return (
                json.dumps(
                    {"consensus": ["共识"], "disputes": ["有分歧"], "tips": []},
                    ensure_ascii=False,
                ),
                Usage(1, 1, 0.0),
            )
        if "片段内容" in user:
            return (
                '{"problem": "p", "method": "m", "evidence": [], "conclusions": [], '
                '"limitations": [], "claims": []}',
                Usage(1, 1, 0.0),
            )
        return (
            '{"problem": "p2", "method": "m2", "evidence": [], "conclusions": [], '
            '"limitations": [], "l3": "x"}',
            Usage(1, 1, 0.0),
        )

    monkeypatch.setattr(deep_read_mod.DeepSeekLLM, "chat", fake_chat)

    result = deep_read_document(conn, config, repo.get_document(conn, document_id))
    assert result.card["community"]["disputes"] == ["有分歧"]
    stored = json.loads(repo.get_kb_article(conn, document_id)["card_json"])
    assert stored["community"]["consensus"] == ["共识"]


def test_l3_card_renders_community(tmp_path: Path) -> None:
    config = make_config(tmp_path, with_llm=False)
    conn = connect(config.db_path)
    init_db(conn)
    document = repo.get_document(conn, seed_hn_document(conn))

    card = build_l3_card(
        document,
        {
            "problem": "问题",
            "method": "方法",
            "community": {"consensus": ["共识"], "disputes": ["分歧"], "tips": []},
        },
    )
    content = str(card)
    assert "社区怎么看" in content
    assert "共识" in content and "分歧" in content
