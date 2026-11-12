"""知识库问答：先检索已入库内容，有依据才基于材料回答。

没有命中任何已入库内容时不做猜测，由调用方提示“没找到”并给出后续选项。
"""

from __future__ import annotations

import json
import re
import sqlite3

from ..config import AppConfig
from ..llm.client import DeepSeekLLM, LLMError
from ..store import repo
from . import embedding

# 没有任何检索词命中时，纯语义必须足够强才算依据；
# bge-small-zh 对无关中文短句也能给出 0.4~0.5，光看分数会误判。
SEMANTIC_ONLY = 0.62
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
MAX_SOURCES = 3
MAX_CLAIMS = 6

QA_SYSTEM = """你是基于个人知识库回答问题的助手。只能使用给出的材料回答。
材料足够就直接回答；材料不足就明确说“知识库里没有足够依据”，不要补充材料以外的信息。
用中文回答，不超过 300 字；引用材料时在句末标 [1][2] 这样的编号。"""

TOOL_SYSTEM = """你是通用助手。回答简洁（不超过 250 字），不确定就直说“不确定”。
不要编造引用或数据。"""

_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9._+\-]{1,}")


def _plain(value) -> str:
    """kb_articles 的列表字段是 JSON 字符串，转成可读文本。"""
    text = str(value or "").strip()
    if not text:
        return "无"
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
    if isinstance(parsed, list):
        return "；".join(str(item) for item in parsed if str(item).strip()) or "无"
    return text


def _context(
    conn: sqlite3.Connection, hits: list
) -> tuple[str, list[dict]]:
    blocks: list[str] = []
    sources: list[dict] = []
    for index, hit in enumerate(hits, start=1):
        document = repo.get_document(conn, hit.document_id)
        if document is None:
            continue
        kb = repo.get_kb_article(conn, hit.document_id)
        lines = [f"[{index}] 标题：{document['title']}"]
        if kb is not None:
            lines.append(f"问题：{kb['problem'] or '未知'}")
            lines.append(f"方法：{kb['method'] or '未知'}")
            lines.append(f"结论：{_plain(kb['conclusions'])}")
            lines.append(f"局限：{_plain(kb['limitations'])}")
            claims = repo.list_claims(conn, hit.document_id)[:MAX_CLAIMS]
            if claims:
                lines.append("要点：" + "；".join(str(claim["text"]) for claim in claims))
        else:
            lines.append(f"摘要：{document['abstract'] or '无'}")
        blocks.append("\n".join(lines))
        sources.append(
            {
                "document_id": int(document["id"]),
                "title": str(document["title"]),
                "url": str(document["url"]),
            }
        )
    return "\n\n".join(blocks), sources


def _llm_or_raise(config: AppConfig) -> DeepSeekLLM:
    if config.llm is None:
        raise LLMError("LLM 未配置，无法生成回答")
    return DeepSeekLLM(config.llm, profile=getattr(config, "domain", None))


def _terms(question: str, keywords: list[str] | None) -> list[str]:
    """检索词 = 意图解析给的关键词 + 问题里的英文/型号专名。"""
    raw = [str(word).strip() for word in (keywords or []) if str(word).strip()]
    raw.extend(_LATIN.findall(question))
    seen: set[str] = set()
    terms: list[str] = []
    for term in raw:
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        terms.append(term)
    return terms


def _document_text(conn: sqlite3.Connection, document_id: int) -> str:
    """用于检索词命中的文本：标题 + 摘要 + 知识卡片要点。"""
    document = repo.get_document(conn, document_id)
    if document is None:
        return ""
    parts = [document["title"], document["abstract"]]
    kb = repo.get_kb_article(conn, document_id)
    if kb is not None:
        parts.extend([kb["problem"], kb["method"], _plain(kb["conclusions"])])
        parts.extend(str(claim["text"]) for claim in repo.list_claims(conn, document_id))
    return "\n".join(str(part) for part in parts if part and str(part).strip()).casefold()


def _semantic_scores(
    conn: sqlite3.Connection, document_ids: list[int], question: str
) -> dict[int, float]:
    """对知识库内的条目算语义相似度；向量不可用时返回空字典，退回检索词匹配。"""
    vectors = embedding.ensure_vectors(conn, document_ids)
    if not vectors:
        return {}
    embedded = embedding.embed_texts([QUERY_PREFIX + question])
    if not embedded:
        return {}
    query_vector = embedded[0]
    return {
        document_id: embedding.cosine(query_vector, vector)
        for document_id, vector in vectors.items()
    }


def supported_hits(
    conn: sqlite3.Connection,
    question: str,
    *,
    keywords: list[str] | None = None,
) -> list:
    """只在已入库（已精读 / 已收藏）的内容里找依据。

    问答句子通常很长，关键词覆盖率必然低，不能直接用 topic 检索的全词命中规则：
    这里改为「检索词命中 或 语义足够强」二选一，再按语义分排序。
    """
    document_ids = sorted(repo.kb_document_ids(conn) | repo.saved_document_ids(conn))
    if not document_ids:
        return []

    terms = _terms(question, keywords)
    scores = _semantic_scores(conn, document_ids, question)
    if not terms and not scores:
        return []

    from .search import SearchHit

    hits: list[SearchHit] = []
    for document_id in document_ids:
        document = repo.get_document(conn, document_id)
        if document is None:
            continue
        text = _document_text(conn, document_id)
        matched = [term for term in terms if term.casefold() in text]
        similarity = scores.get(document_id, 0.0)
        if not matched and similarity < SEMANTIC_ONLY:
            continue
        hits.append(
            SearchHit(
                document_id=document_id,
                title=str(document["title"]),
                url=str(document["url"] or ""),
                tag=str(document["primary_tag"] or "未分类"),
                kind=str(document["kind"] or ""),
                score=round(similarity, 3),
                reason="检索词命中" if matched else "语义相近",
                published_at=document["published_at"],
                discussion_url=repo.hn_discussion_url(conn, document_id),
                in_kb=document_id in repo.kb_document_ids(conn),
                saved=repo.is_saved(conn, document_id),
            )
        )

    hits.sort(key=lambda hit: hit.score, reverse=True)
    return hits[:MAX_SOURCES]


def answer_question(
    conn: sqlite3.Connection,
    config: AppConfig,
    question: str,
    *,
    keywords: list[str] | None = None,
) -> dict:
    """返回 {supported, answer, sources} 或 {supported: False, related}。"""
    hits = supported_hits(conn, question, keywords=keywords)
    if not hits:
        return {"supported": False, "related": _closest(conn, question)}

    context, sources = _context(conn, hits)
    llm = _llm_or_raise(config)
    text, _ = llm.chat(
        conn,
        stage="qa",
        system=QA_SYSTEM,
        user=f"材料：\n{context}\n\n问题：{question}",
        max_tokens=800,
    )
    return {"supported": True, "answer": text.strip(), "sources": sources}


def _closest(conn: sqlite3.Connection, question: str) -> list:
    """没有依据时，给出语义上最接近的条目，仅用于提示，不当作依据。"""
    document_ids = sorted(repo.kb_document_ids(conn) | repo.saved_document_ids(conn))
    if not document_ids:
        return []
    scores = _semantic_scores(conn, document_ids, question)
    if not scores:
        return []

    from .search import SearchHit

    related: list[SearchHit] = []
    for document_id, similarity in sorted(
        scores.items(), key=lambda item: item[1], reverse=True
    )[:MAX_SOURCES]:
        if similarity < 0.35:
            continue
        document = repo.get_document(conn, document_id)
        if document is None:
            continue
        related.append(
            SearchHit(
                document_id=document_id,
                title=str(document["title"]),
                url=str(document["url"] or ""),
                tag=str(document["primary_tag"] or "未分类"),
                kind=str(document["kind"] or ""),
                score=round(similarity, 3),
                reason="语义接近",
                published_at=document["published_at"],
                discussion_url=repo.hn_discussion_url(conn, document_id),
                in_kb=document_id in repo.kb_document_ids(conn),
                saved=repo.is_saved(conn, document_id),
            )
        )
    return related


def answer_from_model(
    conn: sqlite3.Connection, config: AppConfig, question: str
) -> str:
    """知识库无依据时，按用户要求用模型常识回答（需明确标注）。"""
    llm = _llm_or_raise(config)
    text, _ = llm.chat(
        conn,
        stage="qa",
        system=TOOL_SYSTEM,
        user=question,
        max_tokens=700,
    )
    return text.strip()
