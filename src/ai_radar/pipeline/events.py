"""事件优先的聚合：先按实体粗分组，再让模型把桶切成「一件事」。

为什么不再用向量阈值：

- 同一个实体的两件不同事，bge 余弦能给到 0.79（Claude Fable 5.2 vs Claude Opus 5.5）；
- 同一件事的中英两篇报道只有 0.63（Meta Muse Ember）；
- 无关内容之间也有 0.65 左右的基线。

不存在一个阈值能同时成立，所以「是不是一件事」交给模型判断。
本模块只做确定性部分：粗分组、打分、把模型返回的分组还原成事件对象。
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..domain.profile import DomainProfile, default_profile
from ..util import parse_iso
from . import entities

# 一个事件最多呈现几条；多出来的留在候选池里做快讯。
MAX_EVENT_ITEMS = 6


@dataclass(slots=True)
class Bucket:
    """粗分组：同一个实体下的一批内容，可能包含多件事。"""

    index: int
    entity: str
    items: list[dict] = field(default_factory=list)
    score: float = 0.0
    source_count: int = 0


@dataclass(slots=True)
class Event:
    """最终事件：模型确认过的「一件事」，也是主线和进展的单位。"""

    key: str
    label: str
    entity: str
    items: list[dict] = field(default_factory=list)
    entities: set[str] = field(default_factory=set)
    score: float = 0.0
    source_count: int = 0
    why: str = ""
    when: str = ""
    is_update: bool | None = None
    change: str = ""
    day_index: int = 1
    new_count: int = 0
    first_date: str = ""

    @property
    def kinds(self) -> set[str]:
        return {str(item["doc"]["kind"]) for item in self.items}


def _doc_id(entry: dict) -> int:
    try:
        return int(entry["doc"]["id"])
    except (KeyError, IndexError, TypeError, ValueError):
        return 0


def action_of(text: str, *, profile: DomainProfile | None = None) -> str:
    return (profile or default_profile()).action_of(text)


def make_key(entity: str, action: str, when: str = "") -> str:
    """事件键：实体 + 动作 + 事件日期。

    事件日期进键是为了区分同一产品的两次发布：同一天同一动作视为同一件事，
    换了日期就是新版本，不会把「Qwen 3」并进「Qwen 2」。
    """
    return f"{entity}|{action}|{when}"


def _dominant_action(items: list[dict], *, profile: DomainProfile | None = None) -> str:
    default = (profile or default_profile()).default_action
    counter: Counter[str] = Counter(
        action_of(str(item["doc"]["title"] or ""), profile=profile) for item in items
    )
    for name, _count in counter.most_common():
        if name != default:
            return name
    return default


def _recency_share(items: list[dict], now: datetime | None) -> float:
    if not items or now is None:
        return 0.0
    fresh = 0
    for item in items:
        doc = item["doc"]
        published = parse_iso(doc["published_at"]) or parse_iso(doc["first_seen_at"])
        if published is not None and now - published <= timedelta(hours=24):
            fresh += 1
    return fresh / len(items)


def score_items(
    items: list[dict],
    *,
    sources_map: dict[int, set[str]] | None = None,
    authority_weights: dict | None = None,
    now: datetime | None = None,
) -> tuple[float, int]:
    """事件分：覆盖度 > 条数 > 权威 > 新鲜度 > 单篇最高分。

    覆盖度不是「几个来源」的硬门槛：同一来源的第二篇起按半条折半计入。
    头部媒体一天连发 4 篇，本身就是「这事很大」的信号，不能因为只有一个来源
    就被「两路各发 1 条」的普通事件压下去。
    """
    sources_map = sources_map or {}
    authority_weights = authority_weights or {}
    ids = {_doc_id(item) for item in items}
    source_count = len(
        {source for doc_id in ids for source in (sources_map.get(doc_id) or set())}
    )
    # 没有来源映射时（测试/离线）退回内容类型，至少不把多类型事件算成单来源。
    if source_count == 0:
        source_count = len({str(item["doc"]["kind"]) for item in items})
    reach = source_count + 0.5 * max(0, len(items) - source_count)
    authority = max(
        float(authority_weights.get(str(item["doc"]["authority"]), 0.3))
        for item in items
    )
    best = max(float(item["score"]) for item in items)
    score = (
        0.30 * min(1.0, reach / 3)
        + 0.30 * min(1.0, len(items) / 3)
        + 0.20 * authority
        + 0.10 * _recency_share(items, now)
        + 0.10 * best
    )
    return round(score, 6), source_count


def group_buckets(
    entries: list[dict],
    *,
    sources_map: dict[int, set[str]] | None = None,
    authority_weights: dict | None = None,
    now: datetime | None = None,
    min_members: int = 2,
    entity_max_docs: int = 12,
    max_buckets: int = 20,
    max_items: int = 8,
    profile: DomainProfile | None = None,
) -> list[Bucket]:
    """按实体粗分组；一条内容只归到最具体的实体，避免一稿多桶。"""
    if not entries:
        return []
    raw = {
        _doc_id(entry): entities.extract(
            str(entry["doc"]["title"] or ""), profile=profile
        )
        for entry in entries
    }
    kept = entities.filter_by_frequency(
        raw, min_docs=min_members, max_docs=entity_max_docs
    )
    grouped: dict[str, list[dict]] = defaultdict(list)
    for entry in entries:
        names = kept.get(_doc_id(entry)) or set()
        if not names:
            continue
        grouped[max(names, key=len)].append(entry)

    buckets: list[Bucket] = []
    for entity, items in grouped.items():
        if len(items) < min_members:
            continue
        items.sort(key=lambda item: float(item["score"]), reverse=True)
        trimmed = items[:max_items]
        score, source_count = score_items(
            trimmed,
            sources_map=sources_map,
            authority_weights=authority_weights,
            now=now,
        )
        buckets.append(
            Bucket(
                index=0,
                entity=entity,
                items=trimmed,
                score=score,
                source_count=source_count,
            )
        )
    buckets.sort(key=lambda bucket: bucket.score, reverse=True)
    result = buckets[:max_buckets]
    for index, bucket in enumerate(result, start=1):
        bucket.index = index
    return result


def bucket_payload(buckets: list[Bucket]) -> list[dict]:
    """喂给模型的输入：桶号 + 实体 + 标题（不带分数，避免模型被排序带偏）。"""
    return [
        {
            "bucket": bucket.index,
            "entity": bucket.entity,
            "items": [
                {"id": _doc_id(item), "title": str(item["doc"]["title"] or "")[:100]}
                for item in bucket.items
            ],
        }
        for bucket in buckets
    ]


def _clean_when(value) -> str:
    text = str(value or "").strip()
    return text if re.fullmatch(r"\d{4}(-\d{2}){1,2}", text) else ""


def build_events(
    buckets: list[Bucket],
    assignments: list[dict],
    *,
    sources_map: dict[int, set[str]] | None = None,
    authority_weights: dict | None = None,
    now: datetime | None = None,
    profile: DomainProfile | None = None,
) -> list[Event]:
    """把模型的分组还原成事件；只接受桶内真实存在的条目 id。"""
    by_index = {bucket.index: bucket for bucket in buckets}
    used: set[int] = set()
    events: list[Event] = []
    for item in assignments or []:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("bucket"))
        except (TypeError, ValueError):
            continue
        bucket = by_index.get(index)
        if bucket is None:
            continue
        ids: set[int] = set()
        for raw_id in item.get("item_ids") or []:
            try:
                ids.add(int(raw_id))
            except (TypeError, ValueError):
                continue
        chosen = [
            entry
            for entry in bucket.items
            if _doc_id(entry) in ids and _doc_id(entry) not in used
        ][:MAX_EVENT_ITEMS]
        if len(chosen) < 2:
            continue
        label = str(item.get("label") or "").strip()[:40] or bucket.entity
        when = _clean_when(item.get("when"))
        action = action_of(
            f"{label} {' '.join(str(e['doc']['title'] or '') for e in chosen)}",
            profile=profile,
        )
        score, source_count = score_items(
            chosen,
            sources_map=sources_map,
            authority_weights=authority_weights,
            now=now,
        )
        events.append(
            Event(
                key=make_key(bucket.entity, action, when),
                label=label,
                entity=bucket.entity,
                items=chosen,
                entities={bucket.entity},
                score=score,
                source_count=source_count,
                why=str(item.get("why") or "").strip(),
                when=when,
                is_update=item.get("is_update")
                if isinstance(item.get("is_update"), bool)
                else None,
                change=str(item.get("change") or "").strip(),
            )
        )
        used.update(_doc_id(entry) for entry in chosen)
    events.sort(key=lambda event: event.score, reverse=True)
    return events


def standalone_events(
    entries: list[dict],
    *,
    used_ids: set[int] | None = None,
    sources_map: dict[int, set[str]] | None = None,
    authority_weights: dict | None = None,
    now: datetime | None = None,
    limit: int = 40,
    min_score: float = 0.5,
    kinds: tuple[str, ...] = ("blog", "forum"),
    profile: DomainProfile | None = None,
) -> list[Event]:
    """没归进任何事件的内容，各自算一个「单篇事件」。

    以前这些内容会掉进「快讯」或者干脆不显示；现在它们也是事件，只是分数低、
    排在末尾。这样一级列表永远是满的，不会因为聚类弱就整天空白。

    论文与仓库不参与单篇事件：它们属于周报，日推只收新闻与讨论，
    否则 arXiv 每天几十篇会把事件列表挤满，也会把周报的候选提前消耗掉。
    """
    used_ids = used_ids or set()
    singles: list[Event] = []
    ordered = sorted(entries, key=lambda item: float(item["score"]), reverse=True)
    for entry in ordered:
        if len(singles) >= limit:
            break
        document_id = _doc_id(entry)
        if document_id in used_ids or float(entry["score"]) < min_score:
            continue
        if str(entry["doc"]["kind"]) not in kinds:
            continue
        title = str(entry["doc"]["title"] or "").strip()
        if not title:
            continue
        names = entities.extract(title, profile=profile)
        entity = max(names, key=len) if names else ""
        score, source_count = score_items(
            [entry],
            sources_map=sources_map,
            authority_weights=authority_weights,
            now=now,
        )
        singles.append(
            Event(
                # 单篇事件的键带上文档 id：同一天同一产品的两条独立新闻不能互相认亲。
                key=f"{entity or 'misc'}|{action_of(title, profile=profile)}|doc{document_id}",
                label=title[:40],
                entity=entity,
                items=[entry],
                entities={entity} if entity else set(),
                score=score,
                source_count=source_count,
            )
        )
    return singles


def fallback_events(
    buckets: list[Bucket],
    *,
    sources_map: dict[int, set[str]] | None = None,
    authority_weights: dict | None = None,
    now: datetime | None = None,
    profile: DomainProfile | None = None,
) -> list[Event]:
    """模型不可用时的兜底：一个实体桶就是一件事。

    宁可退回旧口径（可能把几件事揉在一起，命名时才暴露），也不能让当天没有主线。
    """
    events: list[Event] = []
    for bucket in buckets:
        action = _dominant_action(bucket.items, profile=profile)
        score, source_count = score_items(
            bucket.items,
            sources_map=sources_map,
            authority_weights=authority_weights,
            now=now,
        )
        events.append(
            Event(
                key=make_key(bucket.entity, action),
                label=bucket.entity,
                entity=bucket.entity,
                items=list(bucket.items),
                entities={bucket.entity},
                score=score,
                source_count=source_count,
            )
        )
    events.sort(key=lambda event: event.score, reverse=True)
    return events
