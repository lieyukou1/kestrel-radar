"""与渠道无关的 Markdown 渲染。

飞书卡片之外的渠道（Webhook / 本地文件）都发同一份 Markdown，
避免每个渠道各写一套格式，也保证内容口径一致。
"""

from __future__ import annotations

from .base import SECTION_TITLES, DigestItemView, EventView, parse_published, published_label


def _render_events(events: list[EventView], reference) -> list[str]:
    """一级事件列表 + 二级文章明细，离线渠道一次看全。"""
    lines = [f"## 今日事件（{len(events)}）", ""]
    for event in events:
        meta = [f"{event.item_count} 篇"]
        if event.source_count:
            meta.append(f"{event.source_count} 来源")
        if event.day_index > 1:
            meta.append(f"第 {event.day_index} 天 · 新增 {event.new_count} 条")
        lines.append(f"### {event.rank}. {event.label}")
        lines.append("")
        lines.append(f"- {' · '.join(meta)}")
        if event.why:
            lines.append(f"- 理由：{event.why}")
        lines.append("")
        for index, item in enumerate(event.items, start=1):
            lines.append(f"#### {event.rank}.{index} {item.title}")
            lines.append("")
            lines.append(f"- 维度：{item.tag}")
            lines.append(f"- 来源：{item.source}")
            published = published_label(item.published_at, item.source, reference)
            if published:
                lines.append(f"- 发布：{published}")
            if item.reason:
                lines.append(f"- 理由：{item.reason}")
            if item.discussion_url:
                label = item.discussion_label or "社区"
                lines.append(f"- 社区讨论（{label}）：{item.discussion_url}")
            lines.append(f"- 链接：{item.url}")
            lines.append("")
    return lines


def render_digest(
    digest_date: str,
    items: list[DigestItemView],
    *,
    kind: str = "daily",
    theme_label: str = "",
    theme_reason: str = "",
    theme_when: str = "",
    theme_when_note: str = "",
    preview: bool = False,
    events: list[EventView] | None = None,
) -> str:
    title = "Kestrel Radar 周报" if kind == "weekly" else "Kestrel Radar"
    if preview:
        title += " 预览"
    lines = [f"# {title} · {digest_date}", ""]
    reference = parse_published(digest_date)
    if preview:
        lines.extend(["> 预演内容：基于当前候选生成，不会记入已推记录。", ""])
    if events:
        return "\n".join(lines + _render_events(events, reference))
    if theme_label:
        lines.extend([f"## {theme_label}", "", theme_reason, ""])
        if theme_when:
            note = theme_when_note or "模型推断"
            lines.extend([f"事件时间：{theme_when}（{note}）", ""])
    elif theme_reason:
        lines.extend([theme_reason, ""])
    if not items:
        lines.append("今日候选不足，未生成推送。")
        return "\n".join(lines)

    section = ""
    for item in items:
        if item.section != section:
            section = item.section
            # 主线标题或降级说明已在顶部展示，这里不再重复分区头。
            if section != "theme":
                lines.extend([f"### {SECTION_TITLES.get(section, section)}", ""])
        lines.append(f"#### {item.rank}. {item.title}")
        lines.append("")
        lines.append(f"- 维度：{item.tag}")
        lines.append(f"- 来源：{item.source}")
        published = published_label(item.published_at, item.source, reference)
        if published:
            lines.append(f"- 发布：{published}")
        if item.reason:
            lines.append(f"- 理由：{item.reason}")
        if item.discussion_url:
            label = item.discussion_label or "社区"
            lines.append(f"- 社区讨论（{label}）：{item.discussion_url}")
        if item.note:
            lines.append(f"- 回看：{item.note}")
        if item.followed:
            lines.append("- 关注命中")
        lines.append(f"- 分数：{item.score:.3f}")
        lines.append(f"- 链接：{item.url}")
        lines.append("")
    return "\n".join(lines)


def card_to_text(card: dict) -> str:
    """把飞书卡片降级成纯文本，供不支持卡片的渠道使用。"""
    lines: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            # 按钮文字对纯文本渠道没有意义，跳过
            if node.get("tag") == "button":
                return
            content = node.get("content")
            if isinstance(content, str) and content.strip():
                lines.append(content.strip())
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(card)
    return "\n\n".join(lines)
