"""打标：分类体系与关键词来自领域包，代码只负责匹配。"""

from __future__ import annotations

from ..adapters.sources.base import RawItem
from ..domain.profile import DomainProfile, default_profile

# 兼容旧导入（intent / rotation / 测试）：数据本身在 config/domains/ai/domain.yaml。
# P1 起调用方改为从 AppConfig.domain 取，不再读这几个模块级别名。
TAGS = default_profile().tags
KEYWORDS = default_profile().keywords
DEFAULT_TAG = default_profile().defaults


def classify(
    item: RawItem, *, profile: DomainProfile | None = None
) -> tuple[str, list[str]]:
    profile = profile or default_profile()
    text = f"{item.title} {item.summary or ''}".lower()
    scores: dict[str, int] = {}
    for tag, words in profile.keywords.items():
        scores[tag] = sum(text.count(word) for word in words)

    ranked = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
    primary = (
        ranked[0][0]
        if ranked and ranked[0][1] > 0
        else profile.defaults.get(item.kind, profile.tags[0])
    )
    tags = [tag for tag, score in ranked if score > 0][:3]
    if primary not in tags:
        tags.insert(0, primary)
    return primary, tags[:3]
