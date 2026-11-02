from __future__ import annotations

from ai_radar.adapters.notify.base import DigestItemView
from ai_radar.adapters.notify.feishu import (
    build_detail_card,
    build_digest_card,
    build_kb_card,
    build_reason_card,
    dimension_name,
    kind_label,
)


def all_text(card: dict) -> str:
    """卡片按分区插了区块标题，正文要汇总所有 div 才能断言。"""
    return "\n".join(
        element["text"]["content"]
        for element in card["elements"]
        if element.get("tag") == "div"
    )


def item(document_id: int, rank: int) -> DigestItemView:
    return DigestItemView(
        rank=rank,
        document_id=document_id,
        title=f"Paper {rank}",
        url=f"https://example.com/{rank}",
        tag="1 模型与架构",
        source="paper",
        reason="arXiv 120 分",
        score=0.8,
    )


def test_digest_card_has_actions() -> None:
    card = build_digest_card("2026-10-21", [item(1, 1), item(2, 2)])
    assert card["header"]["title"]["content"] == "AI Radar · 2026-10-21"

    actions = [element for element in card["elements"] if element["tag"] == "action"]
    values = [action["value"] for element in actions for action in element["actions"]]
    assert {"action": "save", "document_id": 1, "digest_date": "2026-10-21"} in values
    assert {"action": "dislike", "document_id": 1, "digest_date": "2026-10-21"} in values
    assert {"action": "kb"} in values
    assert {"action": "favorites"} in values
    assert {"action": "status"} in values


def test_empty_digest_card() -> None:
    card = build_digest_card("2026-10-21", [])
    text = card["elements"][0]["text"]["content"]
    assert "候选不足" in text


def test_digest_card_shows_hn_discussion_link() -> None:
    entry = item(1, 1)
    entry.discussion_url = "https://news.ycombinator.com/item?id=1"
    card = build_digest_card("2026-10-21", [entry])
    content = all_text(card)
    assert "社区讨论（HN）" in content


def test_digest_card_labels_lobsters_discussion_link() -> None:
    entry = item(1, 1)
    entry.discussion_url = "https://lobste.rs/s/abc123"
    card = build_digest_card("2026-10-21", [entry])
    assert "社区讨论（Lobsters）" in all_text(card)


def test_digest_card_escapes_brackets_in_titles() -> None:
    entry = item(1, 1)
    entry.title = "标题 [x](https://evil.example)"
    card = build_digest_card("2026-10-21", [entry])
    assert "[x](https://evil.example)" not in all_text(card)


def test_detail_card_renders_community_points() -> None:
    document = {
        "id": 1,
        "kind": "forum",
        "primary_tag": "4 Agent 与应用",
        "url": "https://example.com/post",
        "title": "A story",
    }
    card = build_detail_card(
        document,
        {"abstract": "摘要", "discussion": ["社区认为成本偏高", "有人复现失败"]},
        "2026-11-02",
    )
    content = all_text(card)
    assert "社区怎么看" in content
    assert "社区认为成本偏高" in content
    assert "有人复现失败" in content


def test_digest_card_meta_line_is_clean() -> None:
    """元信息行只出现一次维度、类型用中文、不暴露内部编号。"""
    card = build_digest_card("2026-10-21", [item(1, 1)])
    content = all_text(card)
    assert content.count("模型与架构") == 1
    assert "1 模型与架构" not in content
    assert "论文" in content
    assert "paper" not in content


def test_digest_card_explains_missing_theme() -> None:
    """降级成纯快讯时要写明原因，不能凭空少一块。"""
    card = build_digest_card(
        "2026-10-27",
        [item(1, 1)],
        theme_reason="今日没有跨来源的强主线，只推送时效性内容。",
    )
    assert "没有跨来源的强主线" in all_text(card)


def test_preview_card_is_labelled() -> None:
    """预演卡不能和真实推送长得一样，否则会被记成“重复推送”。"""
    card = build_digest_card("2026-10-28", [item(1, 1)], preview=True)
    assert "预览" in card["header"]["title"]["content"]
    assert "预演卡片" in all_text(card)
    assert card["header"]["template"] == "orange"


def test_digest_card_shows_publish_date_except_repo() -> None:
    """论文/讨论/媒体必须带发布时间，否则两年前的旧内容会被读成新闻。"""
    paper = item(1, 1)
    paper.published_at = "2025-03-04T08:00:00+00:00"
    forum = item(2, 2)
    forum.source = "forum"
    forum.published_at = "2026-10-20T08:00:00+00:00"
    repo = item(3, 3)
    repo.source = "repo"
    repo.published_at = "2019-01-01T00:00:00+00:00"

    content = all_text(build_digest_card("2026-10-28", [paper, forum, repo]))

    assert "发布于 2025-03-04（1 年前）" in content
    assert "发布于 2026-10-20（8 天前）" in content
    assert "2019-01-01" not in content


def test_digest_card_theme_keeps_material_dates_out_of_event_time() -> None:
    """主线只放事件时间；材料发布日期不能混进主线，否则旧料会被读成新事件。"""
    early = item(1, 1)
    early.section = "theme"
    early.published_at = "2024-11-02T00:00:00+00:00"

    content = all_text(build_digest_card("2026-10-28", [early], theme_label="JEV"))

    assert "材料时间" not in content
    assert "事件时间" not in content
    assert "发布于 2024-11-02（1 年前）" in content


def test_digest_card_ignores_invalid_publish_date() -> None:
    broken = item(1, 1)
    broken.published_at = "not-a-date"

    content = all_text(build_digest_card("2026-10-28", [broken]))

    assert "not-a-date" not in content


def test_digest_card_theme_shows_inferred_event_time() -> None:
    content = all_text(
        build_digest_card(
            "2026-10-29",
            [],
            theme_label="Jev 类型化概率决策模型",
            theme_reason="多个来源同时讨论",
            theme_when="2025-08",
        )
    )
    assert "事件时间：2025-08（模型推断）" in content


def test_digest_card_theme_omits_event_time_when_unknown() -> None:
    content = all_text(
        build_digest_card("2026-10-29", [], theme_label="某话题", theme_when="")
    )
    assert "事件时间" not in content


def test_digest_card_theme_shows_search_evidence_note() -> None:
    content = all_text(
        build_digest_card(
            "2026-10-29",
            [],
            theme_label="JEV",
            theme_when="2026-09-17",
            theme_when_note="据 8 条检索报道",
        )
    )
    assert "事件时间：2026-09-17（据 8 条检索报道）" in content


def test_dimension_and_kind_labels() -> None:
    assert dimension_name("5 多模态与生成") == "多模态与生成"
    assert dimension_name("未分类") == "未分类"
    assert dimension_name(None) == "未分类"
    assert kind_label("paper") == "论文"
    assert kind_label("repo") == "仓库"


def test_reason_card_has_all_reasons() -> None:
    reasons = ["维度不相关", "太偏理论", "太偏工程"]
    card = build_reason_card(42, "2026-10-21", reasons)
    actions = [element for element in card["elements"] if element["tag"] == "action"]
    values = [action["value"] for element in actions for action in element["actions"]]
    assert {value["reason"] for value in values} == set(reasons)
    assert all(value["document_id"] == 42 for value in values)


def test_detail_card_without_summary_shows_hint() -> None:
    """没有摘要时不要显示「问题 未知」，给一句可操作提示。"""
    document = {
        "id": 7,
        "kind": "forum",
        "primary_tag": None,
        "url": "https://example.com/story",
        "title": "HN story",
    }
    card = build_detail_card(document, {}, None, "https://news.ycombinator.com/item?id=7")
    content = all_text(card)
    assert "未知" not in content
    assert "暂无摘要" in content
    assert "news.ycombinator.com/item?id=7" in content


def test_kb_card_marks_saved_only_and_offers_deep_read() -> None:
    entries = [
        {
            "id": 1,
            "title": "只收藏的",
            "url": "https://example.com/a",
            "kind": "blog",
            "primary_tag": None,
            "deep_read": 0,
        },
        {
            "id": 2,
            "title": "精读过的",
            "url": "https://example.com/b",
            "kind": "paper",
            "primary_tag": "1 模型与架构",
            "deep_read": 1,
        },
    ]
    card = build_kb_card(entries, {"deep": 1, "only_saved": 1, "total": 2})
    content = all_text(card)
    assert "仅收藏" in content
    assert "已精读" in content
    assert card["header"]["title"]["content"] == "知识库 · 2 篇"

    item_actions = [
        element["actions"]
        for element in card["elements"]
        if element.get("tag") == "action"
        and any(button["text"]["content"] == "详情" for button in element["actions"])
    ]
    assert [button["text"]["content"] for button in item_actions[0]] == ["详情", "精读", "移出"]
    assert [button["text"]["content"] for button in item_actions[1]] == ["详情", "移出"]


def test_kb_card_filters_and_pagination() -> None:
    entries = [
        {
            "id": 3,
            "title": "第三篇",
            "url": "https://example.com/c",
            "kind": "paper",
            "primary_tag": "4 Agent 与应用",
            "deep_read": 1,
        }
    ]
    card = build_kb_card(
        entries,
        {"total": 13, "deep": 12, "only_saved": 1},
        filters={"kind": "paper"},
        page=0,
        pages=3,
        facets={"dimensions": ["4 Agent 与应用"], "kinds": ["paper", "blog"]},
    )
    buttons = [
        button
        for element in card["elements"]
        if element.get("tag") == "action"
        for button in element["actions"]
    ]
    labels = [button["text"]["content"] for button in buttons]
    assert "✓ 论文" in labels
    assert "✓ 全部" in labels  # 维度未筛选
    assert "下一页" in labels
    assert "上一页" not in labels
    assert "第 1/3 页" in str(card)

    next_button = [button for button in buttons if button["text"]["content"] == "下一页"][0]
    assert next_button["value"]["page"] == 1
    assert next_button["value"]["filters"]["kind"] == "paper"
    assert next_button["value"]["action"] == "kb"


def test_favorites_card_hides_status_filter() -> None:
    card = build_kb_card(
        [],
        {"total": 0, "deep": 0, "only_saved": 0},
        title="收藏夹",
        scope="favorites",
        facets={"dimensions": [], "kinds": []},
    )
    labels = [
        button["text"]["content"]
        for element in card["elements"]
        if element.get("tag") == "action"
        for button in element["actions"]
    ]
    assert "已精读" not in labels
    assert "✓ 全部" in labels


def test_detail_card_toggles_save_button() -> None:
    document = {
        "id": 7,
        "kind": "blog",
        "primary_tag": "1 模型与架构",
        "url": "https://example.com/story",
        "title": "Story",
    }
    saved_card = build_detail_card(document, {"problem": "p"}, None, saved=True)
    buttons = [
        action
        for element in saved_card["elements"]
        if element.get("tag") == "action"
        for action in element["actions"]
    ]
    save_button = [button for button in buttons if button["value"].get("action") in ("save", "unsave")]
    assert save_button[0]["text"]["content"] == "取消收藏"
    assert save_button[0]["value"]["action"] == "unsave"


def test_favorites_card_has_its_own_title() -> None:
    """收藏夹要有独立界面，不能只回一段纯文本。"""
    card = build_kb_card([], {"total": 0, "deep": 0, "only_saved": 0}, title="收藏夹")
    assert card["header"]["title"]["content"] == "收藏夹 · 0 篇"
    assert "收藏夹还是空的" in all_text(card)


def test_remove_confirm_card_buttons() -> None:
    from ai_radar.adapters.notify.feishu import build_remove_confirm_card

    document = {"id": 9, "title": "待移出"}
    card = build_remove_confirm_card(document)
    actions = [element for element in card["elements"] if element["tag"] == "action"]
    values = [action["value"] for action in actions[0]["actions"]]
    assert {"action": "kb_remove_confirm", "document_id": 9} in values
    assert {"action": "kb_remove_cancel", "document_id": 9} in values
    assert "原文文件保留" in all_text(card)


def test_qa_fallback_card_offers_two_paths() -> None:
    """没有知识库依据时，让用户自己选站外检索还是用常识回答。"""
    from ai_radar.adapters.notify.feishu import build_qa_fallback_card
    from ai_radar.pipeline.search import SearchHit

    related = [
        SearchHit(
            document_id=3,
            title="最接近但不是依据",
            url="https://example.com/near",
            tag="1 模型与架构",
            kind="paper",
            score=0.2,
            reason="",
        )
    ]
    card = build_qa_fallback_card("RSI 的结论是什么", related)
    assert card["header"]["title"]["content"] == "知识库 · 没有依据"
    assert "没有找到足够依据" in all_text(card)
    assert "https://example.com/near" in all_text(card)

    actions = [element for element in card["elements"] if element["tag"] == "action"]
    values = [action["value"] for action in actions[0]["actions"]]
    assert {"action": "topic_search", "query": "RSI 的结论是什么"} in values
    assert {"action": "qa_common", "question": "RSI 的结论是什么"} in values


def test_qa_fallback_card_without_related() -> None:
    from ai_radar.adapters.notify.feishu import build_qa_fallback_card

    card = build_qa_fallback_card("什么是 MoE", [])
    assert "最接近的条目" not in all_text(card)
