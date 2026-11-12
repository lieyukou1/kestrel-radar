"""领域包的加载与校验。

约束：
- 领域数据只来自 YAML，不在代码里写任何领域词表；
- 加载即校验，坏配置在启动时就报错，不要带病跑一天；
- profile 是冻结对象，可安全地在多个线程/请求间共享。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

DEFAULT_DOMAIN = "ai"
DEFAULT_ACTION = "other"
# 仓库根目录：src/ai_radar/domain/profile.py → parents[3]
_REPO_ROOT = Path(__file__).resolve().parents[3]


class DomainError(ValueError):
    """领域包缺失或字段不合法。"""


@dataclass(frozen=True, slots=True)
class Taxonomy:
    """一个分类：展示名（也是入库的 tag 值）+ 一句话说明 + 关键词。"""

    name: str
    keywords: tuple[str, ...]
    description: str = ""


@dataclass(frozen=True, slots=True)
class EntityVocab:
    """实体词表：拉丁/中文专名、出处名、通用词。"""

    latin: frozenset[str]
    cjk: tuple[str, ...]
    venue: frozenset[str]
    generic: frozenset[str]
    generic_cjk: frozenset[str]


@dataclass(frozen=True, slots=True)
class ActionRule:
    """事件动作：顺序即优先级。"""

    name: str
    pattern: re.Pattern[str]


@dataclass(frozen=True, slots=True)
class DomainProfile:
    id: str
    name: str
    taxonomy: tuple[Taxonomy, ...]
    defaults: dict[str, str]
    entities: EntityVocab
    actions: tuple[ActionRule, ...]
    default_action: str = DEFAULT_ACTION
    # 与 config/weights.yaml 深合并，领域内的成熟窗口/节奏等可以单独覆盖
    weights_overrides: dict = field(default_factory=dict)
    # 领域语气：persona / 软文口径 / 偏题口径，供提示词模板替换
    prompts: dict = field(default_factory=dict)

    @property
    def tags(self) -> list[str]:
        return [item.name for item in self.taxonomy]

    @property
    def keywords(self) -> dict[str, tuple[str, ...]]:
        return {item.name: item.keywords for item in self.taxonomy}

    def action_of(self, text: str) -> str:
        for rule in self.actions:
            if rule.pattern.search(str(text or "")):
                return rule.name
        return self.default_action


def domain_path(root: Path, domain_id: str = DEFAULT_DOMAIN) -> Path:
    return Path(root) / "config" / "domains" / domain_id / "domain.yaml"


def _string_list(value, field: str, path: Path) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise DomainError(f"{path}: {field} 必须是非空列表")
    items = []
    for raw in value:
        text = str(raw or "").strip()
        if not text:
            raise DomainError(f"{path}: {field} 含空值")
        items.append(text)
    return tuple(items)


def _build_taxonomy(doc: dict, path: Path) -> tuple[Taxonomy, ...]:
    raw = doc.get("taxonomy")
    if not isinstance(raw, list) or not raw:
        raise DomainError(f"{path}: taxonomy 必须是非空列表")
    taxonomy: list[Taxonomy] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise DomainError(f"{path}: taxonomy 每项必须是对象")
        name = str(item.get("name") or "").strip()
        if not name:
            raise DomainError(f"{path}: taxonomy 缺少 name")
        if name in seen:
            raise DomainError(f"{path}: 分类名重复：{name}")
        seen.add(name)
        keywords = _string_list(item.get("keywords"), f"taxonomy[{name}].keywords", path)
        description = str(item.get("description") or "").strip()
        taxonomy.append(Taxonomy(name=name, keywords=keywords, description=description))
    return tuple(taxonomy)


def _build_defaults(doc: dict, taxonomy: tuple[Taxonomy, ...], path: Path) -> dict[str, str]:
    raw = doc.get("defaults") or {}
    if not isinstance(raw, dict):
        raise DomainError(f"{path}: defaults 必须是对象")
    names = {item.name for item in taxonomy}
    defaults: dict[str, str] = {}
    for kind, tag in raw.items():
        tag = str(tag or "").strip()
        if tag not in names:
            raise DomainError(f"{path}: defaults[{kind}] 指向不存在的分类：{tag}")
        defaults[str(kind)] = tag
    return defaults


def _build_entities(doc: dict, path: Path) -> EntityVocab:
    raw = doc.get("entities") or {}
    if not isinstance(raw, dict):
        raise DomainError(f"{path}: entities 必须是对象")

    def lower_set(key: str) -> frozenset[str]:
        values = raw.get(key) or []
        if not isinstance(values, list):
            raise DomainError(f"{path}: entities.{key} 必须是列表")
        return frozenset(str(value or "").strip().lower() for value in values if str(value).strip())

    cjk = raw.get("cjk") or []
    generic_cjk = raw.get("generic_cjk") or []
    if not isinstance(cjk, list) or not isinstance(generic_cjk, list):
        raise DomainError(f"{path}: entities.cjk / generic_cjk 必须是列表")
    return EntityVocab(
        latin=lower_set("latin"),
        cjk=tuple(str(value).strip() for value in cjk if str(value).strip()),
        venue=lower_set("venue"),
        generic=lower_set("generic"),
        generic_cjk=frozenset(
            str(value).strip() for value in generic_cjk if str(value).strip()
        ),
    )


def _build_actions(doc: dict, path: Path) -> tuple[ActionRule, ...]:
    raw = doc.get("actions") or []
    if not isinstance(raw, list):
        raise DomainError(f"{path}: actions 必须是列表")
    rules: list[ActionRule] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise DomainError(f"{path}: actions 每项必须是对象")
        name = str(item.get("name") or "").strip()
        pattern_text = str(item.get("pattern") or "")
        if not name or not pattern_text:
            raise DomainError(f"{path}: actions 每项需要 name 与 pattern")
        if name in seen:
            raise DomainError(f"{path}: 动作名重复：{name}")
        seen.add(name)
        flags = re.IGNORECASE if item.get("ignore_case") else 0
        try:
            pattern = re.compile(pattern_text, flags)
        except re.error as exc:
            raise DomainError(f"{path}: actions[{name}] 正则不合法：{exc}") from exc
        rules.append(ActionRule(name=name, pattern=pattern))
    return tuple(rules)


@lru_cache(maxsize=8)
def load_profile(root: Path, domain_id: str = DEFAULT_DOMAIN) -> DomainProfile:
    """读取并校验一个领域包；同一 (root, id) 只解析一次。"""
    path = domain_path(root, domain_id)
    if not path.exists():
        raise DomainError(f"缺少领域包：{path}")
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict):
        raise DomainError(f"{path}: 顶层必须是对象")
    taxonomy = _build_taxonomy(doc, path)
    overrides = doc.get("weights_overrides") or {}
    if not isinstance(overrides, dict):
        raise DomainError(f"{path}: weights_overrides 必须是对象")
    prompts = doc.get("prompts") or {}
    if not isinstance(prompts, dict):
        raise DomainError(f"{path}: prompts 必须是对象")
    return DomainProfile(
        id=str(doc.get("id") or domain_id).strip(),
        name=str(doc.get("name") or domain_id).strip(),
        taxonomy=taxonomy,
        defaults=_build_defaults(doc, taxonomy, path),
        entities=_build_entities(doc, path),
        actions=_build_actions(doc, path),
        default_action=str(doc.get("default_action") or DEFAULT_ACTION).strip(),
        weights_overrides=dict(overrides),
        prompts={str(key): str(value) for key, value in prompts.items()},
    )


@lru_cache(maxsize=1)
def default_profile() -> DomainProfile:
    """未显式传 profile 时的兜底：仓库自带的 AI 领域包。

    正式调用链（digest/collect）会传 `config.domain`；这里的兜底只为让纯函数
    （normalize 打标、单测）在不持有配置时仍按 AI 口径工作。
    """
    return load_profile(_REPO_ROOT, DEFAULT_DOMAIN)
