from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..adapters.http import get_with_retry
from ..adapters.sources.hackernews import fetch_comments
from ..adapters.url_safety import is_public_http_url
from ..config import AppConfig
from ..llm.client import DeepSeekLLM, LLMError, LLMJSONError, parse_json_object
from ..llm.prompts import (
    COMMUNITY_SYSTEM,
    DEEP_READ_SYSTEM,
    render_chunk_prompt,
    render_community_prompt,
    render_reduce_prompt,
)
from ..store import repo
from .cleaning import (
    CleanError,
    arxiv_html_url,
    arxiv_pdf_url,
    clean_html,
    pdf_to_text,
)
from .embedding import ensure_vectors

MAX_CHUNK_CHARS = 6000
MAX_CHUNKS = 12
MAX_COMMENTS = 8
MIN_COMMENTS = 3
HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(slots=True)
class DeepReadResult:
    document_id: int
    local_path: str
    chars: int
    chunks: int
    claims: int
    card: dict
    source_format: str = "html"


def safe_name(value: str) -> str:
    return SAFE_NAME_RE.sub("_", value).strip("_") or "document"


def fetch_html(url: str, proxy: str | None, timeout: float = 60.0) -> tuple[str, str]:
    """返回 (最终 HTML, 实际使用的 URL)。代理失败时自动直连重试。"""
    target = arxiv_html_url(url)
    if not is_public_http_url(target):
        raise CleanError(f"blocked non-public url: {target}")
    headers = {"User-Agent": "Mozilla/5.0 ai-radar/0.1"}
    attempts: list[str | None] = []
    if proxy:
        attempts.append(proxy)
    attempts.append(None)

    last_error: Exception | None = None
    for candidate in attempts:
        try:
            with httpx.Client(
                proxy=candidate,
                timeout=timeout,
                headers=headers,
                follow_redirects=True,
            ) as client:
                response = get_with_retry(client, target, attempts=2, base_delay=2.0)
                return response.text, target
        except httpx.HTTPError as exc:
            last_error = exc
    raise CleanError(f"fetch failed: {target}: {last_error}")


def fetch_pdf(url: str, proxy: str | None, timeout: float = 120.0) -> bytes:
    """下载 PDF；代理失败时自动直连重试。"""
    target = arxiv_pdf_url(url)
    if not is_public_http_url(target):
        raise CleanError(f"blocked non-public url: {target}")
    headers = {"User-Agent": "Mozilla/5.0 ai-radar/0.1"}
    attempts: list[str | None] = []
    if proxy:
        attempts.append(proxy)
    attempts.append(None)

    last_error: Exception | None = None
    for candidate in attempts:
        try:
            with httpx.Client(
                proxy=candidate,
                timeout=timeout,
                headers=headers,
                follow_redirects=True,
            ) as client:
                response = get_with_retry(client, target, attempts=2, base_delay=2.0)
                return response.content
        except httpx.HTTPError as exc:
            last_error = exc
    raise CleanError(f"pdf fetch failed: {target}: {last_error}")


def chunk_markdown(text: str, *, max_chars: int = MAX_CHUNK_CHARS) -> list[dict]:
    """按 Markdown 标题切块；单块超过 max_chars 时按段落继续拆。"""
    chunks: list[dict] = []
    section = "正文"
    buffer: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal buffer, size
        body = "\n".join(buffer).strip()
        if body:
            chunks.append(
                {"section": section, "ord": len(chunks) + 1, "text": body, "anchor": None}
            )
        buffer = []
        size = 0

    for line in text.splitlines():
        heading = HEADING_RE.match(line)
        if heading:
            flush()
            section = heading.group(2).strip() or section
            continue

        if size + len(line) + 1 > max_chars:
            flush()
        # 单行超长（压缩成一行的正文）时强制硬切
        while len(line) > max_chars:
            buffer.append(line[:max_chars])
            flush()
            line = line[max_chars:]
        buffer.append(line)
        size += len(line) + 1

    flush()
    return chunks


def _as_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip() and value.strip() != "未知":
        return [value.strip()]
    return []


def _chat_json(
    llm: DeepSeekLLM,
    conn: sqlite3.Connection,
    *,
    stage: str,
    system: str,
    user: str,
    max_tokens: int,
    document_id: int,
) -> dict:
    """调用模型并解析 JSON；格式坏了就放大额度重试一次，避免整篇精读失败。"""
    text, _ = llm.chat(
        conn,
        stage=stage,
        system=system,
        user=user,
        max_tokens=max_tokens,
        document_id=document_id,
    )
    try:
        return parse_json_object(text)
    except LLMJSONError:
        retry_text, _ = llm.chat(
            conn,
            stage=stage,
            system=system,
            user=user,
            max_tokens=min(max_tokens * 3, 8000),
            document_id=document_id,
        )
        return parse_json_object(retry_text)


def _extract_chunk(
    llm: DeepSeekLLM,
    conn: sqlite3.Connection,
    document: sqlite3.Row,
    chunk: dict,
    total: int,
) -> dict:
    user = render_chunk_prompt(
        title=document["title"],
        ord=str(chunk["ord"]),
        total=str(total),
        section=chunk["section"],
        content=chunk["text"],
    )
    return _chat_json(
        llm,
        conn,
        stage="deep_read",
        system=DEEP_READ_SYSTEM,
        user=user,
        max_tokens=2000,
        document_id=int(document["id"]),
    )


def _reduce(
    llm: DeepSeekLLM,
    conn: sqlite3.Connection,
    document: sqlite3.Row,
    cards: list[dict],
) -> dict:
    user = render_reduce_prompt(
        title=document["title"],
        kind=document["kind"],
        url=document["url"],
        chunk_cards=json.dumps(cards, ensure_ascii=False),
    )
    return _chat_json(
        llm,
        conn,
        stage="deep_read",
        system=DEEP_READ_SYSTEM,
        user=user,
        max_tokens=8000,
        document_id=int(document["id"]),
    )


def _community_card(
    llm: DeepSeekLLM,
    conn: sqlite3.Connection,
    document: sqlite3.Row,
    comments: list[str],
) -> dict | None:
    """把 HN 评论压成「共识 / 争议 / 补充」；评论太少就不做。"""
    if len(comments) < MIN_COMMENTS:
        return None
    user = render_community_prompt(
        title=document["title"],
        comments="\n\n".join(f"- {item}" for item in comments),
    )
    data = _chat_json(
        llm,
        conn,
        stage="deep_read",
        system=COMMUNITY_SYSTEM,
        user=user,
        max_tokens=1200,
        document_id=int(document["id"]),
    )
    groups = {
        key: _as_list(data.get(key))[:4] for key in ("consensus", "disputes", "tips")
    }
    if not any(groups.values()):
        return None
    return groups


def deep_read_document(
    conn: sqlite3.Connection,
    config: AppConfig,
    document: sqlite3.Row,
    *,
    llm: DeepSeekLLM | None = None,
) -> DeepReadResult:
    if config.llm is None:
        raise LLMError("DEEPSEEK_API_KEY 未配置，无法精读")
    llm = llm or DeepSeekLLM(config.llm)

    proxy = config.network.proxy if config.network.mode == "overseas-only" else None
    source_format = "html"
    try:
        html, final_url = fetch_html(
            document["url"], proxy, timeout=config.network.timeout_seconds
        )
        content = clean_html(html, final_url, kind=document["kind"])
        if document["kind"] == "paper" and "abstract" not in content[:5000].lower():
            raise CleanError("paper html has no abstract section")
        raw_name = "article.html"
        raw_bytes: bytes | None = None
        raw_text: str | None = html
    except CleanError:
        if document["kind"] != "paper":
            raise
        # 论文没有可用 HTML 全文时回退 PDF。
        pdf_bytes = fetch_pdf(document["url"], proxy, timeout=120.0)
        content = pdf_to_text(pdf_bytes)
        source_format = "pdf"
        raw_name = "article.pdf"
        raw_bytes = pdf_bytes
        raw_text = None

    folder = config.root / "data" / "articles" / safe_name(document["canonical_id"])
    (folder / "raw").mkdir(parents=True, exist_ok=True)
    content_path = folder / "content.md"
    content_path.write_text(content, encoding="utf-8")
    if raw_bytes is not None:
        (folder / "raw" / raw_name).write_bytes(raw_bytes)
    else:
        (folder / "raw" / raw_name).write_text(raw_text or "", encoding="utf-8")
    repo.set_local_path(conn, int(document["id"]), str(content_path))

    chunks = chunk_markdown(content)
    if not chunks:
        raise CleanError("no chunks produced")

    chunk_ids = repo.replace_chunks(conn, int(document["id"]), chunks)

    cards: list[dict] = []
    claims = 0
    # 全部切块入库供检索；模型只处理前 MAX_CHUNKS 块以控制成本。
    processed = min(len(chunks), MAX_CHUNKS)
    for chunk_id, chunk in zip(chunk_ids[:MAX_CHUNKS], chunks[:MAX_CHUNKS], strict=True):
        card = _extract_chunk(llm, conn, document, chunk, processed)
        raw_claims = card.get("claims")
        if isinstance(raw_claims, list):
            claims += repo.insert_claims(
                conn,
                document_id=int(document["id"]),
                chunk_id=chunk_id,
                claims=[item for item in raw_claims if isinstance(item, dict)],
            )
        cards.append(
            {
                "problem": str(card.get("problem") or "未知"),
                "method": str(card.get("method") or "未知"),
                "evidence": _as_list(card.get("evidence")),
                "conclusions": _as_list(card.get("conclusions")),
                "limitations": _as_list(card.get("limitations")),
            }
        )

    merged = _reduce(llm, conn, document, cards)
    card = {
        "problem": str(merged.get("problem") or "未知"),
        "method": str(merged.get("method") or "未知"),
        "evidence": _as_list(merged.get("evidence")),
        "conclusions": _as_list(merged.get("conclusions")),
        "limitations": _as_list(merged.get("limitations")),
        "l3": str(merged.get("l3") or ""),
    }
    # HN 条目额外读一批评论，作为「社区怎么看」补充；抓不到或模型失败都不影响精读本身。
    try:
        object_id = repo.hn_object_id(conn, int(document["id"]))
        if object_id:
            community = _community_card(
                llm,
                conn,
                document,
                fetch_comments(object_id, proxy=proxy, limit=MAX_COMMENTS),
            )
            if community:
                card["community"] = community
    except Exception:
        pass

    repo.save_kb_article(
        conn,
        document_id=int(document["id"]),
        problem=card["problem"],
        method=card["method"],
        evidence=card["evidence"],
        conclusions=card["conclusions"],
        limitations=card["limitations"],
        card=card,
    )
    repo.mark_status(conn, [int(document["id"])], "kb_ready")
    # 入库后立即建向量；模型不可用时该调用静默返回空，不影响精读结果。
    ensure_vectors(conn, [int(document["id"])])

    return DeepReadResult(
        document_id=int(document["id"]),
        local_path=str(content_path),
        chars=len(content),
        chunks=len(chunks),
        claims=claims,
        card=card,
        source_format=source_format,
    )


def article_dir(config: AppConfig, canonical_id: str) -> Path:
    return config.root / "data" / "articles" / safe_name(canonical_id)
