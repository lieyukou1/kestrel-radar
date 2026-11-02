"""按渠道名构造通知器，CLI 与常驻服务共用同一份选择逻辑。"""

from __future__ import annotations

from .adapters.notify.base import Notifier
from .adapters.notify.fake import FakeNotifier
from .adapters.notify.feishu import FeishuNotifier
from .adapters.notify.webhook import WebhookNotifier
from .config import AppConfig

CHANNELS = ("fake", "feishu", "webhook")


def build_notifier(config: AppConfig, channel: str) -> Notifier:
    if channel == "feishu":
        if config.feishu is None:
            raise SystemExit(
                "飞书凭据缺失：请在 .env 设置 FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_USER_OPEN_ID"
            )
        return FeishuNotifier(config.feishu)
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
    if channel != "fake":
        raise SystemExit(f"未知渠道：{channel}（可选 {', '.join(CHANNELS)}）")
    return FakeNotifier(config.outbox_dir)
