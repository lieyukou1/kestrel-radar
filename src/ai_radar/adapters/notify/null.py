from __future__ import annotations

from .base import DigestItemView, EventView


class NullNotifier:
    """只生成不投递：日报已写库、网页可看，飞书/Webhook 都不发。

    返回值统一为空字符串，digest 的 stats.outbox 会因此为空，便于区分
    「没有投递」和「投递到了某个地址」。
    """

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
        return ""

    def send_text(self, text: str) -> str:
        return ""

    def send_card(self, card: dict) -> str:
        return ""
