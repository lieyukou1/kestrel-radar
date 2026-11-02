from __future__ import annotations

import json
import threading
from types import SimpleNamespace

from ai_radar.jobs.router import HELP_TEXT, MessageRouter
from ai_radar.pipeline import qa
from ai_radar.store import repo
from ai_radar.store.db import connect, init_db


class StubNotifier:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.cards: list[dict] = []

    def send_text(self, text: str) -> str:
        self.texts.append(text)
        return "om_text"

    def send_text_to(self, receive_id: str, text: str) -> str:
        self.texts.append(text)
        return "om_text"

    def send_card(self, card: dict) -> str:
        self.cards.append(card)
        return "om_card"


def make_router(tmp_path) -> tuple[MessageRouter, StubNotifier]:
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    init_db(conn)
    conn.close()
    config = SimpleNamespace(
        db_path=db_path,
        feishu=SimpleNamespace(open_id="ou_me"),
        llm=None,
        weights={},
    )
    notifier = StubNotifier()
    return MessageRouter(config, notifier), notifier


def add_document(tmp_path, title: str, *, tag: str = "4 Agent 与应用") -> int:
    conn = connect(tmp_path / "test.db")
    document_id = repo.insert_document(
        conn,
        canonical_id=f"test:{title}",
        kind="paper",
        title=title,
        abstract="abstract",
        url=f"https://example.com/{title}",
        published_at="2026-10-19T08:00:00+00:00",
        authority="A",
        primary_tag=tag,
        tags=[tag],
    )
    conn.close()
    return document_id


def background_spy(router: MessageRouter) -> list[tuple[str, tuple]]:
    """把后台线程替换成同步记录，避免测试依赖线程调度。"""
    calls: list[tuple[str, tuple]] = []

    def fake(target, *args, name: str = "") -> None:
        calls.append((getattr(target, "__name__", str(target)), args))

    router._background = fake  # type: ignore[method-assign]
    return calls


def make_event(
    *,
    sender_type: str = "user",
    open_id: str | None = "ou_me",
    message_type: str = "text",
    text: str = "/status",
    message_id: str = "om_1",
) -> SimpleNamespace:
    message = SimpleNamespace(
        message_type=message_type,
        content=json.dumps({"text": text}),
        message_id=message_id,
    )
    sender_id = None if open_id is None else SimpleNamespace(open_id=open_id)
    sender = SimpleNamespace(sender_type=sender_type, sender_id=sender_id)
    return SimpleNamespace(event=SimpleNamespace(sender=sender, message=message))


# ---------- 命令分派表 ----------


def test_help_and_unknown_command(tmp_path) -> None:
    router, notifier = make_router(tmp_path)
    router.process_text("/help")
    router.process_text("/nope")
    assert notifier.texts[0] == HELP_TEXT
    assert "未识别的命令" in notifier.texts[1]


def test_status_reads_database(tmp_path) -> None:
    router, notifier = make_router(tmp_path)
    router.process_text("/status")
    assert "documents_by_status" in notifier.texts[0]


def test_kb_command_sends_card(tmp_path) -> None:
    router, notifier = make_router(tmp_path)
    router.process_text("/kb")
    assert notifier.cards and notifier.cards[0]["header"]["title"]["content"].startswith("知识库")


def test_favorites_command_sends_favorites_card(tmp_path) -> None:
    router, notifier = make_router(tmp_path)
    router.process_text("/favorites")
    assert notifier.cards and notifier.cards[0]["header"]["title"]["content"].startswith("收藏夹")


def test_prefs_command_renders_preferences(tmp_path) -> None:
    router, notifier = make_router(tmp_path)
    router.process_text("/prefs")
    assert "偏好设置" in notifier.texts[0]


def test_same_message_is_only_handled_once(tmp_path) -> None:
    router, notifier = make_router(tmp_path)
    router.process_text("/status", "om_same")
    router.process_text("/status", "om_same")
    assert len(notifier.texts) == 1


def test_topic_command_requires_keyword_and_dispatches(tmp_path) -> None:
    router, notifier = make_router(tmp_path)
    calls = background_spy(router)

    router.process_text("/topic")
    assert "用法：/topic" in notifier.texts[-1]

    router.process_text("/topic 强化学习")
    assert calls == [("topic_search", ("强化学习",))]
    assert "正在检索" in notifier.texts[-1]


def test_related_command_validates_argument(tmp_path) -> None:
    router, notifier = make_router(tmp_path)
    router.process_text("/related")
    router.process_text("/related abc")
    assert all("用法：/related" in text for text in notifier.texts)
    assert len(notifier.texts) == 2

    router.process_text("/related 9999")
    assert notifier.texts[-1] == "文章不存在。"


def test_compare_command_validates_arguments(tmp_path) -> None:
    router, notifier = make_router(tmp_path)
    router.process_text("/compare 1")
    router.process_text("/compare 1 abc")
    assert all("用法：/compare" in text for text in notifier.texts)

    router.process_text("/compare 1 2")
    assert notifier.texts[-1] == "文章不存在。"


def test_pause_and_resume_dimension(tmp_path) -> None:
    router, notifier = make_router(tmp_path)
    add_document(tmp_path, "维度文章", tag="4 Agent 与应用")

    router.process_text("/pause dimension 4")
    assert notifier.texts[-1] == "已暂停维度：4 Agent 与应用"

    router.process_text("/resume dimension 4")
    assert notifier.texts[-1] == "已恢复维度：4 Agent 与应用"

    router.process_text("/pause dimension 9")
    assert "未找到维度" in notifier.texts[-1]


def test_pause_and_resume_source(tmp_path) -> None:
    router, notifier = make_router(tmp_path)
    router.process_text("/pause source hackernews")
    assert notifier.texts[-1] == "已暂停来源：hackernews"

    router.process_text("/resume source hackernews")
    assert notifier.texts[-1] == "已恢复来源：hackernews"


def test_pause_without_target_is_rejected(tmp_path) -> None:
    router, notifier = make_router(tmp_path)
    router.process_text("/pause")
    assert "用法" in notifier.texts[0]


def test_natural_language_runs_in_background(tmp_path) -> None:
    router, _ = make_router(tmp_path)
    calls = background_spy(router)

    router.process_text("知识库里多模态的论文")
    assert calls == [("handle_natural_language", ("知识库里多模态的论文",))]


# ---------- 卡片动作 ----------


def test_card_action_returns_toast_text(tmp_path) -> None:
    router, _ = make_router(tmp_path)
    assert router.handle_card_action({"action": "不存在的动作"}) == "未识别的操作。"


def test_card_action_save_read_detail_and_related(tmp_path) -> None:
    router, notifier = make_router(tmp_path)
    document_id = add_document(tmp_path, "卡片文章")
    repo.create_digest(router.db(), "2026-10-19")
    save = {"action": "save", "document_id": document_id, "digest_date": "2026-10-19"}

    assert router.handle_card_action(dict(save)) == "已加入知识库，可用 /kb 查看。"
    assert router.handle_card_action(dict(save)) == "已经在知识库里了。"
    assert router.handle_card_action({"action": "read", "document_id": document_id}) == "已标记为已读。"
    assert router.handle_card_action({"action": "detail", "document_id": document_id}) == "已打开详情"
    assert router.handle_card_action({"action": "dislike", "document_id": document_id}) == "请选择原因"
    assert router.handle_card_action({"action": "related", "document_id": document_id}) == "找到 0 篇相关文章"
    assert len(notifier.cards) == 3


def test_card_action_library_status_and_removal(tmp_path) -> None:
    router, _ = make_router(tmp_path)
    document_id = add_document(tmp_path, "库文章")
    router.handle_card_action({"action": "save", "document_id": document_id})

    assert router.handle_card_action({"action": "kb"}) == "知识库共 1 篇"
    assert router.handle_card_action({"action": "favorites"}) == "收藏夹共 1 篇"
    assert "documents_by_status" in router.handle_card_action({"action": "status"})

    assert (
        router.handle_card_action({"action": "kb_remove", "document_id": document_id})
        == "已移出知识库。"
    )
    assert (
        router.handle_card_action({"action": "kb_remove", "document_id": document_id})
        == "这篇不在知识库里。"
    )
    assert router.handle_card_action({"action": "kb_remove_cancel"}) == "已取消，文章仍在知识库里。"


def test_card_action_qa_common_runs_in_background(tmp_path) -> None:
    router, notifier = make_router(tmp_path)
    calls = background_spy(router)

    assert router.handle_card_action({"action": "qa_common", "question": "为什么"}) == "正在用常识回答…"
    assert calls == [("answer_from_model", ("为什么",))]
    assert notifier.texts == []


# ---------- 问答 ----------


def test_answer_from_kb_supported_and_unsupported(tmp_path, monkeypatch) -> None:
    router, notifier = make_router(tmp_path)
    conn = router.db()

    monkeypatch.setattr(qa, "answer_question", lambda *a, **k: {"supported": False, "related": []})
    router.answer_from_kb(conn, "没有依据的问题")
    assert "没有依据" in notifier.cards[0]["header"]["title"]["content"]

    monkeypatch.setattr(
        qa,
        "answer_question",
        lambda *a, **k: {
            "supported": True,
            "answer": "结论是 A",
            "sources": [{"title": "某篇", "url": "https://example.com/a"}],
        },
    )
    router.answer_from_kb(conn, "有依据的问题")
    text = notifier.texts[-1]
    assert "结论是 A" in text and "依据：" in text and "https://example.com/a" in text


def test_answer_from_kb_failure_is_soft(tmp_path, monkeypatch) -> None:
    router, notifier = make_router(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("llm down")

    monkeypatch.setattr(qa, "answer_question", boom)
    router.answer_from_kb(router.db(), "问题")
    assert "问答暂时不可用" in notifier.texts[-1]


def test_answer_from_model_marks_missing_evidence(tmp_path, monkeypatch) -> None:
    router, notifier = make_router(tmp_path)
    monkeypatch.setattr(qa, "answer_from_model", lambda *a, **k: "答案是 42")
    router.answer_from_model("答案是什么")
    text = notifier.texts[-1]
    assert "没有知识库依据" in text and "答案是 42" in text


def test_answer_from_model_failure_is_soft(tmp_path, monkeypatch) -> None:
    router, notifier = make_router(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("network")

    monkeypatch.setattr(qa, "answer_from_model", boom)
    router.answer_from_model("问题")
    assert "回答失败" in notifier.texts[-1]


def test_topic_search_sends_card_and_handles_failure(tmp_path, monkeypatch) -> None:
    router, notifier = make_router(tmp_path)
    monkeypatch.setattr(
        "ai_radar.jobs.router.search_documents",
        lambda *a, **k: SimpleNamespace(
            local=[], external=[], elapsed_ms=0, note=""
        ),
    )
    router.topic_search("量子计算")
    assert notifier.cards

    def boom(*args, **kwargs):
        raise RuntimeError("network")

    monkeypatch.setattr("ai_radar.jobs.router.search_documents", boom)
    router.topic_search("量子计算")
    assert "检索失败" in notifier.texts[-1]


# ---------- 自然语言意图 ----------


def test_natural_language_chat_and_unknown_intent(tmp_path, monkeypatch) -> None:
    router, notifier = make_router(tmp_path)

    monkeypatch.setattr("ai_radar.jobs.router.parse_intent", lambda *a, **k: {"intent": "chat"})
    router.handle_natural_language("在吗")
    assert "我在。" in notifier.texts[-1]

    monkeypatch.setattr("ai_radar.jobs.router.parse_intent", lambda *a, **k: {"intent": "other"})
    router.handle_natural_language("???")
    assert notifier.texts[-1] == HELP_TEXT


def test_natural_language_unparsed_intent(tmp_path, monkeypatch) -> None:
    router, notifier = make_router(tmp_path)
    monkeypatch.setattr("ai_radar.jobs.router.parse_intent", lambda *a, **k: None)
    router.handle_natural_language("嗯？")
    assert "没太理解" in notifier.texts[-1]


def test_natural_language_sets_and_clears_theme(tmp_path, monkeypatch) -> None:
    router, notifier = make_router(tmp_path)
    monkeypatch.setattr(
        "ai_radar.jobs.router.parse_intent",
        lambda *a, **k: {
            "intent": "theme",
            "keywords": ["推理", "加速"],
            "dimension": "3 推理与加速",
            "days": 7,
        },
    )
    router.handle_natural_language("最近多推点推理加速的内容")
    assert "已设定临时主题" in notifier.texts[-1]

    conn = connect(tmp_path / "test.db")
    assert repo.get_theme_preference(conn) is not None
    conn.close()

    monkeypatch.setattr("ai_radar.jobs.router.parse_intent", lambda *a, **k: {"intent": "theme_clear"})
    router.handle_natural_language("取消主题")
    assert "已取消临时主题" in notifier.texts[-1]


def test_natural_language_library_renders_card(tmp_path, monkeypatch) -> None:
    router, notifier = make_router(tmp_path)
    monkeypatch.setattr(
        "ai_radar.jobs.router.parse_intent",
        lambda *a, **k: {
            "intent": "library",
            "scope": "favorites",
            "filters": {"dimension": "4 Agent 与应用"},
        },
    )
    router.handle_natural_language("我收藏的 agent 文章")
    assert notifier.cards[0]["header"]["title"]["content"].startswith("收藏夹")


def test_natural_language_topic_searches_in_background(tmp_path, monkeypatch) -> None:
    router, notifier = make_router(tmp_path)
    calls = background_spy(router)
    monkeypatch.setattr(
        "ai_radar.jobs.router.parse_intent",
        lambda *a, **k: {"intent": "topic", "query": "强化学习"},
    )
    router.handle_natural_language("帮我找找强化学习")
    assert calls == [("topic_search", ("强化学习",))]


def test_natural_language_question_uses_kb(tmp_path, monkeypatch) -> None:
    router, notifier = make_router(tmp_path)
    monkeypatch.setattr(
        "ai_radar.jobs.router.parse_intent",
        lambda *a, **k: {"intent": "question", "query": "RSI 的结论", "keywords": ["RSI"]},
    )
    monkeypatch.setattr(
        qa,
        "answer_question",
        lambda *a, **k: {
            "supported": True,
            "answer": "结论是 X",
            "sources": [{"title": "RSI", "url": "https://example.com/rsi"}],
        },
    )
    router.handle_natural_language("RSI 那篇的结论是什么")
    text = notifier.texts[-1]
    assert "结论是 X" in text and "https://example.com/rsi" in text


def test_natural_language_error_is_soft(tmp_path, monkeypatch) -> None:
    router, notifier = make_router(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("llm down")

    monkeypatch.setattr("ai_radar.jobs.router.parse_intent", boom)
    router.handle_natural_language("随便说点什么")
    assert "刚才没处理好" in notifier.texts[-1]


# ---------- 飞书事件 ----------


def test_event_message_ignores_unusable_messages(tmp_path) -> None:
    router, notifier = make_router(tmp_path)
    router.on_event_message(SimpleNamespace(event=None))
    router.on_event_message(make_event(sender_type="app"))
    router.on_event_message(make_event(open_id=None))
    router.on_event_message(make_event(message_type="image"))
    assert notifier.texts == []


def test_event_message_rejects_unknown_sender(tmp_path) -> None:
    router, notifier = make_router(tmp_path)
    router.on_event_message(make_event(open_id="ou_other"))
    assert "你的 open_id 是：ou_other" in notifier.texts[0]


def test_event_message_processes_text(tmp_path) -> None:
    router, notifier = make_router(tmp_path)
    router.on_event_message(make_event())
    assert "documents_by_status" in notifier.texts[0]


def test_event_message_ignores_broken_content(tmp_path) -> None:
    router, notifier = make_router(tmp_path)
    event = make_event()
    event.event.message.content = "not-json"
    router.on_event_message(event)
    assert notifier.texts == []


# ---------- 基础设施 ----------


def test_db_connection_is_thread_local(tmp_path) -> None:
    """回归：跨线程复用 sqlite 连接会抛 ProgrammingError，导致消息静默失败。"""
    router, _ = make_router(tmp_path)
    main_conn = router.db()
    seen: dict = {}

    def work() -> None:
        seen["conn"] = router.db()
        seen["rows"] = router.db().execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()

    assert seen["conn"] is not main_conn
    assert seen["rows"] == 0
