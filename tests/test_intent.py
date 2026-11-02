from __future__ import annotations

from pathlib import Path

from ai_radar.pipeline import rank
from ai_radar.pipeline.intent import _normalize
from ai_radar.store import repo
from ai_radar.store.db import connect, init_db


def test_normalize_library_filters() -> None:
    intent = _normalize(
        {
            "intent": "library",
            "scope": "favorites",
            "dimension": "5 多模态与生成",
            "kind": "paper",
            "status": "saved",
            "window": 7,
        }
    )
    assert intent == {
        "intent": "library",
        "scope": "favorites",
        "filters": {
            "dimension": "5 多模态与生成",
            "kind": "paper",
            "status": "saved",
            "window": "7",
        },
    }


def test_normalize_drops_unknown_values() -> None:
    intent = _normalize(
        {
            "intent": "library",
            "dimension": "9 不存在的维度",
            "kind": "podcast",
            "status": "unread",
            "window": 3,
        }
    )
    assert intent == {"intent": "library", "scope": "kb", "filters": {}}


def test_normalize_theme_and_clear() -> None:
    theme = _normalize(
        {
            "intent": "theme",
            "keywords": ["MoE", " 推理优化 "],
            "dimension": "3 推理与加速",
            "days": 90,
        }
    )
    assert theme == {
        "intent": "theme",
        "keywords": ["MoE", "推理优化"],
        "dimension": "3 推理与加速",
        "days": 30,
    }
    assert _normalize({"intent": "theme", "keywords": []}) is None
    assert _normalize({"intent": "theme_clear"}) == {"intent": "theme_clear"}


def test_normalize_topic_requires_query() -> None:
    assert _normalize({"intent": "topic", "query": " 强化学习 "}) == {
        "intent": "topic",
        "query": "强化学习",
    }
    assert _normalize({"intent": "topic", "query": ""}) is None
    assert _normalize({"intent": "unknown"}) is None


def test_normalize_question_and_chat() -> None:
    assert _normalize(
        {"intent": "question", "query": " RSI 那篇的结论是什么 ", "keywords": [" RSI ", ""]}
    ) == {
        "intent": "question",
        "query": "RSI 那篇的结论是什么",
        "keywords": ["RSI"],
    }
    assert _normalize({"intent": "question", "query": "", "keywords": ["RSI"]}) is None
    assert _normalize({"intent": "chat"}) == {"intent": "chat"}
    assert _normalize({"intent": "help"}) == {"intent": "help"}


def test_theme_preference_roundtrip_and_expiry(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)

    payload = repo.set_theme_preference(
        conn, keywords=["MoE"], dimension="3 推理与加速", days=7, raw="多推点 MoE"
    )
    assert payload["keywords"] == ["MoE"]
    active = repo.get_theme_preference(conn)
    assert active is not None and active["dimension"] == "3 推理与加速"

    conn.execute(
        "UPDATE preferences SET value_json = ? WHERE key = ?",
        ('{"keywords": ["MoE"], "dimension": null, "expires_at": "2000-01-01T00:00:00+00:00"}', repo.THEME_KEY),
    )
    conn.commit()
    assert repo.get_theme_preference(conn) is None
    assert repo.get_theme_preference(conn) is None  # 过期后被清掉

    repo.set_theme_preference(conn, keywords=["RAG"], dimension=None)
    repo.clear_theme_preference(conn)
    assert repo.get_theme_preference(conn) is None


def test_rank_boosts_theme_matches(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document_id = repo.insert_document(
        conn,
        canonical_id="test:theme",
        kind="paper",
        title="MoE inference optimization",
        abstract="A study on mixture of experts serving",
        url="https://example.com/theme",
        published_at="2026-10-27T00:00:00+00:00",
        authority="A",
        primary_tag="3 推理与加速",
        tags=["3 推理与加速"],
    )
    document = repo.get_document(conn, document_id)
    now = __import__("datetime").datetime(2026, 10, 28, tzinfo=__import__("datetime").timezone.utc)
    weights = {"scoring": {}, "authority": {"A": 1.0}}

    plain = rank.score_document(
        document,
        heat=0.5,
        recent_titles=[],
        weights=weights,
        preferences={},
        now=now,
    )
    boosted = rank.score_document(
        document,
        heat=0.5,
        recent_titles=[],
        weights=weights,
        preferences={},
        now=now,
        theme={"keywords": ["MoE"], "dimension": None},
    )
    assert boosted["score"] > plain["score"]
    assert rank.theme_matches(document, {"keywords": ["不存在的词"], "dimension": None}) is False
    assert rank.theme_matches(document, {"keywords": [], "dimension": "3 推理与加速"}) is True
