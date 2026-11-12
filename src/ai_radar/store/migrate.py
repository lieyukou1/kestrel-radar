"""旧数据布局迁移：data/ai_radar.db → data/domains/<id>/ai_radar.db。

领域包上线后每个领域一套库；老库只搬一次，搬之前 checkpoint WAL，
避免把 `-wal` 里的最新事务丢在旧路径。迁移失败直接抛错，不做半吊子拷贝。
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

DEFAULT_DOMAIN = "ai"


def _checkpoint(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    finally:
        conn.close()


def migrate_legacy_layout(root: Path, domain_id: str = DEFAULT_DOMAIN) -> bool:
    """把旧版单库布局搬到领域目录；已迁移或没有旧库时返回 False。"""
    if domain_id != DEFAULT_DOMAIN:
        return False
    old_db = Path(root) / "data" / "ai_radar.db"
    new_db = Path(root) / "data" / "domains" / domain_id / "ai_radar.db"
    if not old_db.exists() or new_db.exists():
        return False
    new_db.parent.mkdir(parents=True, exist_ok=True)
    _checkpoint(old_db)
    shutil.move(str(old_db), str(new_db))
    for suffix in ("-wal", "-shm"):
        sidecar = old_db.with_name(old_db.name + suffix)
        if sidecar.exists():
            sidecar.unlink()
    # outbox 是可再生的调试产物，顺手搬过去，避免「明明推过却看不到文件」。
    old_outbox = Path(root) / "data" / "outbox"
    new_outbox = Path(root) / "data" / "domains" / domain_id / "outbox"
    if old_outbox.exists() and not new_outbox.exists():
        shutil.move(str(old_outbox), str(new_outbox))
    return True
