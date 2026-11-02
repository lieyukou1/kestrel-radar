from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from ..adapters.notify.base import Notifier
from ..config import AppConfig
from ..pipeline.digest import build_digest
from ..store import repo
from ..store.db import connect, init_db
from ..util import parse_iso, utcnow
from .collect import run_collect

# 本机 Windows 的 Python 默认不带 tzdata，且中国无夏令时，固定 UTC+8 足够。
LOCAL_TZ = timezone(timedelta(hours=8))
TICK_SECONDS = 30
COLLECT_INTERVAL = timedelta(minutes=55)
DIGEST_HOUR = 10
# 周一发周报（论文 + 仓库），周二到周五发日报；周末不推。
WEEKLY_WEEKDAY = 0
FAILED_RETRY_INTERVAL = timedelta(minutes=10)


def _log(message: str) -> None:
    print(f"[scheduler] {message}", flush=True)


class Scheduler:
    """单进程内的轻量调度：每小时采集 + 每天 10:00 推送 + 启动补跑。

    跑在独立线程里，自己持有 SQLite 连接（sqlite3 连接不能跨线程共享）。
    """

    def __init__(
        self,
        config: AppConfig,
        notifier: Notifier,
        *,
        collect_interval: timedelta = COLLECT_INTERVAL,
        digest_hour: int = DIGEST_HOUR,
    ) -> None:
        self.config = config
        self.notifier = notifier
        self.collect_interval = collect_interval
        self.digest_hour = digest_hour
        self._last_collect_attempt: datetime | None = None
        self._last_push_attempt: datetime | None = None
        self._stop = threading.Event()

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._run, name="ai-radar-scheduler", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        conn = connect(self.config.db_path)
        init_db(conn)
        self._catch_up(conn)
        while not self._stop.wait(TICK_SECONDS):
            try:
                self._tick(conn)
            except Exception as exc:  # 调度线程不能因单次异常退出
                _log(f"tick failed: {type(exc).__name__}: {exc}")

    def _catch_up(self, conn) -> None:
        _log("catch-up start")
        self._maybe_collect(conn)
        self._maybe_digest(conn, catch_up=True)

    def _tick(self, conn) -> None:
        self._maybe_collect(conn)
        self._maybe_digest(conn)

    def _maybe_collect(self, conn) -> None:
        now = utcnow()
        if (
            self._last_collect_attempt is not None
            and now - self._last_collect_attempt < self.collect_interval
        ):
            return

        last = repo.last_run(conn, "collect")
        last_at = parse_iso(last["finished_at"]) if last else None
        if last_at is not None and now - last_at < self.collect_interval:
            return

        self._last_collect_attempt = now
        _log("collect start")
        stats = run_collect(conn, self.config)
        _log(
            f"collect done items={stats.get('items', 0)} errors={len(stats.get('errors') or [])}"
        )

    def _maybe_digest(self, conn, *, catch_up: bool = False) -> None:
        now = datetime.now(LOCAL_TZ)
        today = now.date().isoformat()
        # 周一是周报 + 日报两班，其余工作日只有日报；周末不发。
        for kind in self._digest_kinds(now):
            self._maybe_digest_kind(conn, today=today, kind=kind, now=now, catch_up=catch_up)

    def _maybe_digest_kind(
        self, conn, *, today: str, kind: str, now: datetime, catch_up: bool
    ) -> None:
        row = repo.get_digest(conn, today, kind)
        if row is not None and row["status"] == "sent":
            return

        # 上次构建中途崩溃：超过重试间隔后清掉重建。
        if row is not None and row["status"] == "building":
            created = parse_iso(row["created_at"])
            if created is None or utcnow() - created > FAILED_RETRY_INTERVAL:
                repo.reset_digest(conn, int(row["id"]))
                row = None

        failed = row is not None and row["status"] == "failed"
        if not failed and now.hour < self.digest_hour:
            return

        attempt_now = utcnow()
        if (
            failed
            and self._last_push_attempt is not None
            and attempt_now - self._last_push_attempt < FAILED_RETRY_INTERVAL
        ):
            return

        self._last_push_attempt = attempt_now
        _log(f"digest start date={today} kind={kind} failed_retry={failed} catch_up={catch_up}")
        try:
            result = build_digest(
                conn,
                self.config,
                notifier=self.notifier,
                digest_date=today,
                kind=kind,
            )
            _log(
                f"digest done kind={kind} count={result.get('count', 0)} "
                f"theme={result.get('theme', '')!r} "
                f"summaries={result.get('summaries', 0)} skipped={result.get('skipped', False)}"
            )
        except Exception as exc:
            _log(f"digest failed kind={kind}: {type(exc).__name__}: {exc}")

    @staticmethod
    def _digest_kinds(now: datetime) -> list[str]:
        """周一先周报后日报，周二~周五日报，周末不推。"""
        if now.weekday() == WEEKLY_WEEKDAY:
            return ["weekly", "daily"]
        if now.weekday() >= 5:
            return []
        return ["daily"]
