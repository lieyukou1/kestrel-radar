from __future__ import annotations

import json
import sqlite3

from rapidfuzz import fuzz

from ..store import repo
from .embedding import cosine, ensure_vectors


def document_tags(document: sqlite3.Row) -> list[str]:
    try:
        value = json.loads(document["tags_json"] or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def find_related(
    conn: sqlite3.Connection, document_id: int, *, limit: int = 3
) -> list[dict]:
    """优先用本地向量算语义相关度，模型不可用时退回维度/标签/标题词面匹配。"""
    document = repo.get_document(conn, document_id)
    if document is None:
        raise ValueError(f"document not found: {document_id}")

    base_tags = set(document_tags(document))
    base_kb = repo.get_kb_article(conn, document_id)
    base_problem = (base_kb["problem"] or "") if base_kb else ""

    candidates = conn.execute(
        """
        SELECT * FROM documents
        WHERE status = 'kb_ready' AND id != ? AND merged_into IS NULL
        """,
        (document_id,),
    ).fetchall()

    candidate_ids = [int(candidate["id"]) for candidate in candidates]
    vectors = ensure_vectors(conn, [document_id, *candidate_ids])
    base_vector = vectors.get(document_id)

    results: list[dict] = []
    for candidate in candidates:
        candidate_id = int(candidate["id"])
        tags = set(document_tags(candidate))
        same_dimension = bool(document["primary_tag"]) and (
            candidate["primary_tag"] == document["primary_tag"]
        )
        union = base_tags | tags
        tag_overlap = len(base_tags & tags) / len(union) if union else 0.0
        title_similarity = fuzz.token_set_ratio(document["title"], candidate["title"]) / 100
        other_kb = repo.get_kb_article(conn, candidate_id)
        problem_similarity = 0.0
        if base_problem and other_kb and other_kb["problem"]:
            problem_similarity = fuzz.token_set_ratio(base_problem, other_kb["problem"]) / 100

        semantic: float | None = None
        if base_vector is not None and candidate_id in vectors:
            semantic = cosine(base_vector, vectors[candidate_id])

        if semantic is None:
            score = (
                0.5 * (1.0 if same_dimension else 0.0)
                + 0.3 * tag_overlap
                + 0.2 * title_similarity
                + 0.2 * problem_similarity
            )
            threshold = 0.3
        else:
            score = (
                0.45 * semantic
                + 0.25 * (1.0 if same_dimension else 0.0)
                + 0.15 * tag_overlap
                + 0.10 * title_similarity
                + 0.05 * problem_similarity
            )
            threshold = 0.3
        if score < threshold:
            continue

        reasons: list[str] = []
        if same_dimension:
            reasons.append("同维度")
        if base_tags & tags:
            reasons.append(f"共享标签 {len(base_tags & tags)} 个")
        if semantic is not None and semantic >= 0.55:
            reasons.append(f"语义相似 {int(semantic * 100)}%")
        elif title_similarity >= 0.6:
            reasons.append(f"标题相似 {int(title_similarity * 100)}%")
        if problem_similarity >= 0.6:
            reasons.append(f"问题描述相近 {int(problem_similarity * 100)}%")

        results.append(
            {
                "document_id": candidate_id,
                "title": candidate["title"],
                "tag": candidate["primary_tag"] or "未分类",
                "url": candidate["url"],
                "score": round(score, 3),
                "semantic": round(semantic, 3) if semantic is not None else None,
                "reason": " · ".join(reasons) or "关键词相近",
            }
        )

    results.sort(key=lambda item: item["score"], reverse=True)
    return results[:limit]


def compare_documents(
    conn: sqlite3.Connection, left_id: int, right_id: int
) -> dict:
    """基于两篇已精读文章的知识卡片做结构化对比，不引入卡片外信息。"""
    left = repo.get_document(conn, left_id)
    right = repo.get_document(conn, right_id)
    if left is None or right is None:
        raise ValueError("document not found")

    left_kb = repo.get_kb_article(conn, left_id)
    right_kb = repo.get_kb_article(conn, right_id)
    if left_kb is None or right_kb is None:
        raise ValueError("两篇都需要先完成精读")

    left_card = json.loads(left_kb["card_json"])
    right_card = json.loads(right_kb["card_json"])

    def joined(card: dict, key: str) -> str:
        values = card.get(key) or []
        return "；".join(str(item) for item in values) if values else "未知"

    rows = [
        {"aspect": "问题", "left": left_card.get("problem") or "未知",
         "right": right_card.get("problem") or "未知"},
        {"aspect": "方法", "left": left_card.get("method") or "未知",
         "right": right_card.get("method") or "未知"},
        {"aspect": "证据", "left": joined(left_card, "evidence"),
         "right": joined(right_card, "evidence")},
        {"aspect": "结论", "left": joined(left_card, "conclusions"),
         "right": joined(right_card, "conclusions")},
        {"aspect": "局限", "left": joined(left_card, "limitations"),
         "right": joined(right_card, "limitations")},
    ]

    shared = sorted(set(document_tags(left)) & set(document_tags(right)))
    same_dimension = bool(left["primary_tag"]) and left["primary_tag"] == right["primary_tag"]
    summary = (
        f"A《{left['title'][:36]}》归入 {left['primary_tag'] or '未分类'}；"
        f"B《{right['title'][:36]}》归入 {right['primary_tag'] or '未分类'}。"
        + ("两者属于同一维度。" if same_dimension else "两者分属不同维度。")
        + (f" 共同标签：{'、'.join(shared)}。" if shared else " 无共同标签。")
    )

    evidence = []
    for side, document_id in (("A", left_id), ("B", right_id)):
        claims = repo.list_claims(conn, document_id)[:5]
        evidence.append(
            {
                "side": side,
                "title": (left if side == "A" else right)["title"],
                "claims": [
                    {"id": int(claim["id"]), "kind": claim["kind"], "text": claim["text"]}
                    for claim in claims
                ],
            }
        )

    return {
        "left_id": left_id,
        "right_id": right_id,
        "same_dimension": same_dimension,
        "shared_tags": shared,
        "summary": summary,
        "rows": rows,
        "evidence": evidence,
    }
