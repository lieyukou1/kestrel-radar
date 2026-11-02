from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from ai_radar.pipeline.composer import _duplicate_of, compose_daily, compose_weekly
from ai_radar.pipeline.entities import extract, filter_by_frequency
from ai_radar.pipeline.topics import build_topics
from ai_radar.store.db import connect, init_db

NOW = datetime(2026, 10, 26, 3, tzinfo=UTC)
DIM_1 = "1 模型与架构"
DIM_3 = "3 推理与加速"
DIM_8 = "8 数据与评测"


class RotationStub:
    """composer 只用到 active / theme_weight / stats[tag].weight。"""

    active = True

    def __init__(
        self,
        theme_weights: dict[str, float] | None = None,
        item_weights: dict[str, float] | None = None,
    ) -> None:
        self._theme = theme_weights or {}
        self.stats = {
            tag: SimpleNamespace(tag=tag, weight=weight)
            for tag, weight in (item_weights or {}).items()
        }

    def theme_weight(self, tag: str | None) -> float:
        return self._theme.get(str(tag or ""), 1.0)


WEIGHTS = {
    "topics": {"pool": 50, "min_members": 2, "similarity": 0.82, "max_docs": 12},
    "selection": {"max_per_tag": 3, "mmr_lambda": 0.7, "novelty_days": 30},
    "cadence": {"daily_size": 5, "daily_theme_items": 3},
    "blockbuster": {"hn_points": 3000, "hf_upvotes": 3000, "repo_stars_per_day": 100000},
}


class Config:
    weights = WEIGHTS


def entry(
    doc_id: int,
    kind: str,
    title: str,
    score: float,
    *,
    tag: str = "4 Agent 与应用",
    age_hours: float = 20,
    authority: str = "A",
) -> dict:
    published = (NOW - timedelta(hours=age_hours)).isoformat()
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


def test_extract_keeps_product_names_and_drops_generic_words() -> None:
    assert "jev" in extract("JEV 的一个危险误读：每条判断都合理")
    assert "qwen3" in extract("Qwen3 开源了新版本")
    assert "ai" not in extract("AI 写代码越来越快")
    assert "agent" not in extract("10 个值得关注的 Agent 项目")
    assert "豆包" in extract("豆包发布新版本")


def test_venue_names_are_not_topic_entities() -> None:
    """会议/期刊名可以被多篇共享，但那是「同一个会」，不是「同一件事」。"""
    assert extract("EMNLP 2026 | 中科大提出AtomMem，用原子事实重建Agent长期记忆") == set()
    assert extract("EMNLP'26｜GPT也不会「读空气」，狼人杀基准测出社交盲区") == set()
    assert "icml2026" not in extract("ICML2026 最佳论文出炉")


def test_duplicate_check_respects_conflicting_products() -> None:
    """同产品不同事件不是转载，不能因为在同一批候选里就被折叠。"""
    released = entry(1, "blog", "Gemma 4 发布：性能提升 30%", 0.80)
    jailbroken = entry(2, "blog", "Gemma 4 被越狱：性能提升 30%", 0.80)
    syndicated = entry(3, "blog", "Gemma 4 发布：性能提升 30%！", 0.80)

    assert _duplicate_of(jailbroken, [released], {}) is False
    assert _duplicate_of(syndicated, [released], {}) is True


def test_single_shared_entity_needs_semantic_support() -> None:
    """只共享一个实体、语义又不接近时，不能合并成话题。"""
    items = [
        entry(1, "blog", "JEV 新版发布", 0.80),
        entry(2, "blog", "JEV 的另一个用法", 0.70),
    ]
    far = {1: [1.0, 0.0], 2: [0.0, 1.0]}
    assert build_topics(items, vectors=far) == []

    close = {1: [1.0, 0.0], 2: [0.99, 0.01]}
    found = build_topics(items, vectors=close)
    assert len(found) == 1 and len(found[0].items) == 2


def test_entities_filtered_by_document_frequency() -> None:
    raw = {1: {"a", "rare"}, 2: {"a", "b"}, 3: {"a", "b"}, 4: {"generic"}}
    filtered = filter_by_frequency(raw, min_docs=2, max_docs=3)
    assert filtered[1] == {"a"}
    assert filtered[2] == {"a", "b"}
    # 只出现一次的实体无法成题
    assert "generic" not in filtered[4]


def test_topics_merge_by_shared_entity_across_kinds(tmp_path) -> None:
    items = [
        entry(1, "paper", "JEV v2: Browser-Native Agent Sandbox", 0.80),
        entry(2, "blog", "JEV 新版把沙箱搬进浏览器", 0.78),
        entry(3, "forum", "JEV is now sandboxed, thoughts?", 0.76),
        entry(4, "blog", "完全无关的另一篇", 0.60),
    ]
    found = build_topics(items, vectors={})
    assert len(found) == 1
    topic = found[0]
    assert topic.label == "jev"
    assert len(topic.items) == 3
    assert len(topic.kinds) == 3
    assert topic.score > sum(item["score"] for item in topic.items)


def test_single_item_topics_are_dropped() -> None:
    items = [entry(1, "blog", "独立话题 A", 0.8), entry(2, "blog", "独立话题 B", 0.7)]
    assert build_topics(items, vectors={}) == []


def test_compose_daily_puts_theme_first_then_briefs(tmp_path, monkeypatch) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    monkeypatch.setattr("ai_radar.pipeline.composer.embedding.ensure_vectors", lambda *a, **k: {})

    items = [
        entry(1, "paper", "JEV v2: Browser-Native Agent Sandbox", 0.82),
        entry(2, "blog", "JEV 新版把沙箱搬进浏览器", 0.80),
        entry(3, "forum", "JEV is now sandboxed, thoughts?", 0.78, tag="1 模型与架构"),
        entry(4, "blog", "另一条中文快讯", 0.70, tag="2 训练与对齐"),
        entry(5, "forum", "another hn post", 0.69, tag="6 系统与基础设施"),
        entry(6, "repo", "owner/repo", 0.60, tag="8 数据与评测"),
    ]
    composition = compose_daily(conn, Config(), items, now=NOW)

    assert composition.theme is not None
    assert composition.blocks[0].section == "theme"
    theme_items = composition.blocks[0].items
    assert len(theme_items) == 3
    assert sum(1 for item in theme_items if item["doc"]["kind"] == "paper") == 1
    briefs = [block for block in composition.blocks if block.section == "brief"]
    assert briefs and all(item["doc"]["kind"] in ("blog", "forum") for item in briefs[0].items)
    assert len(composition.items) <= 5


def test_compose_daily_drops_duplicate_titles_in_theme(tmp_path, monkeypatch) -> None:
    """同一件事被两个号重复报道时，主线不重复占位。"""
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    monkeypatch.setattr("ai_radar.pipeline.composer.embedding.ensure_vectors", lambda *a, **k: {})

    items = [
        entry(1, "blog", "JEV 新版把沙箱搬进浏览器，性能提升 40%", 0.82),
        entry(2, "blog", "JEV 新版把沙箱搬进浏览器，性能提升 40%", 0.80),
        entry(3, "forum", "JEV is now sandboxed, thoughts?", 0.78, tag="1 模型与架构"),
    ]
    composition = compose_daily(conn, Config(), items, now=NOW)

    titles = [item["doc"]["title"] for item in composition.blocks[0].items]
    assert len(titles) == 2
    assert len(set(titles)) == 2


def test_theme_rotation_prefers_deficit_within_band(tmp_path, monkeypatch) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    monkeypatch.setattr("ai_radar.pipeline.composer.embedding.ensure_vectors", lambda *a, **k: {})

    items = [
        entry(1, "paper", "JEV v2: Browser-Native Agent Sandbox", 0.90, tag=DIM_1),
        entry(2, "blog", "JEV 新版把沙箱搬进浏览器", 0.85, tag=DIM_1),
        entry(3, "paper", "Claude 形式化验证挖出数百个 Bug", 0.80, tag=DIM_8),
        entry(4, "blog", "Claude 写证明挖 Bug 的代价", 0.78, tag=DIM_8),
        # 分数掉出 85% 分带，赤字再大也不该被选中
        entry(5, "paper", "MAMBA 的长序列建模", 0.40, tag=DIM_3),
        entry(6, "blog", "MAMBA 训练加速实践", 0.35, tag=DIM_3),
    ]
    rotation = RotationStub(
        theme_weights={DIM_1: 0.8, DIM_8: 1.2, DIM_3: 1.2},
        item_weights={DIM_1: 0.8, DIM_8: 1.2},
    )

    composition = compose_daily(conn, Config(), items, now=NOW, rotation=rotation)

    assert composition.theme is not None
    assert composition.theme_dimension == DIM_8
    titles = [item["doc"]["title"] for item in composition.blocks[0].items]
    assert all("Claude" in title for title in titles)


def test_briefs_use_rotation_weights(tmp_path, monkeypatch) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    monkeypatch.setattr("ai_radar.pipeline.composer.embedding.ensure_vectors", lambda *a, **k: {})

    items = [
        entry(1, "blog", "独立话题甲", 0.80, tag=DIM_1),
        entry(2, "blog", "独立话题乙", 0.78, tag=DIM_8),
    ]
    rotation = RotationStub(item_weights={DIM_1: 0.8, DIM_8: 1.2})

    composition = compose_daily(conn, Config(), items, now=NOW, size=1, rotation=rotation)

    assert composition.theme is None
    assert [block.section for block in composition.blocks] == ["brief"]
    # 分数略低但维度赤字更大的条目被抬上来
    assert composition.blocks[0].items[0]["doc"]["id"] == 2


def test_briefs_do_not_repeat_theme_news(tmp_path, monkeypatch) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    monkeypatch.setattr("ai_radar.pipeline.composer.embedding.ensure_vectors", lambda *a, **k: {})

    items = [
        entry(1, "paper", "JEV v2: Browser-Native Agent Sandbox", 0.90, tag=DIM_1),
        entry(2, "blog", "JEV 新版把沙箱搬进浏览器", 0.85, tag=DIM_1),
        entry(3, "blog", "JEV 新版把沙箱搬进浏览器（另一家报道）", 0.84, tag=DIM_1),
        entry(4, "blog", "另一条中文快讯", 0.70, tag="2 训练与对齐"),
    ]

    composition = compose_daily(conn, Config(), items, now=NOW, size=4)

    titles = [item["doc"]["title"] for item in composition.items]
    assert sum("JEV" in title for title in titles) == 2
    assert "另一条中文快讯" in titles


def test_compose_daily_falls_back_to_briefs_without_theme(tmp_path, monkeypatch) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    monkeypatch.setattr("ai_radar.pipeline.composer.embedding.ensure_vectors", lambda *a, **k: {})

    items = [
        entry(1, "blog", "独立话题甲", 0.8, tag="1 模型与架构"),
        entry(2, "blog", "独立话题乙", 0.7, tag="2 训练与对齐"),
        entry(3, "forum", "independent post", 0.6, tag="3 推理与加速"),
    ]
    composition = compose_daily(conn, Config(), items, now=NOW)
    assert composition.theme is None
    assert [block.section for block in composition.blocks] == ["brief"]
    assert 1 <= len(composition.items) <= 3


def test_compose_weekly_picks_papers_and_repos(tmp_path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)

    items = [
        entry(1, "paper", "paper one", 0.8, age_hours=48),
        entry(2, "paper", "paper two", 0.7, age_hours=72),
        entry(3, "paper", "paper three", 0.6, age_hours=96),
        entry(4, "paper", "too old paper", 0.9, age_hours=24 * 30),
        entry(5, "repo", "owner/repo", 0.7, age_hours=48),
    ]
    composition = compose_weekly(conn, Config(), items, now=NOW)
    sections = {block.section: block.items for block in composition.blocks}
    assert len(sections["weekly_paper"]) == 3
    assert all(item["title"] != "too old paper" for item in sections["weekly_paper"])
    assert len(sections["weekly_repo"]) == 1
