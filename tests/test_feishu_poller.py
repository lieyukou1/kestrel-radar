from __future__ import annotations

import json

from ai_radar.jobs.feishu_poller import MessagePoller
from ai_radar.store.db import connect, init_db


class FakeNotifier:
    def __init__(self, items: list[dict]) -> None:
        self.items = items

    def list_messages(self, chat_id: str, *, page_size: int = 20) -> list[dict]:
        return self.items

    def send_text_tracked(self, text: str) -> tuple[str, str]:
        return "om_boot", "oc_test"


def item(message_id: str, text: str, created: int, *, sender: str = "user", **extra) -> dict:
    return {
        "message_id": message_id,
        "msg_type": "text",
        "create_time": str(created),
        "sender": {"sender_type": sender},
        "body": {"content": json.dumps({"text": text}, ensure_ascii=False)},
        **extra,
    }


def make_poller(tmp_path, items: list[dict]) -> tuple[MessagePoller, list[str]]:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    handled: list[str] = []
    poller = MessagePoller(None, FakeNotifier(items), lambda text, mid: handled.append(text))
    poller._conn = conn
    poller._chat_id = "oc_test"
    poller._cursor_ms = 1000
    return poller, handled


def test_consume_only_handles_new_user_text(tmp_path) -> None:
    items = [
        item("om_2", "在吗", 1200),
        item("om_1", "旧的", 900),
        item("om_3", "机器人的话", 1300, sender="app"),
        {"message_id": "om_4", "msg_type": "image", "create_time": "1400", "sender": {"sender_type": "user"}},
        item("om_5", "被撤回", 1500, deleted=True, body={"content": "This message was recalled"}),
    ]
    poller, handled = make_poller(tmp_path, items)
    poller._consume(items)
    assert handled == ["在吗"]
    assert poller._cursor_ms == 1200


def test_consume_dedupes_same_message(tmp_path) -> None:
    items = [item("om_7", "你好", 1200)]
    poller, handled = make_poller(tmp_path, items)
    poller._consume(items)
    poller._consume(items)
    assert handled == ["你好"]


def test_consume_orders_by_create_time(tmp_path) -> None:
    items = [item("om_b", "第二条", 2200), item("om_a", "第一条", 2100)]
    poller, handled = make_poller(tmp_path, items)
    poller._consume(items)
    assert handled == ["第一条", "第二条"]
