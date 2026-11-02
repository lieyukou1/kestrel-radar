"""通用 Webhook 渠道：给不支持飞书的用户一个最小可用的推送出口。

支持几种常见格式（generic / slack / discord / dingtalk / feishu），
内容统一是 Markdown；飞书、钉钉这类平台会走它们自带的文本消息结构。
"""

from __future__ import annotations

import time

import httpx

from .base import DigestItemView
from .markdown import card_to_text, render_digest

SUPPORTED_FORMATS = ("generic", "slack", "discord", "dingtalk", "feishu")
# 各平台单条消息的正文上限不同，超出部分截断并标注，避免整条发送失败。
MAX_CHARS = {
    "slack": 38_000,
    "discord": 1_900,
    "dingtalk": 18_000,
    "feishu": 18_000,
}


def build_payload(fmt: str, text: str, *, title: str = "Kestrel Radar") -> dict:
    limit = MAX_CHARS.get(fmt)
    if limit and len(text) > limit:
        text = text[: limit - 20] + "\n\n…（内容过长，已截断）"
    if fmt == "slack":
        return {"text": text}
    if fmt == "discord":
        return {"content": text}
    if fmt == "dingtalk":
        return {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
    if fmt == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    return {"title": title, "text": text, "markdown": text}


class WebhookNotifier:
    def __init__(
        self,
        url: str,
        *,
        format: str = "generic",
        timeout: float = 20.0,
        proxy: str | None = None,
        attempts: int = 3,
    ) -> None:
        self.url = url
        self.format = format if format in SUPPORTED_FORMATS else "generic"
        self.timeout = timeout
        self.proxy = proxy
        self.attempts = max(1, attempts)

    def _post(self, payload: dict) -> str:
        last_error = ""
        for attempt in range(self.attempts):
            try:
                with httpx.Client(proxy=self.proxy, timeout=self.timeout) as client:
                    response = client.post(self.url, json=payload)
                if response.status_code < 400:
                    return f"webhook:{response.status_code}"
                last_error = f"status {response.status_code}"
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt + 1 < self.attempts:
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"webhook delivery failed: {last_error}")

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
        text = render_digest(
            digest_date,
            items,
            kind=kind,
            theme_label=theme_label,
            theme_reason=theme_reason,
            theme_when=theme_when,
            theme_when_note=theme_when_note,
            preview=preview,
        )
        title = "Kestrel Radar 周报" if kind == "weekly" else "Kestrel Radar"
        return self._post(build_payload(self.format, text, title=title))

    def send_text(self, text: str) -> str:
        return self._post(build_payload(self.format, str(text)))

    def send_card(self, card: dict) -> str:
        return self.send_text(card_to_text(card))
