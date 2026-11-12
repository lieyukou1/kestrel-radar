from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import LLMConfig
from ..domain.profile import DomainProfile
from ..store import repo
from ..util import utcnow
from .prompts import (
    SCREEN_TEMPLATE,
    SEGMENT_TEMPLATE,
    SUMMARY_TEMPLATE,
    TAG_TEMPLATE,
    THEME_TEMPLATE,
    dimensions_of,
    screen_system,
    segment_system,
    summary_system,
    tag_system,
    theme_system,
)

MAX_CONTENT_CHARS = 6000
MAX_TAG_DOCS = 12
MAX_TAG_ABSTRACT = 320
# 推送前的候选闸门只看分数最高的一批，条数比重打标多，单独给上限。
MAX_SCREEN_DOCS = 30
# 事件切分输入：最多几个粗分组；事件优先后要覆盖更多实体，20 个桶约 160 条标题。
MAX_SEGMENT_BUCKETS = 20
# 模型只允许用这两个理由剔除内容；其他理由不认，避免「今天心情不好」式的自由发挥。
_SCREEN_REASONS = {"promotional", "off_topic"}

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


def _clean_dimension(value: Any, dimensions: tuple[str, ...] = ()) -> str:
    """只接受领域分类的完整名称或数字前缀；其余一律丢弃，宁可不改也别写脏数据。"""
    text = str(value or "").strip()
    if not dimensions:
        dimensions = dimensions_of()
    if text in dimensions:
        return text
    for dimension in dimensions:
        if text == dimension.split()[0]:
            return dimension
    return ""


def _clean_keep(value: Any) -> bool:
    """只有明确的否才丢弃；缺字段、看不懂一律按保留处理，不能让筛选误伤日报。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "no", "0"}
    if isinstance(value, (int, float)):
        return bool(value)
    return True


def _clean_screen_reason(value: Any) -> str:
    """理由只用于回查；规范值优先，模型若写中文也照收，别让它变成空串。"""
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    lowered = text.lower()
    return lowered if lowered in _SCREEN_REASONS else text[:40]


def estimate_cost(
    usage: dict, *, input_cny_per_million: float, output_cny_per_million: float
) -> float:
    input_tokens = int(usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or 0)
    return (
        input_tokens * input_cny_per_million + output_tokens * output_cny_per_million
    ) / 1_000_000


class DeepSeekLLM:
    def __init__(
        self,
        config: LLMConfig,
        *,
        timeout: float = 120.0,
        profile: DomainProfile | None = None,
    ) -> None:
        self.config = config
        self.timeout = timeout
        # 领域语气与分类体系：不传则用仓库自带的 AI 包
        self.profile = profile

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
            system=theme_system(self.profile),
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

    def classify_dimensions(
        self, conn: sqlite3.Connection, documents: list[dict]
    ) -> dict[int, str]:
        """给即将推送的少量内容重打标，修正关键词启发式的误判。

        documents 每项含 id/title/kind/abstract。返回 {document_id: dimension}；
        解析失败返回空字典，调用方沿用原标签，不影响推送。
        """
        if not documents:
            return {}
        payload = json.dumps(
            [
                {
                    "id": int(item["id"]),
                    "title": str(item.get("title") or "")[:120],
                    "kind": str(item.get("kind") or ""),
                    "abstract": str(item.get("abstract") or "")[:MAX_TAG_ABSTRACT],
                }
                for item in documents[:MAX_TAG_DOCS]
            ],
            ensure_ascii=False,
        )
        text, _ = self.chat(
            conn,
            stage="tag",
            system=tag_system(self.profile),
            user=TAG_TEMPLATE.format(payload=payload),
            # 思考模型先消耗推理 token，额度给小了会返回空内容。
            max_tokens=1200,
        )
        try:
            data = parse_json_object(text)
        except LLMError:
            return {}
        items = data.get("items")
        if not isinstance(items, list):
            return {}
        result: dict[int, str] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                document_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            dimension = _clean_dimension(
                item.get("dimension"), dimensions_of(self.profile)
            )
            if dimension:
                result[document_id] = dimension
        return result

    def screen_candidates(
        self, conn: sqlite3.Connection, documents: list[dict]
    ) -> dict[int, dict]:
        """推送前的候选闸门：一次调用同时给出维度、去留与原因。

        documents 每项含 id/title/kind/abstract。返回
        {document_id: {"dimension": str, "keep": bool, "reason": str}}；
        解析失败返回空字典，调用方按「全部保留」处理，不让模型故障卡住日报。
        """
        if not documents:
            return {}
        payload = json.dumps(
            [
                {
                    "id": int(item["id"]),
                    "title": str(item.get("title") or "")[:120],
                    "kind": str(item.get("kind") or ""),
                    "abstract": str(item.get("abstract") or "")[:MAX_TAG_ABSTRACT],
                }
                for item in documents[:MAX_SCREEN_DOCS]
            ],
            ensure_ascii=False,
        )
        text, _ = self.chat(
            conn,
            stage="tag",
            system=screen_system(self.profile),
            user=SCREEN_TEMPLATE.format(payload=payload),
            # 每条一行 JSON，条数比分类多，额度跟着放大，否则会被截断。
            max_tokens=3000,
        )
        try:
            data = parse_json_object(text)
        except LLMError:
            # 思考模型偶尔把 JSON 截断（只输出半截数组），放大额度再要一次。
            retry_text, _ = self.chat(
                conn,
                stage="tag",
                system=screen_system(self.profile),
                user=SCREEN_TEMPLATE.format(payload=payload),
                max_tokens=4000,
            )
            try:
                data = parse_json_object(retry_text)
            except LLMError:
                return {}
        items = data.get("items")
        if not isinstance(items, list):
            return {}
        result: dict[int, dict] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                document_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            result[document_id] = {
                "dimension": _clean_dimension(
                    item.get("dimension"), dimensions_of(self.profile)
                ),
                "keep": _clean_keep(item.get("keep")),
                "reason": _clean_screen_reason(item.get("reason")),
            }
        return result

    def segment_events(
        self, conn: sqlite3.Connection, buckets: list[dict]
    ) -> list[dict]:
        """把「实体粗分组」切成一件件事，并给每件事起名。

        返回 [{bucket, item_ids, label, why, when, is_update, change}]；
        解析失败返回空列表，调用方退回「一个实体桶就是一件事」。
        """
        if not buckets:
            return []
        payload = json.dumps(buckets[:MAX_SEGMENT_BUCKETS], ensure_ascii=False)
        text, _ = self.chat(
            conn,
            stage="theme",
            system=segment_system(self.profile),
            user=SEGMENT_TEMPLATE.format(payload=payload),
            max_tokens=3000,
        )
        try:
            data = parse_json_object(text)
        except LLMError:
            retry_text, _ = self.chat(
                conn,
                stage="theme",
                system=segment_system(self.profile),
                user=SEGMENT_TEMPLATE.format(payload=payload),
                max_tokens=4000,
            )
            try:
                data = parse_json_object(retry_text)
            except LLMError:
                return []
        raw = data.get("events")
        if not isinstance(raw, list):
            return []
        result: list[dict] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                bucket = int(item.get("bucket"))
            except (TypeError, ValueError):
                continue
            item_ids: list[int] = []
            for value in item.get("item_ids") or []:
                try:
                    item_ids.append(int(value))
                except (TypeError, ValueError):
                    continue
            label = str(item.get("label") or "").strip()
            if not label or len(item_ids) < 2:
                continue
            result.append(
                {
                    "bucket": bucket,
                    "item_ids": item_ids,
                    "label": label[:40],
                    "why": str(item.get("why") or "").strip(),
                    "when": _clean_when(item.get("when")),
                    # 事件是否为新进展由调用方结合跨天历史判断，模型不猜。
                    "is_update": False,
                    "change": "",
                }
            )
        return result

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
            system=summary_system(self.profile),
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
                system=summary_system(self.profile),
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
