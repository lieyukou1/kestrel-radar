from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import LLMConfig
from ..store import repo
from ..util import utcnow
from .prompts import SUMMARY_SYSTEM, SUMMARY_TEMPLATE, THEME_SYSTEM, THEME_TEMPLATE

MAX_CONTENT_CHARS = 6000

# 模型给出的事件时间只接受「年-月」或「年-月-日」，其余一律丢弃，避免脏字符串上卡片。
_WHEN_PATTERN = re.compile(r"^\d{4}(-\d{2}){0,2}$")


class LLMError(RuntimeError):
    pass


class LLMJSONError(LLMError):
    """模型输出不是合法 JSON（通常是额度截断或格式漂移）。"""


class BudgetExceeded(RuntimeError):
    pass


@dataclass(slots=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cost_cny: float


def parse_json_object(text: str) -> dict[str, Any]:
    """容忍代码块和前后说明，提取第一个 JSON 对象。"""
    cleaned = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise LLMJSONError(f"no JSON object in response: {text[:200]}")
    try:
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        # 包成自己的异常类型，调用方才能区分「格式坏了」和「接口/网络失败」并重试。
        raise LLMJSONError(f"invalid JSON ({exc.msg} at char {exc.pos}): {text[:200]}") from exc


def _clean_when(value: Any) -> str:
    text = str(value or "").strip()
    return text if _WHEN_PATTERN.match(text) else ""


def _clean_flag(value: Any) -> bool | None:
    """模型可能把布尔写成字符串；没给就返回 None，交给日期规则判断。"""
    if isinstance(value, bool):
        return value
    text = str(value if value is not None else "").strip().lower()
    if text in ("true", "yes", "1", "是"):
        return True
    if text in ("false", "no", "0", "否"):
        return False
    return None


def estimate_cost(
    usage: dict, *, input_cny_per_million: float, output_cny_per_million: float
) -> float:
    input_tokens = int(usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or 0)
    return (
        input_tokens * input_cny_per_million + output_tokens * output_cny_per_million
    ) / 1_000_000


class DeepSeekLLM:
    def __init__(self, config: LLMConfig, *, timeout: float = 120.0) -> None:
        self.config = config
        self.timeout = timeout

    def _check_budget(self, conn: sqlite3.Connection) -> None:
        day = utcnow().date().isoformat()
        used = repo.daily_usage(conn, day)
        if float(used["cost_cny"]) >= self.config.daily_budget_cny:
            raise BudgetExceeded(
                f"daily budget reached: {used['cost_cny']:.2f} / {self.config.daily_budget_cny:.2f} CNY"
            )

    def chat(
        self,
        conn: sqlite3.Connection,
        *,
        stage: str,
        system: str,
        user: str,
        max_tokens: int = 800,
        document_id: int | None = None,
    ) -> tuple[str, Usage]:
        """发一次对话请求。

        deepseek-flash 是思考模型，推理 token 也算进 max_tokens；复杂抽取时可能把额度
        用完导致 content 为空。因此 content 为空时放宽额度重试一次。
        """
        last_finish = "unknown"
        total_input = 0
        total_output = 0
        total_cost = 0.0

        for attempt in range(2):
            self._check_budget(conn)
            budget = max_tokens if attempt == 0 else min(max_tokens * 3, 8000)
            payload = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.2,
                "max_tokens": budget,
            }
            try:
                response = httpx.post(
                    f"{self.config.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.config.api_key}"},
                    json=payload,
                    timeout=self.timeout,
                )
            except httpx.HTTPError as exc:
                raise LLMError(f"request failed: {exc}") from exc

            data = response.json()
            if response.status_code >= 400 or data.get("error"):
                raise LLMError(f"API error: {data}")

            choices = data.get("choices") or []
            if not choices:
                raise LLMError(f"empty choices: {data}")
            choice = choices[0]
            last_finish = str(choice.get("finish_reason") or "unknown")
            content = (choice.get("message") or {}).get("content") or ""
            raw_usage = data.get("usage") or {}
            cost = estimate_cost(
                raw_usage,
                input_cny_per_million=self.config.input_cny_per_million,
                output_cny_per_million=self.config.output_cny_per_million,
            )
            usage = Usage(
                input_tokens=int(raw_usage.get("prompt_tokens") or 0),
                output_tokens=int(raw_usage.get("completion_tokens") or 0),
                cost_cny=cost,
            )
            repo.record_llm_usage(
                conn,
                day=utcnow().date().isoformat(),
                stage=stage,
                model=self.config.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_cny=usage.cost_cny,
                document_id=document_id,
            )
            total_input += usage.input_tokens
            total_output += usage.output_tokens
            total_cost += usage.cost_cny

            if content.strip():
                return content, Usage(total_input, total_output, total_cost)
            # 只有真被额度截断（finish_reason=length）才值得放宽预算重试；
            # content_filter 或模型异常返回空时，加大额度不会恢复，只会多花一次钱。
            if last_finish != "length":
                break

        raise LLMError(
            f"empty content after retry (finish_reason={last_finish}, "
            f"max_tokens={max_tokens})"
        )

    def label_theme(
        self, conn: sqlite3.Connection, titles: list[str], *, history: str = ""
    ) -> dict:
        """给当天的主线话题起名、说明原因，并尽量给出事件时间。

        history 是该话题上次推送的情况；没有历史时模型一律返回 is_update=false。
        解析失败返回空对象，由调用方回退到实体名。
        """
        if not titles:
            return {}
        user = THEME_TEMPLATE.format(
            titles="\n".join(f"- {title}" for title in titles),
            history=history,
        )
        text, _ = self.chat(
            conn,
            stage="theme",
            system=THEME_SYSTEM,
            user=user,
            # 思考模型会先花掉一批 token 再输出，400 在长标题下会被截成空内容。
            max_tokens=800,
        )
        try:
            data = parse_json_object(text)
        except LLMError:
            return {}
        return {
            "label": str(data.get("label") or "").strip(),
            "why": str(data.get("why") or "").strip(),
            "when": _clean_when(data.get("when")),
            "is_update": _clean_flag(data.get("is_update")),
            "change": str(data.get("change") or "").strip(),
        }

    def summarize(self, conn: sqlite3.Connection, document: sqlite3.Row) -> dict | None:
        cached = repo.find_latest_summary(conn, int(document["id"]))
        if cached:
            return cached

        content = (document["abstract"] or "").strip()
        if not content:
            content = document["title"]
        # 先把正文占位符摘出去，避免正文里的花括号被 str.format 误解析。
        template = SUMMARY_TEMPLATE.replace("{content}", "\x00CONTENT\x00")
        user = template.format(
            title=document["title"],
            kind=document["kind"],
            tags=document["tags_json"] or "[]",
        ).replace("\x00CONTENT\x00", content[:MAX_CONTENT_CHARS])
        text, _ = self.chat(
            conn,
            stage="summary",
            system=SUMMARY_SYSTEM,
            user=user,
            max_tokens=800,
            document_id=int(document["id"]),
        )
        try:
            summary = parse_json_object(text)
        except LLMError:
            # 思考模型偶尔会把 JSON 截断（只输出一个 "{"），放大额度再要一次。
            retry_text, _ = self.chat(
                conn,
                stage="summary",
                system=SUMMARY_SYSTEM,
                user=user,
                max_tokens=2400,
                document_id=int(document["id"]),
            )
            summary = parse_json_object(retry_text)
        summary.setdefault("problem", "未知")
        summary.setdefault("method", "未知")
        summary.setdefault("conclusions", [])
        summary.setdefault("why", "未知")
        summary.setdefault("tags", [])
        return summary
