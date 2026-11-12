"""话题聚类的实体抽取。

目标不是做完整 NER，而是从标题里捞出「能代表一件事」的专名：
模型名（JEV / Qwen3 / Claude）、公司名（DeepSeek / 字节）、仓库名（owner/repo）。

规则：
1. 命中领域词表（中英文）→ 候选实体；
2. 含数字的拉丁词（GPT-5 / Qwen3 / k2.5）→ 候选实体；
3. 全大写缩写（JEV / GLM / VLM 除外）→ 候选实体；
4. 通用词（AI / Agent / RAG / 大模型）直接丢弃，否则会把所有文章粘成一个大话题；
5. 出处名（EMNLP / NeurIPS / arXiv 等会议期刊平台）不参与聚类：
   它们能被多篇共享，但那只是「同一个会」，不是「同一件事」。

词表来自领域包（config/domains/<id>/domain.yaml），本模块只实现规则。
最后再用「文档频次」过滤：在候选池里只出现 1 次的无法成题，
出现超过 12 次的太泛（如某公司名天天出现），都不作为话题键。
"""

from __future__ import annotations

import re
from collections import Counter

from ..domain.profile import DomainProfile, EntityVocab, default_profile

LATIN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[.\-_][A-Za-z0-9]+)*")
CJK_TOKEN = re.compile(r"[\u4e00-\u9fff]{2,8}")
# 「名称 + 版本号」是最稳的产品写法：Fable 5.2、Qwen3、Claude 5.5。
# 新模型名不用每次补词表，靠这条规则就能把名字摘出来（"5.2" 本身不是字母开头，抓不到）。
VERSIONED_NAME = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]+(?:-[A-Za-z]+)*)[\s\-]?v?\d+(?:\.\d+)?")
_VENUE_WITH_YEAR = re.compile(r"^([a-z][a-z\-]*?)(?:['’]?)(\d{2}|20\d{2})$")


def _vocab(profile: DomainProfile | None) -> EntityVocab:
    return (profile or default_profile()).entities


def _is_candidate(word: str, raw: str, vocab: EntityVocab) -> bool:
    lowered = word.lower()
    if lowered in vocab.generic or word in vocab.generic_cjk:
        return False
    if lowered in vocab.latin:
        return True
    if any(ch.isdigit() for ch in word) and len(word) >= 3:
        return True
    # 全大写缩写（长度 >= 3）通常是产品/项目名，但排除纯通用词
    return raw.isupper() and len(raw) >= 3


def is_venue(word: str, *, profile: DomainProfile | None = None) -> bool:
    """会议/期刊名（含「EMNLP2026」这种带年份写法）不是话题实体。"""
    venue = _vocab(profile).venue
    lowered = str(word or "").strip().lower()
    if lowered in venue:
        return True
    match = _VENUE_WITH_YEAR.match(lowered)
    return bool(match and match.group(1) in venue)


def extract(text: str, *, profile: DomainProfile | None = None) -> set[str]:
    """从标题中抽取候选实体，统一小写；中文词保持原样。"""
    vocab = _vocab(profile)
    found: set[str] = set()
    for match in LATIN_TOKEN.finditer(text or ""):
        raw = match.group(0)
        if len(raw) < 2:
            continue
        lowered = raw.lower()
        if lowered in vocab.latin or _is_candidate(lowered, raw, vocab):
            if not any(ch.isdigit() for ch in lowered) or len(lowered) >= 3:
                found.add(lowered)
    for name in vocab.cjk:
        if name in (text or ""):
            found.add(name)
    # 「名字 + 版本号」直接算产品名，不再要求命中词表或全大写。
    for match in VERSIONED_NAME.finditer(text or ""):
        name = match.group(1).lower()
        if len(name) >= 3:
            found.add(name)
    return {
        item
        for item in found
        if item not in vocab.generic
        and item not in vocab.generic_cjk
        and not is_venue(item, profile=profile)
    }


def filter_by_frequency(
    entities_per_doc: dict[int, set[str]], *, min_docs: int = 2, max_docs: int = 12
) -> dict[int, set[str]]:
    """去掉只出现一次（无法成题）和过于宽泛（遍地都是）的实体。"""
    counter: Counter[str] = Counter()
    for values in entities_per_doc.values():
        counter.update(values)
    kept = {
        entity
        for entity, count in counter.items()
        if min_docs <= count <= max_docs
    }
    return {
        document_id: {item for item in values if item in kept}
        for document_id, values in entities_per_doc.items()
    }
