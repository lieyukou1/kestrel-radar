from __future__ import annotations

from datetime import timedelta

import pytest

from ai_radar.llm.client import BudgetExceeded, LLMJSONError
from ai_radar.pipeline import discussion
from ai_radar.store import repo
from ai_radar.store.db import connect, init_db
from ai_radar.util import iso, utcnow


class FakeLLM:
    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[str] = []

    def summarize(self, conn, document):
        return {"problem": "p", "method": "m", "conclusions": [], "why": "w"}

    def chat(self, conn, *, stage, system, user, max_tokens=800, document_id=None):
        self.calls.append(stage)
        if self.error is not None:
            raise self.error
        return self.text, None


def make_document(tmp_path, *, kind: str = "forum", source: str = "lobsters"):
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    repo.upsert_source(
        conn,
        source_id=source,
        kind=source,
        name=source,
        authority="B",
        region="overseas",
        config={},
    )
    document_id = repo.insert_document(
        conn,
        canonical_id=f"test:{source}:{kind}",
        kind=kind,
        title="A story",
        abstract="abstract",
        url="https://example.com/story",
        published_at=iso(utcnow()),
        authority="B",
        primary_tag="4 Agent 与应用",
        tags=["4 Agent 与应用"],
    )
    raw_id = repo.insert_raw_item(
        conn,
        source_id=source,
        external_id="abc123",
        url="https://example.com/story",
        title="A story",
        author=None,
        published_at=None,
        payload={},
    )
    repo.link_document_source(conn, document_id, raw_id)
    return conn, repo.get_document(conn, document_id)


def cache_discussion(conn, document_id: int, points: list[str], *, hours_ago: float) -> None:
    digest_id = repo.create_digest(conn, "2026-11-02")
    repo.add_digest_item(
        conn,
        digest_id=digest_id,
        document_id=document_id,
        rank=1,
        score=1.0,
        reason="",
        summary={
            "discussion": points,
            "discussion_at": iso(utcnow() - timedelta(hours=hours_ago)),
        },
    )


def test_collect_comments_uses_lobsters_and_drops_thin_threads(tmp_path, monkeypatch) -> None:
    conn, document = make_document(tmp_path, source="lobsters")
    monkeypatch.setattr(
        discussion.lobsters,
        "fetch_comments",
        lambda short_id, proxy=None, limit=8: ["a long enough comment", "second one"],
    )
    assert discussion.collect_comments(conn, document) == []

    monkeypatch.setattr(
        discussion.lobsters,
        "fetch_comments",
        lambda short_id, proxy=None, limit=8: ["one", "two", "three"],
    )
    assert discussion.collect_comments(conn, document) == ["one", "two", "three"]


def test_collect_comments_uses_hackernews_id(tmp_path, monkeypatch) -> None:
    conn, document = make_document(tmp_path, source="hackernews")
    captured: dict = {}

    def fake(short_id, proxy=None, limit=8):
        captured["id"] = short_id
        return ["one", "two", "three"]

    monkeypatch.setattr(discussion.hackernews, "fetch_comments", fake)
    assert discussion.collect_comments(conn, document) == ["one", "two", "three"]
    assert captured["id"] == "abc123"


def test_enrich_adds_points_when_comments_are_enough(tmp_path, monkeypatch) -> None:
    conn, document = make_document(tmp_path)
    monkeypatch.setattr(
        discussion, "collect_comments", lambda *a, **k: ["one", "two", "three"]
    )
    llm = FakeLLM('{"points": ["社区觉得成本偏高", "有人复现失败"]}')

    summary = discussion.enrich(conn, document, {"abstract": "x"}, llm=llm)

    assert summary["discussion"] == ["社区觉得成本偏高", "有人复现失败"]
    assert summary["discussion_at"]


def test_enrich_skips_non_forum_documents(tmp_path, monkeypatch) -> None:
    conn, document = make_document(tmp_path, kind="paper")
    monkeypatch.setattr(
        discussion, "collect_comments", lambda *a, **k: pytest.fail("不该抓评论")
    )
    assert discussion.enrich(conn, document, {"abstract": "x"}, llm=FakeLLM()) == {
        "abstract": "x"
    }


def test_enrich_keeps_summary_when_budget_is_exhausted(tmp_path, monkeypatch) -> None:
    conn, document = make_document(tmp_path)
    monkeypatch.setattr(
        discussion, "collect_comments", lambda *a, **k: ["one", "two", "three"]
    )
    summary = discussion.enrich(
        conn,
        document,
        {"abstract": "x"},
        llm=FakeLLM(error=BudgetExceeded("budget")),
    )
    assert summary == {"abstract": "x"}


def test_enrich_keeps_summary_when_json_is_broken(tmp_path, monkeypatch) -> None:
    conn, document = make_document(tmp_path)
    monkeypatch.setattr(
        discussion, "collect_comments", lambda *a, **k: ["one", "two", "three"]
    )
    summary = discussion.enrich(
        conn,
        document,
        {"abstract": "x"},
        llm=FakeLLM('{"points": [oops]}'),
    )
    assert summary == {"abstract": "x"}


def test_fresh_cache_skips_fetch_and_model(tmp_path, monkeypatch) -> None:
    conn, document = make_document(tmp_path)
    cache_discussion(conn, int(document["id"]), ["缓存里的观点"], hours_ago=1)
    monkeypatch.setattr(
        discussion, "collect_comments", lambda *a, **k: pytest.fail("不该重新抓评论")
    )
    llm = FakeLLM('{"points": ["新观点"]}')

    summary = discussion.enrich(conn, document, {"abstract": "x"}, llm=llm)

    assert summary["discussion"] == ["缓存里的观点"]
    assert llm.calls == []


def test_stale_cache_is_recomputed(tmp_path, monkeypatch) -> None:
    conn, document = make_document(tmp_path)
    cache_discussion(conn, int(document["id"]), ["过期观点"], hours_ago=73)
    monkeypatch.setattr(
        discussion, "collect_comments", lambda *a, **k: ["one", "two", "three"]
    )
    llm = FakeLLM('{"points": ["新观点"]}')

    summary = discussion.enrich(conn, document, {"abstract": "x"}, llm=llm)

    assert summary["discussion"] == ["新观点"]
    assert llm.calls == ["discussion"]


def test_budget_error_does_not_escape(tmp_path, monkeypatch) -> None:
    """LLMJSONError 是 LLMError 的子类，也必须被吞掉。"""
    conn, document = make_document(tmp_path)
    monkeypatch.setattr(
        discussion, "collect_comments", lambda *a, **k: ["one", "two", "three"]
    )
    summary = discussion.enrich(
        conn, document, {}, llm=FakeLLM(error=LLMJSONError("bad"))
    )
    assert summary == {}


def test_build_digest_persists_discussion_into_summary(tmp_path, monkeypatch) -> None:
    """端到端：digest 组装时把社区观点写进 summary_json。"""
    from types import SimpleNamespace

    from ai_radar.adapters.notify.fake import FakeNotifier
    from ai_radar.pipeline import digest as digest_mod
    from ai_radar.pipeline.composer import Block, Composition

    conn, document = make_document(tmp_path)
    entry = {"doc": document, "score": 0.8, "tag": "4 Agent 与应用"}
    monkeypatch.setattr(digest_mod, "collect_candidates", lambda *a, **k: [entry])
    monkeypatch.setattr(
        digest_mod.composer,
        "compose_daily",
        lambda *a, **k: Composition(
            kind="daily",
            blocks=[Block(section="brief", title="快讯", items=[entry])],
            theme=None,
        ),
    )
    monkeypatch.setattr(
        discussion, "collect_comments", lambda *a, **k: ["one", "two", "three"]
    )
    monkeypatch.setattr(
        digest_mod, "DeepSeekLLM", lambda cfg: FakeLLM('{"points": ["社区观点一"]}')
    )

    config = SimpleNamespace(
        llm=SimpleNamespace(),
        network=SimpleNamespace(proxy=None, mode="overseas-only"),
        weights={},
        outbox_dir=tmp_path / "outbox",
    )
    digest_mod.build_digest(
        conn,
        config,
        notifier=FakeNotifier(config.outbox_dir),
        digest_date="2026-11-02",
        kind="daily",
    )

    row = conn.execute(
        "SELECT summary_json FROM digest_items WHERE document_id = ?",
        (int(document["id"]),),
    ).fetchone()
    assert "社区观点一" in row["summary_json"]
