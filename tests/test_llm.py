from __future__ import annotations

import httpx
import pytest

from ai_radar.config import LLMConfig
from ai_radar.llm.client import (
    BudgetExceeded,
    DeepSeekLLM,
    LLMError,
    LLMJSONError,
    estimate_cost,
    parse_json_object,
)
from ai_radar.store import repo
from ai_radar.store.db import connect, init_db
from ai_radar.util import utcnow


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload


def completion(
    content: str,
    *,
    finish_reason: str = "stop",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
) -> dict:
    return {
        "choices": [
            {"finish_reason": finish_reason, "message": {"content": content}}
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    }


def make_conn(tmp_path):
    conn = connect(tmp_path / "llm.db")
    init_db(conn)
    return conn


def make_llm(**kwargs) -> DeepSeekLLM:
    return DeepSeekLLM(LLMConfig(api_key="test-key", **kwargs))


def make_document(conn, *, abstract: str = "摘要正文"):
    document_id = repo.insert_document(
        conn,
        canonical_id="test:summarize",
        kind="paper",
        title="标题在正文里",
        abstract=abstract,
        url="https://example.com/sum",
        published_at="2026-10-01T00:00:00+00:00",
        authority="A",
        primary_tag="1 模型与架构",
        tags=["1 模型与架构"],
    )
    return repo.get_document(conn, document_id)


# ---------- JSON 解析 ----------


def test_parse_json_object_accepts_common_shapes() -> None:
    assert parse_json_object('{"a": 1}') == {"a": 1}
    assert parse_json_object('```json\n{"a": 2}\n```') == {"a": 2}
    assert parse_json_object('结果如下：\n{"a": 3}\n以上') == {"a": 3}


def test_parse_json_object_raises_json_error() -> None:
    with pytest.raises(LLMJSONError):
        parse_json_object("没有 JSON 对象")
    with pytest.raises(LLMJSONError):
        parse_json_object('{"a": }')

    # 调用方靠这个父子关系区分「格式坏了」和「接口失败」，必须保持。
    assert issubclass(LLMJSONError, LLMError)


def test_estimate_cost() -> None:
    assert estimate_cost({}, input_cny_per_million=1.0, output_cny_per_million=4.0) == 0.0
    assert (
        estimate_cost(
            {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
            input_cny_per_million=1.0,
            output_cny_per_million=4.0,
        )
        == 5.0
    )


# ---------- chat ----------


def test_chat_returns_content_and_records_usage(tmp_path, monkeypatch) -> None:
    conn = make_conn(tmp_path)
    sent: list[dict] = []

    def fake_post(url, **kwargs):
        sent.append(kwargs["json"])
        return FakeResponse(completion("答案"))

    monkeypatch.setattr("ai_radar.llm.client.httpx.post", fake_post)

    text, usage = make_llm().chat(
        conn, stage="qa", system="s", user="u", max_tokens=300
    )

    assert text == "答案"
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50
    assert usage.cost_cny == pytest.approx((100 * 1.0 + 50 * 4.0) / 1_000_000)
    assert sent[0]["max_tokens"] == 300

    today = repo.daily_usage(conn, utcnow().date().isoformat())
    assert today["calls"] == 1
    assert today["cost_cny"] == pytest.approx(usage.cost_cny)


def test_chat_retries_with_larger_budget_when_content_empty(tmp_path, monkeypatch) -> None:
    conn = make_conn(tmp_path)
    budgets: list[int] = []
    responses = [
        FakeResponse(completion("", finish_reason="length")),
        FakeResponse(completion("补上的答案")),
    ]

    def fake_post(url, **kwargs):
        budgets.append(kwargs["json"]["max_tokens"])
        return responses.pop(0)

    monkeypatch.setattr("ai_radar.llm.client.httpx.post", fake_post)

    text, usage = make_llm().chat(conn, stage="summary", system="s", user="u", max_tokens=800)

    assert text == "补上的答案"
    assert budgets == [800, 2400]
    assert usage.input_tokens == 200
    assert usage.output_tokens == 100


def test_chat_raises_after_two_empty_answers(tmp_path, monkeypatch) -> None:
    conn = make_conn(tmp_path)
    monkeypatch.setattr(
        "ai_radar.llm.client.httpx.post",
        lambda url, **kwargs: FakeResponse(completion("", finish_reason="length")),
    )

    with pytest.raises(LLMError) as excinfo:
        make_llm().chat(conn, stage="summary", system="s", user="u", max_tokens=400)
    assert "finish_reason=length" in str(excinfo.value)
    assert "max_tokens=400" in str(excinfo.value)


def test_chat_does_not_retry_when_not_truncated(tmp_path, monkeypatch) -> None:
    """content_filter 之类的空内容加大额度也恢复不了，不该白花第二次调用。"""
    conn = make_conn(tmp_path)
    budgets: list[int] = []

    def fake_post(url, **kwargs):
        budgets.append(kwargs["json"]["max_tokens"])
        return FakeResponse(completion("", finish_reason="content_filter"))

    monkeypatch.setattr("ai_radar.llm.client.httpx.post", fake_post)

    with pytest.raises(LLMError) as excinfo:
        make_llm().chat(conn, stage="qa", system="s", user="u", max_tokens=800)

    assert budgets == [800]
    assert "content_filter" in str(excinfo.value)


def test_chat_wraps_transport_errors(tmp_path, monkeypatch) -> None:
    conn = make_conn(tmp_path)

    def fake_post(url, **kwargs):
        raise httpx.ConnectError("connection reset")

    monkeypatch.setattr("ai_radar.llm.client.httpx.post", fake_post)
    with pytest.raises(LLMError) as excinfo:
        make_llm().chat(conn, stage="qa", system="s", user="u")
    assert "request failed" in str(excinfo.value)


def test_chat_raises_on_api_error(tmp_path, monkeypatch) -> None:
    conn = make_conn(tmp_path)
    monkeypatch.setattr(
        "ai_radar.llm.client.httpx.post",
        lambda url, **kwargs: FakeResponse({"error": {"message": "bad"}}, status_code=400),
    )
    with pytest.raises(LLMError) as excinfo:
        make_llm().chat(conn, stage="qa", system="s", user="u")
    assert "API error" in str(excinfo.value)


def test_chat_raises_on_empty_choices(tmp_path, monkeypatch) -> None:
    conn = make_conn(tmp_path)
    monkeypatch.setattr(
        "ai_radar.llm.client.httpx.post",
        lambda url, **kwargs: FakeResponse({"choices": []}),
    )
    with pytest.raises(LLMError) as excinfo:
        make_llm().chat(conn, stage="qa", system="s", user="u")
    assert "empty choices" in str(excinfo.value)


def test_chat_checks_budget_before_request(tmp_path, monkeypatch) -> None:
    conn = make_conn(tmp_path)

    def fake_post(url, **kwargs):
        raise AssertionError("预算不足时不应该发请求")

    monkeypatch.setattr("ai_radar.llm.client.httpx.post", fake_post)
    with pytest.raises(BudgetExceeded):
        make_llm(daily_budget_cny=0.0).chat(conn, stage="qa", system="s", user="u")


# ---------- label_theme ----------


def test_label_theme_skips_empty_titles(tmp_path, monkeypatch) -> None:
    conn = make_conn(tmp_path)
    llm = make_llm()

    def fail(*args, **kwargs):
        raise AssertionError("没有标题时不应该调用模型")

    monkeypatch.setattr(llm, "chat", fail)
    assert llm.label_theme(conn, []) == {}


def test_label_theme_falls_back_on_bad_json(tmp_path, monkeypatch) -> None:
    conn = make_conn(tmp_path)
    llm = make_llm()
    monkeypatch.setattr(llm, "chat", lambda *a, **k: ("不是 JSON", None))
    assert llm.label_theme(conn, ["标题一"]) == {}


def test_label_theme_parses_json(tmp_path, monkeypatch) -> None:
    conn = make_conn(tmp_path)
    llm = make_llm()
    monkeypatch.setattr(
        llm,
        "chat",
        lambda *a, **k: ('{"label": "主线", "why": "多源讨论", "when": "2025-08"}', None),
    )
    data = llm.label_theme(conn, ["标题一", "标题二"])
    assert data["label"] == "主线"
    assert data["why"] == "多源讨论"
    assert data["when"] == "2025-08"


def test_label_theme_drops_malformed_when(tmp_path, monkeypatch) -> None:
    """模型给的时间只接受 YYYY-MM / YYYY-MM-DD，别的脏字符串不能上卡片。"""
    conn = make_conn(tmp_path)
    llm = make_llm()
    monkeypatch.setattr(
        llm,
        "chat",
        lambda *a, **k: ('{"label": "主线", "when": "去年夏天"}', None),
    )
    assert llm.label_theme(conn, ["标题一"])["when"] == ""

    monkeypatch.setattr(
        llm,
        "chat",
        lambda *a, **k: ('{"label": "主线", "when": "2025-08-14"}', None),
    )
    assert llm.label_theme(conn, ["标题一"])["when"] == "2025-08-14"


def test_label_theme_parses_update_fields(tmp_path, monkeypatch) -> None:
    """is_update 可能是字符串 true/false；没给就留 None，交给日期规则判断。"""
    conn = make_conn(tmp_path)
    llm = make_llm()
    monkeypatch.setattr(
        llm,
        "chat",
        lambda *a, **k: (
            '{"label": "主线", "is_update": "true", "change": "发布 1.14 版本"}',
            None,
        ),
    )
    data = llm.label_theme(conn, ["标题一"], history="上次推送于 2026-10-29")
    assert data["is_update"] is True
    assert data["change"] == "发布 1.14 版本"

    monkeypatch.setattr(llm, "chat", lambda *a, **k: ('{"label": "主线"}', None))
    assert llm.label_theme(conn, ["标题一"])["is_update"] is None


def test_label_theme_sends_history_to_model(tmp_path, monkeypatch) -> None:
    """上次推送的情况必须真的进到 prompt 里，否则模型无从判断更新。"""
    conn = make_conn(tmp_path)
    llm = make_llm()
    seen: dict = {}

    def fake_chat(conn, **kwargs):
        seen.update(kwargs)
        return ('{"label": "主线"}', None)

    monkeypatch.setattr(llm, "chat", fake_chat)
    llm.label_theme(conn, ["标题一"], history="\n\n该话题上次推送于 2026-10-29。")
    assert "该话题上次推送于 2026-10-29" in seen["user"]


# ---------- summarize ----------


def test_summarize_returns_cache_without_calling_model(tmp_path, monkeypatch) -> None:
    conn = make_conn(tmp_path)
    document = make_document(conn)
    llm = make_llm()
    monkeypatch.setattr(
        "ai_radar.llm.client.repo.find_latest_summary",
        lambda conn, document_id: {"problem": "缓存的问题"},
    )
    monkeypatch.setattr(
        llm, "chat", lambda *a, **k: pytest.fail("有缓存时不应该调用模型")
    )

    assert llm.summarize(conn, document) == {"problem": "缓存的问题"}


def test_summarize_fills_defaults(tmp_path, monkeypatch) -> None:
    conn = make_conn(tmp_path)
    document = make_document(conn)
    llm = make_llm()
    monkeypatch.setattr(llm, "chat", lambda *a, **k: ('{"problem": "提出的问题"}', None))

    summary = llm.summarize(conn, document)

    assert summary["problem"] == "提出的问题"
    assert summary["method"] == "未知"
    assert summary["conclusions"] == []
    assert summary["why"] == "未知"
    assert summary["tags"] == []


def test_summarize_retries_with_bigger_budget_on_bad_json(tmp_path, monkeypatch) -> None:
    conn = make_conn(tmp_path)
    document = make_document(conn)
    llm = make_llm()
    budgets: list[int] = []

    def fake_chat(*args, **kwargs):
        budgets.append(kwargs.get("max_tokens", 800))
        if len(budgets) == 1:
            return ('{"problem": "被截断', None)
        return ('{"problem": "重试成功"}', None)

    monkeypatch.setattr(llm, "chat", fake_chat)

    assert llm.summarize(conn, document)["problem"] == "重试成功"
    assert budgets == [800, 2400]


def test_summarize_raises_when_retry_is_still_broken(tmp_path, monkeypatch) -> None:
    conn = make_conn(tmp_path)
    document = make_document(conn)
    llm = make_llm()
    monkeypatch.setattr(llm, "chat", lambda *a, **k: ("依然不是 JSON", None))

    with pytest.raises(LLMJSONError):
        llm.summarize(conn, document)


def test_summarize_uses_title_when_abstract_missing(tmp_path, monkeypatch) -> None:
    conn = make_conn(tmp_path)
    document = make_document(conn, abstract="")
    llm = make_llm()
    captured: dict = {}

    def fake_chat(*args, **kwargs):
        captured["user"] = kwargs["user"]
        return ('{"problem": "p"}', None)

    monkeypatch.setattr(llm, "chat", fake_chat)
    llm.summarize(conn, document)

    assert "标题在正文里" in captured["user"]
