from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from ai_radar.adapters.notify.notice import NoticeNotifier
from ai_radar.adapters.notify.null import NullNotifier
from ai_radar.config import PROJECT_ROOT, AppConfig, FeishuConfig, load_config
from ai_radar.notify_factory import CHANNELS, build_notifier
from ai_radar.pipeline.digest import build_digest
from ai_radar.store import repo
from ai_radar.store.db import connect, init_db
from ai_radar.util import iso, utcnow


class RecordingNotifier:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.cards: list[dict] = []
        self.polled = False

    def send_digest(self, *args, **kwargs) -> str:
        return "digest-id"

    def send_text(self, text: str) -> str:
        self.texts.append(text)
        return "text-id"

    def send_card(self, card: dict) -> str:
        self.cards.append(card)
        return "card-id"

    def list_messages(self, chat_id: str) -> list[dict]:
        self.polled = True
        return [{"chat_id": chat_id}]


def _config(tmp_path: Path) -> AppConfig:
    base = load_config(PROJECT_ROOT)
    return AppConfig(
        root=tmp_path,
        network=base.network,
        sources=base.sources,
        weights=base.weights,
        db_path=tmp_path / "test.db",
        outbox_dir=tmp_path / "outbox",
        feishu=FeishuConfig(app_id="app", app_secret="secret", open_id="open"),
    )


def test_null_notifier_sends_nothing() -> None:
    notifier = NullNotifier()
    assert notifier.send_digest("2026-11-12", []) == ""
    assert notifier.send_text("hello") == ""
    assert notifier.send_card({"elements": []}) == ""


def test_notice_notifier_only_sends_text() -> None:
    inner = RecordingNotifier()
    notifier = NoticeNotifier(inner, "http://127.0.0.1:8765/")
    events = [SimpleNamespace(), SimpleNamespace()]
    items = [SimpleNamespace(), SimpleNamespace(), SimpleNamespace()]

    result = notifier.send_digest(
        "2026-11-12",
        items,
        kind="daily",
        theme_label="Gemini 4 Carbon",
        events=events,
    )

    assert result == "text-id"
    assert inner.cards == []
    message = inner.texts[0]
    assert "2026-11-12 日报已生成" in message
    assert "2 个事件 · 3 篇内容" in message
    assert "主线：Gemini 4 Carbon" in message
    assert "http://127.0.0.1:8765/" in message

    # 卡片交互仍委托给内层，通知模式不影响飞书其他能力。
    notifier.send_text("hello")
    notifier.send_card({"elements": []})
    assert inner.texts[-1] == "hello"
    assert inner.cards == [{"elements": []}]

    # 轮询兜底会调用协议之外的飞书方法，必须原样转发。
    assert notifier.list_messages("chat-1") == [{"chat_id": "chat-1"}]
    assert inner.polled is True


def test_build_notifier_channels(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert isinstance(build_notifier(config, "none"), NullNotifier)

    notice = build_notifier(config, "feishu-notice")
    assert isinstance(notice, NoticeNotifier)
    assert notice.web_url == "http://127.0.0.1:8765"
    assert set(CHANNELS) == {"none", "fake", "feishu", "feishu-notice", "webhook"}


def test_web_url_env_override(monkeypatch) -> None:
    monkeypatch.setenv("AI_RADAR_WEB_URL", "http://192.168.1.9:9000/")
    config = load_config(PROJECT_ROOT)
    assert config.web_url == "http://192.168.1.9:9000"


def test_digest_with_none_channel_writes_no_outbox(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.llm = None
    conn = connect(config.db_path)
    init_db(conn)
    document_id = repo.insert_document(
        conn,
        canonical_id="test:none-channel",
        kind="blog",
        title="NVIDIA 发布新芯片",
        abstract="abstract",
        url="https://example.com/none-channel",
        published_at=iso(utcnow() - timedelta(days=3)),
        authority="A",
        primary_tag="1 模型与架构",
        tags=["1 模型与架构"],
    )
    repo.mark_status(conn, [document_id], "observing")

    result = build_digest(
        conn,
        config,
        notifier=NullNotifier(),
        digest_date="2026-10-21",
    )

    assert result["status"] == "sent"
    assert result["count"] == 1
    assert result["outbox"] == ""
    assert not (tmp_path / "outbox").exists()
    assert repo.get_document(conn, document_id)["status"] == "pushed"


def test_cli_channel_choices() -> None:
    from ai_radar.__main__ import build_parser

    parser = build_parser()
    assert parser.parse_args(["digest", "--channel", "none"]).channel == "none"
    assert parser.parse_args(["serve"]).channel == "feishu-notice"
