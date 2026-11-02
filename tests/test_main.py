from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from ai_radar.__main__ import build_notifier, build_parser, main
from ai_radar.adapters.notify.fake import FakeNotifier
from ai_radar.adapters.notify.webhook import WebhookNotifier
from ai_radar.config import load_config
from ai_radar.store import repo
from ai_radar.store.db import connect, init_db
from ai_radar.util import utcnow

ENV_KEYS = (
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_USER_OPEN_ID",
    "FEISHU_BASE_URL",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "AI_RADAR_PROXY",
    "AI_RADAR_WEBHOOK_URL",
    "AI_RADAR_WEBHOOK_FORMAT",
    "AI_RADAR_WEBHOOK_PROXY",
)


@pytest.fixture
def root(tmp_path: Path, monkeypatch) -> Path:
    """一份最小可用的项目根：只放 load_config 必需的三个 yaml。"""
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
    (config_dir / "weights.yaml").write_text("{}\n", encoding="utf-8")
    (config_dir / "models.yaml").write_text("{}\n", encoding="utf-8")
    return tmp_path


def add_document(root: Path, title: str = "冒烟文章") -> int:
    conn = connect(root / "data" / "ai_radar.db")
    init_db(conn)
    document_id = repo.insert_document(
        conn,
        canonical_id=f"test:{title}",
        kind="paper",
        title=title,
        abstract="abstract",
        url=f"https://example.com/{title}",
        published_at="2026-10-19T08:00:00+00:00",
        authority="A",
        primary_tag="4 Agent 与应用",
        tags=["4 Agent 与应用"],
    )
    conn.close()
    return document_id


# ---------- 参数解析 ----------


def test_parser_builds_every_subcommand() -> None:
    parser = build_parser()
    cases = {
        "init": [],
        "config-check": [],
        "collect": [],
        "status": [],
        "favorites": [],
        "prefs": [],
        "backfill-vectors": [],
        "serve": [],
        "feishu-info": [],
        "related": ["--document-id", "3"],
        "compare": ["--left", "1", "--right", "2"],
        "test-push": ["--channel", "fake"],
        "digest": ["--kind", "weekly", "--size", "5", "--preview", "--channel", "fake"],
        "run": ["--kind", "weekly"],
        "search": ["查询词", "--limit", "3", "--no-external"],
        "interact": ["--document-id", "1", "--action", "save"],
        "deep-read": ["--document-id", "1"],
    }
    for command, argv in cases.items():
        args = parser.parse_args([command, *argv])
        assert args.command == command


def test_parser_defaults() -> None:
    args = build_parser().parse_args(["digest"])
    assert args.kind == "daily"
    assert args.channel == "fake"
    assert args.preview is False

    search = build_parser().parse_args(["search", "关键词"])
    assert search.limit == 5
    assert search.no_external is False

    deep_read = build_parser().parse_args(["deep-read"])
    assert deep_read.limit == 1


def test_version_flag(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == "kestrel-radar 0.1.0"


def test_parser_rejects_missing_or_invalid_arguments() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
    with pytest.raises(SystemExit):
        parser.parse_args(["related"])
    with pytest.raises(SystemExit):
        parser.parse_args(["interact", "--document-id", "1", "--action", "nope"])
    with pytest.raises(SystemExit):
        parser.parse_args(["digest", "--kind", "hourly"])


# ---------- 通知器 ----------


def test_build_notifier_selection(root: Path) -> None:
    config = load_config(root)
    assert isinstance(build_notifier(config, "fake"), FakeNotifier)
    with pytest.raises(SystemExit):
        build_notifier(config, "feishu")


def test_build_notifier_webhook_selection(root: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_RADAR_WEBHOOK_URL", "https://example.com/hook")
    monkeypatch.setenv("AI_RADAR_WEBHOOK_FORMAT", "slack")
    config = load_config(root)
    notifier = build_notifier(config, "webhook")
    assert isinstance(notifier, WebhookNotifier)
    assert notifier.format == "slack"


# ---------- 无副作用命令 ----------


def test_init_creates_database(root: Path, capsys) -> None:
    assert main(["--root", str(root), "init"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert Path(payload["db"]).exists()


def test_config_check_reports_summary(root: Path, capsys) -> None:
    assert main(["--root", str(root), "config-check"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["sources"] == 0
    assert payload["feeds"] == 0


def test_config_check_reports_errors(root: Path, capsys) -> None:
    (root / "config" / "sources.yaml").write_text(
        "sources:\n  - id: broken\n    kind: nope\n", encoding="utf-8"
    )
    assert main(["--root", str(root), "config-check"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "kind='nope'" in payload["error"]


def test_readonly_commands_smoke(root: Path, capsys) -> None:
    assert main(["--root", str(root), "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "documents_by_status" in payload
    assert payload["last_7_days"]["window_days"] == 7

    assert main(["--root", str(root), "favorites"]) == 0
    assert "收藏夹为空" in capsys.readouterr().out

    assert main(["--root", str(root), "prefs"]) == 0
    assert "偏好设置" in capsys.readouterr().out


def test_status_reports_recent_window(root: Path, capsys) -> None:
    conn = connect(root / "data" / "ai_radar.db")
    init_db(conn)
    repo.upsert_source(
        conn,
        source_id="arxiv",
        kind="arxiv",
        name="arXiv",
        authority="A",
        region="overseas",
        config={},
    )
    repo.insert_raw_item(
        conn,
        source_id="arxiv",
        external_id="2401.00001",
        url="https://arxiv.org/abs/2401.00001",
        title="A paper",
        author=None,
        published_at=None,
        payload={},
    )
    run_id = repo.start_run(conn, "collect")
    repo.finish_run(conn, run_id, status="failed", error="timeout")
    repo.record_llm_usage(
        conn,
        day=utcnow().date().isoformat(),
        stage="summary",
        model="deepseek-flash",
        input_tokens=100,
        output_tokens=20,
        cost_cny=0.02,
    )
    conn.close()

    assert main(["--root", str(root), "status"]) == 0
    window = json.loads(capsys.readouterr().out)["last_7_days"]
    assert window["sources"][0]["source_id"] == "arxiv"
    assert window["sources"][0]["items_in_window"] == 1
    assert window["llm_calls"] == 1
    assert window["llm_total_cny"] == 0.02
    assert window["failed_runs"][0]["error"] == "timeout"


def test_test_push_with_fake_channel(root: Path, capsys) -> None:
    assert main(["--root", str(root), "test-push", "--channel", "fake"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["channel"] == "fake"
    assert list((root / "data" / "outbox").glob("*.md"))


def test_feishu_commands_require_credentials(root: Path) -> None:
    with pytest.raises(SystemExit):
        main(["--root", str(root), "feishu-info"])
    with pytest.raises(SystemExit):
        main(["--root", str(root), "test-push"])


# ---------- 依赖外部能力的命令（打桩） ----------


def test_related_and_interact_use_database(root: Path, capsys) -> None:
    document_id = add_document(root)

    assert main(["--root", str(root), "related", "--document-id", str(document_id)]) == 0
    assert json.loads(capsys.readouterr().out) == []

    assert (
        main(
            [
                "--root",
                str(root),
                "interact",
                "--document-id",
                str(document_id),
                "--action",
                "save",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["type"] == "text"
    assert "已加入知识库" in payload["text"]


def test_search_serializes_result(root: Path, capsys, monkeypatch) -> None:
    @dataclass
    class FakeOutcome:
        query: str
        local: list
        external: list

    monkeypatch.setattr(
        "ai_radar.pipeline.search.search_documents",
        lambda *args, **kwargs: FakeOutcome(query="量子", local=[], external=[]),
    )
    assert main(["--root", str(root), "search", "量子", "--no-external"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["query"] == "量子"


def test_collect_digest_and_run_are_wired(root: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr("ai_radar.__main__.run_collect", lambda conn, config: {"sources": 0})
    monkeypatch.setattr("ai_radar.__main__.build_digest", lambda conn, config, **kw: {"items": 0})

    assert main(["--root", str(root), "collect"]) == 0
    assert json.loads(capsys.readouterr().out) == {"sources": 0}

    assert main(["--root", str(root), "digest", "--channel", "fake", "--preview"]) == 0
    assert json.loads(capsys.readouterr().out) == {"items": 0}

    assert main(["--root", str(root), "run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"collect": {"sources": 0}, "digest": {"items": 0}}


def test_backfill_vectors_command(root: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(
        "ai_radar.pipeline.embedding.backfill_vectors", lambda conn: {"vectors": 0}
    )
    assert main(["--root", str(root), "backfill-vectors"]) == 0
    assert json.loads(capsys.readouterr().out) == {"vectors": 0}


def test_deep_read_missing_document_returns_error_code(root: Path, capsys) -> None:
    assert main(["--root", str(root), "deep-read", "--document-id", "999"]) == 1
    assert "document not found" in capsys.readouterr().out


def test_deep_read_queue_uses_worker(root: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(
        "ai_radar.jobs.deep_read_worker.process_next",
        lambda conn, config, notifier: False,
    )
    assert main(["--root", str(root), "deep-read", "--limit", "3"]) == 0
    assert json.loads(capsys.readouterr().out) == {"processed": 0}


def test_serve_delegates_to_run_bot(root: Path, monkeypatch) -> None:
    called: dict = {}
    monkeypatch.setattr(
        "ai_radar.jobs.serve.run_bot",
        lambda config, channel="feishu": called.update(root=config.root, channel=channel),
    )
    assert main(["--root", str(root), "serve"]) == 0
    assert called == {"root": root, "channel": "feishu"}


def test_serve_passes_explicit_channel(root: Path, monkeypatch) -> None:
    called: dict = {}
    monkeypatch.setattr(
        "ai_radar.jobs.serve.run_bot",
        lambda config, channel="feishu": called.update(channel=channel),
    )
    assert main(["--root", str(root), "serve", "--channel", "webhook"]) == 0
    assert called["channel"] == "webhook"
