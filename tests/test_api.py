from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_radar.api.app import create_app
from ai_radar.config import PROJECT_ROOT
from ai_radar.store import repo
from ai_radar.store.db import connect, init_db
from ai_radar.util import iso, utcnow


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    # API 会按 root 读取领域包，测试里复制一份公开配置即可。
    shutil.copytree(PROJECT_ROOT / "config", tmp_path / "config")
    return tmp_path


@pytest.fixture()
def client(root: Path):
    with TestClient(create_app(root)) as client:
        yield client


def _seed(root: Path, *, title: str = "NVIDIA 发布新芯片") -> dict:
    conn = connect(root / "data" / "domains" / "ai" / "ai_radar.db")
    init_db(conn)
    document_id = repo.insert_document(
        conn,
        canonical_id=f"test:{title}",
        kind="blog",
        title=title,
        abstract="abstract",
        url="https://example.com/article",
        published_at=iso(utcnow()),
        authority="A",
        primary_tag="1 模型与架构",
        tags=["1 模型与架构"],
    )
    repo.mark_status(conn, [document_id], "pushed")
    digest_id = repo.create_digest(conn, "2026-11-12", kind="daily")
    topic_id = repo.add_topic(
        conn,
        digest_id=digest_id,
        topic_date="2026-11-12",
        kind="daily",
        label="测试主线",
        summary="主线摘要",
        entity="nvidia",
        dimension="1 模型与架构",
        score=0.9,
        item_count=1,
    )
    repo.add_topic_item(
        conn, topic_id=topic_id, document_id=document_id, angle="blog", rank=1
    )
    repo.add_digest_item(
        conn,
        digest_id=digest_id,
        document_id=document_id,
        rank=1,
        score=0.9,
        reason="值得关注",
        summary={"why": "因为"},
        section="theme",
    )
    repo.mark_digest_sent(
        conn, digest_id, {"count": 1, "events_count": 1, "theme": "测试主线"}
    )
    conn.close()
    return {
        "document_id": document_id,
        "topic_id": topic_id,
        "digest_id": digest_id,
    }


def test_healthz_and_domains(client) -> None:
    response = client.get("/api/v1/healthz")
    assert response.status_code == 200
    assert response.json()["ok"] is True

    response = client.get("/api/v1/domains")
    assert response.status_code == 200
    assert {item["id"] for item in response.json()} == {"ai", "finance"}
    # 还没建库的领域也要能列出来，只是标记为无数据。
    assert all(item["db_exists"] is False for item in response.json())
    assert all(item["last_digest"] is None for item in response.json())

    finance = client.get("/api/v1/domains/finance")
    assert finance.status_code == 200
    payload = finance.json()
    assert payload["name"] == "金融市场观察"
    assert len(payload["taxonomy"]) == 8
    assert {source["id"] for source in payload["sources"]} == {
        "finance_cn",
        "finance_en",
        "official_finance",
    }
    assert client.get("/api/v1/domains/nope").status_code == 404


def test_domain_reports_latest_digest(client, root: Path) -> None:
    _seed(root)
    payload = client.get("/api/v1/domains/ai").json()
    assert payload["db_exists"] is True
    assert payload["last_digest"] == {
        "date": "2026-11-12",
        "kind": "daily",
        "status": "sent",
    }


def test_digest_list_and_detail(client, root: Path) -> None:
    seed = _seed(root)

    response = client.get("/api/v1/digests")
    assert response.status_code == 200
    assert response.json()[0]["date"] == "2026-11-12"
    assert response.json()[0]["theme"] == "测试主线"

    response = client.get("/api/v1/digests/2026-11-12")
    assert response.status_code == 200
    payload = response.json()
    assert payload["digest"]["count"] == 1
    assert payload["events"][0]["id"] == seed["topic_id"]
    assert payload["events"][0]["items"][0]["summary"]["why"] == "因为"
    assert payload["events"][0]["items"][0]["saved"] is False

    assert client.get("/api/v1/digests/2026-11-11").status_code == 404


def test_export_digest_markdown_and_json(client, root: Path) -> None:
    seed = _seed(root)

    response = client.get("/api/v1/export/digest/2026-11-12?kind=daily")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "attachment" in response.headers["content-disposition"]
    assert "# Kestrel Radar · 2026-11-12" in response.text
    assert "## 1. 测试主线" in response.text
    assert "NVIDIA 发布新芯片" in response.text
    assert "为什么值得关注：因为" in response.text

    as_json = client.get("/api/v1/export/digest/2026-11-12?format=json")
    assert as_json.status_code == 200
    assert as_json.json()["events"][0]["id"] == seed["topic_id"]

    assert client.get("/api/v1/export/digest/2026-11-11").status_code == 404
    assert client.get("/api/v1/export/digest/2026-11-12?format=pdf").status_code == 422


def test_export_backup_is_readable_sqlite(client, root: Path) -> None:
    _seed(root)

    response = client.get("/api/v1/export/backup")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert "attachment" in response.headers["content-disposition"]

    copy = root / "backup.db"
    copy.write_bytes(response.content)
    conn = sqlite3.connect(copy)
    try:
        row = conn.execute("SELECT title FROM documents").fetchone()
    finally:
        conn.close()
    assert row[0] == "NVIDIA 发布新芯片"


def test_events_endpoints(client, root: Path) -> None:
    seed = _seed(root)

    response = client.get("/api/v1/events?date=2026-11-12")
    assert response.status_code == 200
    assert response.json()["date"] == "2026-11-12"
    assert response.json()["events"][0]["label"] == "测试主线"

    response = client.get(f"/api/v1/events/{seed['topic_id']}")
    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == seed["document_id"]
    assert client.get("/api/v1/events/99999").status_code == 404


def test_article_detail_and_actions(client, root: Path) -> None:
    seed = _seed(root)
    document_id = seed["document_id"]

    detail = client.get(f"/api/v1/articles/{document_id}")
    assert detail.status_code == 200
    assert detail.json()["document"]["title"] == "NVIDIA 发布新芯片"
    assert detail.json()["summary"]["why"] == "因为"

    saved = client.post(
        f"/api/v1/articles/{document_id}/actions",
        json={"action": "save", "digest_date": "2026-11-12"},
    )
    assert saved.status_code == 200
    assert saved.json()["saved"] is True

    read = client.post(
        f"/api/v1/articles/{document_id}/actions", json={"action": "read"}
    )
    assert read.status_code == 200
    assert read.json()["read"] is True

    assert (
        client.post(
            f"/api/v1/articles/{document_id}/actions", json={"action": "explode"}
        ).status_code
        == 400
    )
    assert client.get("/api/v1/articles/99999").status_code == 404


def test_article_detail_returns_reading_structure(client, root: Path) -> None:
    seed = _seed(root)
    document_id = seed["document_id"]
    conn = connect(root / "data" / "domains" / "ai" / "ai_radar.db")
    repo.save_reading(
        conn,
        document_id=document_id,
        reading={
            "thesis": "Carbon 用更低成本达到第一梯队",
            "problem": "成本太高",
            "narrative": "Carbon 在 CursorBench 上排名第一 [C1]。",
            "questions": ["CursorBench 表现如何？"],
            "outline": [],
            "claims": [],
            "main_claims": [],
            "relations": [],
            "critique": {"weak_evidence": ["缺少独立复现"]},
        },
        problem="成本太高",
        method="分阶段开放",
        evidence=["CursorBench 53.9%"],
        conclusions=["Carbon 排名第一"],
        limitations=["缺少独立复现"],
    )
    section_ids = repo.replace_sections(
        conn,
        document_id,
        [{"ord": 1, "heading": "评测", "role": "experiment", "summary": "三项第一"}],
    )
    claim_ids = repo.replace_claims(
        conn,
        document_id,
        [
            {
                "section_id": section_ids[0],
                "text": "Carbon 在 CursorBench 上排名第一",
                "question": "CursorBench 表现如何？",
                "claim_type": "main",
                "confidence": "source",
                "quote": "CursorBench 4.0: Carbon 53.9%",
                "anchor": "评测 · 片段 1",
            }
        ],
    )
    repo.insert_evidence(
        conn,
        document_id=document_id,
        claim_id=claim_ids[0],
        items=[
            {
                "kind": "data",
                "quote": "CursorBench 4.0: Carbon 53.9%",
                "metric": "CursorBench",
                "value": "53.9",
                "unit": "%",
            }
        ],
    )
    conn.close()

    payload = client.get(f"/api/v1/articles/{document_id}").json()
    assert payload["reading"]["thesis"] == "Carbon 用更低成本达到第一梯队"
    assert payload["sections"][0]["heading"] == "评测"
    assert payload["claims"][0]["question"] == "CursorBench 表现如何？"
    assert payload["claims"][0]["evidence"][0]["value"] == "53.9"


def test_archive_query(client, root: Path) -> None:
    seed = _seed(root)

    response = client.get("/api/v1/archive?q=NVIDIA")
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["id"] == seed["document_id"]

    response = client.get("/api/v1/archive?q=不存在的词")
    assert response.status_code == 200
    assert response.json()["total"] == 0

    # from / to 是 Python 关键字，接口用 alias 接收、SQL 里按日期过滤。
    response = client.get("/api/v1/archive?from=2999-01-01")
    assert response.json()["total"] == 0


def test_search_serializes_result(client, monkeypatch) -> None:
    from ai_radar.pipeline.search import SearchHit, SearchResult

    def fake_search(conn, config, query, *, limit=5, external=True):
        return SearchResult(
            query=query,
            local=[
                SearchHit(
                    document_id=1,
                    title="t",
                    url="https://example.com",
                    tag="1 模型与架构",
                    kind="blog",
                    score=0.9,
                    reason="检索词命中",
                )
            ],
            note="",
        )

    monkeypatch.setattr("ai_radar.api.routes.query.search_documents", fake_search)
    response = client.get("/api/v1/search?q=NVIDIA&external=false")
    assert response.status_code == 200
    assert response.json()["local"][0]["document_id"] == 1


def test_settings_pause_dimension(client) -> None:
    payload = client.get("/api/v1/settings").json()
    assert payload["domain"]["id"] == "ai"

    response = client.put(
        "/api/v1/settings", json={"paused_dimensions": ["3 推理与加速"]}
    )
    assert response.status_code == 200
    assert response.json()["paused_dimensions"] == ["3 推理与加速"]

    bad = client.put("/api/v1/settings", json={"paused_dimensions": ["不存在的分类"]})
    assert bad.status_code == 400

    cleared = client.put("/api/v1/settings", json={"paused_dimensions": []})
    assert cleared.json()["paused_dimensions"] == []


def test_job_trigger_runs_in_background(client, monkeypatch) -> None:
    calls: list[str] = []

    def fake_collect(conn, config):
        calls.append(config.domain.id)
        return {"items": 0, "errors": []}

    monkeypatch.setattr("ai_radar.api.routes.jobs.run_collect", fake_collect)
    response = client.post("/api/v1/jobs/collect")
    assert response.status_code == 202
    # TestClient 会等 BackgroundTasks 执行完，能直接断言副作用。
    assert calls == ["ai"]
    assert isinstance(client.get("/api/v1/jobs").json(), list)


def test_qa_without_kb_returns_no_support(client, root: Path) -> None:
    _seed(root)
    response = client.post("/api/v1/qa", json={"question": "这篇讲了什么？"})
    assert response.status_code == 200
    assert response.json()["supported"] is False


def test_kb_endpoint_and_remove(client, root: Path) -> None:
    seed = _seed(root)
    document_id = seed["document_id"]

    saved = client.post(
        f"/api/v1/articles/{document_id}/actions", json={"action": "save"}
    )
    assert saved.status_code == 200

    kb = client.get("/api/v1/kb?limit=10").json()
    assert kb["counts"] == {"deep": 0, "only_saved": 1, "total": 1}
    assert kb["items"][0]["id"] == document_id
    assert kb["items"][0]["saved_at"]
    assert kb["facets"]["dimensions"] == ["1 模型与架构"]

    removed = client.post(
        f"/api/v1/articles/{document_id}/actions", json={"action": "kb_remove"}
    )
    assert removed.status_code == 200
    assert client.get("/api/v1/kb").json()["counts"]["total"] == 0


def test_spa_fallback_when_dist_exists(tmp_path: Path) -> None:
    shutil.copytree(PROJECT_ROOT / "config", tmp_path / "config")
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html>spa</html>", encoding="utf-8")

    with TestClient(create_app(tmp_path)) as spa_client:
        response = spa_client.get("/archive")
        assert response.text == "<html>spa</html>"
        # 发版后 index.html 必须回源，避免浏览器继续加载旧 assets。
        assert response.headers["cache-control"] == "no-cache"
        assert spa_client.get("/api/v1/healthz").json()["ok"] is True
        assert spa_client.get("/api/v1/nope").status_code == 404
