from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from ai_radar.adapters.notify.base import DigestItemView
from ai_radar.adapters.notify.fake import FakeNotifier
from ai_radar.adapters.notify.markdown import card_to_text, render_digest
from ai_radar.adapters.notify.webhook import WebhookNotifier, build_payload
from ai_radar.adapters.url_safety import is_public_http_url


def item() -> DigestItemView:
    return DigestItemView(
        rank=1,
        document_id=1,
        title="A story",
        url="https://example.com/a",
        tag="1 模型与架构",
        source="blog",
        reason="理由",
        score=0.8,
        discussion_url="https://lobste.rs/s/abc",
        discussion_label="Lobsters",
    )


def test_render_digest_contains_items_and_discussion() -> None:
    text = render_digest("2026-11-02", [item()])
    assert "A story" in text
    assert "社区讨论（Lobsters）" in text
    assert "https://example.com/a" in text


def test_card_to_text_skips_buttons() -> None:
    card = {
        "header": {"title": {"tag": "plain_text", "content": "标题"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "正文"}},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "详情"},
                    }
                ],
            },
        ],
    }
    text = card_to_text(card)
    assert "标题" in text and "正文" in text
    assert "详情" not in text


def test_build_payload_formats() -> None:
    assert build_payload("slack", "hi") == {"text": "hi"}
    assert build_payload("discord", "hi") == {"content": "hi"}
    assert build_payload("dingtalk", "hi")["msgtype"] == "markdown"
    assert build_payload("feishu", "hi")["msg_type"] == "text"
    assert build_payload("generic", "hi") == {
        "title": "Kestrel Radar",
        "text": "hi",
        "markdown": "hi",
    }


def test_webhook_posts_and_returns_status(monkeypatch) -> None:
    captured: dict = {}

    class FakeResponse:
        status_code = 200

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

        def post(self, url, json=None):
            captured.update(url=url, payload=json)
            return FakeResponse()

    monkeypatch.setattr("ai_radar.adapters.notify.webhook.httpx.Client", FakeClient)
    notifier = WebhookNotifier("https://hook.example/x", format="slack")
    assert notifier.send_text("hello") == "webhook:200"
    assert captured == {"url": "https://hook.example/x", "payload": {"text": "hello"}}


def test_webhook_raises_after_retries(monkeypatch) -> None:
    class FailingClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

        def post(self, url, json=None):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr("ai_radar.adapters.notify.webhook.httpx.Client", FailingClient)
    monkeypatch.setattr("ai_radar.adapters.notify.webhook.time.sleep", lambda *_: None)
    notifier = WebhookNotifier("https://hook.example/x", attempts=2)
    with pytest.raises(RuntimeError):
        notifier.send_text("hello")


def test_fake_notifier_writes_text_and_card(tmp_path: Path) -> None:
    notifier = FakeNotifier(tmp_path)
    path = notifier.send_text("hello")
    assert Path(path).read_text(encoding="utf-8") == "hello"
    card = {"elements": [{"tag": "div", "text": {"content": "卡片正文"}}]}
    assert Path(notifier.send_card(card)).read_text(encoding="utf-8") == "卡片正文"


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://example.com/post", True),
        ("http://arxiv.org/abs/2401.00001", True),
        ("http://localhost:8080/x", False),
        ("http://127.0.0.1/x", False),
        ("http://10.0.0.5/x", False),
        ("http://192.168.1.2/x", False),
        ("http://169.254.169.254/latest/meta-data", False),
        ("http://[::1]/x", False),
        ("file:///etc/passwd", False),
        ("ftp://example.com/x", False),
        ("javascript:alert(1)", False),
    ],
)
def test_is_public_http_url(url: str, allowed: bool) -> None:
    assert is_public_http_url(url) is allowed
