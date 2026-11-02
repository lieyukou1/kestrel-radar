"""飞书会话轮询：长连接收不到消息事件时，用会话历史兜底。

只处理用户发的文本消息；机器人自己发的、系统消息一律跳过。
轮询游标只在内存里，启动前的历史消息不会重放。
"""

from __future__ import annotations

import json
import os
import threading
import time

from ..config import AppConfig
from ..store import repo
from ..store.db import connect, init_db

CHAT_KEY = "feishu:chat_id"
POLL_INTERVAL = 5.0
PAGE_SIZE = 20
BOOTSTRAP_TEXT = "已切换到会话轮询模式：现在发消息会正常回复。"


class MessagePoller:
    def __init__(self, config: AppConfig, notifier, handler) -> None:
        self.config = config
        self.notifier = notifier
        self.handler = handler
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._conn = None
        self._chat_id = ""
        self._cursor_ms = 0
        self._seen: set[str] = set()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="ai-radar-poll", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _ensure_chat_id(self) -> str:
        chat_id = repo.get_text_preference(self._conn, CHAT_KEY)
        if chat_id:
            return chat_id
        # 第一次没有会话 id：发一条消息，从响应里拿到单聊会话 id 并落库。
        _, chat_id = self.notifier.send_text_tracked(BOOTSTRAP_TEXT)
        if chat_id:
            repo.set_text_preference(self._conn, CHAT_KEY, chat_id)
        return chat_id

    def _run(self) -> None:
        self._conn = connect(self.config.db_path)
        try:
            init_db(self._conn)
            try:
                self._chat_id = self._ensure_chat_id()
            except Exception as exc:
                print(f"[poll] bootstrap failed: {type(exc).__name__}: {exc}", flush=True)
                return
            if not self._chat_id:
                print("[poll] disabled: no chat id", flush=True)
                return
            self._cursor_ms = _start_cursor()
            print(f"[poll] watching {self._chat_id}", flush=True)

            while not self._stop.is_set():
                try:
                    self._consume(self.notifier.list_messages(self._chat_id, page_size=PAGE_SIZE))
                except Exception as exc:
                    print(f"[poll] poll failed: {type(exc).__name__}: {exc}", flush=True)
                self._stop.wait(POLL_INTERVAL)
        finally:
            # 必须在建连的线程里关：在 stop() 里关会变成跨线程使用，直接抛 ProgrammingError。
            self._conn.close()

    def _consume(self, items: list[dict]) -> None:
        fresh: list[tuple[int, str, dict]] = []
        for item in items:
            message_id = str(item.get("message_id") or "")
            if not message_id or message_id in self._seen:
                continue
            if item.get("deleted"):
                continue
            created = int(item.get("create_time") or 0)
            if created <= self._cursor_ms:
                continue
            if (item.get("sender") or {}).get("sender_type") != "user":
                continue
            if item.get("msg_type") != "text":
                continue
            fresh.append((created, message_id, item))

        for created, message_id, item in sorted(fresh, key=lambda row: row[0]):
            self._seen.add(message_id)
            self._cursor_ms = max(self._cursor_ms, created)
            text = _text_of(item)
            if not text:
                continue
            print(f"[poll] recv {text[:60]}", flush=True)
            try:
                self.handler(text, message_id)
            except Exception as exc:
                print(f"[poll] handler failed: {type(exc).__name__}: {exc}", flush=True)


def _text_of(item: dict) -> str:
    body = (item.get("body") or {}).get("content") or ""
    try:
        return str(json.loads(body).get("text") or "").strip()
    except (json.JSONDecodeError, TypeError):
        return ""


def _start_cursor() -> int:
    """默认从现在开始；AI_RADAR_POLL_SINCE_MS 用于排障时回放历史消息。"""
    override = os.getenv("AI_RADAR_POLL_SINCE_MS", "").strip()
    if override.isdigit():
        return int(override)
    return int(time.time() * 1000)
