from __future__ import annotations

from urllib.parse import urlsplit

from ..base import (
    SECTION_TITLES,
    DigestItemView,
    parse_published,
    published_label,
)

# 卡片上展示的是中文，内部 kind 保持英文不变。
KIND_LABEL = {
    "paper": "论文",
    "forum": "讨论",
    "blog": "媒体",
    "repo": "仓库",
}


def dimension_name(tag: str | None) -> str:
    """「5 多模态与生成」→「多模态与生成」：编号只用于内部分类与排序。"""
    if not tag:
        return "未分类"
    head, _, rest = tag.partition(" ")
    return rest if head.isdigit() and rest else tag


def kind_label(kind: str | None) -> str:
    return KIND_LABEL.get(kind or "", kind or "")


def discussion_label_for(url: str | None) -> str:
    """讨论链接的来源名；搜索/详情卡只拿到 URL，按域名反推。"""
    host = (urlsplit(str(url or "")).hostname or "").lower()
    if host.endswith("ycombinator.com"):
        return "HN"
    if host.endswith("lobste.rs"):
        return "Lobsters"
    return "社区"


def _escape_md(text: str) -> str:
    """标题来自外部内容，方括号会破坏 lark_md 的链接语法。"""
    return str(text).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def md_link(text: str, url: str | None) -> str:
    """只给 http(s) 链接生成 Markdown，其余退回纯文本，避免注入伪协议。"""
    scheme = urlsplit(str(url or "")).scheme.lower()
    label = _escape_md(text)
    if scheme not in ("http", "https"):
        return label
    return f"[{label}]({url})"


def meta_line(*parts: str | None) -> str:
    return " · ".join(part for part in parts if part)


def build_digest_card(
    digest_date: str,
    items: list[DigestItemView],
    *,
    kind: str = "daily",
    theme_label: str = "",
    theme_reason: str = "",
    theme_query: str = "",
    theme_when: str = "",
    theme_when_note: str = "",
    preview: bool = False,
) -> dict:
    elements: list[dict] = []
    title = "AI Radar 周报" if kind == "weekly" else "AI Radar"
    reference = parse_published(digest_date)
    if preview:
        title += " 预览"
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "<font color='orange'>预演卡片：基于当前候选生成，不会记入已推记录。</font>",
                },
            }
        )
    if theme_label:
        content = f"**今日主线：{theme_label}**\n<font color='grey'>{theme_reason}</font>"
        # 只展示事件本身的时间；材料的发布时间写在各自的条目上，两者不能混。
        if theme_when:
            note = theme_when_note or "模型推断"
            content += f"\n<font color='grey'>事件时间：{theme_when}（{note}）</font>"
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": content,
                },
            }
        )
        query = (theme_query or theme_label).strip()
        if query:
            elements.append(
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "围绕此话题找"},
                            "type": "default",
                            "value": {"action": "topic_search", "query": query},
                        }
                    ],
                }
            )
    elif theme_reason:
        # 降级成纯快讯时要把原因写出来，否则用户只会觉得「主题没了」。
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"<font color='grey'>{theme_reason}</font>"},
            }
        )
    if not items:
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "今日候选不足，未生成推送。"},
            }
        )
    else:
        section = ""
        for item in items:
            if item.section != section:
                section = item.section
                # 主线有标题时已在卡片顶部展示；没有标题时只展示降级说明。
                # 两种情况下都不该再出现一个空的「今日主线」分区头。
                if section != "theme":
                    header = SECTION_TITLES.get(section, section)
                    elements.append(
                        {
                            "tag": "div",
                            "text": {"tag": "lark_md", "content": f"**{header}**"},
                        }
                    )
            content = (
                f"**{item.rank}. {md_link(item.title, item.url)}**\n"
                f"<font color='grey'>"
                f"{meta_line(kind_label(item.source), dimension_name(item.tag), published_label(item.published_at, item.source, reference), item.reason)}"
                f"</font>"
            )
            if item.note:
                content += f"\n<font color='grey'>{_escape_md(item.note)}</font>"
            if item.discussion_url:
                label = item.discussion_label or discussion_label_for(item.discussion_url)
                content += f"\n[社区讨论（{label}）]({item.discussion_url})"
            elements.append(
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": content},
                }
            )
            elements.append(
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "详情"},
                            "type": "primary",
                            "value": {
                                "action": "detail",
                                "document_id": item.document_id,
                                "digest_date": digest_date,
                            },
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "收藏"},
                            "type": "default",
                            "value": {
                                "action": "save",
                                "document_id": item.document_id,
                                "digest_date": digest_date,
                            },
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "不感兴趣"},
                            "type": "default",
                            "value": {
                                "action": "dislike",
                                "document_id": item.document_id,
                                "digest_date": digest_date,
                            },
                        },
                    ],
                }
            )
            elements.append({"tag": "hr"})

    elements.append(
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "知识库"},
                    "type": "primary",
                    "value": {"action": "kb"},
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "收藏夹"},
                    "type": "default",
                    "value": {"action": "favorites"},
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "状态"},
                    "type": "default",
                    "value": {"action": "status"},
                },
            ],
        }
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"{title} · {digest_date}"},
            "template": "orange" if preview else ("blue" if kind == "daily" else "indigo"),
        },
        "elements": elements,
    }


def build_topic_card(query: str, result, *, limit: int = 5) -> dict:
    """主动查询结果：本地命中可交互，站外补充只给链接。"""
    elements: list[dict] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"围绕「**{query}**」的结果，时间不限。",
            },
        }
    ]

    if not result.local and not result.external:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "没有找到相关内容。换个更短的关键词试试（只留产品名或技术名）。",
                },
            }
        )

    for index, hit in enumerate(result.local[:limit], start=1):
        published = str(hit.published_at or "")[:10]
        meta = meta_line(
            kind_label(hit.kind),
            dimension_name(hit.tag),
            published,
            hit.reason,
        )
        content = f"**{index}. {md_link(hit.title, hit.url)}**\n<font color='grey'>{meta}</font>"
        if hit.in_kb:
            content += "\n<font color='grey'>知识库 · 已精读</font>"
        elif hit.saved:
            content += "\n<font color='grey'>知识库 · 仅收藏</font>"
        if hit.pushed_recently:
            content += "\n<font color='grey'>近 30 天推过</font>"
        if hit.discussion_url:
            content += (
                f"\n[社区讨论（{discussion_label_for(hit.discussion_url)}）]"
                f"({hit.discussion_url})"
            )
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": content}}
        )
        actions = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "详情"},
                "type": "primary",
                "value": {"action": "detail", "document_id": hit.document_id},
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "移出" if hit.saved else "收藏"},
                "type": "default",
                "value": {
                    "action": "kb_remove" if hit.saved else "save",
                    "document_id": hit.document_id,
                },
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "不感兴趣"},
                "type": "default",
                "value": {"action": "dislike", "document_id": hit.document_id},
            },
        ]
        if not hit.in_kb:
            actions.insert(
                1,
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "精读"},
                    "type": "default",
                    "value": {"action": "deep_read", "document_id": hit.document_id},
                },
            )
        elements.append({"tag": "action", "actions": actions})
        elements.append({"tag": "hr"})

    if result.external:
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "**站外补充**"},
            }
        )
        for hit in result.external:
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"{md_link(hit.title, hit.url)}\n"
                            f"<font color='grey'>{meta_line(hit.source, hit.meta)}</font>"
                        ),
                    },
                }
            )

    footer = f"用时 {result.elapsed_ms / 1000:.1f}s"
    if result.note:
        footer += f" · {result.note}"
    elements.append(
        {"tag": "div", "text": {"tag": "lark_md", "content": f"<font color='grey'>{footer}</font>"}}
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"话题 · {query[:40]}"},
            "template": "turquoise",
        },
        "elements": elements,
    }


def build_detail_card(
    document,
    summary: dict,
    digest_date: str | None,
    discussion_url: str | None = None,
    *,
    discussion_label: str = "",
    saved: bool = False,
) -> dict:
    header = meta_line(kind_label(document["kind"]), dimension_name(document["primary_tag"]))
    lines = [
        f"**{header}**",
        md_link("原文链接", document["url"]),
    ]
    if discussion_url:
        label = discussion_label or discussion_label_for(discussion_url)
        lines.append(f"[社区讨论（{label}）]({discussion_url})")
    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}
    ]

    problem = summary.get("problem") or summary.get("abstract")
    if problem and problem != "未知":
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**问题**\n{problem}"}}
        )
    else:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "<font color='grey'>暂无摘要，点「精读」获取结构化讲解。</font>",
                },
            }
        )

    method = summary.get("method")
    if method:
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**方法**\n{method}"}}
        )

    conclusions = summary.get("conclusions") or []
    if conclusions:
        text = "\n".join(f"- {item}" for item in conclusions)
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**结论**\n{text}"}}
        )

    why = summary.get("why")
    if why:
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**为什么值得关注**\n{why}"}}
        )

    community = summary.get("discussion") or []
    if community:
        text = "\n".join(f"- {point}" for point in community)
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**社区怎么看**\n{text}"}}
        )

    elements.append(
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "精读"},
                    "type": "primary",
                    "value": {
                        "action": "deep_read",
                        "document_id": int(document["id"]),
                        "digest_date": digest_date,
                    },
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "取消收藏" if saved else "收藏"},
                    "type": "default",
                    "value": {
                        "action": "unsave" if saved else "save",
                        "document_id": int(document["id"]),
                        "digest_date": digest_date,
                    },
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "不感兴趣"},
                    "type": "default",
                    "value": {
                        "action": "dislike",
                        "document_id": int(document["id"]),
                        "digest_date": digest_date,
                    },
                },
            ],
        }
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": document["title"][:80]},
            "template": "green",
        },
        "elements": elements,
    }


def build_qa_fallback_card(question: str, related: list) -> dict:
    """知识库没有依据时：说明情况，并把「站外检索 / 常识回答」交给用户决定。"""
    lines = ["知识库里没有找到足够依据，不能凭空回答。"]
    if related:
        lines.append("\n最接近的条目（不足以作为依据）：")
        for item in related:
            lines.append(f"- {md_link(item.title[:60], item.url)}")
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "知识库 · 没有依据"},
            "template": "grey",
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(lines)},
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "站外检索"},
                        "type": "primary",
                        "value": {"action": "topic_search", "query": question},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "用常识回答"},
                        "type": "default",
                        "value": {"action": "qa_common", "question": question},
                    },
                ],
            },
        ],
    }


def build_remove_confirm_card(document) -> dict:
    """精读卡片移出前的二次确认。"""
    content = (
        f"**{document['title'][:70]}**\n"
        "移出后会删除知识卡片、原文切块、断言和向量；原文文件保留在本地，"
        "文档标记为已读，之后不会再被推送。"
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "从知识库移出"},
            "template": "red",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": content}},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "确认移出"},
                        "type": "danger",
                        "value": {
                            "action": "kb_remove_confirm",
                            "document_id": int(document["id"]),
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "取消"},
                        "type": "default",
                        "value": {
                            "action": "kb_remove_cancel",
                            "document_id": int(document["id"]),
                        },
                    },
                ],
            },
        ],
    }


def build_l3_card(document, card: dict) -> dict:
    header = meta_line(kind_label(document["kind"]), dimension_name(document["primary_tag"]))
    elements: list[dict] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"**{header}**\n"
                    + md_link("原文链接", document["url"])
                ),
            },
        },
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**问题**\n{card.get('problem', '未知')}"},
        },
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**方法**\n{card.get('method', '未知')}"},
        },
    ]

    for title, key in (("证据", "evidence"), ("结论", "conclusions"), ("局限", "limitations")):
        values = card.get(key) or []
        if values:
            text = "\n".join(f"- {item}" for item in values)
            elements.append(
                {"tag": "div", "text": {"tag": "lark_md", "content": f"**{title}**\n{text}"}}
            )

    community = card.get("community") or {}
    if isinstance(community, dict):
        lines: list[str] = []
        for label, key in (("共识", "consensus"), ("争议", "disputes"), ("补充", "tips")):
            values = community.get(key) or []
            if values:
                lines.append(f"**{label}**")
                lines.extend(f"- {item}" for item in values)
        if lines:
            elements.append({"tag": "hr"})
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "**社区怎么看（Hacker News）**\n" + "\n".join(lines),
                    },
                }
            )

    l3 = str(card.get("l3") or "").strip()
    if l3:
        # 飞书卡片有体积上限，深度讲解过长容易被拒发。
        if len(l3) > 3000:
            l3 = l3[:2999] + "…"
        elements.append({"tag": "hr"})
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**深度讲解**\n{l3}"}}
        )

    actions: list[dict] = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "相关"},
            "type": "default",
            "value": {"action": "related", "document_id": int(document["id"])},
        },
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "收藏"},
            "type": "primary",
            "value": {"action": "save", "document_id": int(document["id"])},
        },
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "不感兴趣"},
            "type": "default",
            "value": {"action": "dislike", "document_id": int(document["id"])},
        },
    ]
    elements.append({"tag": "action", "actions": actions})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"精读 · {document['title'][:60]}"},
            "template": "purple",
        },
        "elements": elements,
    }


def build_related_card(document, related: list[dict]) -> dict:
    elements: list[dict] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"与《{document['title'][:50]}》相关的已精读文章（按相似度排序）：",
            },
        }
    ]

    if not related:
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "知识库里还没有可对比的文章。"},
            }
        )

    for item in related:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**{item['title'][:70]}**\n"
                        f"<font color='grey'>{dimension_name(item['tag'])} · 相似度 {item['score']:.0%} · {item['reason']}</font>\n"
                        + md_link("原文", item["url"])
                    ),
                },
            }
        )
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "详情"},
                        "type": "default",
                        "value": {"action": "detail", "document_id": item["document_id"]},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "对比"},
                        "type": "primary",
                        "value": {
                            "action": "compare",
                            "left_id": int(document["id"]),
                            "right_id": item["document_id"],
                        },
                    },
                ],
            }
        )
        elements.append({"tag": "hr"})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"相关 · {document['title'][:50]}"},
            "template": "turquoise",
        },
        "elements": elements,
    }


KB_PAGE_SIZE = 6
KB_WINDOWS = [("7", "近 7 天"), ("30", "近 30 天")]


def _filter_value(action: str, scope: str, filters: dict, key: str, value: str | None) -> dict:
    merged = dict(filters or {})
    merged[key] = value
    return {"action": action, "scope": scope, "page": 0, "filters": merged}


def _filter_row(
    label: str,
    key: str,
    options: list[tuple[str, str]],
    current: str | None,
    *,
    action: str,
    scope: str,
    filters: dict,
) -> tuple[dict, dict]:
    """一行筛选按钮；点已选中的项等于取消该筛选。"""
    actions = []
    for value, text in [("", "全部"), *options]:
        selected = (not current and not value) or current == value
        actions.append(
            {
                "tag": "button",
                "text": {
                    "tag": "plain_text",
                    "content": ("✓ " if selected else "") + text,
                },
                "type": "primary" if selected else "default",
                "value": _filter_value(action, scope, filters, key, value or None),
            }
        )
    return {
        "tag": "div",
        "text": {"tag": "lark_md", "content": f"<font color='grey'>{label}</font>"},
    }, {"tag": "action", "actions": actions}


def build_kb_card(
    entries,
    counts: dict,
    *,
    title: str = "知识库",
    scope: str = "kb",
    filters: dict | None = None,
    page: int = 0,
    pages: int = 1,
    facets: dict | None = None,
    empty_text: str | None = None,
) -> dict:
    """知识库 / 收藏夹列表：支持筛选与翻页，未精读的条目可一键精读，每条都能移出。"""
    if empty_text is None:
        empty_text = f"{title}还是空的：在任意卡片上点「收藏」，或直接精读一篇。"
    filters = filters or {}
    facets = facets or {}
    total = int(counts.get("total") or 0)
    action = "favorites" if scope == "favorites" else "kb"

    elements: list[dict] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"共 **{total}** 篇：已精读 {int(counts.get('deep') or 0)}"
                    f" · 仅收藏 {int(counts.get('only_saved') or 0)}"
                ),
            },
        }
    ]

    rows: list[tuple[str, str, list[tuple[str, str]]]] = []
    if facets.get("dimensions"):
        rows.append(
            (
                "维度",
                "dimension",
                [(tag, dimension_name(tag)) for tag in facets["dimensions"]],
            )
        )
    if facets.get("kinds"):
        rows.append(
            ("类型", "kind", [(kind, kind_label(kind)) for kind in facets["kinds"]])
        )
    if scope != "favorites":
        rows.append(("状态", "status", [("deep", "已精读"), ("saved", "仅收藏")]))
    rows.append(("时间", "window", KB_WINDOWS))

    for label, key, options in rows:
        head, buttons = _filter_row(
            label,
            key,
            options,
            filters.get(key),
            action=action,
            scope=scope,
            filters=filters,
        )
        elements.append(head)
        elements.append(buttons)

    if not entries:
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": empty_text + "（可先清空筛选）"},
            }
        )

    for index, row in enumerate(entries, start=1):
        deep_read = bool(row["deep_read"])
        meta = meta_line(
            kind_label(row["kind"]),
            dimension_name(row["primary_tag"]),
            "已精读" if deep_read else "仅收藏",
        )
        content = (
            f"**{index + page * KB_PAGE_SIZE}. {md_link(row['title'], row['url'])}**\n"
            f"<font color='grey'>{meta}</font>"
        )
        if not deep_read:
            content += "\n<font color='grey'>还没精读，点「精读」升级成完整卡片</font>"
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": content}})

        actions = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "详情"},
                "type": "primary",
                "value": {"action": "detail", "document_id": int(row["id"])},
            }
        ]
        if not deep_read:
            actions.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "精读"},
                    "type": "default",
                    "value": {"action": "deep_read", "document_id": int(row["id"])},
                }
            )
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "移出"},
                "type": "danger",
                "value": {"action": "kb_remove", "document_id": int(row["id"])},
            }
        )
        elements.append({"tag": "action", "actions": actions})
        elements.append({"tag": "hr"})

    if pages > 1:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"<font color='grey'>第 {page + 1}/{pages} 页 · 每页 {KB_PAGE_SIZE} 条</font>",
                },
            }
        )
        nav = []
        if page > 0:
            nav.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "上一页"},
                    "type": "default",
                    "value": _filter_value(action, scope, filters, "page", None)
                    | {"page": page - 1},
                }
            )
        if page < pages - 1:
            nav.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "下一页"},
                    "type": "primary",
                    "value": _filter_value(action, scope, filters, "page", None)
                    | {"page": page + 1},
                }
            )
        elements.append({"tag": "action", "actions": nav})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"{title} · {total} 篇"},
            "template": "blue",
        },
        "elements": elements,
    }


def build_compare_card(left, right, comparison: dict) -> dict:
    def clip(text: str, limit: int = 320) -> str:
        text = str(text)
        return text if len(text) <= limit else text[: limit - 1] + "…"

    elements: list[dict] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    f"{md_link('A 原文', left['url'])} · {md_link('B 原文', right['url'])}\n"
                    f"{comparison['summary']}"
                ),
            },
        },
        {"tag": "hr"},
    ]

    for row in comparison["rows"]:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**{row['aspect']}**\n"
                        f"A：{clip(row['left'])}\n"
                        f"B：{clip(row['right'])}"
                    ),
                },
            }
        )

    evidence: list[str] = []
    for block in comparison.get("evidence") or []:
        for claim in block["claims"][:3]:
            text = claim["text"][:110]
            evidence.append(f"- {block['side']}#{claim['id']}（{claim['kind']}）{text}")
    if evidence:
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "**原文依据**\n" + "\n".join(evidence),
                },
            }
        )

    elements.append(
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "A 详情"},
                    "type": "default",
                    "value": {"action": "detail", "document_id": int(left["id"])},
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "B 详情"},
                    "type": "default",
                    "value": {"action": "detail", "document_id": int(right["id"])},
                },
            ],
        }
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"对比 · {left['title'][:32]} vs {right['title'][:32]}",
            },
            "template": "indigo",
        },
        "elements": elements,
    }


def build_reason_card(
    document_id: int,
    digest_date: str | None,
    reasons: list[str],
) -> dict:
    actions: list[dict] = []
    for reason in reasons:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": reason},
                "type": "default",
                "value": {
                    "action": "dislike_reason",
                    "document_id": document_id,
                    "digest_date": digest_date,
                    "reason": reason,
                },
            }
        )

    rows = [actions[index : index + 4] for index in range(0, len(actions), 4)]
    elements: list[dict] = [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": "请选择原因，后续会据此调整推送权重。"},
        }
    ]
    elements.extend({"tag": "action", "actions": row} for row in rows)

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "不感兴趣的原因"},
            "template": "orange",
        },
        "elements": elements,
    }


