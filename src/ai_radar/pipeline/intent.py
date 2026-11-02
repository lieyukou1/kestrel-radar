"""自然语言路由：把用户的一句话解析成知识库筛选、检索或主题设置。

只做意图识别，不直接改数据；调用方拿到结构化结果后再执行。
模型不可用或解析失败时返回 None，由调用方回退到帮助文案。
"""

from __future__ import annotations

import sqlite3

from ..config import AppConfig
from ..llm.client import DeepSeekLLM, parse_json_object
from .tagging import TAGS

KINDS = {"paper", "repo", "blog", "forum"}
STATUSES = {"deep", "saved"}
WINDOWS = {7, 30}

INTENT_SYSTEM = """你是个人情报助手的意图路由器。把用户的一句话解析成一个 JSON 对象，只输出 JSON，不要解释。

intent 取值：
- "library"：想查看或筛选知识库 / 收藏夹
- "topic"：想按关键词检索内容（找资料，不是问结论）
- "question"：提出一个需要回答的问题（可能基于知识库，也可能没有依据）
- "theme"：想设定接下来推送的主题偏好
- "theme_clear"：想取消已设定的推送主题
- "chat"：寒暄、闲聊、表达情绪，不需要查资料
- "help"：询问怎么用
- "none"：无法判断

字段约束：
- scope：library 时取 "kb"（知识库）或 "favorites"（收藏夹），默认 "kb"
- dimension：只能原样取下面 8 个之一，或 null
  {dimensions}
- kind：paper（论文）/ repo（仓库）/ blog（媒体）/ forum（讨论）/ null
- status：deep（已精读）/ saved（仅收藏）/ null
- window：7 或 30（表示近 7 天 / 近 30 天）/ null
- query：topic 时的检索词；question 时保留用户原问题
- keywords：theme 时的关键词数组（1-4 个，尽量用原文出现的词）；
  question 时给 1-4 个检索关键词（专有名词、型号、方法名优先，不要整句）
- days：theme 持续天数，默认 7，最大 30

示例：
“知识库里多模态的论文” → {{"intent":"library","scope":"kb","dimension":"5 多模态与生成","kind":"paper"}}
“上周收藏的东西” → {{"intent":"library","scope":"favorites","window":7}}
“最近多推点 MoE 推理优化” → {{"intent":"theme","keywords":["MoE","推理优化"],"dimension":"3 推理与加速","days":7}}
“查一下强化学习” → {{"intent":"topic","query":"强化学习"}}
“RSI 那篇的结论是什么” → {{"intent":"question","query":"RSI 那篇的结论是什么","keywords":["RSI"]}}
“在吗 / 今天好累” → {{"intent":"chat"}}
“取消主题” → {{"intent":"theme_clear"}}"""


def parse_intent(
    conn: sqlite3.Connection, config: AppConfig, text: str
) -> dict | None:
    """把一句话解析成结构化意图；任何失败都返回 None。"""
    if config.llm is None:
        return None
    message = " ".join(str(text or "").split())
    if len(message) < 2:
        return None

    llm = DeepSeekLLM(config.llm)
    system = INTENT_SYSTEM.format(dimensions="、".join(TAGS))
    try:
        reply, _ = llm.chat(
            conn,
            stage="intent",
            system=system,
            user=message,
            # 思考模型的推理 token 也计入额度，400 在长句下会被截成空内容。
            max_tokens=800,
        )
        data = parse_json_object(reply)
    except Exception:
        return None
    return _normalize(data)


def _normalize(data: dict) -> dict | None:
    kind = str(data.get("intent") or "none")
    if kind not in {
        "library",
        "topic",
        "question",
        "theme",
        "theme_clear",
        "chat",
        "help",
        "none",
    }:
        return None

    if kind == "library":
        filters: dict[str, str] = {}
        dimension = data.get("dimension")
        if dimension in TAGS:
            filters["dimension"] = dimension
        if data.get("kind") in KINDS:
            filters["kind"] = str(data["kind"])
        if data.get("status") in STATUSES:
            filters["status"] = str(data["status"])
        try:
            window = int(data.get("window"))
        except (TypeError, ValueError):
            window = None
        if window in WINDOWS:
            filters["window"] = str(window)
        scope = "favorites" if data.get("scope") == "favorites" else "kb"
        return {"intent": "library", "scope": scope, "filters": filters}

    if kind == "topic":
        query = " ".join(str(data.get("query") or "").split())
        if not query:
            return None
        return {"intent": "topic", "query": query}

    if kind == "question":
        question = " ".join(str(data.get("query") or "").split())
        if not question:
            return None
        keywords = [
            str(word).strip()
            for word in (data.get("keywords") or [])
            if str(word).strip()
        ][:4]
        return {"intent": "question", "query": question, "keywords": keywords}

    if kind == "theme":
        keywords = [
            str(word).strip()
            for word in (data.get("keywords") or [])
            if str(word).strip()
        ][:4]
        dimension = data.get("dimension")
        if dimension not in TAGS:
            dimension = None
        if not keywords and not dimension:
            return None
        try:
            days = int(data.get("days") or 7)
        except (TypeError, ValueError):
            days = 7
        days = max(1, min(30, days))
        return {
            "intent": "theme",
            "keywords": keywords,
            "dimension": dimension,
            "days": days,
        }

    if kind in {"theme_clear", "help", "chat"}:
        return {"intent": kind}

    return None
