"""领域包：把「领域知识」从流水线里拆出来，流水线只读配置。

一个领域 = `config/domains/<id>/domain.yaml`，只放数据、不放代码：

- 分类体系（名称 + 关键词）与默认分类
- 实体词表（产品/机构、出处、通用词）
- 事件动作词（发布/融资/事故……）
- 领域级权重覆盖

调用方通过 `load_profile(root, domain_id)` 或 `default_profile()` 取到不可变的
`DomainProfile`，再传给 `tagging` / `entities` / `events`，不再读模块级常量。
"""

from .profile import (
    DEFAULT_DOMAIN,
    ActionRule,
    DomainError,
    DomainProfile,
    EntityVocab,
    Taxonomy,
    default_profile,
    load_profile,
)

__all__ = [
    "DEFAULT_DOMAIN",
    "ActionRule",
    "DomainError",
    "DomainProfile",
    "EntityVocab",
    "Taxonomy",
    "default_profile",
    "load_profile",
]
