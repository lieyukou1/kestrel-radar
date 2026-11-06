from __future__ import annotations

from ai_radar.adapters.sources.base import RawItem
from ai_radar.pipeline.normalize import (
    canonical_from_url,
    canonical_id,
    merge_item,
    normalize_url,
)
from ai_radar.store import repo
from ai_radar.store.db import connect, init_db


def _conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    # raw_items.source_id 有外键，先把测试源登记好
    repo.upsert_source(
        conn,
        source_id="test",
        kind="blog",
        name="test",
        authority="B",
        region="domestic",
        config={},
    )
    return conn


def item(
    url: str, *, kind: str = "forum", external_id: str = "x", title: str = "title"
) -> RawItem:
    return RawItem(
        source_id="test",
        external_id=external_id,
        url=url,
        title=title,
        kind=kind,
    )


def test_arxiv_version_is_stripped() -> None:
    assert canonical_id(item("https://arxiv.org/abs/2401.12345v2")) == "arxiv:2401.12345"
    assert canonical_id(item("https://arxiv.org/pdf/2401.12345v3")) == "arxiv:2401.12345"


def test_hn_story_pointing_to_arxiv_merges() -> None:
    hn = item("https://arxiv.org/abs/2401.12345v2")
    paper = item("https://arxiv.org/abs/2401.12345", kind="paper", external_id="2401.12345")
    assert canonical_id(hn) == canonical_id(paper)


def test_github_repo_is_normalized() -> None:
    assert canonical_id(item("https://github.com/Owner/Repo.git")) == "github:owner/repo"


def test_url_only_canonical_matches_collector() -> None:
    """手动发链接和采集侧必须算出同一个身份，否则同一条会被存两份。"""
    for url in (
        "https://arxiv.org/abs/2401.12345v2",
        "https://arxiv.org/pdf/2401.12345v3",
        "https://github.com/Owner/Repo.git",
        "https://Example.com/Path/?utm_source=x&keep=1",
    ):
        assert canonical_from_url(url) == canonical_id(item(url))


def test_tracking_params_are_removed() -> None:
    url = "https://Example.com/Path/?utm_source=x&utm_medium=y&keep=1#frag"
    assert normalize_url(url) == "https://example.com/Path?keep=1"


def test_similar_titles_with_different_versions_are_not_merged(tmp_path) -> None:
    """同系列两代产品的标题很像，但不能因此并成一条。"""
    conn = _conn(tmp_path)

    first = merge_item(
        conn,
        item(
            "https://example.com/gpt5",
            kind="blog",
            external_id="a",
            title="GPT-5 正式发布，性能大幅提升",
        ),
        "B",
    )
    second = merge_item(
        conn,
        item(
            "https://example.com/gpt51",
            kind="blog",
            external_id="b",
            title="GPT-5.1 正式发布，性能大幅提升",
        ),
        "B",
    )
    assert first != second
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2


def test_near_identical_titles_still_merge(tmp_path) -> None:
    conn = _conn(tmp_path)

    first = merge_item(
        conn,
        item(
            "https://example.com/a",
            kind="blog",
            external_id="a",
            title="Gemma 4 发布：性能提升 30%",
        ),
        "B",
    )
    second = merge_item(
        conn,
        item(
            "https://example.com/b",
            kind="blog",
            external_id="b",
            title="Gemma 4 发布：性能提升 30%！",
        ),
        "B",
    )
    assert first == second
