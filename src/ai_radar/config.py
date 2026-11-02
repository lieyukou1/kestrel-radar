from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(slots=True)
class NetworkConfig:
    proxy: str | None
    mode: str = "overseas-only"
    timeout_seconds: float = 30.0


@dataclass(slots=True)
class SourceConfig:
    id: str
    kind: str
    name: str
    authority: str
    region: str
    enabled: bool = True
    options: dict = field(default_factory=dict)


@dataclass(slots=True)
class FeishuConfig:
    app_id: str
    app_secret: str
    open_id: str
    base_url: str = "https://open.feishu.cn"


@dataclass(slots=True)
class WebhookConfig:
    url: str
    format: str = "generic"
    proxy: str | None = None


@dataclass(slots=True)
class LLMConfig:
    api_key: str
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    timeout: float = 120.0
    daily_budget_cny: float = 5.0
    input_cny_per_million: float = 1.0
    output_cny_per_million: float = 4.0


@dataclass(slots=True)
class AppConfig:
    root: Path
    network: NetworkConfig
    sources: list[SourceConfig]
    weights: dict
    db_path: Path
    outbox_dir: Path
    feishu: FeishuConfig | None = None
    llm: LLMConfig | None = None
    webhook: WebhookConfig | None = None

    def proxy_for(self, source: SourceConfig) -> str | None:
        override = source.options.get("proxy")
        if override == "none":
            return None
        if isinstance(override, str) and override:
            return override
        if self.network.mode == "overseas-only" and source.region == "overseas":
            return self.network.proxy
        return None


def _merge_sources(base: list[dict], extra: list[dict]) -> list[dict]:
    """把 config/sources.local.yaml 的源并进公开配置：同 id 追加 feeds，其余字段本地覆盖。"""
    merged = [dict(item) for item in base]
    index = {str(item.get("id")): i for i, item in enumerate(merged)}
    for item in extra:
        source_id = str(item.get("id"))
        if source_id not in index:
            index[source_id] = len(merged)
            merged.append(dict(item))
            continue
        target = merged[index[source_id]]
        for key, value in item.items():
            if key == "feeds" and isinstance(value, list):
                feeds = [dict(feed) for feed in target.get("feeds") or []]
                positions = {str(feed.get("id")): i for i, feed in enumerate(feeds)}
                for feed in value:
                    feed_id = str(feed.get("id"))
                    if feed_id in positions:
                        feeds[positions[feed_id]] = {**feeds[positions[feed_id]], **feed}
                    else:
                        positions[feed_id] = len(feeds)
                        feeds.append(dict(feed))
                target["feeds"] = feeds
            else:
                target[key] = value
    return merged


def load_config(root: Path | None = None) -> AppConfig:
    root = Path(root) if root else PROJECT_ROOT
    load_dotenv(root / ".env")

    sources_path = root / "config" / "sources.yaml"
    local_sources_path = root / "config" / "sources.local.yaml"
    weights_path = root / "config" / "weights.yaml"
    models_path = root / "config" / "models.yaml"

    sources_doc = yaml.safe_load(sources_path.read_text(encoding="utf-8")) or {}
    sources_items = sources_doc.get("sources") or []
    if local_sources_path.exists():
        local_doc = yaml.safe_load(local_sources_path.read_text(encoding="utf-8")) or {}
        sources_items = _merge_sources(sources_items, local_doc.get("sources") or [])
    weights = yaml.safe_load(weights_path.read_text(encoding="utf-8")) or {}
    models_doc = yaml.safe_load(models_path.read_text(encoding="utf-8")) or {}

    network_doc = sources_doc.get("network") or {}
    env_proxy = os.getenv("AI_RADAR_PROXY")
    if env_proxy is not None:
        proxy = None if env_proxy.strip().lower() in ("", "none", "off") else env_proxy.strip()
    else:
        proxy = network_doc.get("proxy") or None
    network = NetworkConfig(
        proxy=proxy,
        mode=network_doc.get("mode", "overseas-only"),
        timeout_seconds=float(network_doc.get("timeout_seconds", 30)),
    )

    sources: list[SourceConfig] = []
    for item in sources_items:
        sources.append(
            SourceConfig(
                id=str(item["id"]),
                kind=str(item["kind"]),
                name=str(item.get("name", item["id"])),
                authority=str(item.get("authority", "B")),
                region=str(item.get("region", "domestic")),
                enabled=bool(item.get("enabled", True)),
                options=dict(item),
            )
        )

    app_id = (os.getenv("FEISHU_APP_ID") or "").strip()
    app_secret = (os.getenv("FEISHU_APP_SECRET") or "").strip()
    open_id = (os.getenv("FEISHU_USER_OPEN_ID") or "").strip()
    feishu = (
        FeishuConfig(
            app_id=app_id,
            app_secret=app_secret,
            open_id=open_id,
            base_url=(os.getenv("FEISHU_BASE_URL") or "https://open.feishu.cn").rstrip("/"),
        )
        if app_id and app_secret and open_id
        else None
    )

    webhook_url = (os.getenv("AI_RADAR_WEBHOOK_URL") or "").strip()
    webhook = (
        WebhookConfig(
            url=webhook_url,
            format=(os.getenv("AI_RADAR_WEBHOOK_FORMAT") or "generic").strip().lower(),
            proxy=(os.getenv("AI_RADAR_WEBHOOK_PROXY") or "").strip() or None,
        )
        if webhook_url
        else None
    )

    api_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    model = str((models_doc.get("models") or {}).get("default", "deepseek-flash"))
    pricing = (models_doc.get("pricing_cny_per_million") or {}).get(model) or {}
    budget = models_doc.get("budget") or {}
    llm = (
        LLMConfig(
            api_key=api_key,
            base_url=(os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/"),
            model=model,
            daily_budget_cny=float(budget.get("daily_cny", 5.0)),
            input_cny_per_million=float(pricing.get("input", 1.0)),
            output_cny_per_million=float(pricing.get("output", 4.0)),
        )
        if api_key
        else None
    )

    return AppConfig(
        root=root,
        network=network,
        sources=sources,
        weights=weights,
        db_path=root / "data" / "ai_radar.db",
        outbox_dir=root / "data" / "outbox",
        feishu=feishu,
        llm=llm,
        webhook=webhook,
    )
