"""飞书常驻服务：只做 SDK 接线与进程生命周期，业务分派在 `router.py`。"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from ..config import AppConfig
from ..notify_factory import build_notifier
from ..store.db import connect, init_db
from .deep_read_worker import DeepReadWorker
from .feishu_poller import MessagePoller
from .router import MessageRouter
from .scheduler import Scheduler


def _backfill_vectors(config: AppConfig) -> None:
    """后台为历史精读文章补算向量，避免首次点击「相关」时阻塞事件回调。"""
    from ..pipeline.embedding import backfill_vectors

    conn = connect(config.db_path)
    init_db(conn)
    try:
        result = backfill_vectors(conn)
        print(f"[serve] vector backfill {result}", flush=True)
    except Exception as exc:
        print(f"[serve] vector backfill failed: {type(exc).__name__}: {exc}", flush=True)


def _acquire_single_instance(config: AppConfig):
    """同一个 app 只允许一条长连接：多个实例会互相抢消息，表现为“发了没反应”。"""
    lock_path = config.db_path.parent / "serve.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = Path(lock_path).open("a+")
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def _watch_loop(interval: float = 60.0, timeout: float = 15.0) -> None:
    """看住 lark 的事件循环：它一旦被阻塞，长连接会“看着还连着”但收不到任何消息。"""
    from lark_oapi.ws.client import loop as ws_loop

    while True:
        time.sleep(interval)
        done = threading.Event()
        try:
            ws_loop.call_soon_threadsafe(done.set)
        except RuntimeError as exc:
            print(f"[serve] ws loop closed: {exc}", flush=True)
            continue
        if done.wait(timeout):
            print("[serve] ws loop alive", flush=True)
        else:
            print(f"[serve] ws loop BLOCKED >{int(timeout)}s", flush=True)


def _start_background(config: AppConfig, notifier) -> tuple[Scheduler, DeepReadWorker]:
    """调度器、精读 worker、向量回填与渠道无关，三种渠道都需要。"""
    init_db(connect(config.db_path))
    scheduler = Scheduler(config, notifier)
    scheduler.start()
    worker = DeepReadWorker(config, notifier)
    worker.start()
    threading.Thread(
        target=_backfill_vectors,
        args=(config,),
        name="ai-radar-vectors",
        daemon=True,
    ).start()
    return scheduler, worker


def _run_headless(config: AppConfig, notifier, channel: str) -> None:
    """单向渠道：只跑定时任务，不起飞书长连接。"""
    scheduler, worker = _start_background(config, notifier)
    print(f"[serve] headless channel={channel} started", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.stop()
        worker.stop()


def run_bot(config: AppConfig, *, channel: str = "feishu-notice") -> None:
    """启动常驻服务。

    feishu 发完整卡片；feishu-notice 只发一条带网页链接的摘要（交互能力不变）；
    其余渠道不起长连接，只跑定时任务。
    """
    if channel not in ("feishu", "feishu-notice"):
        _run_headless(config, build_notifier(config, channel), channel)
        return
    if config.feishu is None:
        raise RuntimeError(
            "飞书凭据缺失：请在 .env 设置 FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_USER_OPEN_ID"
        )
    lock = _acquire_single_instance(config)
    if lock is None:
        raise SystemExit(
            "已有一个 ai_radar serve 在运行（data/serve.lock 被占用）。"
            "请先结束旧进程，否则消息会被两个实例争抢。"
        )

    import lark_oapi as lark
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        CallBackToast,
        P2CardActionTriggerResponse,
    )
    from lark_oapi.ws import Client as WSClient

    notifier = build_notifier(config, channel)
    router = MessageRouter(config, notifier)

    def on_card_action(data) -> P2CardActionTriggerResponse:
        value = {}
        if data.event and data.event.action and data.event.action.value:
            value = data.event.action.value
        toast_text = router.handle_card_action(value)

        response = P2CardActionTriggerResponse()
        toast = CallBackToast()
        toast.type = "info"
        toast.content = toast_text
        response.toast = toast
        return response

    def on_message(data) -> None:
        try:
            router.on_event_message(data)
        except Exception as exc:  # 任何异常都要留痕，否则表现为“发了没反应”
            import traceback

            traceback.print_exc()
            print(f"[feishu] handler failed: {type(exc).__name__}: {exc}", flush=True)

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_card_action_trigger(on_card_action)
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )
    client = WSClient(
        config.feishu.app_id,
        config.feishu.app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.DEBUG
        if os.getenv("AI_RADAR_LARK_DEBUG")
        else lark.LogLevel.INFO,
    )
    scheduler, worker = _start_background(config, notifier)
    threading.Thread(target=_watch_loop, name="ai-radar-watchdog", daemon=True).start()
    poller = MessagePoller(config, notifier, router.process_text)
    poller.start()
    print("[serve] scheduler + deep-read worker + vector backfill started", flush=True)
    try:
        client.start()
    finally:
        poller.stop()
        scheduler.stop()
        worker.stop()
