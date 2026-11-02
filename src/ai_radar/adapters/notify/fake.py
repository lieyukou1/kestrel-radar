from __future__ import annotations

from pathlib import Path

from .base import DigestItemView
from .markdown import card_to_text, render_digest


class FakeNotifier:
    """本地渠道：把 digest 写到 data/outbox，便于离线验证与无飞书部署。"""

    def __init__(self, outbox_dir: Path) -> None:
        self.outbox_dir = outbox_dir

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
    ) -> str:
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        suffix = "weekly" if kind == "weekly" else "daily"
        path = self.outbox_dir / f"digest-{digest_date}-{suffix}.md"
        content = render_digest(
            digest_date,
            items,
            kind=kind,
            theme_label=theme_label,
            theme_reason=theme_reason,
            theme_when=theme_when,
            theme_when_note=theme_when_note,
            preview=preview,
        )
        path.write_text(content, encoding="utf-8")
        return str(path)

    def send_text(self, text: str) -> str:
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        path = self.outbox_dir / "last-text.md"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def send_card(self, card: dict) -> str:
        return self.send_text(card_to_text(card))
