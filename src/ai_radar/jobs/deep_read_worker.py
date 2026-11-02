from __future__ import annotations

import threading

from ..adapters.notify.base import Notifier
from ..adapters.notify.feishu import build_l3_card
from ..config import AppConfig
from ..pipeline.deep_read import deep_read_document
from ..store import repo
from ..store.db import connect, init_db
from ..util import iso, utcnow

POLL_SECONDS = 20


def _log(message: str) -> None:
    print(f"[deep-read] {message}", flush=True)


def _failure_message(document, exc: Exception, discussion_url: str | None) -> str:
    """把原始异常翻译成能看懂的失败原因，并保留原文与讨论两个入口。

    最常见的失败是付费墙/反爬（403）与链接失效（404），直接甩 HTTP 异常
    对用户没有意义。
    """
    detail = str(exc)
    if "403" in detail or "402" in detail:
        reason = "原文返回 403/402，通常是付费墙或站点拒绝抓取。"
    elif "404" in detail:
        reason = "原文链接 404，可能已经失效。"
    elif "timed out" in detail.lower() or "timeout" in detail.lower():
        reason = "抓取原文超时，可以稍后重试。"
    elif "pdf" in detail.lower():
        reason = "原文 HTML 与 PDF 都没抓下来。"
    else:
        reason = "原文抓取失败，可以稍后重试。"

    lines = [f"精读失败：{document['title'][:60]}", reason]
    lines.append(f"原文：{document['url']}")
    if discussion_url:
        lines.append(f"社区讨论：{discussion_url}")
    return "\n".join(lines)


def process_next(conn, config: AppConfig, notifier: Notifier) -> bool:
    """处理一篇排队中的文章；返回是否做了工作。"""
    queue = repo.list_deep_read_queue(conn, limit=1)
    if not queue:
        return False

    document = queue[0]
    document_id = int(document["id"])
    run_id = repo.start_run(conn, "deep_read")
    _log(f"start document_id={document_id} title={document['title'][:60]}")
    try:
        result = deep_read_document(conn, config, document)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        repo.finish_run(conn, run_id, status="failed", error=message)
        # 标记 failed 而不是 read：read 表示「已读」，会把失败记录从统计里抹掉。
        repo.mark_status(conn, [document_id], "failed")
        _log(f"failed document_id={document_id} {message}")
        try:
            notifier.send_text(
                _failure_message(
                    document,
                    exc,
                    repo.hn_discussion_url(conn, document_id),
                )
            )
        except Exception:
            pass
        return True

    repo.finish_run(
        conn,
        run_id,
        status="success",
        stats={
            "document_id": document_id,
            "chars": result.chars,
            "chunks": result.chunks,
            "claims": result.claims,
            "local_path": result.local_path,
            "finished_at": iso(utcnow()),
        },
    )
    _log(
        f"done document_id={document_id} chars={result.chars} "
        f"chunks={result.chunks} claims={result.claims}"
    )
    fresh = repo.get_document(conn, document_id)
    if fresh is not None:
        notifier.send_card(build_l3_card(fresh, result.card))
    return True


class DeepReadWorker:
    def __init__(
        self,
        config: AppConfig,
        notifier: Notifier,
        *,
        interval: float = POLL_SECONDS,
    ) -> None:
        self.config = config
        self.notifier = notifier
        self.interval = interval
        self._stop = threading.Event()

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._run, name="ai-radar-deep-read", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        conn = connect(self.config.db_path)
        init_db(conn)
        while not self._stop.wait(self.interval):
            try:
                while process_next(conn, self.config, self.notifier):
                    if self._stop.is_set():
                        break
            except Exception as exc:  # 单次异常不能让线程退出
                _log(f"worker failed: {type(exc).__name__}: {exc}")
