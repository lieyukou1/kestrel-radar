from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from ai_radar.adapters.notify.fake import FakeNotifier
from ai_radar.adapters.sources.arxiv import ArxivAdapter
from ai_radar.adapters.sources.hackernews import HackerNewsAdapter
from ai_radar.config import AppConfig, load_config
from ai_radar.pipeline import digest as digest_mod
from ai_radar.pipeline.digest import build_digest
from ai_radar.pipeline.normalize import merge_item
from ai_radar.store import repo
from ai_radar.store.db import connect, init_db

FIXTURES = Path(__file__).parent / "fixtures"


def test_collect_normalize_and_digest(tmp_path: Path) -> None:
    base = load_config()
    config = AppConfig(
        root=base.root,
        network=base.network,
        sources=base.sources,
        weights=base.weights,
        db_path=tmp_path / "test.db",
        outbox_dir=tmp_path / "outbox",
    )
    conn = connect(config.db_path)
    init_db(conn)
    for source in config.sources:
        repo.upsert_source(
            conn,
            source_id=source.id,
            kind=source.kind,
            name=source.name,
            authority=source.authority,
            region=source.region,
            config=source.options,
        )

    arxiv_items = ArxivAdapter().parse((FIXTURES / "arxiv.xml").read_text(encoding="utf-8"))
    hn_items = HackerNewsAdapter().parse(
        json.loads((FIXTURES / "hackernews.json").read_text(encoding="utf-8"))
    )
    for item in [*arxiv_items, *hn_items]:
        merge_item(conn, item, "A")

    # arXiv 论文 + HN 指向同一篇 arXiv 的帖子合并；另一篇 HN 独立。
    documents = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    assert documents == 3

    now = datetime(2026, 10, 21, 12, tzinfo=UTC)
    notifier = FakeNotifier(config.outbox_dir)
    result = build_digest(
        conn,
        config,
        notifier=notifier,
        digest_date="2026-10-21",
        size=6,
        now=now,
    )

    # 日报只推中文媒体与论坛：论文不再每天占位，未聊过天的独立论文不进日报。
    assert result["count"] == 1
    outbox = Path(result["outbox"])
    assert outbox.exists()
    content = outbox.read_text(encoding="utf-8")
    assert "agent framework" in content

    # 周报才收论文与仓库，且用更长的成熟窗口。
    weekly = build_digest(
        conn,
        config,
        notifier=notifier,
        digest_date="2026-10-22",
        kind="weekly",
        now=now,
    )
    assert weekly["count"] >= 1
    weekly_content = Path(weekly["outbox"]).read_text(encoding="utf-8")
    assert "Mixture of Experts" in weekly_content

    again = build_digest(
        conn,
        config,
        notifier=notifier,
        digest_date="2026-10-21",
        size=6,
        now=now,
    )
    assert again["skipped"] is True

    # 即使状态被手动改回 observing（例如重推、重放），近 30 天推过的文档也不能再进候选。
    pushed_ids = [
        int(row["document_id"])
        for row in conn.execute(
            "SELECT document_id FROM digest_items di "
            "JOIN digests d ON d.id = di.digest_id WHERE d.digest_date = '2026-10-21'"
        )
    ]
    assert pushed_ids
    repo.mark_status(conn, pushed_ids, "observing")
    candidates = digest_mod.collect_candidates(conn, config, now=now)
    assert not ({int(entry["doc"]["id"]) for entry in candidates} & set(pushed_ids))
