from __future__ import annotations

from pathlib import Path

import pytest

from ai_radar.adapters.notify.feishu import build_detail_card
from ai_radar.adapters.sources.github import GitHubAdapter
from ai_radar.adapters.sources.hf_daily import HFDailyAdapter
from ai_radar.adapters.sources.rss import RSSAdapter, strip_html
from ai_radar.llm.client import LLMError, estimate_cost, parse_json_object
from ai_radar.pipeline.interactions import handle_action
from ai_radar.store import repo
from ai_radar.store.db import connect, init_db


def test_parse_json_object_handles_fences_and_noise() -> None:
    assert parse_json_object('{"a": 1}') == {"a": 1}
    assert parse_json_object('```json\n{"problem": "x"}\n```') == {"problem": "x"}
    assert parse_json_object('说明文字 {"a": {"b": 2}} 结束') == {"a": {"b": 2}}
    with pytest.raises(LLMError):
        parse_json_object("no json here")


def test_estimate_cost_uses_per_million_prices() -> None:
    usage = {"prompt_tokens": 1000, "completion_tokens": 500}
    cost = estimate_cost(usage, input_cny_per_million=1.0, output_cny_per_million=4.0)
    assert cost == pytest.approx((1000 * 1.0 + 500 * 4.0) / 1_000_000)


def test_summarize_handles_braces_in_abstract(tmp_path: Path, monkeypatch) -> None:
    from ai_radar.config import LLMConfig
    from ai_radar.llm.client import DeepSeekLLM, Usage

    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = repo.insert_document(
        conn,
        canonical_id="test:braces",
        kind="paper",
        title="Braces",
        abstract=r"we use \mathrm{softmax}(QK^T) and {json: true}",
        url="https://example.com/braces",
        published_at="2026-10-19T08:00:00+00:00",
        authority="A",
        primary_tag="1 模型与架构",
        tags=["1 模型与架构"],
    )
    document = repo.get_document(conn, document_id)

    captured: dict = {}

    def fake_chat(self, conn, *, stage, system, user, max_tokens=800, document_id=None):
        captured["user"] = user
        return '{"problem": "p", "method": "m", "conclusions": [], "why": "w"}', Usage(1, 1, 0.0)

    monkeypatch.setattr(DeepSeekLLM, "chat", fake_chat)
    summary = DeepSeekLLM(LLMConfig(api_key="x")).summarize(conn, document)

    assert summary["problem"] == "p"
    assert r"\mathrm{softmax}" in captured["user"]


def test_hf_daily_parse() -> None:
    payload = [
        {
            "title": "A Paper",
            "publishedAt": "2026-10-20T00:00:00.000Z",
            "upvotes": 12,
            "numComments": 3,
            "paper": {
                "id": "2601.00001",
                "title": "A Paper",
                "summary": "abstract",
                "authors": [{"name": "Alice"}, {"name": "Bob"}],
                "githubStars": 42,
            },
        },
        # HF 现在把 upvotes 放在 paper 里面，只读顶层会一直是 0
        {
            "title": "Another Paper",
            "publishedAt": "2026-10-21T00:00:00.000Z",
            "numComments": 1,
            "paper": {
                "id": "2601.00003",
                "title": "Another Paper",
                "summary": "abstract",
                "upvotes": 11,
            },
        },
        {"title": "no paper id"},
    ]
    items = HFDailyAdapter().parse(payload)
    assert len(items) == 2
    item = items[0]
    assert item.external_id == "2601.00001"
    assert item.url == "https://arxiv.org/abs/2601.00001"
    assert item.kind == "paper"
    assert item.author == "Alice, Bob"
    assert item.signals == {"points": 12, "comments": 3, "stars": 42}
    assert items[1].signals["points"] == 11


def test_github_parse() -> None:
    payload = {
        "items": [
            {
                "full_name": "acme/llm-kit",
                "html_url": "https://github.com/acme/llm-kit",
                "description": "A toolkit",
                "created_at": "2026-10-18T00:00:00Z",
                "stargazers_count": 999,
                "owner": {"login": "acme"},
            },
            {"description": "missing name"},
        ]
    }
    items = GitHubAdapter().parse(payload)
    assert len(items) == 1
    assert items[0].kind == "repo"
    assert items[0].external_id == "acme/llm-kit"
    assert items[0].signals["stars"] == 999


def test_rss_parse_feed_strips_html() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>Feed</title>
      <item>
        <title>文章 &amp; 标题</title>
        <link>https://example.com/a</link>
        <description><![CDATA[<p>hello <b>world</b></p>]]></description>
        <pubDate>Tue, 20 Oct 2026 08:00:00 GMT</pubDate>
      </item>
    </channel></rss>"""
    items = RSSAdapter().parse_feed(xml, {"name": "测试源", "authority": "B"})
    assert len(items) == 1
    assert items[0].title == "文章 & 标题"
    assert items[0].summary == "hello world"
    assert items[0].kind == "blog"
    assert items[0].published_at == "2026-10-20T08:00:00+00:00"
    assert strip_html("<p>a</p> b") == "a b"


def make_document(conn, title: str = "Detail paper") -> int:
    return repo.insert_document(
        conn,
        canonical_id=f"test:{title}",
        kind="paper",
        title=title,
        abstract="short abstract",
        url="https://example.com/detail",
        published_at="2026-10-19T08:00:00+00:00",
        authority="A",
        primary_tag="1 模型与架构",
        tags=["1 模型与架构"],
    )


def test_detail_action_returns_card_and_marks_read(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = make_document(conn)

    result = handle_action(conn, {"action": "detail", "document_id": document_id})
    assert result["type"] == "detail_card"
    assert result["document_id"] == document_id

    row = repo.get_document(conn, document_id)
    assert row["read_count"] == 1
    assert row["read_at"] is not None
    assert row["status"] == "read"


def test_detail_card_renders_summary_sections(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = make_document(conn)
    document = repo.get_document(conn, document_id)

    summary = {
        "problem": "问题描述",
        "method": "方法描述",
        "conclusions": ["结论一", "结论二"],
        "why": "值得关注",
    }
    card = build_detail_card(document, summary, "2026-10-21")
    content = str(card)
    assert "问题描述" in content
    assert "方法描述" in content
    assert "结论一" in content
    assert "值得关注" in content
    assert len(card["elements"]) >= 5


def test_detail_card_falls_back_to_abstract(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = make_document(conn)
    document = repo.get_document(conn, document_id)

    card = build_detail_card(document, {"abstract": "只有摘要"}, None)
    assert "只有摘要" in str(card)
