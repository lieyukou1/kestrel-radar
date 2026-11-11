from __future__ import annotations

import json
import time

import httpx

from ....config import FeishuConfig
from ..base import DigestItemView, EventView
from .cards import build_digest_card, build_event_overview_card


class FeishuError(RuntimeError):
    pass


class FeishuNotifier:
    """飞书自建应用：HTTP 发送 + 长连接接收（接收在 jobs/serve.py）。"""

    def __init__(self, config: FeishuConfig, *, timeout: float = 20.0) -> None:
        self.config = config
        self.timeout = timeout
        self._token: str | None = None
        self._token_deadline = 0.0

    def _tenant_token(self) -> str:
        if self._token and time.monotonic() < self._token_deadline:
            return self._token

        url = f"{self.config.base_url}/open-apis/auth/v3/tenant_access_token/internal"
        response = httpx.post(
            url,
            json={"app_id": self.config.app_id, "app_secret": self.config.app_secret},
            timeout=self.timeout,
        )
        data = response.json()
        if response.status_code >= 400 or data.get("code") != 0:
            raise FeishuError(f"tenant_access_token failed: {data}")
        self._token = data["tenant_access_token"]
        # 提前 60 秒过期，避免临界失效。
        self._token_deadline = time.monotonic() + int(data.get("expire", 7200)) - 60
        return self._token

    def _post_message(self, msg_type: str, content: dict, receive_id: str | None = None) -> dict:
        url = (
            f"{self.config.base_url}/open-apis/im/v1/messages"
            "?receive_id_type=open_id"
        )
        response = httpx.post(
            url,
            headers={"Authorization": f"Bearer {self._tenant_token()}"},
            json={
                "receive_id": receive_id or self.config.open_id,
                "msg_type": msg_type,
                "content": json.dumps(content, ensure_ascii=False),
            },
            timeout=self.timeout,
        )
        data = response.json()
        if response.status_code >= 400 or data.get("code") != 0:
            raise FeishuError(f"send message failed: {data}")
        return data.get("data") or {}

    def _send(self, msg_type: str, content: dict, receive_id: str | None = None) -> str:
        return str(self._post_message(msg_type, content, receive_id).get("message_id", ""))

    def send_text_tracked(self, text: str) -> tuple[str, str]:
        """返回 (message_id, chat_id)：首次发送时顺带拿到单聊会话 id。"""
        data = self._post_message("text", {"text": text})
        return str(data.get("message_id", "")), str(data.get("chat_id", ""))

    def list_messages(self, chat_id: str, *, page_size: int = 20) -> list[dict]:
        """读取会话历史，按时间倒序。长连接收不到事件时用它兜底收消息。"""
        response = httpx.get(
            f"{self.config.base_url}/open-apis/im/v1/messages",
            headers={"Authorization": f"Bearer {self._tenant_token()}"},
            params={
                "container_id_type": "chat",
                "container_id": chat_id,
                "sort_type": "ByCreateTimeDesc",
                "page_size": page_size,
            },
            timeout=self.timeout,
        )
        data = response.json()
        if response.status_code >= 400 or data.get("code") != 0:
            raise FeishuError(f"list messages failed: {data}")
        return (data.get("data") or {}).get("items") or []

    def send_text(self, text: str) -> str:
        return self._send("text", {"text": text})

    def send_text_to(self, receive_id: str, text: str) -> str:
        return self._send("text", {"text": text}, receive_id=receive_id)

    def bot_info(self) -> dict:
        response = httpx.get(
            f"{self.config.base_url}/open-apis/bot/v3/info",
            headers={"Authorization": f"Bearer {self._tenant_token()}"},
            timeout=self.timeout,
        )
        return response.json()

    def send_card(self, card: dict) -> str:
        return self._send("interactive", card)

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
        if events:
            return self.send_card(
                build_event_overview_card(digest_date, events, preview=preview)
            )
        return self.send_card(
            build_digest_card(
                digest_date,
                items,
                kind=kind,
                theme_label=theme_label,
                theme_reason=theme_reason,
                theme_query=theme_query,
                theme_when=theme_when,
                theme_when_note=theme_when_note,
                preview=preview,
            )
        )
