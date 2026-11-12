"""按渠道名构造通知器，CLI 与常驻服务共用同一份选择逻辑。

渠道分工：
- none           只生成写库，不投递（网页为主时的默认）
- fake           落 outbox Markdown，离线调试
- feishu         完整事件卡片（旧行为，仍保留）
- feishu-notice  飞书只发一条带网页链接的摘要，交互能力不变
- webhook        单向 Webhook
"""

from __future__ import annotations

from .adapters.notify.base import Notifier
from .adapters.notify.fake import FakeNotifier
from .adapters.notify.feishu import FeishuNotifier
from .adapters.notify.notice import NoticeNotifier
from .adapters.notify.null import NullNotifier
from .adapters.notify.webhook import WebhookNotifier
from .config import AppConfig

CHANNELS = ("none", "fake", "feishu", "feishu-notice", "webhook")


def _feishu(config: AppConfig) -> FeishuNotifier:
    if config.feishu is None:
        raise SystemExit(
            "飞书凭据缺失：请在 .env 设置 FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_USER_OPEN_ID"
        )
    return FeishuNotifier(config.feishu)


def build_notifier(config: AppConfig, channel: str) -> Notifier:
    if channel == "none":
        return NullNotifier()
    if channel == "fake":
        return FakeNotifier(config.outbox_dir)
    if channel == "feishu":
        return _feishu(config)
    if channel == "feishu-notice":
        return NoticeNotifier(_feishu(config), config.web_url)
    if channel == "webhook":
        if config.webhook is None:
            raise SystemExit(
                "Webhook 未配置：请在 .env 设置 AI_RADAR_WEBHOOK_URL（可选 AI_RADAR_WEBHOOK_FORMAT）"
            )
        return WebhookNotifier(
            config.webhook.url,
            format=config.webhook.format,
            proxy=config.webhook.proxy,
        )
    raise SystemExit(f"未知渠道：{channel}（可选 {', '.join(CHANNELS)}）")
