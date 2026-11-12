from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .domain.profile import (
    DEFAULT_DOMAIN,
    DomainProfile,
    default_profile,
    domain_path,
    load_profile,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

SUPPORTED_SOURCE_KINDS = ("arxiv", "hackernews", "hf_daily", "github", "rss", "lobsters")
VALID_AUTHORITY = ("S", "A", "B", "C")
VALID_REGION = ("domestic", "overseas")
# overseas-only：只有 region=overseas 的来源走代理；direct：全部直连
VALID_NETWORK_MODES = ("overseas-only", "direct")


class ConfigError(ValueError):
    """配置有误时尽早失败，并把所有问题一次列出来。"""


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
    # 领域包：分类/实体/动作词与权重覆盖都从这里来；手工构造 AppConfig 时可为 None
    domain: DomainProfile | None = None
    # 网页地址：飞书通知里拼「打开网页」，部署到别的端口时用 AI_RADAR_WEB_URL 覆盖
    web_url: str = "http://127.0.0.1:8765"

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


def _validate_sources(items: list[dict], network_doc: dict) -> None:
    """一次列出所有配置问题，避免拼错字段后静默少采集。"""
    problems: list[str] = []
    mode = str(network_doc.get("mode", "overseas-only"))
    if mode not in VALID_NETWORK_MODES:
        problems.append(
            f"network.mode={mode!r} 不支持，可选：{', '.join(VALID_NETWORK_MODES)}"
        )

    seen_sources: set[str] = set()
    for index, item in enumerate(items, start=1):
        source_id = str(item.get("id") or "").strip()
        kind = str(item.get("kind") or "").strip()
        label = source_id or f"第 {index} 个来源"
        if not source_id:
            problems.append(f"{label}: 缺少 id")
            continue
        if source_id in seen_sources:
            problems.append(f"{label}: id 重复")
        seen_sources.add(source_id)

        if kind not in SUPPORTED_SOURCE_KINDS:
            problems.append(
                f"{label}: kind={kind!r} 不支持，可选：{', '.join(SUPPORTED_SOURCE_KINDS)}"
            )
        authority = str(item.get("authority", "B")).upper()
        if authority not in VALID_AUTHORITY:
            problems.append(
                f"{label}: authority={authority!r} 不支持，可选：{', '.join(VALID_AUTHORITY)}"
            )
        region = str(item.get("region", "domestic"))
        if region not in VALID_REGION:
            problems.append(
                f"{label}: region={region!r} 不支持，可选：{', '.join(VALID_REGION)}"
            )

        if kind != "rss":
            continue
        feeds = item.get("feeds") or []
        if not isinstance(feeds, list) or not feeds:
            problems.append(f"{label}: rss 源必须配置非空 feeds")
            continue
        seen_feeds: set[str] = set()
        for feed in feeds:
            feed = feed if isinstance(feed, dict) else {}
            feed_id = str(feed.get("id") or "").strip()
            url = str(feed.get("url") or "").strip()
            feed_label = f"{label}.{feed_id or '?'}"
            if not feed_id:
                problems.append(f"{feed_label}: 缺少 id")
            elif feed_id in seen_feeds:
                problems.append(f"{feed_label}: feed id 重复")
            seen_feeds.add(feed_id)
            if not url.startswith(("http://", "https://")):
                problems.append(f"{feed_label}: url 必须是 http(s)，当前为 {url!r}")

    if problems:
        raise ConfigError("配置有问题：\n- " + "\n- ".join(problems))


def _merge_weights(base: dict, override: dict) -> dict:
    """领域包的 weights_overrides 与全局 weights 深合并（领域优先）。"""
    merged = dict(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_weights(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(root: Path | None = None, *, domain: str | None = None) -> AppConfig:
    root = Path(root) if root else PROJECT_ROOT
    load_dotenv(root / ".env")

    domain_id = (domain or os.getenv("AI_RADAR_DOMAIN") or DEFAULT_DOMAIN).strip()
    # `--root` 指向的精简目录可能只带三个 yaml：默认领域缺失时退回仓库自带的 AI 包；
    # 显式指定其他领域则必须存在，避免拼错域名后静默跑错口径。
    if domain_id == DEFAULT_DOMAIN and not domain_path(root, domain_id).exists():
        profile = default_profile()
    else:
        profile = load_profile(root, domain_id)

    pack_dir = root / "config" / "domains" / domain_id
    # 领域包自带的源文件优先；精简 root（只带三个 yaml）回退到根目录。
    sources_path = pack_dir / "sources.yaml"
    local_sources_path = pack_dir / "sources.local.yaml"
    if not sources_path.exists():
        sources_path = root / "config" / "sources.yaml"
    if not local_sources_path.exists():
        local_sources_path = root / "config" / "sources.local.yaml"
    weights_path = root / "config" / "weights.yaml"
    models_path = root / "config" / "models.yaml"

    sources_doc = yaml.safe_load(sources_path.read_text(encoding="utf-8")) or {}
    sources_items = sources_doc.get("sources") or []
    if not isinstance(sources_items, list):
        raise ConfigError("config/sources.yaml 的 sources 必须是列表")
    if local_sources_path.exists():
        local_doc = yaml.safe_load(local_sources_path.read_text(encoding="utf-8")) or {}
        local_items = local_doc.get("sources") or []
        if not isinstance(local_items, list):
            raise ConfigError("config/sources.local.yaml 的 sources 必须是列表")
        sources_items = _merge_sources(sources_items, local_items)
    weights = yaml.safe_load(weights_path.read_text(encoding="utf-8")) or {}
    weights = _merge_weights(weights, profile.weights_overrides)
    models_doc = yaml.safe_load(models_path.read_text(encoding="utf-8")) or {}

    network_doc = sources_doc.get("network") or {}
    _validate_sources(sources_items, network_doc)
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

    data_dir = root / "data" / "domains" / domain_id
    web_url = (
        os.getenv("AI_RADAR_WEB_URL") or "http://127.0.0.1:8765"
    ).strip().rstrip("/")
    return AppConfig(
        root=root,
        network=network,
        sources=sources,
        weights=weights,
        db_path=data_dir / "ai_radar.db",
        outbox_dir=data_dir / "outbox",
        feishu=feishu,
        llm=llm,
        webhook=webhook,
        domain=profile,
        web_url=web_url,
    )
