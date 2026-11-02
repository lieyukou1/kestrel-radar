"""飞书渠道。

- `cards.py`：纯渲染，输入数据输出卡片 JSON，不碰网络。
- `client.py`：发送、授权与读取（长连接接收在 `jobs/serve.py`）。
"""

from __future__ import annotations

from .cards import (
    KB_PAGE_SIZE,
    KB_WINDOWS,
    KIND_LABEL,
    build_compare_card,
    build_detail_card,
    build_digest_card,
    build_kb_card,
    build_l3_card,
    build_qa_fallback_card,
    build_reason_card,
    build_related_card,
    build_remove_confirm_card,
    build_topic_card,
    dimension_name,
    kind_label,
    meta_line,
)
from .client import FeishuError, FeishuNotifier

__all__ = [
    "KIND_LABEL",
    "KB_PAGE_SIZE",
    "KB_WINDOWS",
    "FeishuError",
    "FeishuNotifier",
    "build_compare_card",
    "build_detail_card",
    "build_digest_card",
    "build_kb_card",
    "build_l3_card",
    "build_qa_fallback_card",
    "build_reason_card",
    "build_related_card",
    "build_remove_confirm_card",
    "build_topic_card",
    "dimension_name",
    "kind_label",
    "meta_line",
]
