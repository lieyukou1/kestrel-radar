from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ai_radar.pipeline import events
from ai_radar.pipeline.composer import compose_daily
from ai_radar.store.db import connect, init_db

NOW = datetime(2026, 11, 11, 3, tzinfo=UTC)


class Config:
    weights = {
        "topics": {"pool": 50, "min_members": 2, "max_docs": 12},
        "selection": {"max_per_tag": 3, "mmr_lambda": 0.7},
        "cadence": {"daily_size": 5, "daily_theme_items": 3},
        "blockbuster": {"hn_points": 3000, "hf_upvotes": 3000, "repo_stars_per_day": 100000},
    }


def entry(
    doc_id: int,
    title: str,
    *,
    kind: str = "blog",
    score: float = 0.8,
    authority: str = "A",
    hours_ago: float = 5,
    tag: str = "1 模型与架构",
) -> dict:
    published = (NOW - timedelta(hours=hours_ago)).isoformat()
    return {
        "doc": {
            "id": doc_id,
            "kind": kind,
            "title": title,
            "authority": authority,
            "published_at": published,
            "first_seen_at": published,
        },
        "score": score,
        "tag": tag,
        "title": title,
    }


def test_group_buckets_assigns_each_doc_to_its_most_specific_entity() -> None:
    items = [
        entry(1, "千问办公2.0发布"),
        entry(2, "千问办公2.0实测"),
        entry(3, "完全独立的一条内容"),
    ]

    buckets = events.group_buckets(items, now=NOW)

    assert len(buckets) == 1
    assert buckets[0].entity == "千问"
    assert {item["doc"]["id"] for item in buckets[0].items} == {1, 2}


def test_build_events_splits_one_bucket_into_two_events() -> None:
    """同一个实体下的发布和融资必须拆成两件事，不能糊成一个公司名。"""
    items = [
        entry(1, "千问办公2.0发布"),
        entry(2, "千问办公2.0实测"),
        entry(3, "千问完成新一轮融资"),
        entry(4, "千问估值创新高"),
    ]
    buckets = events.group_buckets(items, now=NOW)
    assignments = [
        {"bucket": 1, "item_ids": [1, 2], "label": "千问办公 2.0 发布", "when": "2026-11-10"},
        {"bucket": 1, "item_ids": [3, 4], "label": "千问新一轮融资"},
    ]

    found = events.build_events(buckets, assignments, now=NOW)

    assert len(found) == 2
    assert [len(event.items) for event in found] == [2, 2]
    assert {event.key.split("|")[1] for event in found} == {"release", "funding"}


def test_build_events_rejects_unknown_bucket_and_foreign_items() -> None:
    items = [entry(1, "千问办公2.0发布"), entry(2, "千问办公2.0实测")]
    buckets = events.group_buckets(items, now=NOW)

    assert events.build_events(buckets, [{"bucket": 9, "item_ids": [1, 2]}], now=NOW) == []
    # 999 不在桶里、只剩 1 条，凑不成事件
    assert (
        events.build_events(
            buckets, [{"bucket": 1, "item_ids": [1, 999], "label": "千问"}], now=NOW
        )
        == []
    )


def test_build_events_does_not_reuse_the_same_document() -> None:
    items = [entry(1, "千问办公2.0发布"), entry(2, "千问办公2.0实测")]
    buckets = events.group_buckets(items, now=NOW)

    found = events.build_events(
        buckets,
        [
            {"bucket": 1, "item_ids": [1, 2], "label": "事件一"},
            {"bucket": 1, "item_ids": [1, 2], "label": "事件二"},
        ],
        now=NOW,
    )

    assert len(found) == 1


def test_fallback_events_use_action_in_the_key() -> None:
    items = [entry(1, "Qwen3 发布"), entry(2, "Qwen3 实测")]
    buckets = events.group_buckets(items, now=NOW)

    found = events.fallback_events(buckets, now=NOW)

    assert len(found) == 1
    assert found[0].key == "qwen3|release|"


def test_event_score_rewards_source_diversity() -> None:
    items = [entry(1, "千问办公2.0发布"), entry(2, "千问办公2.0实测")]

    same_source, _ = events.score_items(
        items, sources_map={1: {"qbitai"}, 2: {"qbitai"}}, now=NOW
    )
    cross_source, count = events.score_items(
        items, sources_map={1: {"qbitai"}, 2: {"infoq"}}, now=NOW
    )

    assert count == 2
    assert cross_source > same_source


def test_event_score_counts_same_source_burst_as_coverage() -> None:
    """同一来源连发 4 条，要压过「两路各 1 条」的小事：大新闻不能被来源多样性压死。"""
    burst = [entry(i, f"Fable 5.2 实测 {i}") for i in range(1, 5)]
    spread = [entry(11, "Seedream 5.5 发布"), entry(12, "Seedream 5.5 实测")]

    burst_score, _ = events.score_items(
        burst, sources_map={i: {"qbitai"} for i in range(1, 5)}, now=NOW
    )
    spread_score, _ = events.score_items(
        spread, sources_map={11: {"qbitai"}, 12: {"hackernews"}}, now=NOW
    )

    assert burst_score > spread_score


def test_compose_daily_renders_theme_then_progress(tmp_path, monkeypatch) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    monkeypatch.setattr(
        "ai_radar.pipeline.composer.embedding.ensure_vectors", lambda *a, **k: {}
    )
    items = [
        entry(1, "千问办公2.0发布", score=0.80),
        entry(2, "千问办公2.0实测", score=0.78),
        entry(3, "蚂蚁灵波开源触觉模型", score=0.75),
        entry(4, "蚂蚁灵波触觉模型实测", score=0.70),
        entry(5, "一条没有实体的快讯", score=0.60, tag="2 训练与对齐"),
    ]
    buckets = events.group_buckets(items, now=NOW)
    by_entity = {bucket.entity: bucket for bucket in buckets}
    assignments = [
        {
            "bucket": by_entity["千问"].index,
            "item_ids": [1, 2],
            "label": "千问办公 2.0 发布",
            "when": "2026-11-10",
        },
        {
            "bucket": by_entity["蚂蚁"].index,
            "item_ids": [3, 4],
            "label": "蚂蚁灵波开源触觉模型",
        },
    ]
    found = events.build_events(buckets, assignments, now=NOW)

    composition = compose_daily(conn, Config(), items, now=NOW, found_events=found)

    assert composition.theme is not None
    assert composition.theme.label == "千问办公 2.0 发布"
    assert composition.events[0] is composition.theme
    assert len(composition.progress) == 1
    assert composition.progress[0].label == "蚂蚁灵波开源触觉模型"


def test_compose_daily_caps_event_count(tmp_path, monkeypatch) -> None:
    """一级只展示前 N 个事件，多出来的不进当天卡片。"""
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    monkeypatch.setattr(
        "ai_radar.pipeline.composer.embedding.ensure_vectors", lambda *a, **k: {}
    )
    items = [
        entry(1, "千问办公套件正式发布", score=0.80),
        entry(2, "阿里千问开源权重更新", score=0.78),
        entry(3, "蚂蚁灵波开源触觉模型", score=0.75),
        entry(4, "蚂蚁灵波触觉模型实测", score=0.70),
        entry(5, "DeepSeek 开源新模型", score=0.68),
        entry(6, "DeepSeek 新模型实测", score=0.66),
    ]
    buckets = events.group_buckets(items, now=NOW)
    by_entity = {bucket.entity: bucket for bucket in buckets}
    assignments = [
        {
            "bucket": by_entity["千问"].index,
            "item_ids": [1, 2],
            "label": "千问办公 2.0 发布",
            "when": "2026-11-10",
        },
        {
            "bucket": by_entity["蚂蚁"].index,
            "item_ids": [3, 4],
            "label": "蚂蚁灵波开源触觉模型",
        },
        {
            "bucket": by_entity["deepseek"].index,
            "item_ids": [5, 6],
            "label": "DeepSeek 开源新模型",
        },
    ]
    found = events.build_events(buckets, assignments, now=NOW)

    composition = compose_daily(conn, Config(), items, now=NOW, found_events=found, size=2)

    assert len(composition.events) == 2
    assert composition.events[0] is composition.theme
