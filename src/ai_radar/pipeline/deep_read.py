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
    argument_system,
    community_system,
    critique_system,
    narrative_system,
    part_summary_system,
    reading_map_system,
    render_argument_prompt,
    render_community_prompt,
    render_critique_prompt,
    render_narrative_prompt,
    render_part_prompt,
    render_reading_map_prompt,
    render_section_prompt,
    section_read_system,
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
MAX_CHUNKS = 80
PART_CHUNKS = 8
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


def _to_local_id(value) -> int:
    """模型有时把 claim_id 写成 "C5"，统一抽成整数。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    match = re.search(r"\d+", str(value or ""))
    return int(match.group()) if match else 0


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
    """调用模型并解析 JSON；格式坏了就带着错误提示放大额度重试一次。"""
    text, _ = llm.chat(
        conn,
        stage=stage,
        system=system,
        user=user,
        max_tokens=max_tokens,
        document_id=document_id,
    )
    last: LLMJSONError | None = None
    for _ in range(2):
        try:
            return parse_json_object(text)
        except LLMJSONError as exc:
            last = exc
            # 直接把错误反馈给模型，比单纯放大额度更能治住未转义的引号。
            text, _ = llm.chat(
                conn,
                stage=stage,
                system=system,
                user=user
                + "\n\n严格要求：上一次输出不是合法 JSON。"
                "请只输出一个 JSON 对象；字符串内部不要出现未转义的英文双引号，"
                "需要引用术语时改用「」。",
                max_tokens=min(max_tokens * 3, 8000),
                document_id=document_id,
            )
    raise last or LLMJSONError("invalid JSON")


def _reading_map(
    llm: DeepSeekLLM,
    conn: sqlite3.Connection,
    document: sqlite3.Row,
    chunks: list[dict],
) -> dict:
    """预读：在进入细节之前先建立全文论证骨架。"""
    headings = "\n".join(f"- {chunk['section']}" for chunk in chunks)
    head = "\n\n".join(chunk["text"] for chunk in chunks[:2])[:8000]
    tail = "\n\n".join(chunk["text"] for chunk in chunks[-1:])[:4000]
    user = render_reading_map_prompt(
        title=document["title"],
        kind=document["kind"],
        url=document["url"],
        headings=headings,
        head=head,
        tail=tail,
    )
    try:
        data = _chat_json(
            llm,
            conn,
            stage="deep_read",
            system=reading_map_system(getattr(llm, "profile", None)),
            user=user,
            max_tokens=2500,
            document_id=int(document["id"]),
        )
    except LLMError:
        # 预读失败不拖垮整篇精读，退回按章节顺序阅读。
        return {
            "thesis": "",
            "problem": "",
            "method_summary": "",
            "outline": [],
            "questions": [],
            "key_terms": [],
        }
    data["outline"] = [item for item in data.get("outline") or [] if isinstance(item, dict)]
    data["questions"] = _as_list(data.get("questions"))[:10]
    return data


def _outline_text(reading: dict) -> str:
    lines = []
    for item in reading.get("outline") or []:
        if not isinstance(item, dict):
            continue
        lines.append(
            f"- {item.get('heading') or ''}（{item.get('role') or 'other'}）："
            f"{item.get('summary') or ''}"
        )
    return "\n".join(lines) or "（预读没有给出结构）"


def _section_card(
    llm: DeepSeekLLM,
    conn: sqlite3.Connection,
    document: sqlite3.Row,
    chunk: dict,
    total: int,
    reading: dict,
) -> dict:
    role = "other"
    for item in reading.get("outline") or []:
        if isinstance(item, dict) and item.get("heading") == chunk["section"]:
            role = str(item.get("role") or "other")
            break
    user = render_section_prompt(
        title=document["title"],
        thesis=str(reading.get("thesis") or ""),
        outline=_outline_text(reading),
        section=str(chunk["section"] or "正文"),
        role=role,
        ord=str(chunk["ord"]),
        total=str(total),
        content=chunk["text"],
    )
    try:
        return _chat_json(
            llm,
            conn,
            stage="deep_read",
            system=section_read_system(getattr(llm, "profile", None)),
            user=user,
            # 6000 字的分块配上证据，3000 tokens 容易被截断成半截 JSON。
            max_tokens=4500,
            document_id=int(document["id"]),
        )
    except LLMError as exc:
        # 单个分块格式坏掉不该让整篇长文重来，记录后跳过这一块。
        print(
            f"[deep_read] section skipped: {chunk['section']} "
            f"ord={chunk['ord']}: {exc}",
            flush=True,
        )
        return {
            "section_summary": "",
            "claims": [],
            "limitations": [],
            "open_questions": [],
            "_failed": True,
        }


def _argument_map(
    llm: DeepSeekLLM,
    conn: sqlite3.Connection,
    document: sqlite3.Row,
    reading: dict,
    claims: list[dict],
    parts: list[dict],
) -> dict:
    payload = [
        {
            "local_id": int(item["local_id"]),
            "text": str(item["text"])[:140],
            "question": str(item.get("question") or "")[:50],
            "claim_type": item.get("claim_type") or "sub",
            "confidence": item.get("confidence") or "source",
            "quote": str(item.get("quote") or "")[:80],
        }
        for item in claims[:160]
    ]
    user = render_argument_prompt(
        title=document["title"],
        thesis=str(reading.get("thesis") or ""),
        parts=_parts_text(parts),
        claims=json.dumps(payload, ensure_ascii=False)[:20000],
    )
    data: dict = {}
    for _ in range(2):
        try:
            data = _chat_json(
                llm,
                conn,
                stage="deep_read",
                system=argument_system(getattr(llm, "profile", None)),
                user=user,
                max_tokens=5000,
                document_id=int(document["id"]),
            )
        except LLMError:
            return {
                "thesis": reading.get("thesis") or "",
                "main_claims": [],
                "relations": [],
            }
        has_main = any(isinstance(item, dict) for item in data.get("main_claims") or [])
        has_relation = any(isinstance(item, dict) for item in data.get("relations") or [])
        # 主主张有了但关系为空，多半是模型偷懒，再要一次。
        if not has_main or has_relation:
            break
    main_claims = []
    for item in data.get("main_claims") or []:
        if not isinstance(item, dict):
            continue
        local = _to_local_id(item.get("claim_id") or item.get("id"))
        if local <= 0:
            continue
        main_claims.append({**item, "claim_id": local})
    data["main_claims"] = main_claims
    relations = []
    for item in data.get("relations") or []:
        if not isinstance(item, dict):
            continue
        source = _to_local_id(item.get("from") or item.get("from_id"))
        target = _to_local_id(item.get("to") or item.get("to_id"))
        if source <= 0 or target <= 0 or source == target:
            continue
        relations.append({**item, "from": source, "to": target})
    data["relations"] = relations
    return data


def _narrative(
    llm: DeepSeekLLM,
    conn: sqlite3.Connection,
    document: sqlite3.Row,
    reading: dict,
    claims: list[dict],
    argument: dict,
    parts: list[dict],
) -> str:
    material = {
        "thesis": argument.get("thesis") or reading.get("thesis") or "",
        "outline": reading.get("outline") or [],
        "parts": parts,
        "claims": claims[:80],
        "main_claims": argument.get("main_claims") or [],
        "relations": argument.get("relations") or [],
    }
    user = render_narrative_prompt(
        title=document["title"],
        thesis=str(material["thesis"]),
        material=json.dumps(material, ensure_ascii=False)[:30000],
    )
    try:
        text, _ = llm.chat(
            conn,
            stage="deep_read",
            system=narrative_system(getattr(llm, "profile", None)),
            user=user,
            max_tokens=5000,
            document_id=int(document["id"]),
        )
    except LLMError:
        return ""
    return text.strip()


def _critique(
    llm: DeepSeekLLM,
    conn: sqlite3.Connection,
    document: sqlite3.Row,
    reading: dict,
    narrative: str,
    claims: list[dict],
    parts: list[dict],
) -> dict:
    material = {
        "narrative": narrative,
        "parts": parts,
        "claims": claims[:80],
        "outline": reading.get("outline") or [],
    }
    user = render_critique_prompt(
        title=document["title"],
        material=json.dumps(material, ensure_ascii=False)[:24000],
    )
    try:
        data = _chat_json(
            llm,
            conn,
            stage="deep_read",
            system=critique_system(getattr(llm, "profile", None)),
            user=user,
            max_tokens=2200,
            document_id=int(document["id"]),
        )
    except LLMError:
        return {}
    for key in ("unverified", "weak_evidence", "counterpoints", "open_questions"):
        data[key] = _as_list(data.get(key))[:12]
    return data


def _retrieval_text(
    *,
    title: str,
    thesis: str,
    section: str,
    role: str,
    question: str,
    claim: str,
    evidence: list[dict],
) -> str:
    """检索单元必须自包含：文档、章节、全文主张、问题、主张、证据。"""
    parts = [f"《{title}》", f"章节：{section}（{role}）"]
    if thesis:
        parts.append(f"本文主张：{thesis}")
    if question:
        parts.append(f"该主张回答的问题：{question}")
    parts.append(f"主张：{claim}")
    evidence_lines = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        quote = str(item.get("quote") or "").strip()
        if not quote:
            continue
        data = " / ".join(
            str(item.get(key) or "")
            for key in ("metric", "value", "unit", "baseline", "condition", "sample")
            if str(item.get(key) or "").strip()
        )
        evidence_lines.append(f"{quote}（{data}）" if data else quote)
    if evidence_lines:
        parts.append("证据：" + "；".join(evidence_lines[:4]))
    return "\n".join(parts)


def _section_rows(
    chunks: list[dict], reading: dict
) -> list[dict]:
    """把 chunk 的章节去重成 sections，并补上预读给的角色与摘要。"""
    outline = {
        str(item.get("heading") or ""): item
        for item in reading.get("outline") or []
        if isinstance(item, dict)
    }
    seen: set[str] = set()
    rows: list[dict] = []
    for chunk in chunks:
        heading = str(chunk["section"] or "正文")
        if heading in seen:
            continue
        seen.add(heading)
        source = outline.get(heading) or {}
        rows.append(
            {
                "ord": len(rows) + 1,
                "heading": heading,
                "role": str(source.get("role") or "other"),
                "summary": str(source.get("summary") or ""),
                "anchor": heading,
            }
        )
    return rows


def _parts_text(parts: list[dict]) -> str:
    if not parts:
        return "（未生成部分小结）"
    lines = []
    for part in parts:
        key = "、".join(f"C{item}" for item in part.get("key_claims") or [])
        lines.append(
            f"第 {part.get('index')}/{part.get('total')} 部分"
            f"（{part.get('headings') or '正文'}）：{part.get('summary') or '（无小结）'}"
            + (f"；关键主张：{key}" if key else "")
        )
    return "\n".join(lines)


def _part_summaries(
    llm: DeepSeekLLM,
    conn: sqlite3.Connection,
    document: sqlite3.Row,
    reading: dict,
    claims: list[dict],
    chunks: list[dict],
    chunk_ids: list[int],
) -> list[dict]:
    """长文分段：每 PART_CHUNKS 块一个部分，产出可承接上下文的小结。"""
    chunk_ord = {
        int(chunk_id): int(chunk["ord"])
        for chunk_id, chunk in zip(chunk_ids, chunks, strict=True)
    }
    heading_by_ord = {
        int(chunk["ord"]): str(chunk["section"] or "正文") for chunk in chunks
    }
    total_parts = (len(chunks) + PART_CHUNKS - 1) // PART_CHUNKS
    parts: list[dict] = []
    for part_index, start in enumerate(range(0, len(chunks), PART_CHUNKS), start=1):
        end = min(start + PART_CHUNKS, len(chunks))
        allowed = set(range(start + 1, end + 1))
        part_claims = [
            claim
            for claim in claims
            if claim.get("chunk_id") is not None
            and chunk_ord.get(int(claim["chunk_id"])) in allowed
        ]
        if not part_claims:
            continue
        headings = "、".join(
            dict.fromkeys(
                heading_by_ord[ordinal]
                for ordinal in sorted(allowed)
                if ordinal in heading_by_ord
            )
        )
        payload = [
            {
                "local_id": int(claim["local_id"]),
                "text": str(claim["text"])[:160],
                "question": str(claim.get("question") or "")[:60],
                "quote": str(claim.get("quote") or "")[:80],
            }
            for claim in part_claims[:40]
        ]
        user = render_part_prompt(
            title=document["title"],
            thesis=str(reading.get("thesis") or ""),
            outline=_outline_text(reading),
            index=str(part_index),
            total=str(total_parts),
            headings=headings,
            claims=json.dumps(payload, ensure_ascii=False)[:14000],
        )
        try:
            data = _chat_json(
                llm,
                conn,
                stage="deep_read",
                system=part_summary_system(getattr(llm, "profile", None)),
                user=user,
                max_tokens=1600,
                document_id=int(document["id"]),
            )
        except LLMError:
            data = {}
        valid_ids = {int(claim["local_id"]) for claim in part_claims}
        key_claims = []
        for value in data.get("key_claims") or []:
            local = _to_local_id(value)
            if local in valid_ids and local not in key_claims:
                key_claims.append(local)
        parts.append(
            {
                "index": part_index,
                "total": total_parts,
                "headings": headings,
                "summary": str(data.get("summary") or "").strip(),
                "key_claims": key_claims[:6],
                "open_questions": _as_list(data.get("open_questions"))[:6],
            }
        )
    return parts


def _key_claims(
    claims: list[dict],
    parts: list[dict],
    argument: dict,
    *,
    limit: int = 80,
) -> list[dict]:
    """叙事与审计只带关键主张，避免长文被截断成前 60 条。"""
    wanted: set[int] = set()
    for item in argument.get("main_claims") or []:
        wanted.add(_to_local_id(item.get("claim_id")))
        for value in item.get("supports") or []:
            wanted.add(_to_local_id(value))
    for part in parts:
        wanted.update(int(value) for value in part.get("key_claims") or [])
    wanted.discard(0)
    selected = [claim for claim in claims if int(claim["local_id"]) in wanted]
    selected_ids = {int(claim["local_id"]) for claim in selected}
    for claim_type in ("main", "counter", "limitation", "assumption"):
        for claim in claims:
            if len(selected) >= limit:
                break
            local = int(claim["local_id"])
            if local not in selected_ids and claim.get("claim_type") == claim_type:
                selected.append(claim)
                selected_ids.add(local)
    for claim in claims:
        if len(selected) >= limit:
            break
        local = int(claim["local_id"])
        if local not in selected_ids:
            selected.append(claim)
            selected_ids.add(local)
    selected.sort(key=lambda claim: int(claim["local_id"]))
    return selected[:limit]


def _reduce(
    llm: DeepSeekLLM,
    conn: sqlite3.Connection,
    document: sqlite3.Row,
    cards: list[dict],
) -> dict:
    """兼容旧调用：把新结构压回旧字段。"""
    claims = [
        claim
        for card in cards
        for claim in (card.get("claims") or [])
        if isinstance(claim, dict)
    ]
    return {
        "problem": next((str(card.get("problem") or "") for card in cards if card.get("problem")), ""),
        "method": next((str(card.get("method") or "") for card in cards if card.get("method")), ""),
        "evidence": [
            str(item.get("quote") or "")
            for claim in claims
            for item in (claim.get("evidence") or [])
            if isinstance(item, dict) and item.get("quote")
        ][:12],
        "conclusions": [str(claim.get("text") or "") for claim in claims if claim.get("text")][:12],
        "limitations": [
            str(item)
            for card in cards
            for item in (card.get("limitations") or [])
            if str(item).strip()
        ][:12],
        "l3": "",
    }


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
        system=community_system(getattr(llm, "profile", None)),
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
    llm = llm or DeepSeekLLM(config.llm, profile=getattr(config, "domain", None))

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

    document_id = int(document["id"])
    chunk_ids = repo.replace_chunks(conn, document_id, chunks)

    # 1. 预读：先建立全文骨架，再进入细节。
    reading = _reading_map(llm, conn, document, chunks)
    section_rows = _section_rows(chunks, reading)
    section_ids = repo.replace_sections(conn, document_id, section_rows)
    section_id_by_heading = {
        row["heading"]: section_id
        for row, section_id in zip(section_rows, section_ids, strict=True)
    }
    role_by_heading = {row["heading"]: row["role"] for row in section_rows}

    # 2. 分节取证：每个 chunk 带全文主张和章节角色。
    processed = min(len(chunks), MAX_CHUNKS)
    claims_payload: list[dict] = []
    section_cards: list[dict] = []
    skipped_chunks = 0
    local_id = 0
    for chunk_id, chunk in zip(
        chunk_ids[:processed], chunks[:processed], strict=True
    ):
        card = _section_card(llm, conn, document, chunk, processed, reading)
        if card.get("_failed"):
            skipped_chunks += 1
        heading = str(chunk["section"] or "正文")
        role = role_by_heading.get(heading, "other")
        anchor = f"{heading} · 片段 {chunk['ord']}"
        section_cards.append(
            {
                "section": heading,
                "summary": str(card.get("section_summary") or ""),
                "limitations": _as_list(card.get("limitations")),
                "open_questions": _as_list(card.get("open_questions")),
            }
        )
        for item in card.get("claims") or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            local_id += 1
            evidence = [
                entry for entry in (item.get("evidence") or []) if isinstance(entry, dict)
            ]
            question = str(item.get("question") or "").strip()
            claims_payload.append(
                {
                    "local_id": local_id,
                    "chunk_id": chunk_id,
                    "section_id": section_id_by_heading.get(heading),
                    "text": text,
                    "question": question,
                    "claim_type": str(item.get("claim_type") or "sub"),
                    "confidence": str(item.get("confidence") or "source"),
                    "quote": str(item.get("quote") or ""),
                    "anchor": anchor,
                    "kind": str(item.get("claim_type") or "finding"),
                    "retrieval_text": _retrieval_text(
                        title=str(document["title"]),
                        thesis=str(reading.get("thesis") or ""),
                        section=heading,
                        role=role,
                        question=question,
                        claim=text,
                        evidence=evidence,
                    ),
                    "evidence": evidence,
                }
            )

    # 3. 主张入库；证据挂在主张上。
    claim_rows = [
        {key: value for key, value in item.items() if key != "evidence"}
        for item in claims_payload
    ]
    claim_ids = repo.replace_claims(conn, document_id, claim_rows)
    claim_id_by_local: dict[int, int] = {}
    for item, claim_id in zip(claims_payload, claim_ids, strict=True):
        item["id"] = claim_id
        claim_id_by_local[int(item["local_id"])] = claim_id
        repo.insert_evidence(
            conn,
            document_id=document_id,
            claim_id=claim_id,
            items=item.get("evidence") or [],
        )

    # 4. 长文分段：把已读分块按顺序压成可承接上下文的小结。
    parts = _part_summaries(
        llm,
        conn,
        document,
        reading,
        claims_payload,
        chunks[:processed],
        chunk_ids[:processed],
    )

    # 5. 论证重建：主主张 + 关系。
    argument = _argument_map(llm, conn, document, reading, claims_payload, parts)
    relation_rows: list[dict] = []
    for item in argument.get("relations") or []:
        try:
            source = claim_id_by_local.get(int(item.get("from") or 0))
            target = claim_id_by_local.get(int(item.get("to") or 0))
        except (TypeError, ValueError):
            continue
        if not source or not target:
            continue
        relation_rows.append(
            {
                "from_claim_id": source,
                "to_claim_id": target,
                "relation": str(item.get("relation") or "supports"),
                "note": str(item.get("note") or ""),
            }
        )
    repo.replace_claim_relations(conn, document_id, relation_rows)

    main_claims: list[dict] = []
    for item in argument.get("main_claims") or []:
        try:
            local = int(item.get("claim_id") or 0)
        except (TypeError, ValueError):
            continue
        database_id = claim_id_by_local.get(local)
        if not database_id:
            continue
        main_claims.append(
            {
                **item,
                "claim_id": database_id,
                "local_id": local,
            }
        )

    # 论证重建失败时，用分节阶段标出的 main 主张兜底，至少保留论证地图。
    if not main_claims:
        main_claims = [
            {
                "claim_id": int(item["id"]),
                "local_id": int(item["local_id"]),
                "text": str(item["text"]),
                "supports": [],
                "note": "",
            }
            for item in claims_payload
            if item.get("claim_type") == "main"
        ][:5]

    # 6. 叙事合成 + 7. 证据审计。
    key_claims = _key_claims(claims_payload, parts, argument)
    narrative = _narrative(llm, conn, document, reading, key_claims, argument, parts)
    critique = _critique(
        llm, conn, document, reading, narrative, key_claims, parts
    )

    conclusions = [
        str(item.get("text") or "") for item in main_claims if item.get("text")
    ] or [str(item["text"]) for item in claims_payload[:8]]
    evidence_list = [
        str(entry.get("quote") or "")
        for item in claims_payload
        for entry in item.get("evidence") or []
        if entry.get("quote")
    ][:16]
    limitations = list(critique.get("weak_evidence") or [])
    for card in section_cards:
        limitations.extend(card.get("limitations") or [])
    limitations = list(dict.fromkeys(str(item) for item in limitations if str(item).strip()))[
        :16
    ]

    payload = {
        "thesis": str(argument.get("thesis") or reading.get("thesis") or ""),
        "problem": str(reading.get("problem") or ""),
        "method": str(reading.get("method_summary") or ""),
        "article_type": str(reading.get("article_type") or ""),
        "audience": str(reading.get("audience") or ""),
        "outline": section_rows,
        "parts": parts,
        "coverage": {
            "chunks_total": len(chunks),
            "chunks_read": processed,
            "chunks_skipped": skipped_chunks,
        },
        "questions": reading.get("questions") or [],
        "key_terms": reading.get("key_terms") or [],
        "claims": claims_payload,
        "main_claims": main_claims,
        "relations": relation_rows,
        "narrative": narrative,
        "critique": critique,
        "limitations": limitations,
        "evidence": evidence_list,
        "l3": narrative,
    }

    # HN 条目额外读一批评论，作为「社区怎么看」补充；抓不到或模型失败都不影响精读本身。
    try:
        object_id = repo.hn_object_id(conn, document_id)
        if object_id:
            community = _community_card(
                llm,
                conn,
                document,
                fetch_comments(object_id, proxy=proxy, limit=MAX_COMMENTS),
            )
            if community:
                payload["community"] = community
    except Exception:
        pass

    repo.save_reading(
        conn,
        document_id=document_id,
        reading=payload,
        problem=payload["problem"] or "未知",
        method=payload["method"] or "未知",
        evidence=evidence_list,
        conclusions=conclusions,
        limitations=limitations,
    )
    repo.mark_status(conn, [document_id], "kb_ready")
    # 入库后立即建向量；模型不可用时该调用静默返回空，不影响精读结果。
    ensure_vectors(conn, [document_id])
    try:
        from .retrieval import index_document_units

        index_document_units(conn, document_id)
    except Exception as exc:  # 检索索引失败不能回滚已经完成的精读
        print(f"[deep_read] index units skipped: {type(exc).__name__}: {exc}", flush=True)

    return DeepReadResult(
        document_id=document_id,
        local_path=str(content_path),
        chars=len(content),
        chunks=len(chunks),
        claims=len(claim_ids),
        card=payload,
        source_format=source_format,
    )


def article_dir(config: AppConfig, canonical_id: str) -> Path:
    return config.root / "data" / "articles" / safe_name(canonical_id)
