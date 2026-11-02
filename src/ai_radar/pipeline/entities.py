"""话题聚类的实体抽取。

目标不是做完整 NER，而是从标题里捞出「能代表一件事」的专名：
模型名（JEV / Qwen3 / Claude）、公司名（DeepSeek / 字节）、仓库名（owner/repo）。

规则：
1. 命中词表（中英文）→ 候选实体；
2. 含数字的拉丁词（GPT-5 / Qwen3 / k2.5）→ 候选实体；
3. 全大写缩写（JEV / GLM / VLM 除外）→ 候选实体；
4. 通用词（AI / Agent / RAG / 大模型）直接丢弃，否则会把所有文章粘成一个大话题；
5. 出处名（EMNLP / NeurIPS / arXiv 等会议期刊平台）不参与聚类：
   它们能被多篇共享，但那只是「同一个会」，不是「同一件事」。

最后再用「文档频次」过滤：在候选池里只出现 1 次的无法成题，
出现超过 12 次的太泛（如某公司名天天出现），都不作为话题键。
"""

from __future__ import annotations

import re
from collections import Counter

LATIN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[.\-_][A-Za-z0-9]+)*")
CJK_TOKEN = re.compile(r"[\u4e00-\u9fff]{2,8}")

# 具体产品/公司/模型名：命中即视为实体
GAZETTEER_LATIN = {
    "jev", "qwen", "deepseek", "kimi", "claude", "gemini", "llama", "mistral",
    "grok", "sora", "glm", "minimax", "seedance", "vidu", "hunyuan", "ernie",
    "doubao", "chatgpt", "copilot", "cursor", "windsurf", "devin", "midjourney",
    "stable", "comfyui", "vllm", "sglang", "ollama", "langchain", "llamaindex",
    "autogen", "crewai", "openai", "anthropic", "nvidia", "amd", "bytedance",
    "alibaba", "tencent", "baidu", "moonshot", "zhipu", "baichuan", "stepfun",
    "sensetime", "unitree", "figure", "tesla", "xai", "hopper", "blackwell",
}

GAZETTEER_CJK = (
    "豆包", "文心", "通义", "千问", "混元", "星火", "智谱", "月之暗面", "商汤",
    "旷视", "智元", "宇树", "深度求索", "面壁", "零一万物", "硅基流动", "百川",
    "阶跃", "可灵", "即梦", "海螺", "讯飞", "华为", "小米", "阿里", "腾讯",
    "字节", "百度", "蚂蚁", "网易", "美团", "智源", "书生", "紫东太初",
)

# 出处类专名：会议、期刊、预印本平台。它们会被多篇共享，但不代表「同一件事」。
VENUE = {
    "emnlp", "neurips", "nips", "icml", "iclr", "acl", "naacl", "eacl", "coling",
    "aaai", "ijcai", "cvpr", "iccv", "eccv", "kdd", "sigir", "wsdm", "recsys",
    "chi", "uist", "icra", "iros", "corl", "rss", "tmlr", "jmlr", "pami",
    "tpami", "openreview", "biorxiv", "medrxiv", "ssrn",
}

# 通用词：出现频率高但不能代表「一件事」
GENERIC = {
    "ai", "llm", "llms", "gpt", "api", "apis", "gpu", "gpus", "cpu", "tpu",
    "agent", "agents", "rag", "mcp", "aigc", "agi", "vlm", "vla", "nlp", "cv",
    "transformer", "diffusion", "model", "models", "benchmark", "benchmarks",
    "dataset", "datasets", "paper", "papers", "github", "arxiv", "sota", "demo",
    "tutorial", "code", "tool", "tools", "framework", "pipeline", "workflow",
    "multimodal", "vision", "video", "image", "speech", "audio", "token",
    "tokens", "prompt", "prompts", "fine-tuning", "finetune", "training",
    "inference", "deployment", "server", "cloud", "open", "source", "new",
    "how", "why", "what", "the", "and", "for", "with", "from", "your", "you",
    "this", "that", "are", "not", "all", "can", "its", "our", "we", "it",
}

GENERIC_CJK = (
    "大模型", "智能体", "多模态", "模型", "推理", "训练", "开源", "论文",
    "技术", "架构", "应用", "平台", "发布", "新进展", "人工智能", "机器学习",
    "深度学习", "神经网络", "算法", "芯片", "算力", "机器人", "评测", "数据集",
)


def _is_candidate(word: str, raw: str) -> bool:
    lowered = word.lower()
    if lowered in GENERIC or word in GENERIC_CJK:
        return False
    if lowered in GAZETTEER_LATIN:
        return True
    if any(ch.isdigit() for ch in word) and len(word) >= 3:
        return True
    # 全大写缩写（长度 >= 3）通常是产品/项目名，但排除纯通用词
    return raw.isupper() and len(raw) >= 3


_VENUE_WITH_YEAR = re.compile(r"^([a-z][a-z\-]*?)(?:['’]?)(\d{2}|20\d{2})$")


def is_venue(word: str) -> bool:
    """会议/期刊名（含「EMNLP2026」这种带年份写法）不是话题实体。"""
    lowered = str(word or "").strip().lower()
    if lowered in VENUE:
        return True
    match = _VENUE_WITH_YEAR.match(lowered)
    return bool(match and match.group(1) in VENUE)


def extract(text: str) -> set[str]:
    """从标题中抽取候选实体，统一小写；中文词保持原样。"""
    found: set[str] = set()
    for match in LATIN_TOKEN.finditer(text or ""):
        raw = match.group(0)
        if len(raw) < 2:
            continue
        lowered = raw.lower()
        if lowered in GAZETTEER_LATIN or _is_candidate(lowered, raw):
            if not any(ch.isdigit() for ch in lowered) or len(lowered) >= 3:
                found.add(lowered)
    for name in GAZETTEER_CJK:
        if name in (text or ""):
            found.add(name)
    return {
        item
        for item in found
        if item not in GENERIC and item not in GENERIC_CJK and not is_venue(item)
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
