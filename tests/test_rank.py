from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ai_radar.pipeline.rank import novelty_score, recency_score

WEIGHTS = {
    "authority": {"A": 0.8, "B": 0.55},
    "scoring": {"authority": 0.3, "heat": 0.3, "recency": 0.15, "novelty": 0.15, "preference": 0.1},
    "half_life_hours": {"paper": 96, "forum": 36, "blog": 48, "repo": 72},
}


def document(kind: str, title: str, published: datetime) -> dict:
    return {
        "kind": kind,
        "title": title,
        "published_at": published.isoformat(),
        "first_seen_at": published.isoformat(),
    }


def test_recency_decays_with_age() -> None:
    now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
    fresh = document("forum", "fresh", now - timedelta(hours=1))
    old = document("forum", "old", now - timedelta(hours=72))
    assert recency_score(fresh, WEIGHTS, now) > recency_score(old, WEIGHTS, now)


def test_novelty_penalizes_duplicate_title() -> None:
    assert novelty_score("New agent framework", ["New agent framework"]) == 0.0
    assert novelty_score("Diffusion video model", ["New agent framework"]) > 0.5


def test_repo_recency_uses_first_seen_not_created_at() -> None:
    now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
    repo_doc = {
        "kind": "repo",
        "published_at": (now - timedelta(days=100)).isoformat(),
        "first_seen_at": (now - timedelta(hours=2)).isoformat(),
    }
    assert recency_score(repo_doc, WEIGHTS, now) > 0.9
