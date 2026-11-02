"""消息路由：命令、卡片动作、自然语言三条入口统一在这里落地。

`serve.py` 只负责飞书 SDK 的接线（长连接、回调响应），业务分派全部交给本类，
这样路由可以脱离飞书 SDK 单测。
"""

from __future__ import annotations

import json
import threading
from datetime import timedelta

from ..adapters.notify.feishu import (
    KB_PAGE_SIZE,
    build_compare_card,
    build_detail_card,
    build_kb_card,
    build_qa_fallback_card,
    build_reason_card,
    build_related_card,
    build_remove_confirm_card,
    build_topic_card,
)
from ..config import AppConfig
from ..pipeline import qa
from ..pipeline.compare import compare_documents, find_related
from ..pipeline.intent import parse_intent
from ..pipeline.interactions import DISLIKE_REASONS, format_status, handle_action
from ..pipeline.preferences import (
    format_preferences,
    pause_dimension,
    pause_source,
    resolve_dimension,
    resume_dimension,
    resume_source,
)
from ..pipeline.search import search_documents
from ..store import repo
from ..store.db import connect, init_db
from ..util import iso, utcnow

HELP_TEXT = """AI Radar 命令
/status                   查看采集与推送状态
/kb                       知识库：已精读 + 已收藏
/favorites                只看收藏（知识库子集）
/prefs                    查看偏好、权重与负反馈
/topic <关键词>            按话题检索全库，本地不足时补 arXiv / HN / GitHub
/related <文章ID>          找知识库里的相关文章
/compare <ID1> <ID2>      对比两篇已精读文章
/pause dimension 4        暂停某个维度
/resume dimension 4       恢复某个维度
/pause source hackernews  暂停某个来源
/resume source hackernews 恢复某个来源

卡片按钮：详情 / 精读 / 收藏 / 不感兴趣 / 相关 / 对比 / 围绕此话题找。

也可以用自然语言，例如：
  知识库里多模态的论文       → 直接筛选知识库
  我上周收藏的东西           → 打开收藏夹并限定时间
  最近多推点推理加速的内容    → 设定 7 天临时主题（说“取消主题”清除）
  RSI 那篇的结论是什么       → 基于知识库回答；没有依据会让你选站外检索或常识回答"""


class MessageRouter:
    """把一条消息/一次点击翻译成飞书发送动作。"""

    def __init__(self, config: AppConfig, notifier) -> None:
        self.config = config
        self.notifier = notifier
        self._local = threading.local()
        # 事件通道与会话轮询可能同时看到同一条消息，按 message_id 去重。
        self._seen: set[str] = set()
        self._seen_lock = threading.Lock()

    # ---------- 基础设施 ----------

    def db(self):
        """sqlite 连接不能跨线程共享；事件线程、轮询线程各持有一份。"""
        current = getattr(self._local, "conn", None)
        if current is None:
            current = connect(self.config.db_path)
            init_db(current)
            self._local.conn = current
        return current

    def _background(self, target, *args, name: str) -> None:
        threading.Thread(target=target, args=args, name=name, daemon=True).start()

    # ---------- 入口 ----------

    def process_text(self, text: str, message_id: str = "") -> None:
        """命令与自然语言的统一入口：长连接事件和会话轮询都走这里。"""
        if message_id:
            with self._seen_lock:
                if message_id in self._seen:
                    return
                self._seen.add(message_id)
                if len(self._seen) > 500:
                    self._seen.clear()

        command = text.split()[0].lower() if text else ""
        print(f"[feishu] recv {command or '(nl)'} · {text[:60]}", flush=True)
        conn = self.db()
        if command == "/favorites":
            self.notifier.send_text(self.dispatch({"type": "favorites_card"}))
        elif command == "/kb":
            self.notifier.send_text(self.dispatch({"type": "kb_card"}))
        elif command == "/status":
            self.notifier.send_text(format_status(conn))
        elif command == "/prefs":
            self.notifier.send_text(format_preferences(conn, self.config))
        elif command == "/topic":
            query = " ".join(text.split()[1:]).strip()
            if len(query) < 2:
                self.notifier.send_text("用法：/topic <关键词>，例如 /topic 强化学习")
            else:
                self.notifier.send_text(self.dispatch({"type": "topic_card", "query": query}))
        elif command == "/related":
            parts = text.split()
            if len(parts) < 2 or not parts[1].isdigit():
                self.notifier.send_text("用法：/related <文章ID>")
            else:
                self.respond({"type": "related_card", "document_id": int(parts[1])})
        elif command == "/compare":
            parts = text.split()
            if len(parts) < 3 or not parts[1].isdigit() or not parts[2].isdigit():
                self.notifier.send_text("用法：/compare <ID1> <ID2>")
            else:
                self.respond(
                    {
                        "type": "compare_card",
                        "left_id": int(parts[1]),
                        "right_id": int(parts[2]),
                    }
                )
        elif command in ("/pause", "/resume"):
            self._toggle(text, command)
        elif command == "/help":
            self.notifier.send_text(HELP_TEXT)
        elif text.startswith("/"):
            self.notifier.send_text(f"未识别的命令。\n\n{HELP_TEXT}")
        else:
            self._background(self.handle_natural_language, text, name="ai-radar-nl")

    def on_event_message(self, data) -> None:
        """长连接消息事件：解析 SDK 对象后交给 process_text。"""
        sender = data.event.sender if data.event else None
        message = data.event.message if data.event else None
        print(
            "[feishu] event message"
            f" sender_type={getattr(sender, 'sender_type', None)}"
            f" type={getattr(message, 'message_type', None)}",
            flush=True,
        )
        if sender is None or message is None:
            return
        if sender.sender_type == "app":
            return
        if sender.sender_id is None or not sender.sender_id.open_id:
            return
        open_id = sender.sender_id.open_id
        if self.config.feishu is not None and open_id != self.config.feishu.open_id:
            # 日志里不留完整 open_id（它是账号标识），只保留足以排障的前缀。
            print(f"[feishu] unknown sender open_id={open_id[:6]}***", flush=True)
            self.notifier.send_text_to(
                open_id,
                f"你的 open_id 是：{open_id}\n请填入 .env 的 FEISHU_USER_OPEN_ID 后重启。",
            )
            return
        if message.message_type != "text":
            print(f"[feishu] skip non-text {message.message_type}", flush=True)
            return
        text = _text_of(message)
        if not text:
            return
        self.process_text(text, str(getattr(message, "message_id", "") or ""))

    def handle_card_action(self, value: dict) -> str:
        """卡片按钮：返回 toast 文案。"""
        print(f"[feishu] card action {value.get('action')}", flush=True)
        return self.dispatch(handle_action(self.db(), value))

    # ---------- 分派 ----------

    def dispatch(self, result: dict) -> str:
        if result.get("type") == "topic_card":
            query = str(result["query"])
            self._background(self.topic_search, query, name="ai-radar-topic")
            return f"正在检索「{query[:24]}」…"
        if result.get("type") == "qa_common":
            question = str(result["question"])
            self._background(self.answer_from_model, question, name="ai-radar-qa")
            return "正在用常识回答…"
        return self.respond(result)

    def respond(self, result: dict) -> str:
        conn = self.db()
        if result.get("type") in ("kb_card", "favorites_card"):
            return self.render_library(result)
        if result.get("type") == "related_card":
            document = repo.get_document(conn, int(result["document_id"]))
            if document is None:
                self.notifier.send_text("文章不存在。")
                return "文章不存在"
            related = find_related(conn, int(document["id"]))
            self.notifier.send_card(build_related_card(document, related))
            return f"找到 {len(related)} 篇相关文章"
        if result.get("type") == "compare_card":
            left = repo.get_document(conn, int(result["left_id"]))
            right = repo.get_document(conn, int(result["right_id"]))
            if left is None or right is None:
                self.notifier.send_text("文章不存在。")
                return "文章不存在"
            try:
                comparison = compare_documents(conn, int(left["id"]), int(right["id"]))
            except ValueError as exc:
                self.notifier.send_text(str(exc))
                return str(exc)
            self.notifier.send_card(build_compare_card(left, right, comparison))
            return "已生成对比"
        if result.get("type") == "remove_confirm_card":
            document = repo.get_document(conn, int(result["document_id"]))
            if document is None:
                self.notifier.send_text("文章不存在。")
                return "文章不存在"
            self.notifier.send_card(build_remove_confirm_card(document))
            return "请确认移出"
        if result.get("type") == "detail_card":
            document = repo.get_document(conn, int(result["document_id"]))
            if document is None:
                self.notifier.send_text("文章不存在。")
                return "文章不存在"
            summary = repo.find_latest_summary(conn, int(document["id"])) or {
                "abstract": document["abstract"] or ""
            }
            target = repo.discussion_target(conn, int(document["id"]))
            self.notifier.send_card(
                build_detail_card(
                    document,
                    summary,
                    result.get("digest_date"),
                    target[0] if target else None,
                    discussion_label=target[1] if target else "",
                    saved=repo.is_saved(conn, int(document["id"])),
                )
            )
            return "已打开详情"
        if result.get("type") == "reason_card":
            self.notifier.send_card(
                build_reason_card(
                    int(result["document_id"]),
                    result.get("digest_date"),
                    DISLIKE_REASONS,
                )
            )
            return "请选择原因"
        text = str(result.get("text") or "")
        self.notifier.send_text(text)
        return text[:60]

    # ---------- 知识库视图 ----------

    def render_library(self, result: dict, source_conn=None) -> str:
        """知识库 / 收藏夹共用：按筛选条件取一页并渲染卡片。

        source_conn 只允许传本线程建的连接（handle_natural_language 就是在自己的线程里
        建连再传进来）；跨线程传 conn 会抛 ProgrammingError。
        """
        db = source_conn or self.db()
        scope = "favorites" if result.get("type") == "favorites_card" else "kb"
        filters = {k: v for k, v in (result.get("filters") or {}).items() if v}
        window = filters.get("window")
        params = {
            "only_saved": scope == "favorites",
            "dimension": filters.get("dimension"),
            "kind": filters.get("kind"),
            "status": filters.get("status"),
            "since": iso(utcnow() - timedelta(days=int(window))) if window else None,
        }
        counts = repo.kb_counts(db, **params)
        pages = max(1, (counts["total"] + KB_PAGE_SIZE - 1) // KB_PAGE_SIZE)
        page = min(max(int(result.get("page") or 0), 0), pages - 1)
        entries = repo.kb_entries(
            db,
            limit=KB_PAGE_SIZE,
            offset=page * KB_PAGE_SIZE,
            **params,
        )
        facets = repo.kb_facets(db, only_saved=params["only_saved"])
        title = "收藏夹" if scope == "favorites" else "知识库"
        self.notifier.send_card(
            build_kb_card(
                entries,
                counts,
                title=title,
                scope=scope,
                filters=filters,
                page=page,
                pages=pages,
                facets=facets,
            )
        )
        return f"{title}共 {counts['total']} 篇"

    # ---------- 自然语言 ----------

    def handle_natural_language(self, text: str) -> None:
        """非命令消息：先解析意图，再复用知识库筛选 / 检索 / 主题偏好。"""
        worker_conn = connect(self.config.db_path)
        init_db(worker_conn)
        try:
            intent = parse_intent(worker_conn, self.config, text)
            if intent is None:
                print(f"[nl] intent=none text={text[:40]}", flush=True)
                self.notifier.send_text(
                    "没太理解这句话。可以试试：\n"
                    "· 知识库里多模态的论文\n"
                    "· 我上周收藏的东西\n"
                    "· 最近多推点推理加速的内容\n"
                    "也支持 /kb、/topic 关键词。"
                )
                return
            kind = intent["intent"]
            print(f"[nl] intent={kind} text={text[:40]}", flush=True)
            if kind == "library":
                self.render_library(
                    {
                        "type": "favorites_card"
                        if intent.get("scope") == "favorites"
                        else "kb_card",
                        "filters": intent.get("filters") or {},
                        "page": 0,
                    },
                    worker_conn,
                )
            elif kind == "topic":
                self.dispatch({"type": "topic_card", "query": intent["query"]})
            elif kind == "theme":
                payload = repo.set_theme_preference(
                    worker_conn,
                    keywords=intent.get("keywords") or [],
                    dimension=intent.get("dimension"),
                    days=int(intent.get("days") or 7),
                    raw=text,
                )
                parts = []
                if payload["keywords"]:
                    parts.append("关键词 " + "、".join(payload["keywords"]))
                if payload["dimension"]:
                    parts.append("维度 " + str(payload["dimension"]))
                self.notifier.send_text(
                    f"已设定临时主题：{'；'.join(parts)}；"
                    f"{str(payload['expires_at'])[:10]} 前生效。"
                    "说“取消主题”可清除，/prefs 可查看。"
                )
            elif kind == "theme_clear":
                repo.clear_theme_preference(worker_conn)
                self.notifier.send_text("已取消临时主题，恢复按常规信号推送。")
            elif kind == "chat":
                self.notifier.send_text("我在。想查知识库、找资料，或者设定推送主题，直接说就行。")
            elif kind == "question":
                self.answer_from_kb(
                    worker_conn,
                    intent["query"],
                    intent.get("keywords") or [],
                )
            else:
                self.notifier.send_text(HELP_TEXT)
        except Exception as exc:
            print(f"[nl] failed: {type(exc).__name__}: {exc}", flush=True)
            self.notifier.send_text("刚才没处理好，换个说法，或直接用 /kb、/topic。")
        finally:
            worker_conn.close()

    # ---------- 问答与检索 ----------

    def topic_search(self, query: str) -> None:
        """检索可能等到 10 秒，飞书回调只有 3 秒，所以放后台线程并把结果单独发一张卡。"""
        worker_conn = connect(self.config.db_path)
        try:
            init_db(worker_conn)
            outcome = search_documents(worker_conn, self.config, query)
            self.notifier.send_card(build_topic_card(query, outcome))
        except Exception as exc:
            self.notifier.send_text(f"检索失败：{type(exc).__name__}: {exc}")
        finally:
            worker_conn.close()

    def answer_from_kb(self, worker_conn, question: str, keywords=None) -> None:
        """有依据就基于知识库回答，没有依据就交给用户选「站外检索 / 常识回答」。"""
        try:
            result = qa.answer_question(
                worker_conn, self.config, question, keywords=keywords or []
            )
        except Exception as exc:
            print(f"[qa] failed: {type(exc).__name__}: {exc}", flush=True)
            self.notifier.send_text(
                f"问答暂时不可用（{type(exc).__name__}）。可以先用 /topic 关键词 检索。"
            )
            return
        if not result.get("supported"):
            self.notifier.send_card(
                build_qa_fallback_card(question, result.get("related") or [])
            )
            return
        lines = [str(result.get("answer") or "").strip(), "", "依据："]
        for index, source in enumerate(result.get("sources") or [], start=1):
            lines.append(f"{index}. {str(source['title'])[:60]}")
            lines.append(f"   {source['url']}")
        self.notifier.send_text("\n".join(lines))

    def answer_from_model(self, question: str) -> None:
        """用户明确选择「用常识回答」后，才在无依据的情况下让模型回答，并标注来源性质。"""
        worker_conn = connect(self.config.db_path)
        try:
            init_db(worker_conn)
            text = qa.answer_from_model(worker_conn, self.config, question)
            self.notifier.send_text(f"以下内容没有知识库依据，仅供参考：\n\n{text}")
        except Exception as exc:
            print(f"[qa] common failed: {type(exc).__name__}: {exc}", flush=True)
            self.notifier.send_text(
                f"回答失败（{type(exc).__name__}）。可以改用「站外检索」。"
            )
        finally:
            worker_conn.close()

    # ---------- 偏好开关 ----------

    def _toggle(self, text: str, command: str) -> None:
        parts = text.split()
        if len(parts) < 3 or parts[1].lower() not in ("source", "dimension"):
            self.notifier.send_text("用法：/pause dimension 4 或 /pause source hackernews")
            return
        kind = parts[1].lower()
        target = " ".join(parts[2:]).strip()
        pausing = command == "/pause"
        conn = self.db()
        if kind == "source":
            if pausing:
                pause_source(conn, target)
                self.notifier.send_text(f"已暂停来源：{target}")
            else:
                resume_source(conn, target)
                self.notifier.send_text(f"已恢复来源：{target}")
            return
        tag = resolve_dimension(conn, target)
        if tag is None:
            self.notifier.send_text(f"未找到维度：{target}，可用 /prefs 查看")
        elif pausing:
            pause_dimension(conn, tag)
            self.notifier.send_text(f"已暂停维度：{tag}")
        else:
            resume_dimension(conn, tag)
            self.notifier.send_text(f"已恢复维度：{tag}")


def _text_of(message) -> str:
    try:
        return str(json.loads(message.content or "{}").get("text") or "").strip()
    except (json.JSONDecodeError, TypeError):
        return ""
