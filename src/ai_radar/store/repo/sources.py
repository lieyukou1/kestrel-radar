from __future__ import annotations

import sqlite3

from ...util import content_hash, iso, utcnow
from ._common import _dumps


def upsert_source(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    kind: str,
    name: str,
    authority: str,
    region: str,
    config: dict,
) -> None:
    conn.execute(
        """
        INSERT INTO sources(id, kind, name, authority, region, config_json)
        VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            kind = excluded.kind,
            name = excluded.name,
            authority = excluded.authority,
            region = excluded.region,
            config_json = excluded.config_json
        """,
        (source_id, kind, name, authority, region, _dumps(config)),
    )
    conn.commit()

def start_run(conn: sqlite3.Connection, job: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs(job, started_at, status) VALUES(?, ?, 'running')",
        (job, iso(utcnow())),
    )
    conn.commit()
    return int(cur.lastrowid)

def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    stats: dict | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE runs SET finished_at = ?, status = ?, stats_json = ?, error = ? WHERE id = ?",
        (iso(utcnow()), status, _dumps(stats or {}), error, run_id),
    )
    conn.commit()

def insert_raw_item(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    external_id: str,
    url: str,
    title: str | None,
    author: str | None,
    published_at: str | None,
    payload: dict,
) -> int:
    now = iso(utcnow())
    digest = content_hash(payload)
    conn.execute(
        """
        INSERT OR IGNORE INTO raw_items(
            source_id, external_id, url, title, author, published_at,
            fetched_at, content_hash, payload_json
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (source_id, external_id, url, title, author, published_at, now, digest, _dumps(payload)),
    )
    row = conn.execute(
        "SELECT id FROM raw_items WHERE source_id = ? AND external_id = ?",
        (source_id, external_id),
    ).fetchone()
    conn.commit()
    return int(row["id"])

def last_run(conn: sqlite3.Connection, job: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM runs WHERE job = ? AND status = 'success' ORDER BY id DESC LIMIT 1",
        (job,),
    ).fetchone()
