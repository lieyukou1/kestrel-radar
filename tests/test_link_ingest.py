from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_radar.pipeline import link_ingest
from ai_radar.pipeline.cleaning import CleanError
from ai_radar.store import repo
from ai_radar.store.db import connect, init_db

BODY = "这是一段用于测试抓取与精读流程的中文正文，包含足够多的字符以便通过清洗阈值。" * 40
ARTICLE_HTML = f"""<html><head>
<title>测试文章标题</title>
<meta property="og:title" content="测试文章标题">
<meta property="article:published_time" content="2026-11-02T08:00:00+08:00">
</head><body><article><h1>测试文章标题</h1><p>{BODY}</p></article></body></html>"""


def make_conn(tmp_path: Path):
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    return conn


def make_config() -> SimpleNamespace:
    return SimpleNamespace(
        network=SimpleNamespace(proxy=None, mode="overseas-only"),
    )


def test_extract_url_strips_chinese_punctuation() -> None:
    assert (
        link_ingest.extract_url("看看这个 https://mp.weixin.qq.com/s/abc）")
        == "https://mp.weixin.qq.com/s/abc"
    )
    assert link_ingest.extract_url("没有链接") is None
    assert link_ingest.extract_url("") is None


def test_ingest_creates_document_and_queues(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        link_ingest,
        "fetch_html",
        lambda url, proxy: (ARTICLE_HTML, "https://example.com/post"),
    )
    conn = make_conn(tmp_path)

    result = link_ingest.ingest_link(conn, make_config(), "https://example.com/post")

    assert result["state"] == "queued"
    document = repo.get_document(conn, result["document_id"])
    assert document["status"] == "deep_reading"
    assert document["title"] == "测试文章标题"
    assert document["published_at"] == "2026-11-02"
    assert repo.action_count(conn, int(document["id"]), "deep_read") == 1


def test_second_ingest_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        link_ingest,
        "fetch_html",
        lambda url, proxy: (ARTICLE_HTML, url),
    )
    conn = make_conn(tmp_path)
    first = link_ingest.ingest_link(conn, make_config(), "https://example.com/post")

    monkeypatch.setattr(
        link_ingest,
        "fetch_html",
        lambda *a, **k: pytest.fail("已在队列里，不该重新抓取"),
    )
    second = link_ingest.ingest_link(conn, make_config(), "https://example.com/post")

    assert second["state"] == "queued_existing"
    assert second["document_id"] == first["document_id"]


def test_arxiv_link_reuses_collected_document(tmp_path: Path, monkeypatch) -> None:
    """发一条已采集过的 arXiv 链接，应该复用原文档，不能再抓一遍、再存一份。"""
    conn = make_conn(tmp_path)
    document_id = repo.insert_document(
        conn,
        canonical_id="arxiv:2401.12345",
        kind="paper",
        title="已采集的论文",
        abstract="abstract",
        url="https://arxiv.org/abs/2401.12345",
        published_at="2026-11-01",
        authority="A",
        primary_tag="1 模型与架构",
        tags=["1 模型与架构"],
    )
    monkeypatch.setattr(link_ingest, "fetch_html", lambda *a, **k: pytest.fail("应复用已有文档"))

    result = link_ingest.ingest_link(
        conn, make_config(), "https://arxiv.org/abs/2401.12345v2"
    )

    assert result["state"] == "requeued"
    assert result["document_id"] == document_id
    assert repo.get_document(conn, document_id)["status"] == "deep_reading"


def test_ready_document_is_not_requeued(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        link_ingest,
        "fetch_html",
        lambda url, proxy: (ARTICLE_HTML, url),
    )
    conn = make_conn(tmp_path)
    first = link_ingest.ingest_link(conn, make_config(), "https://example.com/post")
    monkeypatch.setattr(link_ingest.repo, "get_kb_article", lambda *a, **k: {"id": 1})

    result = link_ingest.ingest_link(conn, make_config(), "https://example.com/post")

    assert result["state"] == "ready"
    assert result["document_id"] == first["document_id"]


def test_wechat_verify_page_raises_friendly_error(tmp_path: Path, monkeypatch) -> None:
    blocked = "<html><body>环境异常，完成验证后即可继续访问。</body></html>"
    monkeypatch.setattr(
        link_ingest,
        "fetch_html",
        lambda url, proxy: (blocked, "https://mp.weixin.qq.com/s/abc"),
    )

    with pytest.raises(link_ingest.LinkIngestError, match="验证"):
        link_ingest.ingest_link(
            make_conn(tmp_path), make_config(), "https://mp.weixin.qq.com/s/abc"
        )


def test_fetch_failure_maps_to_user_message(tmp_path: Path, monkeypatch) -> None:
    def boom(url, proxy):
        raise CleanError("fetch failed: https://example.com/x: 403 Forbidden")

    monkeypatch.setattr(link_ingest, "fetch_html", boom)

    with pytest.raises(link_ingest.LinkIngestError, match="403"):
        link_ingest.ingest_link(
            make_conn(tmp_path), make_config(), "https://example.com/x"
        )
