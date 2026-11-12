from __future__ import annotations

from .base import DigestItemView, EventView, Notifier


class NoticeNotifier:
    """飞书降级为通知：只发一条带网页链接的摘要，不再发完整事件卡片。

    其余方法（send_text / send_card）全部委托给内层通知器，保证飞书的
    卡片交互、精读完成提醒等既有能力不受影响。
    """

    def __init__(self, inner: Notifier, web_url: str) -> None:
        self.inner = inner
        self.web_url = web_url.rstrip("/")

    def __getattr__(self, name: str):
        """轮询兜底等场景会用到 Notifier 协议之外的飞书方法（list_messages 等）。

        通知模式只是替换日报投递方式，不应削弱内层的其他能力，因此未知属性
        一律委托给内层通知器。
        """
        return getattr(self.inner, name)

    def _message(
        self,
        digest_date: str,
        items: list[DigestItemView],
        *,
        kind: str,
        theme_label: str,
        preview: bool,
        events: list[EventView] | None,
    ) -> str:
        label = "周报" if kind == "weekly" else "日报"
        prefix = "【预览】" if preview else ""
        lines = [f"{prefix}Kestrel Radar · {digest_date} {label}已生成"]
        if events:
            lines.append(f"{len(events)} 个事件 · {len(items)} 篇内容")
        elif items:
            lines.append(f"{len(items)} 篇内容")
        if theme_label:
            lines.append(f"主线：{theme_label}")
        lines.append(f"打开：{self.web_url}/")
        return "\n".join(lines)

    def send_digest(
        self,
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
        events: list[EventView] | None = None,
    ) -> str:
        return self.inner.send_text(
            self._message(
                digest_date,
                items,
                kind=kind,
                theme_label=theme_label,
                preview=preview,
                events=events,
            )
        )

    def send_text(self, text: str) -> str:
        return self.inner.send_text(text)

    def send_card(self, card: dict) -> str:
        return self.inner.send_card(card)
