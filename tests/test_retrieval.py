from __future__ import annotations

from pathlib import Path

import pytest

from ai_radar.pipeline import retrieval
from ai_radar.store import repo
from ai_radar.store.db import connect, init_db
from ai_radar.util import iso, utcnow


@pytest.fixture()
def conn(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AI_RADAR_EMBED", "0")
    connection = connect(tmp_path / "ai_radar.db")
    init_db(connection)
    retrieval._INDEX = None
    yield connection
    connection.close()
    retrieval._INDEX = None


def _seed(conn) -> int:
    document_id = repo.insert_document(
        conn,
        canonical_id="test:deep-read",
        kind="blog",
        title="Carbon 模型评测",
        abstract="Carbon 在 CursorBench 上的表现",
        url="https://example.com/carbon",
        published_at=iso(utcnow()),
        authority="A",
        primary_tag="1 模型与架构",
        tags=["1 模型与架构"],
    )
    repo.mark_status(conn, [document_id], "kb_ready")
    reading = {
        "thesis": "Carbon 用更低成本达到第一梯队编码能力",
        "problem": "编码智能体的成本太高",
        "method": "分阶段开放 + 工具调用",
        "outline": [
            {"heading": "评测", "role": "experiment", "summary": "三项基准第一", "anchor": "评测"}
        ],
        "questions": ["Carbon 在 CursorBench 上表现如何？"],
        "narrative": "Carbon 在 CursorBench 4.0 上排名第一 [C1]。",
        "claims": [],
        "main_claims": [],
        "relations": [],
        "critique": {"weak_evidence": ["缺少独立复现"]},
    }
    repo.save_reading(
        conn,
        document_id=document_id,
        reading=reading,
        problem=reading["problem"],
        method=reading["method"],
        evidence=["CursorBench 53.9%"],
        conclusions=["Carbon 排名第一"],
        limitations=["缺少独立复现"],
    )
    section_ids = repo.replace_sections(
        conn,
        document_id,
        [
            {
                "ord": 1,
                "heading": "评测",
                "role": "experiment",
                "summary": "三项基准第一",
                "anchor": "评测",
            }
        ],
    )
    claim_ids = repo.replace_claims(
        conn,
        document_id,
        [
            {
                "section_id": section_ids[0],
                "text": "Carbon 在 CursorBench 4.0 上排名第一",
                "question": "Carbon 在 CursorBench 上表现如何？",
                "claim_type": "main",
                "confidence": "source",
                "quote": "CursorBench 4.0: Carbon 53.9%",
                "anchor": "评测 · 片段 1",
                "kind": "main",
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
                "baseline": "Gemini 4 Argon 51.3%",
                "anchor": "评测 · 片段 1",
            }
        ],
    )
    retrieval.index_document_units(conn, document_id)
    return document_id


def test_index_builds_four_unit_layers(conn) -> None:
    _seed(conn)
    counts = repo.unit_counts(conn)
    assert counts["doc"] == 1
    assert counts["section"] == 1
    assert counts["claim"] == 1
    assert counts["evidence"] == 1


def test_search_finds_claim_by_exact_term(conn) -> None:
    _seed(conn)
    hits = retrieval.search_units(conn, "CursorBench 排名")
    assert hits
    assert any(hit.claim_text and "CursorBench" in hit.claim_text for hit in hits)
    context, citations = retrieval.format_context(conn, hits)
    assert "回答的问题" in context
    assert "CursorBench" in context
    assert citations and citations[0]["label"]


def test_search_finds_claim_by_chinese_question(conn) -> None:
    _seed(conn)
    hits = retrieval.search_units(conn, "Carbon 在 CursorBench 上表现如何")
    assert hits
    assert any("Carbon" in (hit.claim_text or hit.text) for hit in hits)


def test_reindex_all_reports_documents(conn) -> None:
    _seed(conn)
    result = retrieval.reindex_all(conn)
    assert result["documents"] == 1
    assert result["units"]["claim"] == 1
