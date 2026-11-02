"""标题级去重时的实体互斥判定。

去重靠的是「标题像不像」，但像不代表是同一件事：
「Gemma 4 发布」和「Gemma 4 被越狱」讲的是同一个产品的两件事；
「GPT-5」和「GPT-5.1」也是两个版本。这类情况不该被合并成一条。

规则刻意保守——只有**双方都能抽出产品名**时才可能否决：
抽不到（中文标题里常见的泛指、仓库名、论文标题）就退回原来的相似度判断，
宁可漏拦，也不误伤跨来源转载。
"""

from __future__ import annotations

import re

# 产品 → 厂商。只用于判断「两个标题说的是不是同一家/同一系列」。
PRODUCT_VENDOR = {
    "gemma": "google",
    "gemini": "google",
    "gpt": "openai",
    "chatgpt": "openai",
    "claude": "anthropic",
    "llama": "meta",
    "qwen": "alibaba",
    "通义": "alibaba",
    "千问": "alibaba",
    "deepseek": "deepseek",
    "深度求索": "deepseek",
    "kimi": "moonshot",
    "月之暗面": "moonshot",
    "doubao": "bytedance",
    "豆包": "bytedance",
    "ernie": "baidu",
    "文心": "baidu",
    "glm": "zhipu",
    "智谱": "zhipu",
    "grok": "xai",
    "mistral": "mistral",
    "phi": "microsoft",
    "hunyuan": "tencent",
    "混元": "tencent",
    "minimax": "minimax",
    "海螺": "minimax",
}

_LATIN_PRODUCTS = "|".join(
    sorted((name for name in PRODUCT_VENDOR if name.isascii()), key=len, reverse=True)
)
_CJK_PRODUCTS = "|".join(
    sorted((name for name in PRODUCT_VENDOR if not name.isascii()), key=len, reverse=True)
)

PRODUCT_RE = re.compile(
    # 拉丁产品名不能嵌在单词里（phi 不能命中 philosophy），但要允许紧跟版本号（Qwen3）。
    # 中文没有空格，不能加 \b。
    rf"(?:(?<![A-Za-z])(?P<latin>(?:{_LATIN_PRODUCTS}))(?![A-Za-z])|(?P<cjk>{_CJK_PRODUCTS}))"
    rf"(?:[\s\-_]?v?(?P<version>\d+(?:\.\d+)?))?",
    re.IGNORECASE,
)

RELEASE_HINTS = (
    "发布", "上线", "开源", "推出", "开放", "正式发布", "公测",
    "release", "released", "launch", "launched", "unveil", "open-source",
)
INCIDENT_HINTS = (
    "越狱", "破解", "漏洞", "事故", "泄露", "宕机", "故障", "封禁",
    "jailbreak", "jailbroken", "exploit", "vulnerability", "breach", "outage",
)


def _is_year(value: str) -> bool:
    return bool(re.fullmatch(r"(?:19|20)\d{2}", value))


def signature(title: str) -> tuple[set[str], set[tuple[str, str]]]:
    """抽出标题里的厂商集合与「产品 + 版本」集合；版本可能为空串。"""
    text = str(title or "")
    vendors: set[str] = set()
    products: set[tuple[str, str]] = set()
    for match in PRODUCT_RE.finditer(text):
        name = (match.group("cjk") or match.group("latin") or "").lower()
        if not name:
            continue
        version = match.group("version") or ""
        if version and _is_year(version):
            version = ""
        vendors.add(PRODUCT_VENDOR.get(name, name))
        products.add((name, version))
    return vendors, products


def _event_kinds(text: str) -> set[str]:
    lowered = str(text or "").lower()
    kinds: set[str] = set()
    if any(word in lowered for word in RELEASE_HINTS):
        kinds.add("release")
    if any(word in lowered for word in INCIDENT_HINTS):
        kinds.add("incident")
    return kinds


def titles_conflict(left: str, right: str) -> bool:
    """两个标题是否在实体层面互斥（不该合并）。任一方抽不到产品就返回 False。"""
    _, left_products = signature(left)
    _, right_products = signature(right)
    if not left_products or not right_products:
        return False

    left_names = {name for name, _ in left_products}
    right_names = {name for name, _ in right_products}
    if left_names.isdisjoint(right_names):
        return True

    common = left_names & right_names
    if len(common) == 1:
        name = next(iter(common))
        left_versions = {version for product, version in left_products if product == name and version}
        right_versions = {
            version for product, version in right_products if product == name and version
        }
        # 两边都明确写了版本且没有交集，才当成两代产品。
        if left_versions and right_versions and left_versions.isdisjoint(right_versions):
            return True

    left_events = _event_kinds(left)
    right_events = _event_kinds(right)
    # 一边只有发布类、另一边只有事故类，才是两件事；同时命中两类词视为描述不清，放行。
    return bool(left_events and right_events and left_events.isdisjoint(right_events))
