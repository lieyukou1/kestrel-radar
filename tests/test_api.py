from __future__ import annotations

import shutil
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
