from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_radar.adapters.notify.fake import FakeNotifier
from ai_radar.config import AppConfig, load_config
from ai_radar.jobs.collect import run_collect
from ai_radar.pipeline.compare import compare_documents, find_related
from ai_radar.pipeline.digest import build_digest
from ai_radar.pipeline.interactions import handle_action
from ai_radar.pipeline.preferences import (
    format_preferences,
    pause_dimension,
    paused_dimensions,
    resolve_dimension,
    resume_dimension,
)
from ai_radar.store import repo
from ai_radar.store.db import connect, init_db
from ai_radar.util import iso, utcnow


def make_config(tmp_path: Path) -> AppConfig:
    base = load_config()
    return AppConfig(
        root=tmp_path,
        network=base.network,
        sources=base.sources,
        weights=base.weights,
        db_path=tmp_path / "test.db",
        outbox_dir=tmp_path / "outbox",
        llm=None,
    )


def make_llm_config(tmp_path: Path) -> AppConfig:
    """带 llm 配置的 AppConfig：配合假的 DeepSeekLLM 走完整条判定链路。"""
    base = load_config()
    return AppConfig(
        root=tmp_path,
        network=base.network,
        sources=base.sources,
        weights=base.weights,
        db_path=tmp_path / "test.db",
        outbox_dir=tmp_path / "outbox",
        llm=SimpleNamespace(),
    )


def add_document(
    conn,
    *,
    title: str,
    tag: str,
    tags: list[str] | None = None,
    status: str = "kb_ready",
    days_ago: int = 3,
    kind: str = "paper",
) -> int:
    document_id = repo.insert_document(
        conn,
        canonical_id=f"test:{title}",
        kind=kind,
        title=title,
        abstract="abstract",
        url=f"https://example.com/{title}",
        published_at=iso(utcnow() - timedelta(days=days_ago)),
        authority="A",
        primary_tag=tag,
        tags=tags or [tag],
    )
    repo.mark_status(conn, [document_id], status)
    return document_id


def add_kb(conn, document_id: int, *, problem: str, method: str = "method") -> None:
    repo.save_kb_article(
        conn,
        document_id=document_id,
        problem=problem,
        method=method,
        evidence=["evidence"],
        conclusions=["conclusion"],
        limitations=["limitation"],
        card={
            "problem": problem,
            "method": method,
            "evidence": ["evidence"],
            "conclusions": ["conclusion"],
            "limitations": ["limitation"],
            "l3": "l3",
        },
    )
    repo.insert_claims(
        conn,
        document_id=document_id,
        chunk_id=None,
        claims=[{"kind": "finding", "text": f"claim for {document_id}"}],
    )


def test_find_related_ranks_same_dimension_first(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    base = add_document(conn, title="Base routing paper", tag="1 模型与架构", tags=["1 模型与架构", "moe"])
    same = add_document(conn, title="Routing mixture of experts", tag="1 模型与架构", tags=["1 模型与架构", "moe"])
    other = add_document(conn, title="Database storage engine", tag="6 系统与基础设施", tags=["6 系统与基础设施"])
    add_kb(conn, base, problem="mixture of experts routing")
    add_kb(conn, same, problem="mixture of experts routing efficiency")
    add_kb(conn, other, problem="storage engine throughput")

    related = find_related(conn, base)
    assert related
    assert related[0]["document_id"] == same
    assert "同维度" in related[0]["reason"]
    assert all(item["document_id"] != base for item in related)


def test_compare_requires_kb(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    left = add_document(conn, title="A", tag="1 模型与架构", status="pushed")
    right = add_document(conn, title="B", tag="1 模型与架构")
    add_kb(conn, right, problem="b")

    with pytest.raises(ValueError):
        compare_documents(conn, left, right)


def test_compare_rows_and_evidence(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    left = add_document(conn, title="Left", tag="1 模型与架构", tags=["1 模型与架构", "attention"])
    right = add_document(conn, title="Right", tag="2 训练与对齐", tags=["2 训练与对齐", "attention"])
    add_kb(conn, left, problem="left problem", method="left method")
    add_kb(conn, right, problem="right problem", method="right method")

    result = compare_documents(conn, left, right)
    assert [row["aspect"] for row in result["rows"]] == ["问题", "方法", "证据", "结论", "局限"]
    assert result["rows"][0]["left"] == "left problem"
    assert result["shared_tags"] == ["attention"]
    assert "共同标签" in result["summary"]
    assert len(result["evidence"]) == 2
    assert result["evidence"][0]["claims"][0]["text"] == f"claim for {left}"


def test_pause_dimension_and_format(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    add_document(conn, title="Doc", tag="4 Agent 与应用", status="pushed")

    tag = resolve_dimension(conn, "4")
    assert tag == "4 Agent 与应用"

    pause_dimension(conn, tag)
    assert tag in paused_dimensions(conn)
    text = format_preferences(conn)
    assert "暂停维度" in text and tag in text

    resume_dimension(conn, tag)
    assert tag not in paused_dimensions(conn)


def test_digest_skips_paused_dimension(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    kept = add_document(
        conn, title="Kept blog", tag="1 模型与架构", status="observing", kind="blog"
    )
    paused = add_document(
        conn, title="Paused blog", tag="4 Agent 与应用", status="observing", kind="blog"
    )
    pause_dimension(conn, "4 Agent 与应用")

    result = build_digest(
        conn,
        config,
        notifier=FakeNotifier(config.outbox_dir),
        digest_date="2026-10-21",
    )
    assert result["count"] == 1
    content = Path(result["outbox"]).read_text(encoding="utf-8")
    assert "Kept blog" in content
    assert "Paused blog" not in content
    assert repo.get_document(conn, kept)["status"] == "pushed"
    assert repo.get_document(conn, paused)["status"] == "observing"


def test_digest_records_theme_dimension(tmp_path: Path) -> None:
    """主线维度要落库，否则维度轮转永远看不到主线赤字。"""
    config = make_config(tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    add_document(
        conn,
        title="JEV v2: Browser-Native Agent Sandbox",
        tag="1 模型与架构",
        status="observing",
        kind="paper",
    )
    add_document(
        conn,
        title="JEV 新版把沙箱搬进浏览器",
        tag="1 模型与架构",
        status="observing",
        kind="blog",
    )

    result = build_digest(
        conn,
        config,
        notifier=FakeNotifier(config.outbox_dir),
        digest_date="2026-10-21",
    )
    assert result["count"] >= 1

    topic = conn.execute(
        "SELECT dimension FROM topics WHERE topic_date = '2026-10-21'"
    ).fetchone()
    assert topic is not None
    assert topic["dimension"] == "1 模型与架构"


def test_missing_theme_note_only_for_daily(tmp_path: Path) -> None:
    """周报没有「主线」是正常结构，不该显示“今天没有强主线”。"""
    from ai_radar.pipeline.composer import Composition
    from ai_radar.pipeline.digest import _apply_theme_label

    conn = connect(tmp_path / "test.db")
    init_db(conn)

    daily = _apply_theme_label(None, conn, Composition(kind="daily", blocks=[]))
    weekly = _apply_theme_label(None, conn, Composition(kind="weekly", blocks=[]))
    assert daily.label == "" and "没有跨来源的强主线" in daily.why
    assert weekly.label == "" and weekly.why == ""
    assert daily.when == "" and weekly.when == ""
    assert daily.when_note == "" and weekly.when_note == ""


def test_theme_materials_fetch_body_when_abstract_is_placeholder(tmp_path: Path, monkeypatch) -> None:
    """摘要只是「点击查看原文>」时，要抓正文，否则模型看不到事件日期。"""
    from ai_radar.pipeline import digest as digest_mod
    from ai_radar.pipeline.topics import Topic

    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = repo.insert_document(
        conn,
        canonical_id="test:jev",
        kind="blog",
        title="TypeSafe 发布 Jev",
        abstract="点击查看原文>",
        url="https://example.com/jev",
        published_at="2026-10-27T00:00:00+00:00",
        authority="B",
        primary_tag="1 模型与架构",
        tags=["1 模型与架构"],
    )
    document = repo.get_document(conn, document_id)
    topic = Topic(
        key="jev",
        label="JEV",
        items=[{"doc": document, "score": 1.0, "tag": "1 模型与架构"}],
    )
    monkeypatch.setattr(
        digest_mod, "_fetch_body", lambda url: "文章明确写道：Jev 于 9 月 23 日正式发布。"
    )

    lines = digest_mod._theme_material_lines(topic)
    assert "9 月 23 日" in lines[0]


def test_theme_label_rejects_bucket_and_venue_names(tmp_path: Path) -> None:
    """「EMNLP 2026 三篇新论文」这种集合名不能当主线。"""
    from ai_radar.pipeline.composer import Composition
    from ai_radar.pipeline.digest import _apply_theme_label
    from ai_radar.pipeline.topics import Topic

    conn = connect(tmp_path / "test.db")
    init_db(conn)
    topic = Topic(key="emnlp", label="emnlp", items=[])

    class FakeLLM:
        def __init__(self, label: str) -> None:
            self.label = label

        def label_theme(self, conn, titles, history=""):
            return {"label": self.label, "why": "多篇同会论文"}

    bucket = _apply_theme_label(
        FakeLLM("EMNLP 2026 三篇新论文"),
        conn,
        Composition(kind="daily", blocks=[], theme=topic),
    )
    assert bucket.label == "" and "没有清晰的跨来源主线" in bucket.why

    empty = _apply_theme_label(
        FakeLLM(""), conn, Composition(kind="daily", blocks=[], theme=topic)
    )
    assert empty.label == ""


def test_theme_label_passes_through_inferred_event_time(tmp_path: Path) -> None:
    """模型给出事件时间时要一路带到卡片参数上。"""
    from ai_radar.pipeline.composer import Composition
    from ai_radar.pipeline.digest import _apply_theme_label
    from ai_radar.pipeline.topics import Topic

    conn = connect(tmp_path / "test.db")
    init_db(conn)
    topic = Topic(key="jev", label="JEV", items=[])

    class FakeLLM:
        def label_theme(self, conn, titles, history=""):
            return {"label": "Jev 模型", "why": "多源讨论", "when": "2025-08"}

    result = _apply_theme_label(
        FakeLLM(), conn, Composition(kind="daily", blocks=[], theme=topic)
    )
    assert result.label == "Jev 模型" and result.why == "多源讨论"
    assert result.when == "2025-08"
    assert result.when_note == "模型推断"


def test_theme_label_falls_back_to_earliest_material_day(tmp_path: Path, monkeypatch) -> None:
    """模型、原文页面、外网检索都拿不到事件时间时不能写空，否则回声判定会退化成 7 天窗口。"""
    from ai_radar.pipeline import digest as digest_mod
    from ai_radar.pipeline.composer import Composition
    from ai_radar.pipeline.topics import Topic

    conn = connect(tmp_path / "test.db")
    init_db(conn)
    topic = Topic(
        key="jev",
        label="JEV",
        items=[
            {"doc": {"kind": "blog", "url": "https://a.example/1", "title": "报道一",
                     "abstract": "", "published_at": "2026-10-27T00:00:00+00:00"}, "score": 1.0},
            {"doc": {"kind": "blog", "url": "https://a.example/2", "title": "报道二",
                     "abstract": "", "published_at": "2026-10-26T00:00:00+00:00"}, "score": 0.9},
            {"doc": {"kind": "repo", "url": "https://github.com/x/y", "title": "x/y",
                     "abstract": "", "published_at": "2020-01-01T00:00:00+00:00"}, "score": 0.8},
        ],
    )

    class FakeLLM:
        def label_theme(self, conn, titles, history=""):
            return {"label": "Jev 模型", "why": "多源讨论", "when": ""}

    monkeypatch.setattr(digest_mod, "_source_event_date", lambda theme, proxy=None: ("", ""))
    monkeypatch.setattr(
        digest_mod, "_search_event_date", lambda llm, conn, label, proxy=None: ("", "")
    )

    result = digest_mod._apply_theme_label(
        FakeLLM(), conn, Composition(kind="daily", blocks=[], theme=topic)
    )
    assert result.when == "2026-10-26"
    assert result.when_note == "材料最早报道日（上界）"


def test_earliest_material_day_skips_repo_and_falls_back_to_first_seen() -> None:
    from ai_radar.pipeline.digest import _earliest_material_day
    from ai_radar.pipeline.topics import Topic

    topic = Topic(
        key="x",
        label="X",
        items=[
            {"doc": {"kind": "repo", "published_at": "2020-01-01T00:00:00+00:00"}},
            {"doc": {"kind": "blog", "published_at": None,
                     "first_seen_at": "2026-10-28T03:00:00+00:00"}},
        ],
    )
    assert _earliest_material_day(topic) == ("2026-10-28", "材料最早报道日（上界）")
    assert _earliest_material_day(Topic(key="y", label="Y", items=[])) == ("", "")


def test_search_event_date_uses_news_results(tmp_path: Path, monkeypatch) -> None:
    """材料里没有日期时，用检索到的最早报道核对事件时间。"""
    from ai_radar.pipeline import digest as digest_mod

    conn = connect(tmp_path / "test.db")
    init_db(conn)
    monkeypatch.setattr(
        digest_mod,
        "search_news",
        lambda query, proxy=None, limit=8: [
            {"title": "TypeSafe AI Launches Jev Model", "published": "2026-09-17", "source": "SuaraGarut"},
            {"title": "Jev Model Full Public Launch", "published": "2026-09-20", "source": "36k"},
        ],
    )

    class FakeLLM:
        def chat(self, conn, **kwargs):
            return ("2026-09-17", None)

    when, note = digest_mod._search_event_date(FakeLLM(), conn, "JEV")
    assert when == "2026-09-17"
    assert "2 条" in note


def test_search_event_date_falls_back_to_earliest_coverage(tmp_path: Path, monkeypatch) -> None:
    """模型判断不出时，退到最早报道日期，并明确标注这是「最早报道」。"""
    from ai_radar.pipeline import digest as digest_mod

    conn = connect(tmp_path / "test.db")
    init_db(conn)
    monkeypatch.setattr(
        digest_mod, "search_news", lambda query, proxy=None, limit=8: []
    )
    assert digest_mod._search_event_date(object(), conn, "JEV") == ("", "")

    monkeypatch.setattr(
        digest_mod,
        "search_news",
        lambda query, proxy=None, limit=8: [
            {"title": "some report", "published": "2026-10-01", "source": "X"}
        ],
    )

    class UncertainLLM:
        def chat(self, conn, **kwargs):
            return ("不确定", None)

    when, note = digest_mod._search_event_date(UncertainLLM(), conn, "JEV")
    assert when == "2026-10-01"
    assert "最早报道" in note


def test_theme_label_falls_back_to_news_search(tmp_path: Path, monkeypatch) -> None:
    """模型不知道日期时，走外网检索并标注依据来源。"""
    from ai_radar.pipeline import digest as digest_mod
    from ai_radar.pipeline.composer import Composition
    from ai_radar.pipeline.topics import Topic

    conn = connect(tmp_path / "test.db")
    init_db(conn)
    topic = Topic(key="jev", label="JEV", items=[])

    class FakeLLM:
        def label_theme(self, conn, titles, history=""):
            return {"label": "JEV 模型发布", "why": "多源讨论", "when": ""}

        def chat(self, conn, **kwargs):
            return ("2026-09-17", None)

    monkeypatch.setattr(
        digest_mod,
        "search_news",
        lambda query, proxy=None, limit=8: [
            {"title": "TypeSafe AI Launches Jev", "published": "2026-09-17", "source": "SuaraGarut"}
        ],
    )

    result = digest_mod._apply_theme_label(
        FakeLLM(), conn, Composition(kind="daily", blocks=[], theme=topic)
    )
    assert result.label == "JEV 模型发布"
    assert result.when == "2026-09-17"
    assert "检索报道" in result.when_note


def test_page_date_reads_only_structured_fields() -> None:
    """页面日期只认结构化字段，正文里随手出现的日期不采信。"""
    from ai_radar.pipeline.digest import _page_date

    assert _page_date('<meta property="article:published_time" content="2026-09-15T08:00:00+08:00">') == "2026-09-15"
    assert _page_date('<meta content="2026/9/5" name="pubdate">') == "2026-09-05"
    assert _page_date('<script type="application/ld+json">{"datePublished": "2026-09-02"}</script>') == "2026-09-02"
    assert _page_date('<time datetime="2026-09-01">9 月 1 日</time>') == "2026-09-01"
    assert _page_date("<p>我们将在 9 月 23 日发布新版</p>") == ""
    assert _page_date("") == ""


def test_source_event_date_prefers_the_earliest_original_page(tmp_path: Path, monkeypatch) -> None:
    """转载越晚页面日期越晚：取读到的最早那天，仓库不参与。"""
    from ai_radar.pipeline import digest as digest_mod
    from ai_radar.pipeline.topics import Topic

    pages = {
        "https://typesafe.ai/blog/jev": '<meta property="article:published_time" content="2026-09-15">',
        "https://news.example.com/jev": '<meta name="pubdate" content="2026-10-27">',
        "https://github.com/x/jev": "",
    }
    monkeypatch.setattr(digest_mod, "_fetch_html", lambda url, proxy=None: pages.get(url, ""))
    topic = Topic(
        key="jev",
        label="JEV",
        items=[
            {
                "doc": {
                    "kind": "blog",
                    "url": "https://news.example.com/jev",
                    "title": "Jev 报道",
                    "published_at": "2026-10-27T00:00:00+00:00",
                },
                "score": 1.0,
            },
            {
                "doc": {
                    "kind": "repo",
                    "url": "https://github.com/x/jev",
                    "title": "x/jev",
                    "published_at": "2026-10-27T00:00:00+00:00",
                },
                "score": 0.9,
            },
            {
                "doc": {
                    "kind": "blog",
                    "url": "https://typesafe.ai/blog/jev",
                    "title": "Jev 发布",
                    "published_at": "2026-10-26T00:00:00+00:00",
                },
                "score": 0.8,
            },
        ],
    )

    when, note = digest_mod._source_event_date(topic)
    assert when == "2026-09-15"
    assert note == "原文页面 · typesafe.ai"


def test_source_event_date_ignores_pages_dated_like_the_material(tmp_path: Path, monkeypatch) -> None:
    """页面日期和材料发布日期一致时没有新信息，不能拿来充事件时间。"""
    from ai_radar.pipeline import digest as digest_mod
    from ai_radar.pipeline.topics import Topic

    monkeypatch.setattr(
        digest_mod,
        "_fetch_html",
        lambda url, proxy=None: '<meta name="citation_date" content="2026-10-26">'
        '<meta property="article:published_time" content="2026-10-26">',
    )
    topic = Topic(
        key="jev",
        label="JEV",
        items=[
            {
                "doc": {
                    "kind": "paper",
                    "url": "https://arxiv.org/abs/2610.35721",
                    "title": "Can JEV Support Clinical Trial Criteria Assessment?",
                    "published_at": "2026-10-26T16:41:12Z",
                },
                "score": 1.0,
            }
        ],
    )

    assert digest_mod._source_event_date(topic) == ("", "")


def test_theme_label_prefers_original_page_over_news_search(tmp_path: Path, monkeypatch) -> None:
    """原文页面读得到日期时，不再去外网赌检索结果。"""
    from ai_radar.pipeline import digest as digest_mod
    from ai_radar.pipeline.composer import Composition
    from ai_radar.pipeline.topics import Topic

    conn = connect(tmp_path / "test.db")
    init_db(conn)
    topic = Topic(
        key="jev",
        label="JEV",
        items=[
            {
                "doc": {
                    "kind": "blog",
                    "url": "https://typesafe.ai/blog/jev",
                    "title": "Jev 发布",
                    "abstract": "",
                },
                "score": 1.0,
            }
        ],
    )

    class FakeLLM:
        def label_theme(self, conn, titles, history=""):
            return {"label": "JEV 模型发布", "why": "多源讨论", "when": ""}

    searched: list[str] = []

    monkeypatch.setattr(
        digest_mod, "_fetch_html", lambda url, proxy=None: '<meta name="date" content="2026-09-15">'
    )
    monkeypatch.setattr(digest_mod, "search_news", lambda *args, **kwargs: searched.append("x") or [])

    result = digest_mod._apply_theme_label(
        FakeLLM(), conn, Composition(kind="daily", blocks=[], theme=topic)
    )
    assert result.label == "JEV 模型发布"
    assert result.when == "2026-09-15"
    assert "原文页面" in result.when_note
    assert searched == []


def test_event_date_cache_avoids_repeated_search(tmp_path: Path, monkeypatch) -> None:
    """检索结果会抖动：查到一次就复用，不能每次重新赌。"""
    from ai_radar.pipeline import digest as digest_mod

    conn = connect(tmp_path / "test.db")
    init_db(conn)
    calls: list[int] = []
    monkeypatch.setattr(
        digest_mod, "search_news", lambda *args, **kwargs: calls.append(1) or []
    )

    assert digest_mod._search_event_date(object(), conn, "JEV") == ("", "")

    digest_mod._store_event_date(conn, "JEV", "2026-09-21", "据 8 条检索报道")
    calls.clear()

    assert digest_mod._search_event_date(object(), conn, "JEV") == (
        "2026-09-21",
        "据 8 条检索报道",
    )
    assert calls == []


def test_event_date_cache_expires_after_a_week(tmp_path: Path, monkeypatch) -> None:
    from ai_radar.pipeline import digest as digest_mod

    conn = connect(tmp_path / "test.db")
    init_db(conn)
    stale = (utcnow() - timedelta(days=8)).isoformat()
    repo.set_text_preference(
        conn,
        "event_date:jev",
        json.dumps({"date": "2026-09-21", "note": "旧", "at": stale}),
    )
    monkeypatch.setattr(
        digest_mod, "search_news", lambda *args, **kwargs: []
    )
    assert digest_mod._search_event_date(object(), conn, "JEV") == ("", "")


def test_echo_rules_follow_event_time(tmp_path: Path) -> None:
    """事件时间没推进就是回声；推进了、或超出观察窗口，才允许再开主线。"""
    from ai_radar.pipeline import digest as digest_mod

    conn = connect(tmp_path / "test.db")
    init_db(conn)
    today = utcnow().date()
    repo.add_topic(
        conn,
        digest_id=None,
        topic_date=(today - timedelta(days=10)).isoformat(),
        kind="daily",
        label="Jev 模型发布",
        summary="",
        entity="jev",
        score=3.0,
        item_count=3,
        event_date="2026-09-16",
    )

    # 事件时间只差 2 天：同一件事又被报道了一轮
    assert digest_mod._echo_of(conn, "jev", "2026-09-18", today) is not None
    # 只有月份时按月初算，晚于历史日期 → 也算没推进
    assert digest_mod._echo_of(conn, "jev", "2026-09", today) is not None
    # 事件时间推进了一个多月：新版/新阶段，允许再开主线
    assert digest_mod._echo_of(conn, "jev", "2026-10-20", today) is None
    # 实体不同，互不影响
    assert digest_mod._echo_of(conn, "qwen", "2026-09-16", today) is None
    # 上次推送超出窗口，且这次拿不到事件时间：放行
    assert digest_mod._echo_of(conn, "jev", "", today) is None


def test_echo_respects_the_models_update_judgement(tmp_path: Path) -> None:
    """模型读了历史后的 is_update 只做补充：明确没变就降级，说有更新还得有实锤。"""
    from ai_radar.pipeline import digest as digest_mod

    conn = connect(tmp_path / "test.db")
    init_db(conn)
    today = utcnow().date()
    repo.add_topic(
        conn,
        digest_id=None,
        topic_date=(today - timedelta(days=10)).isoformat(),
        kind="daily",
        label="Jev 模型发布",
        summary="",
        entity="jev",
        score=3.0,
        item_count=3,
        event_date="2026-09-16",
    )

    # 事件时间明明往前走了，但模型说「只是后续报道」→ 仍然降级
    assert digest_mod._echo_of(conn, "jev", "2026-10-20", today, is_update=False) is not None
    # 说是新版，change 里有版本号 → 放行
    assert (
        digest_mod._echo_of(
            conn,
            "jev",
            "2026-09-16",
            today,
            is_update=True,
            change="发布 1.14 版本，新增图文判断",
        )
        is None
    )
    # 说是更新，但一句变化都说不出 → 回到事件时间规则
    assert (
        digest_mod._echo_of(conn, "jev", "2026-09-16", today, is_update=True, change="")
        is not None
    )
    # 功能更新没有版本号，只要说得出变化就放行
    assert (
        digest_mod._echo_of(
            conn,
            "jev",
            "2026-09-16",
            today,
            is_update=True,
            change="上线多选评分与置信度校准",
        )
        is None
    )


def test_theme_history_is_fed_to_the_model(tmp_path: Path) -> None:
    """同一实体再出现时，要把上次推送的情况告诉模型，它才判断得了更没更新。"""
    from ai_radar.pipeline import digest as digest_mod
    from ai_radar.pipeline.composer import Composition
    from ai_radar.pipeline.topics import Topic

    conn = connect(tmp_path / "test.db")
    init_db(conn)
    today = utcnow().date()
    repo.add_topic(
        conn,
        digest_id=None,
        topic_date=(today - timedelta(days=5)).isoformat(),
        kind="daily",
        label="Jev 模型发布",
        summary="",
        entity="jev",
        score=3.0,
        item_count=3,
        event_date="2026-09-16",
    )
    seen: list[str] = []

    class FakeLLM:
        def label_theme(self, conn, titles, history=""):
            seen.append(history)
            return {"label": "Jev 更新", "why": "多源讨论", "when": "2026-10-28"}

    topic = Topic(key="jev", label="jev", items=[])
    history = digest_mod._theme_history(conn, "jev", today)
    digest_mod._apply_theme_label(
        FakeLLM(),
        conn,
        Composition(kind="daily", blocks=[], theme=topic),
        history=history,
    )

    assert seen and "2026-09-16" in seen[0] and "Jev 模型发布" in seen[0]


def test_echo_demotes_a_repeat_of_the_same_event(tmp_path: Path) -> None:
    """同一实体刚推过、事件时间又没推进时，不再占主线席位，只发快讯。"""
    config = make_config(tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    repo.add_topic(
        conn,
        digest_id=None,
        topic_date=(utcnow().date() - timedelta(days=1)).isoformat(),
        kind="daily",
        label="Jev 模型发布",
        summary="",
        entity="jev",
        score=3.0,
        item_count=3,
        event_date="2026-09-16",
    )
    add_document(
        conn, title="JEV 又来了一轮讨论", tag="1 模型与架构", status="observing", kind="blog"
    )
    add_document(
        conn, title="JEV v2 的另一篇分析", tag="1 模型与架构", status="observing", kind="paper"
    )

    result = build_digest(
        conn,
        config,
        notifier=FakeNotifier(config.outbox_dir),
        digest_date="2026-10-21",
    )

    stored = repo.get_digest(conn, "2026-10-21")
    stats = json.loads(stored["stats_json"])
    assert result["count"] >= 1
    assert stats["theme"] == ""
    assert stats["theme_echo"] == "jev"


@pytest.mark.parametrize(
    "case, event_date, when, days_ago, is_update, change, demoted",
    [
        ("首次推送，没有历史", "2026-09-16", "2026-09-16", None, None, "", False),
        ("3 天后又一轮复盘，事件时间没动", "2026-09-16", "2026-09-16", 3, None, "", True),
        ("事件时间只晚 1 天", "2026-09-16", "2026-09-17", 3, None, "", True),
        ("事件时间正好推进 3 天（边界）", "2026-09-16", "2026-09-19", 10, None, "", False),
        ("真更新：1.14 + 版本号", "2026-09-16", "2026-11-05", 30, True, "发布 1.14 版本", False),
        ("功能更新，说得出变化内容", "2026-09-16", "2026-09-16", 3, True, "上线多选评分与置信度校准", False),
        ("说是更新但没说变化", "2026-09-16", "2026-09-16", 3, True, "", True),
        ("模型说没变，检索却给出更晚日期", "2026-09-16", "2026-11-05", 30, False, "", True),
        ("时间没动，但模型给出开源版本", "2026-09-16", "2026-09-16", 3, True, "开源 1.14 权重", False),
        ("事件时间缺失，上次推送在窗口内", "", "", 5, None, "", True),
        ("事件时间缺失，上次推送超出窗口", "", "", 30, None, "", False),
        ("只有月份的事件时间按月初算", "2026-09-16", "2026-09", 3, None, "", True),
    ],
)
def test_echo_decision_matrix(
    tmp_path: Path,
    case: str,
    event_date: str,
    when: str,
    days_ago: int | None,
    is_update: bool | None,
    change: str,
    demoted: bool,
) -> None:
    """回声/更新判定的完整决策表：日期推进、模型判定、兜底窗口各管一段。"""
    from ai_radar.pipeline import digest as digest_mod

    conn = connect(tmp_path / "test.db")
    init_db(conn)
    today = utcnow().date()
    if days_ago is not None:
        repo.add_topic(
            conn,
            digest_id=None,
            topic_date=(today - timedelta(days=days_ago)).isoformat(),
            kind="daily",
            label="Jev 模型发布",
            summary="",
            entity="jev",
            score=3.0,
            item_count=3,
            event_date=event_date,
        )

    result = digest_mod._echo_of(
        conn, "jev", when, today, is_update=is_update, change=change
    )
    assert (result is not None) is demoted, case


def test_echo_switches_to_another_strong_theme(tmp_path: Path, monkeypatch) -> None:
    """JEV 被降级后，当天还有别的强主线就换一条，不是简单地把主线整块删掉。"""
    from ai_radar.pipeline import digest as digest_mod

    config = make_llm_config(tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    monkeypatch.setattr(
        "ai_radar.pipeline.composer.embedding.ensure_vectors", lambda *a, **k: {}
    )
    monkeypatch.setattr(digest_mod, "_fetch_html", lambda url, proxy=None: "")

    today = utcnow().date()
    repo.add_topic(
        conn,
        digest_id=None,
        topic_date=(today - timedelta(days=1)).isoformat(),
        kind="daily",
        label="Jev 模型发布",
        summary="",
        entity="jev",
        score=3.0,
        item_count=3,
        event_date="2026-09-16",
    )
    # JEV 更新鲜，会先被选成主线；被降级后应换到另一个强主线 QWEN。
    add_document(conn, title="JEV 复盘：三个视角", tag="1 模型与架构", status="observing", kind="blog", days_ago=1)
    add_document(conn, title="JEV 决策范式再解读", tag="1 模型与架构", status="observing", kind="paper", days_ago=3)
    add_document(conn, title="QWEN 新版发布", tag="1 模型与架构", status="observing", kind="blog", days_ago=5)
    add_document(conn, title="QWEN 新版实测", tag="1 模型与架构", status="observing", kind="paper", days_ago=5)

    class FakeDeepSeek:
        def __init__(self, config) -> None:
            pass

        def label_theme(self, conn, titles, history=""):
            joined = " ".join(titles)
            if "QWEN" in joined:
                return {"label": "Qwen 新版", "why": "多源讨论", "when": "2026-10-29"}
            return {
                "label": "Jev 复盘",
                "why": "多源讨论",
                "when": "2026-09-16",
                "is_update": False,
            }

        def summarize(self, conn, document):
            return None

    monkeypatch.setattr(digest_mod, "DeepSeekLLM", FakeDeepSeek)

    result = build_digest(
        conn, config, notifier=FakeNotifier(config.outbox_dir), digest_date="2026-10-21"
    )

    stats = json.loads(repo.get_digest(conn, "2026-10-21")["stats_json"])
    assert stats["theme_echo"] == "jev"
    assert result["theme"] == "Qwen 新版"
    topic = conn.execute("SELECT entity, event_date FROM topics WHERE topic_date = '2026-10-21'").fetchone()
    assert topic["entity"] == "qwen"
    assert topic["event_date"] == "2026-10-29"


def test_update_keeps_the_theme_and_stores_event_date(tmp_path: Path, monkeypatch) -> None:
    """同一实体明明发了新版本：主线照开，事件时间用新版本的，历史不覆盖。"""
    from ai_radar.pipeline import digest as digest_mod

    config = make_llm_config(tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    monkeypatch.setattr(
        "ai_radar.pipeline.composer.embedding.ensure_vectors", lambda *a, **k: {}
    )
    monkeypatch.setattr(digest_mod, "_fetch_html", lambda url, proxy=None: "")

    today = utcnow().date()
    repo.add_topic(
        conn,
        digest_id=None,
        topic_date=(today - timedelta(days=30)).isoformat(),
        kind="daily",
        label="Jev 模型发布",
        summary="",
        entity="jev",
        score=3.0,
        item_count=3,
        event_date="2026-09-16",
    )
    add_document(conn, title="JEV 1.14 发布：新增图文判断", tag="1 模型与架构", status="observing", kind="blog", days_ago=2)
    add_document(conn, title="JEV 1.14 开源权重", tag="1 模型与架构", status="observing", kind="blog", days_ago=2)
    add_document(conn, title="JEV 1.14 实测报告", tag="1 模型与架构", status="observing", kind="paper", days_ago=2)

    class FakeDeepSeek:
        def __init__(self, config) -> None:
            pass

        def label_theme(self, conn, titles, history=""):
            return {
                "label": "Jev 1.14 发布",
                "why": "新增图文判断并开源权重",
                "when": "2026-11-05",
                "is_update": True,
                "change": "发布 1.14 版本，新增图文判断与多选评分",
            }

        def summarize(self, conn, document):
            return None

    monkeypatch.setattr(digest_mod, "DeepSeekLLM", FakeDeepSeek)

    result = build_digest(
        conn, config, notifier=FakeNotifier(config.outbox_dir), digest_date="2026-10-21"
    )

    stats = json.loads(repo.get_digest(conn, "2026-10-21")["stats_json"])
    assert stats["theme_echo"] == ""
    assert result["theme"] == "Jev 1.14 发布"
    assert stats["theme_when"] == "2026-11-05"
    row = conn.execute("SELECT event_date FROM topics WHERE topic_date = '2026-10-21'").fetchone()
    assert row["event_date"] == "2026-11-05"


def test_digest_skips_documents_from_paused_source(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    document_id = add_document(
        conn, title="Paused source doc", tag="1 模型与架构", status="observing"
    )
    repo.upsert_source(
        conn,
        source_id="arxiv",
        kind="arxiv",
        name="arXiv",
        authority="A",
        region="overseas",
        config={},
    )
    raw_id = repo.insert_raw_item(
        conn,
        source_id="arxiv",
        external_id="test:paused",
        url="https://example.com/paused",
        title="Paused source doc",
        author=None,
        published_at=None,
        payload={},
    )
    repo.link_document_source(conn, document_id, raw_id)
    from ai_radar.pipeline.preferences import pause_source

    pause_source(conn, "arxiv")

    result = build_digest(
        conn,
        config,
        notifier=FakeNotifier(config.outbox_dir),
        digest_date="2026-10-21",
    )
    assert result["count"] == 0


def test_collect_skips_paused_sources(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    conn = connect(config.db_path)
    init_db(conn)
    for source in config.sources:
        repo.set_preference(conn, f"paused:source:{source.id}", 1.0)

    stats = run_collect(conn, config)
    assert stats["items"] == 0
    assert stats["errors"] == []
    assert all(
        value == {"skipped": "paused"} for value in stats["sources"].values()
    )


def test_related_and_compare_cards(tmp_path: Path) -> None:
    from ai_radar.adapters.notify.feishu import build_compare_card, build_related_card

    conn = connect(tmp_path / "test.db")
    init_db(conn)
    left = add_document(conn, title="Left", tag="1 模型与架构")
    right = add_document(conn, title="Right", tag="1 模型与架构")
    add_kb(conn, left, problem="left")
    add_kb(conn, right, problem="right")

    related_card = build_related_card(
        repo.get_document(conn, left),
        [
            {
                "document_id": right,
                "title": "Right",
                "tag": "1 模型与架构",
                "url": "https://example.com/right",
                "score": 0.8,
                "reason": "同维度",
            }
        ],
    )
    assert "相关" in str(related_card)
    assert "对比" in str(related_card)

    compare_card = build_compare_card(
        repo.get_document(conn, left),
        repo.get_document(conn, right),
        compare_documents(conn, left, right),
    )
    assert "原文依据" in str(compare_card)
    assert "A 详情" in str(compare_card)


def test_related_and_compare_actions(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    left = add_document(conn, title="Left", tag="1 模型与架构")
    right = add_document(conn, title="Right", tag="1 模型与架构")
    add_kb(conn, left, problem="left")
    add_kb(conn, right, problem="right")

    related = handle_action(conn, {"action": "related", "document_id": left})
    assert related == {"type": "related_card", "document_id": left}

    compared = handle_action(
        conn, {"action": "compare", "left_id": left, "right_id": right}
    )
    assert compared["type"] == "compare_card"
    assert compared["left_id"] == left
    assert compared["right_id"] == right
