from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    # 调度线程和长连接线程可能同时写库，WAL 下用 busy_timeout 兜住短暂的写锁。
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """老库没有迁移框架，列不存在时补上即可（SQLite 的 ADD COLUMN 是幂等的用法）。"""
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _unique_index_columns(conn: sqlite3.Connection, table: str) -> list[list[str]]:
    """表上所有唯一索引的列组合，用来判断旧库是不是「日期唯一」那一版。"""
    result: list[list[str]] = []
    for index in conn.execute(f"PRAGMA index_list({table})"):
        if not index["unique"]:
            continue
        columns = [
            str(row["name"]) for row in conn.execute(f"PRAGMA index_info('{index['name']}')")
        ]
        result.append(columns)
    return result


def _migrate_digest_uniqueness(conn: sqlite3.Connection) -> None:
    """旧库的 digests 是「日期唯一」，周一只存得下周报或日报中的一个。

    SQLite 改不了已有约束，只能重建表；id 原样保留，digest_items / topics 的外键不受影响。
    """
    if ["digest_date"] not in _unique_index_columns(conn, "digests"):
        return
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript(
            """
            BEGIN;
            CREATE TABLE digests_migr (
              id            INTEGER PRIMARY KEY AUTOINCREMENT,
              digest_date   TEXT NOT NULL,
              created_at    TEXT NOT NULL,
              sent_at       TEXT,
              status        TEXT NOT NULL,
              kind          TEXT NOT NULL DEFAULT 'daily',
              stats_json    TEXT,
              UNIQUE(digest_date, kind)
            );
            INSERT INTO digests_migr(id, digest_date, created_at, sent_at, status, kind, stats_json)
              SELECT id, digest_date, created_at, sent_at, status, kind, stats_json FROM digests;
            DROP TABLE digests;
            ALTER TABLE digests_migr RENAME TO digests;
            COMMIT;
            """
        )
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _add_column(conn, "digests", "kind", "kind TEXT NOT NULL DEFAULT 'daily'")
    _add_column(conn, "digest_items", "section", "section TEXT NOT NULL DEFAULT 'brief'")
    _add_column(conn, "topics", "dimension", "dimension TEXT")
    _add_column(conn, "topics", "event_date", "event_date TEXT")
    _add_column(conn, "topics", "event_key", "event_key TEXT")
    _add_column(conn, "topics", "first_date", "first_date TEXT")
    _add_column(conn, "topics", "day_index", "day_index INTEGER NOT NULL DEFAULT 1")
    _add_column(conn, "topics", "new_count", "new_count INTEGER NOT NULL DEFAULT 0")
    _migrate_digest_uniqueness(conn)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', '7') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    )
    conn.commit()
