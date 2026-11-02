from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import load_config
from .jobs.collect import run_collect
from .notify_factory import CHANNELS, build_notifier
from .pipeline.digest import build_digest
from .pipeline.interactions import format_favorites, handle_action
from .store import repo
from .store.db import connect, init_db


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-radar", description="AI Radar")
    parser.add_argument("--root", type=Path, default=None, help="项目根目录（默认自动推断）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化数据库")
    sub.add_parser("collect", help="拉取所有启用的来源")
    sub.add_parser("status", help="查看状态")
    sub.add_parser("favorites", help="查看收藏夹")
    sub.add_parser("prefs", help="查看偏好设置")
    sub.add_parser("backfill-vectors", help="为已精读文章补算语义向量")
    serve = sub.add_parser("serve", help="启动常驻服务（定时采集与推送）")
    serve.add_argument(
        "--channel",
        choices=list(CHANNELS),
        default="feishu",
        help="推送渠道：feishu 带交互卡片，webhook/fake 为单向推送",
    )

    related = sub.add_parser("related", help="查找相关已精读文章")
    related.add_argument("--document-id", type=int, required=True)

    compare = sub.add_parser("compare", help="对比两篇已精读文章")
    compare.add_argument("--left", type=int, required=True)
    compare.add_argument("--right", type=int, required=True)

    test_push = sub.add_parser("test-push", help="发送一条测试卡片，验证飞书配置")
    test_push.add_argument("--channel", choices=list(CHANNELS), default="feishu")
    sub.add_parser("feishu-info", help="查询当前凭据对应的机器人信息")

    digest = sub.add_parser("digest", help="生成并投递 digest")
    digest.add_argument("--date", default=None, help="digest 日期（YYYY-MM-DD），默认今天")
    digest.add_argument("--size", type=int, default=None, help="条数，默认读取 weights.yaml")
    digest.add_argument("--kind", choices=["daily", "weekly"], default="daily")
    digest.add_argument(
        "--preview",
        action="store_true",
        help="预演：卡片带「预览」标记；仍会写库，建议配合副本库使用",
    )
    digest.add_argument("--channel", choices=["fake", "feishu"], default="fake")

    run = sub.add_parser("run", help="collect + digest")
    run.add_argument("--date", default=None)
    run.add_argument("--size", type=int, default=None)
    run.add_argument("--kind", choices=["daily", "weekly"], default="daily")
    run.add_argument("--channel", choices=["fake", "feishu"], default="fake")

    search = sub.add_parser("search", help="按话题检索全库（本地不足时补站外）")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--no-external", action="store_true")

    interact = sub.add_parser("interact", help="本地模拟一次卡片/命令动作")
    interact.add_argument("--document-id", type=int, required=True)
    interact.add_argument(
        "--action",
        choices=["read", "save", "unsave", "dislike", "detail", "deep_read"],
        required=True,
    )
    interact.add_argument("--reason", default=None)
    interact.add_argument("--digest-date", default=None)

    deep_read = sub.add_parser("deep-read", help="同步执行精读（验证/补跑）")
    deep_read.add_argument("--document-id", type=int, default=None)
    deep_read.add_argument("--limit", type=int, default=1, help="处理队列条数")
    deep_read.add_argument("--channel", choices=["fake", "feishu"], default="fake")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.root)
    conn = connect(config.db_path)
    init_db(conn)

    if args.command == "init":
        print(json.dumps({"db": str(config.db_path), "ok": True}, ensure_ascii=False))
        return 0

    if args.command == "backfill-vectors":
        from .pipeline.embedding import backfill_vectors

        print(json.dumps(backfill_vectors(conn), ensure_ascii=False))
        return 0

    if args.command == "collect":
        print(json.dumps(run_collect(conn, config), ensure_ascii=False, indent=2))
        return 0

    if args.command == "status":
        print(json.dumps(repo.stats(conn), ensure_ascii=False, indent=2, default=str))
        return 0

    if args.command == "favorites":
        print(format_favorites(conn))
        return 0

    if args.command == "feishu-info":
        if config.feishu is None:
            raise SystemExit("飞书凭据缺失")
        notifier = build_notifier(config, "feishu")
        print(json.dumps(notifier.bot_info(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "test-push":
        from .adapters.notify.base import DigestItemView
        from .util import utcnow

        existing = repo.find_document_by_canonical(conn, "test:feishu")
        if existing is not None:
            document_id = int(existing["id"])
        else:
            document_id = repo.insert_document(
                conn,
                canonical_id="test:feishu",
                kind="blog",
                title="测试卡片：AI Radar 已接通飞书",
                abstract="这是一条测试消息，用于验证卡片与按钮。",
                url="https://arxiv.org",
                published_at=utcnow().isoformat(timespec="seconds"),
                authority="C",
                primary_tag="4 Agent 与应用",
                tags=["4 Agent 与应用"],
            )

        items = [
            DigestItemView(
                rank=1,
                document_id=document_id,
                title="测试卡片：AI Radar 已接通飞书",
                url="https://arxiv.org",
                tag="4 Agent 与应用",
                source="blog",
                reason="这是一条测试消息，用于验证卡片与按钮",
                score=1.0,
            )
        ]
        location = build_notifier(config, args.channel).send_digest(
            utcnow().date().isoformat(), items
        )
        print(json.dumps({"channel": args.channel, "result": location}, ensure_ascii=False))
        return 0

    if args.command == "serve":
        from .jobs.serve import run_bot

        run_bot(config, channel=args.channel)
        return 0

    if args.command == "search":
        from dataclasses import asdict

        from .pipeline.search import search_documents

        result = search_documents(
            conn,
            config,
            args.query,
            limit=args.limit,
            external=not args.no_external,
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str))
        return 0

    if args.command == "prefs":
        from .pipeline.preferences import format_preferences

        print(format_preferences(conn, config))
        return 0

    if args.command == "related":
        from .pipeline.compare import find_related

        print(json.dumps(find_related(conn, args.document_id), ensure_ascii=False, indent=2))
        return 0

    if args.command == "compare":
        from .pipeline.compare import compare_documents

        print(
            json.dumps(
                compare_documents(conn, args.left, args.right),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "interact":
        value = {"action": args.action, "document_id": args.document_id}
        if args.reason:
            value["reason"] = args.reason
        if args.digest_date:
            value["digest_date"] = args.digest_date
        print(json.dumps(handle_action(conn, value), ensure_ascii=False, indent=2))
        return 0

    if args.command == "deep-read":
        from dataclasses import asdict

        from .jobs.deep_read_worker import process_next
        from .pipeline.deep_read import deep_read_document

        if args.document_id:
            document = repo.get_document(conn, args.document_id)
            if document is None:
                print(json.dumps({"error": "document not found"}, ensure_ascii=False))
                return 1
            result = deep_read_document(conn, config, document)
            print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
            return 0

        notifier = build_notifier(config, args.channel)
        processed = 0
        for _ in range(args.limit):
            if not process_next(conn, config, notifier):
                break
            processed += 1
        print(json.dumps({"processed": processed}, ensure_ascii=False))
        return 0

    if args.command == "digest":
        result = build_digest(
            conn,
            config,
            notifier=build_notifier(config, args.channel),
            digest_date=args.date,
            size=args.size,
            kind=args.kind,
            preview=args.preview,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "run":
        collected = run_collect(conn, config)
        digest = build_digest(
            conn,
            config,
            notifier=build_notifier(config, args.channel),
            digest_date=args.date,
            size=args.size,
            kind=args.kind,
        )
        print(json.dumps({"collect": collected, "digest": digest}, ensure_ascii=False, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
