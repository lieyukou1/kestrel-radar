from __future__ import annotations

import pytest

from ai_radar.pipeline.entity_conflict import signature, titles_conflict


@pytest.mark.parametrize(
    ("left", "right", "conflict"),
    [
        # 同产品、不同事件：发布 vs 越狱，不该合并
        ("Gemma 4 发布", "Gemma 4 被越狱", True),
        ("Qwen3 发布", "Qwen3 被越狱", True),
        ("Claude 4.5 上线", "Claude 4.5 出现漏洞", True),
        # 同产品、同一类事件：属于同一件事的不同说法，可以合并
        ("Gemma 4 发布", "Gemma 4 开源", False),
        ("豆包 1.5 发布", "豆包 1.5 正式上线", False),
        # 不同产品：不是同一条
        ("GPT-5 is here", "Claude 4 is here", True),
        ("豆包 1.5 发布", "文心 4.0 发布", True),
        # 同产品、不同版本：两代产品
        ("GPT-5 发布", "GPT-5.1 发布", True),
        ("豆包 1.5 发布", "豆包 1.6 发布", True),
        # 描述不清（同时命中发布与事故）放行，宁可漏拦
        ("Gemma 4 发布后被越狱", "Gemma 4 被越狱", False),
        # 单侧没有产品名：退回相似度判断
        ("OpenAI 融资", "中国 AI 公司融资", False),
        ("owner/repo 更新", "owner/repo 发布", False),
        # 产品名不能嵌在英文单词里
        ("Philosophy of mind", "Phi-4 发布", False),
    ],
)
def test_titles_conflict_matrix(left: str, right: str, conflict: bool) -> None:
    assert titles_conflict(left, right) is conflict


def test_signature_extracts_product_and_version() -> None:
    vendors, products = signature("Qwen3 与 GPT-5.1 对比")
    assert vendors == {"alibaba", "openai"}
    assert products == {("qwen", "3"), ("gpt", "5.1")}


def test_signature_ignores_years() -> None:
    _, products = signature("豆包 2026 年路线图")
    assert products == {("豆包", "")}
