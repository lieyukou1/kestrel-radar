from __future__ import annotations

from datetime import timedelta
from email.utils import format_datetime
from pathlib import Path

import pytest

from ai_radar.adapters.sources.github import GitHubAdapter
from ai_radar.adapters.sources.lobsters import LobstersAdapter, parse_comments
from ai_radar.adapters.sources.rss import RSSAdapter, match_keywords
from ai_radar.config import PROJECT_ROOT, ConfigError, load_config
from ai_radar.util import utcnow


def repo(name: str, stars: int) -> dict:
    return {
        "full_name": name,
        "html_url": f"https://github.com/{name}",
        "created_at": "2026-10-01T00:00:00Z",
        "stargazers_count": stars,
        "description": "desc",
        "owner": {"login": name.split("/")[0]},
    }


def test_github_parse_drops_repos_below_min_stars() -> None:
    adapter = GitHubAdapter(min_stars=200)
    items = adapter.parse(
        {"items": [repo("good/repo", 1200), repo("spam/repo", 33)]}
    )
    assert [item.external_id for item in items] == ["good/repo"]
    assert items[0].signals["stars"] == 1200


def rss_xml(published) -> str:
    stamp = format_datetime(published)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>
<item><title>中文标题</title><link>https://example.com/a</link>
<description>摘要</description><pubDate>{stamp}</pubDate></item>
</channel></rss>"""


def test_rss_applies_feed_timezone_offset() -> None:
    adapter = RSSAdapter()
    # InfoQ 把北京时间标成 GMT：喂一个「未来 7 小时」的时间，回拨 8 小时后应落在过去。
    future = utcnow() + timedelta(hours=7)
    items = adapter.parse_feed(rss_xml(future), {"published_tz_offset_hours": 8})
    published = items[0].published_at
    assert published is not None
    assert published < utcnow().isoformat()


def test_rss_clamps_future_timestamps() -> None:
    adapter = RSSAdapter()
    items = adapter.parse_feed(rss_xml(utcnow() + timedelta(hours=3)), {})
    published = items[0].published_at
    assert published is not None
    assert published <= utcnow().isoformat()


def test_rss_drops_items_older_than_max_age() -> None:
    fresh = RSSAdapter(max_age_days=10).parse_feed(rss_xml(utcnow() - timedelta(days=1)), {})
    stale = RSSAdapter(max_age_days=10).parse_feed(rss_xml(utcnow() - timedelta(days=30)), {})
    assert len(fresh) == 1
    assert stale == []


def test_rss_feed_exclude_keywords_drops_roundups() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>
<item><title>AI 早报：A；B；C</title><link>https://example.com/roundup</link></item>
<item><title>AI 芯片新进展</title><link>https://example.com/chip</link></item>
</channel></rss>"""
    items = RSSAdapter().parse_feed(
        xml, {"keywords": ["AI"], "exclude_keywords": ["；"]}
    )
    assert [item.url for item in items] == ["https://example.com/chip"]


def test_rss_items_carry_feed_authority() -> None:
    items = RSSAdapter().parse_feed(rss_xml(utcnow()), {"authority": "B"})
    assert items[0].authority == "B"


def test_match_keywords_uses_word_boundaries_for_english() -> None:
    # "AI" 不能命中 "AirPods"，但中文关键词直接子串匹配。
    assert match_keywords("AirPods 新款发布", ["AI"]) is False
    assert match_keywords("AI 芯片新进展", ["AI"]) is True
    assert match_keywords("自研大模型发布", ["大模型"]) is True
    assert match_keywords("苹果发布会", ["AI", "大模型"]) is False


def test_rss_feed_keywords_filter_non_ai_items() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>
<item><title>Epic 喜加一：免费领游戏</title><link>https://example.com/game</link>
<description>游戏活动</description></item>
<item><title>某公司发布新一代大模型</title><link>https://example.com/llm</link>
<description>模型能力提升</description></item>
</channel></rss>"""
    items = RSSAdapter().parse_feed(xml, {"keywords": ["大模型", "AI"]})
    assert [item.url for item in items] == ["https://example.com/llm"]


def test_official_blogs_source_is_configured() -> None:
    config = load_config(PROJECT_ROOT)
    source = next(item for item in config.sources if item.id == "official_blogs")
    assert source.kind == "rss"
    assert source.region == "overseas"
    assert source.options["max_age_days"] == 7

    feeds = {feed["id"]: feed for feed in source.options["feeds"]}
    assert set(feeds) == {
        "openai-news",
        "deepmind-blog",
        "nvidia-blog",
        "apple-ml",
        "hf-blog",
    }
    # feed 级 authority 必须显式写，否则会落到默认 B
    assert all(feed.get("authority") for feed in feeds.values())
    # NVIDIA 综合博客必须按 AI 关键词过滤
    assert feeds["nvidia-blog"].get("keywords")

    expected_proxy = config.network.proxy if config.network.mode == "overseas-only" else None
    assert config.proxy_for(source) == expected_proxy


def test_local_sources_overlay_merges_feeds(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AI_RADAR_PROXY", raising=False)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "sources.yaml").write_text(
        "sources:\n"
        "  - id: rss\n"
        "    kind: rss\n"
        "    name: RSS\n"
        "    feeds:\n"
        "      - id: a\n"
        "        url: https://example.com/a\n",
        encoding="utf-8",
    )
    (config_dir / "weights.yaml").write_text("{}\n", encoding="utf-8")
    (config_dir / "models.yaml").write_text("{}\n", encoding="utf-8")
    (config_dir / "sources.local.yaml").write_text(
        "sources:\n"
        "  - id: rss\n"
        "    max_results: 3\n"
        "    feeds:\n"
        "      - id: b\n"
        "        url: https://example.com/b\n",
        encoding="utf-8",
    )

    config = load_config(tmp_path)
    source = config.sources[0]
    assert source.options["max_results"] == 3
    assert [feed["id"] for feed in source.options["feeds"]] == ["a", "b"]


def write_config(root: Path, sources_text: str) -> None:
    config_dir = root / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "sources.yaml").write_text(sources_text, encoding="utf-8")
    (config_dir / "weights.yaml").write_text("{}\n", encoding="utf-8")
    (config_dir / "models.yaml").write_text("{}\n", encoding="utf-8")


def test_config_rejects_duplicate_and_invalid_sources(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AI_RADAR_PROXY", raising=False)
    write_config(
        tmp_path,
        "sources:\n"
        "  - id: rss\n"
        "    kind: rss\n"
        "    authority: Z\n"
        "    feeds:\n"
        "      - id: a\n"
        "        url: ftp://example.com/a\n"
        "  - id: rss\n"
        "    kind: nope\n",
    )

    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path)
    message = str(excinfo.value)
    assert "id 重复" in message
    assert "kind='nope' 不支持" in message
    assert "authority='Z'" in message
    assert "url 必须是 http(s)" in message


def test_config_rejects_empty_rss_feeds(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AI_RADAR_PROXY", raising=False)
    write_config(tmp_path, "sources:\n  - id: rss\n    kind: rss\n")

    with pytest.raises(ConfigError, match="必须配置非空 feeds"):
        load_config(tmp_path)


def lobsters_story(
    short_id: str,
    *,
    score: int = 10,
    hours_ago: float = 1,
    url: str | None = "https://example.com/post",
) -> dict:
    return {
        "short_id": short_id,
        "title": f"Story {short_id}",
        "url": url,
        "comments_url": f"https://lobste.rs/s/{short_id}",
        "score": score,
        "comment_count": 4,
        "created_at": (utcnow() - timedelta(hours=hours_ago)).isoformat(),
        "tags": ["ai"],
        "submitter_user": "alice",
        "description_plain": "desc",
    }


def test_lobsters_parse_filters_score_and_age() -> None:
    now = utcnow()
    stories = [
        lobsters_story("fresh"),
        lobsters_story("cold", score=1),
        lobsters_story("stale", hours_ago=48),
    ]
    items = LobstersAdapter(min_score=3, max_age_hours=24).parse(stories, now=now)
    assert [item.external_id for item in items] == ["fresh"]
    assert items[0].kind == "forum"
    assert items[0].signals == {"points": 10, "comments": 4}
    assert items[0].payload["comments_url"] == "https://lobste.rs/s/fresh"


def test_lobsters_falls_back_to_comments_url_for_text_posts() -> None:
    items = LobstersAdapter().parse([lobsters_story("text", url=None)], now=utcnow())
    assert items[0].url == "https://lobste.rs/s/text"


def test_lobsters_comments_keep_top_level_and_skip_deleted() -> None:
    data = {
        "comments": [
            {"comment_plain": "太短", "score": 9, "depth": 0},
            {"comment_plain": "这是一条足够长的正常评论内容，用来测试评论提炼", "score": 5, "depth": 1},
            {"comment_plain": "层级太深的回复不应该被取用", "score": 50, "depth": 3},
            {"comment_plain": "已删除的评论不应该出现", "score": 50, "depth": 0, "is_deleted": True},
        ]
    }
    assert parse_comments(data) == ["这是一条足够长的正常评论内容，用来测试评论提炼"]
