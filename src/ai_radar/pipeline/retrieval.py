"""知识库混合检索。

精读产物（doc / section / claim / evidence）统一写入 `search_units`，
查询时同时跑 BM25 与向量，RRF 融合后按 claim → section → document 展开上下文。
本地规模只有几千个单元，BM25 在 Python 里算即可，不依赖 SQLite 扩展。
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field

from ..store import repo
from . import embedding

CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
WORD_RE = re.compile(r"[a-z0-9][a-z0-9._+\-]*")
RRF_K = 60
# bge-small-zh 对无关中文文本也能给出 0.45~0.55，必须设门槛，
# 否则问答对任何问题都会“检索到东西”，失去拒答能力。
DENSE_MIN = 0.55


@dataclass(slots=True)
class RetrievalHit:
    unit_id: int
    unit_type: str
    document_id: int
    document_title: str
    text: str
    anchor: str
    score: float
    metadata: dict = field(default_factory=dict)
    claim_id: int | None = None
    claim_text: str = ""
    question: str = ""
    claim_type: str = ""
    confidence: str = ""
    quote: str = ""
    evidence_id: int | None = None
    section: str = ""


@dataclass(slots=True)
class _Index:
    key: tuple[int, int, str]
    rows: list[sqlite3.Row]
    tokens: list[list[str]]
    document_freq: dict[str, int]
    avg_len: float


_INDEX: _Index | None = None


def tokenize(text: str) -> list[str]:
    """中文用单字 + 二元组，英文/型号用词；兼顾精确匹配和中文召回。"""
    value = str(text or "").lower()
    tokens: list[str] = []
    for word in WORD_RE.findall(value):
        if len(word) >= 1:
            tokens.append(word)
    for run in CJK_RE.findall(value):
        tokens.extend(run)
        tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def _index_key(rows: list[sqlite3.Row]) -> tuple[int, int, str]:
    if not rows:
        return (0, 0, "")
    return (
        len(rows),
        max(int(row["id"]) for row in rows),
        max(str(row["updated_at"] or "") for row in rows),
    )


def _load_index(conn: sqlite3.Connection) -> _Index:
    global _INDEX
    rows = repo.list_search_units(conn, limit=20000)
    key = _index_key(rows)
    if _INDEX is not None and _INDEX.key == key:
        return _INDEX
    tokens = [tokenize(row["text"]) for row in rows]
    document_freq: dict[str, int] = {}
    for item in tokens:
        for token in set(item):
            document_freq[token] = document_freq.get(token, 0) + 1
    avg_len = sum(len(item) for item in tokens) / len(tokens) if tokens else 0.0
    _INDEX = _Index(
        key=key,
        rows=rows,
        tokens=tokens,
        document_freq=document_freq,
        avg_len=avg_len,
    )
    return _INDEX


def _matches(
    row: sqlite3.Row,
    *,
    document_ids: list[int] | None,
    tag: str | None,
    kind: str | None,
    unit_types: tuple[str, ...] | None,
) -> bool:
    if document_ids and int(row["document_id"]) not in document_ids:
        return False
    if tag and str(row["primary_tag"] or "") != tag:
        return False
    if kind and str(row["kind"] or "") != kind:
        return False
    if unit_types and str(row["unit_type"]) not in unit_types:
        return False
    return True


def _bm25_scores(
    index: _Index,
    query_tokens: list[str],
    allowed: list[int],
) -> dict[int, float]:
    """返回 {行号: 归一化 BM25 分数}。"""
    if not query_tokens or not allowed:
        return {}
    total = len(index.rows)
    k1, b = 1.2, 0.75
    scores: dict[int, float] = {}
    for position in allowed:
        tokens = index.tokens[position]
        if not tokens:
            continue
        length = len(tokens)
        frequencies: dict[str, int] = {}
        for token in tokens:
            frequencies[token] = frequencies.get(token, 0) + 1
        score = 0.0
        for token in query_tokens:
            frequency = frequencies.get(token, 0)
            if not frequency:
                continue
            df = index.document_freq.get(token, 0)
            idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
            denominator = frequency + k1 * (
                1 - b + b * length / (index.avg_len or 1.0)
            )
            score += idf * frequency * (k1 + 1) / denominator
        if score > 0:
            scores[position] = score
    if not scores:
        return {}
    top = max(scores.values())
    return {key: value / top for key, value in scores.items()}


def _dense_scores(
    conn: sqlite3.Connection,
    question: str,
    rows: list[sqlite3.Row],
    allowed: list[int],
) -> dict[int, float]:
    embedded = embedding.embed_texts([question])
    if not embedded:
        return {}
    vectors = repo.list_unit_vectors(
        conn, embedding.model_name(), [int(rows[i]["id"]) for i in allowed]
    )
    if not vectors:
        return {}
    query_vector = embedded[0]
    scores: dict[int, float] = {}
    for position in allowed:
        unit_id = int(rows[position]["id"])
        vector = vectors.get(unit_id)
        if vector:
            scores[position] = max(0.0, embedding.cosine(query_vector, vector))
    return scores


def search_units(
    conn: sqlite3.Connection,
    question: str,
    *,
    limit: int = 8,
    document_ids: list[int] | None = None,
    tag: str | None = None,
    kind: str | None = None,
    unit_types: tuple[str, ...] | None = None,
) -> list[RetrievalHit]:
    """混合检索；没有索引时返回空列表，由调用方回退旧逻辑。"""
    index = _load_index(conn)
    if not index.rows:
        return []

    allowed = [
        position
        for position, row in enumerate(index.rows)
        if _matches(
            row,
            document_ids=document_ids,
            tag=tag,
            kind=kind,
            unit_types=unit_types,
        )
    ]
    if not allowed:
        return []

    query_tokens = tokenize(question)
    sparse = _bm25_scores(index, query_tokens, allowed)
    dense = _dense_scores(conn, question, index.rows, allowed)

    if dense:
        # 有向量时以语义门槛为准：中文单字 BM25 几乎对任何文本都能命中，
        # 不能拿来做"是否相关"的判据，只参与后面的排序融合。
        relevant = {
            position for position, value in dense.items() if value >= DENSE_MIN
        }
    else:
        # 向量不可用时退回词面匹配，但要求至少命中一个二元组/英文词。
        query_set = set(query_tokens)
        relevant = set()
        for position in allowed:
            matched = query_set.intersection(index.tokens[position])
            if any(len(token) >= 2 for token in matched):
                relevant.add(position)
    if not relevant:
        return []

    ranks: dict[int, float] = {}
    for rank, (position, _) in enumerate(
        sorted(
            ((position, value) for position, value in sparse.items() if position in relevant),
            key=lambda item: item[1],
            reverse=True,
        ),
        start=1,
    ):
        ranks[position] = ranks.get(position, 0.0) + 1.0 / (RRF_K + rank)
    for rank, (position, _) in enumerate(
        sorted(
            ((position, value) for position, value in dense.items() if position in relevant),
            key=lambda item: item[1],
            reverse=True,
        ),
        start=1,
    ):
        ranks[position] = ranks.get(position, 0.0) + 1.0 / (RRF_K + rank)

    ordered = sorted(
        ranks.items(),
        key=lambda item: (
            item[1],
            sparse.get(item[0], 0.0),
            dense.get(item[0], 0.0),
        ),
        reverse=True,
    )
    hits: list[RetrievalHit] = []
    seen: set[tuple[str, int]] = set()
    for position, score in ordered[: limit * 2]:
        row = index.rows[position]
        key = (str(row["unit_type"]), int(row["unit_id"]))
        if key in seen:
            continue
        seen.add(key)
        hits.append(_to_hit(conn, row, score))
        if len(hits) >= limit:
            break
    return hits


def _to_hit(conn: sqlite3.Connection, row: sqlite3.Row, score: float) -> RetrievalHit:
    unit_type = str(row["unit_type"])
    unit_id = int(row["unit_id"])
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except (TypeError, ValueError):
        metadata = {}
    hit = RetrievalHit(
        unit_id=unit_id,
        unit_type=unit_type,
        document_id=int(row["document_id"]),
        document_title=str(row["title"] or ""),
        text=str(row["text"] or ""),
        anchor=str(row["anchor"] or ""),
        score=round(score, 4),
        metadata=metadata if isinstance(metadata, dict) else {},
    )
    hit.section = str(hit.metadata.get("section") or "")
    if unit_type in ("claim", "evidence"):
        claim_id = unit_id if unit_type == "claim" else int(metadata.get("claim_id") or 0)
        claim = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
        if claim is not None:
            hit.claim_id = int(claim["id"])
            hit.claim_text = str(claim["text"] or "")
            hit.question = str(claim["question"] or "")
            hit.claim_type = str(claim["claim_type"] or claim["kind"] or "")
            hit.confidence = str(claim["confidence"] or "")
            hit.section = str(metadata.get("section") or "")
        if unit_type == "evidence":
            evidence = conn.execute(
                "SELECT * FROM evidence WHERE id = ?", (unit_id,)
            ).fetchone()
            if evidence is not None:
                hit.evidence_id = int(evidence["id"])
                hit.quote = str(evidence["quote"] or "")
                hit.anchor = str(evidence["anchor"] or hit.anchor)
    elif unit_type == "section":
        section = conn.execute(
            "SELECT * FROM sections WHERE id = ?", (unit_id,)
        ).fetchone()
        if section is not None:
            hit.section = str(section["heading"] or "")
            hit.quote = str(section["summary"] or "")
    return hit


def _reading(conn: sqlite3.Connection, document_id: int) -> dict:
    return repo.read_card(conn, document_id)


def index_document_units(conn: sqlite3.Connection, document_id: int) -> dict:
    """把一篇精读产物展开成 doc / section / claim / evidence 四层检索单元。"""
    document = repo.get_document(conn, document_id)
    if document is None:
        return {"units": 0}
    reading = _reading(conn, document_id)
    title = str(document["title"] or "")
    thesis = str(reading.get("thesis") or "")
    problem = str(reading.get("problem") or "")
    method = str(reading.get("method") or reading.get("method_summary") or "")
    questions = reading.get("questions") or []
    narrative = str(reading.get("narrative") or "")

    units: list[dict] = []
    doc_text = "\n".join(
        part
        for part in [
            f"《{title}》",
            f"核心主张：{thesis}" if thesis else "",
            f"问题：{problem}" if problem else "",
            f"方法：{method}" if method else "",
            ("能回答的问题：" + "；".join(str(item) for item in questions))
            if questions
            else "",
            narrative[:2500],
        ]
        if part
    )
    units.append(
        {
            "unit_type": "doc",
            "unit_id": document_id,
            "text": doc_text,
            "anchor": "",
            "metadata": {"tag": document["primary_tag"], "kind": document["kind"]},
        }
    )

    # 原始分块也进索引：旧精读没有章节/证据结构时，靠它保证召回。
    for chunk in repo.list_chunks(conn, document_id):
        section_name = str(chunk["section"] or "正文")
        chunk_text = "\n".join(
            part
            for part in [
                f"《{title}》",
                f"章节：{section_name}",
                f"全文主张：{thesis}" if thesis else "",
                str(chunk["text"] or ""),
            ]
            if part
        )
        units.append(
            {
                "unit_type": "chunk",
                "unit_id": int(chunk["id"]),
                "text": chunk_text,
                "anchor": str(
                    chunk["anchor"] or f"{section_name} · 片段 {chunk['ord']}"
                ),
                "metadata": {"section": section_name},
            }
        )

    for section in repo.list_sections(conn, document_id):
        section_text = "\n".join(
            part
            for part in [
                f"《{title}》",
                f"章节：{section['heading']}（{section['role']}）",
                f"本章作用：{section['summary']}" if section["summary"] else "",
                f"全文主张：{thesis}" if thesis else "",
            ]
            if part
        )
        units.append(
            {
                "unit_type": "section",
                "unit_id": int(section["id"]),
                "text": section_text,
                "anchor": str(section["anchor"] or section["heading"] or ""),
                "metadata": {
                    "section": section["heading"],
                    "role": section["role"],
                },
            }
        )

    claims = repo.list_claims(conn, document_id)
    for claim in claims:
        retrieval_text = str(claim["retrieval_text"] or "").strip()
        if not retrieval_text:
            retrieval_text = "\n".join(
                part
                for part in [
                    f"《{title}》",
                    f"主张：{claim['text']}",
                    f"问题：{claim['question']}" if claim["question"] else "",
                ]
                if part
            )
        units.append(
            {
                "unit_type": "claim",
                "unit_id": int(claim["id"]),
                "text": retrieval_text,
                "anchor": str(claim["anchor"] or ""),
                "metadata": {
                    "section": claim["anchor"],
                    "claim_type": claim["claim_type"],
                    "confidence": claim["confidence"],
                    "status": claim["status"],
                },
            }
        )

    evidence_by_claim: dict[int, list[sqlite3.Row]] = {}
    for item in repo.list_evidence(conn, document_id):
        evidence_by_claim.setdefault(int(item["claim_id"]), []).append(item)
    for claim_id, items in evidence_by_claim.items():
        claim = next((row for row in claims if int(row["id"]) == claim_id), None)
        question = str(claim["question"] or "") if claim is not None else ""
        for item in items:
            data = " / ".join(
                str(item[key] or "")
                for key in ("metric", "value", "unit", "baseline", "condition", "sample")
                if str(item[key] or "").strip()
            )
            text = "\n".join(
                part
                for part in [
                    f"《{title}》",
                    f"该证据回答的问题：{question}" if question else "",
                    f"证据：{item['quote']}",
                    f"数据：{data}" if data else "",
                ]
                if part
            )
            units.append(
                {
                    "unit_type": "evidence",
                    "unit_id": int(item["id"]),
                    "text": text,
                    "anchor": str(item["anchor"] or ""),
                    "metadata": {
                        "claim_id": claim_id,
                        "kind": item["kind"],
                    },
                }
            )

    repo.replace_search_units(conn, document_id, units)
    ensure_unit_vectors(conn)
    return repo.unit_counts(conn)


def ensure_unit_vectors(conn: sqlite3.Connection, *, limit: int = 400) -> int:
    """按需补算缺失的单元向量；模型不可用时静默返回 0。"""
    model = embedding.model_name()
    rows = repo.units_missing_vectors(conn, model, limit=limit)
    if not rows:
        return 0
    texts = [str(row["text"] or "")[:2000] for row in rows]
    vectors = embedding.embed_texts(texts)
    if vectors is None:
        return 0
    saved = 0
    for row, vector in zip(rows, vectors, strict=True):
        repo.save_unit_vector(
            conn, unit_id=int(row["id"]), model=model, vector=vector
        )
        saved += 1
    return saved


def reindex_all(conn: sqlite3.Connection, *, limit: int | None = None) -> dict:
    document_ids = sorted(repo.kb_document_ids(conn))
    if limit:
        document_ids = document_ids[:limit]
    for document_id in document_ids:
        index_document_units(conn, document_id)
    return {
        "documents": len(document_ids),
        "units": repo.unit_counts(conn),
    }


def format_context(
    conn: sqlite3.Connection, hits: list[RetrievalHit], *, max_chars: int = 8000
) -> tuple[str, list[dict]]:
    """把检索结果拼成带编号的上下文，并返回可点击的引用。"""
    blocks: list[str] = []
    citations: list[dict] = []
    for index, hit in enumerate(hits, start=1):
        label = f"{hit.unit_type[:1].upper()}{index}"
        lines = [f"[{label}] 标题：{hit.document_title}"]
        if hit.section:
            lines.append(f"章节：{hit.section}")
        if hit.question:
            lines.append(f"回答的问题：{hit.question}")
        if hit.claim_text:
            lines.append(f"主张：{hit.claim_text}")
        if hit.quote:
            lines.append(f"原文/证据：{hit.quote}")
        if hit.confidence:
            lines.append(f"置信度：{hit.confidence}")
        if hit.anchor:
            lines.append(f"位置：{hit.anchor}")
        if not hit.claim_text and not hit.quote:
            lines.append(hit.text[:1200])
        blocks.append("\n".join(lines))
        citations.append(
            {
                "label": label,
                "unit_type": hit.unit_type,
                "document_id": hit.document_id,
                "claim_id": hit.claim_id,
                "evidence_id": hit.evidence_id,
                "anchor": hit.anchor,
                "quote": hit.quote,
                "score": hit.score,
            }
        )
    context = "\n\n".join(blocks)
    return context[:max_chars], citations
