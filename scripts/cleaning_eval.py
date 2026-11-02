"""M3 后的清洗方案验证：4 类站点 × 10 篇 × trafilatura / readability-lxml。

用法：
    uv run python scripts/cleaning_eval.py

产物：
    data/cleaning-eval/<category>/<n>-<slug>.<tool>.md
    data/cleaning-eval/summary.json
"""

from __future__ import annotations

import html as html_lib
import json
import os
import re
import socket
import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import httpx
import trafilatura
from readability import Document

from ai_radar.config import load_config, PROJECT_ROOT
from ai_radar.store.db import connect

OUT_DIR = PROJECT_ROOT / "data" / "cleaning-eval"
PROXY = os.getenv("AI_RADAR_PROXY", "")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ai-radar-cleaning-eval/0.1"

SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")

NOISE_WORDS = [
    "登录",
    "注册",
    "订阅",
    "相关阅读",
    "相关推荐",
    "广告",
    "版权所有",
    "All rights reserved",
    "Sign in",
    "Subscribe",
    "Cookie",
]


@dataclass
class Sample:
    category: str
    index: int
    url: str
    slug: str


@dataclass
class Result:
    category: str
    index: int
    tool: str
    url: str
    ok: bool
    error: str | None = None
    chars: int = 0
    raw_chars: int = 0
    ratio: float = 0.0
    links: int = 0
    code_blocks: int = 0
    tables: int = 0
    noise_hits: int = 0
    chinese_ratio: float = 0.0


def proxy_available() -> bool:
    if not PROXY:
        return False
    parsed = urlparse(PROXY)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.8)
        return sock.connect_ex((host, port)) == 0


def text_of(value: str) -> str:
    if not value:
        return ""
    text = SCRIPT_STYLE_RE.sub(" ", value)
    text = TAG_RE.sub(" ", text)
    return " ".join(html_lib.unescape(text).split())


def slug_of(url: str) -> str:
    tail = url.rstrip("/").split("/")[-1]
    tail = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff-]+", "-", tail).strip("-")
    return (tail or "index")[:60]


def paper_samples(conn: sqlite3.Connection) -> list[Sample]:
    rows = conn.execute(
        "SELECT payload_json FROM raw_items WHERE source_id = 'arxiv' "
        "ORDER BY id DESC LIMIT 20"
    ).fetchall()
    urls: list[str] = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        arxiv_id = str(payload.get("arxiv_id") or "").strip()
        if arxiv_id:
            urls.append(f"https://arxiv.org/html/{arxiv_id}")
    return [Sample("paper", index, url, slug_of(url)) for index, url in enumerate(urls, 1)]


def forum_samples(conn: sqlite3.Connection) -> list[Sample]:
    rows = conn.execute(
        "SELECT external_id, url, payload_json FROM raw_items "
        "WHERE source_id = 'hackernews' ORDER BY id DESC LIMIT 40"
    ).fetchall()
    discussion: list[str] = []
    external: list[str] = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        object_id = str(payload.get("object_id") or row["external_id"])
        discussion.append(f"https://news.ycombinator.com/item?id={object_id}")
        url = row["url"] or ""
        if (
            url
            and "news.ycombinator.com" not in url
            and "arxiv.org" not in url
            and url not in external
        ):
            external.append(url)

    urls = discussion[:5] + external[:8]
    return [Sample("forum", index, url, slug_of(url)) for index, url in enumerate(urls, 1)]


def feed_samples(
    client: httpx.Client, category: str, feeds: list[str], limit: int = 12
) -> list[Sample]:
    urls: list[str] = []
    for feed in feeds:
        try:
            response = client.get(feed)
            response.raise_for_status()
            parsed = feedparser.parse(response.text)
        except Exception:
            continue
        for entry in parsed.entries[:5]:
            link = str(entry.get("link") or "").strip()
            if link and link not in urls:
                urls.append(link)
    return [Sample(category, index, url, slug_of(url)) for index, url in enumerate(urls[:limit], 1)]


def chinese_samples(client: httpx.Client) -> list[Sample]:
    return feed_samples(
        client,
        "chinese",
        ["https://www.qbitai.com/feed", "https://www.infoq.cn/feed"],
    )


def english_samples(client: httpx.Client) -> list[Sample]:
    return feed_samples(
        client,
        "english",
        [
            "https://huggingface.co/blog/feed.xml",
            "https://simonwillison.net/atom/everything/",
            "https://www.latent.space/feed",
            "https://blog.google/technology/ai/rss/",
        ],
    )


def run_trafilatura(html: str, url: str) -> str:
    return (
        trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_links=True,
            include_tables=True,
            include_formatting=True,
            include_comments=False,
            favor_recall=True,
        )
        or ""
    )


def run_readability(html: str) -> str:
    article = Document(html)
    return article.summary(html_partial=True) or ""


def measure(result: Result, raw_html: str, extracted: str) -> Result:
    raw_text = text_of(raw_html)
    extracted_text = text_of(extracted)
    result.raw_chars = len(raw_text)
    result.chars = len(extracted_text)
    result.ratio = round(result.chars / result.raw_chars, 3) if result.raw_chars else 0.0
    result.links = len(re.findall(r"https?://", extracted))
    result.code_blocks = extracted.count("```") // 2 + extracted.count("<pre")
    result.tables = extracted.count("<table") + len(
        re.findall(r"^\s*\|?[\s:|-]+\|[\s:|-]+$", extracted, re.M)
    )
    result.noise_hits = sum(extracted.count(word) for word in NOISE_WORDS)
    if extracted_text:
        result.chinese_ratio = round(len(CJK_RE.findall(extracted_text)) / len(extracted_text), 3)
    return result


def main() -> int:
    config = load_config()
    conn = connect(config.db_path)
    proxy = PROXY if proxy_available() else None
    print(f"proxy={proxy or 'none (direct)'}", flush=True)

    with httpx.Client(
        proxy=proxy,
        timeout=45.0,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:
        samples: list[Sample] = []
        samples += paper_samples(conn)[:15]
        samples += forum_samples(conn)
        samples += english_samples(client)
        samples += chinese_samples(client)

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        results: list[Result] = []
        per_category: dict[str, int] = {}

        for sample in samples:
            if per_category.get(sample.category, 0) >= 10:
                continue
            try:
                response = client.get(sample.url)
                response.raise_for_status()
                raw_html = response.text
            except Exception as exc:
                print(f"skip {sample.category}#{sample.index} {sample.url}: {type(exc).__name__}", flush=True)
                continue

            if len(text_of(raw_html)) < 500:
                print(f"skip thin {sample.category}#{sample.index} {sample.url}", flush=True)
                continue

            per_category[sample.category] = per_category.get(sample.category, 0) + 1
            index = per_category[sample.category]
            folder = OUT_DIR / sample.category
            folder.mkdir(parents=True, exist_ok=True)

            for tool, runner in (
                ("trafilatura", lambda: run_trafilatura(raw_html, sample.url)),
                ("readability", lambda: run_readability(raw_html)),
            ):
                result = Result(sample.category, index, tool, sample.url, ok=True)
                try:
                    extracted = runner()
                    suffix = "md" if tool == "trafilatura" else "html"
                    (folder / f"{index:02d}-{sample.slug}.{tool}.{suffix}").write_text(
                        extracted, encoding="utf-8"
                    )
                    measure(result, raw_html, extracted)
                except Exception as exc:
                    result.ok = False
                    result.error = f"{type(exc).__name__}: {exc}"
                results.append(result)

            print(
                f"{sample.category}#{index} ok "
                f"traf_chars={next((r.chars for r in results[-2:]), 0)} "
                f"read_chars={next((r.chars for r in results[-1:]), 0)}",
                flush=True,
            )

    summary = {
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "proxy": proxy,
        "counts": per_category,
        "results": [asdict(result) for result in results],
    }
    (OUT_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(per_category, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
