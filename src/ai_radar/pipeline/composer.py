"""日报 / 周报的编排。

日报（每个工作日）：
  1. 以「事件」为展示单位：主线排第一，其余事件按分数排在后面，最多 12 个；
  2. 每个事件的详情保留全部成员（同题转载折叠），不再单独切「快讯」；
  3. 单篇事件（只有 1 篇文章）同样算事件，排在末尾，保证任何一天都有东西可看。

周报（周一）：
  论文 3 篇 + 仓库 2 个，按「发酵后的热度」而不是发布新鲜度排序。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz

from ..store import repo
from ..util import parse_iso, utcnow
from . import embedding, events, signals
from .entity_conflict import titles_conflict

# 一个事件的详情最多展示几篇，避免单个热点刷屏。
EVENT_MAX_ITEMS = 6


@dataclass(slots=True)
class Block:
    section: str
    title: str
    items: list[dict] = field(default_factory=list)


@dataclass(slots=True)
class Composition:
    kind: str
    blocks: list[Block] = field(default_factory=list)
    # 一级展示单位：事件列表（已有的排序结果）
    events: list[events.Event] = field(default_factory=list)
    theme: events.Event | None = None
    theme_reason: str = ""
    theme_dimension: str = ""
    # 主线之外的事件：用于跨天跟踪与「其他进展」标注
    progress: list[events.Event] = field(default_factory=list)

    @property
    def items(self) -> list[dict]:
        if self.events:
            return [item for event in self.events for item in event.items]
        return [item for block in self.blocks for item in block.items]


def _doc_id(entry: dict) -> int:
    try:
        return int(entry["doc"]["id"])
    except (KeyError, IndexError, TypeError, ValueError):
        return 0


def _kind(entry: dict) -> str:
    try:
        return str(entry["doc"]["kind"])
    except (KeyError, IndexError, TypeError):
        return ""


def _age_hours(entry: dict, now) -> float:
    document = entry["doc"]
    published = parse_iso(document["published_at"]) or parse_iso(document["first_seen_at"])
    if published is None:
        return 10_000.0
    return max(0.0, (now - published).total_seconds() / 3600)


def _topic_dimension(topic: events.Event) -> str:
    """主线的维度取成员里出现最多的那个；平票时看谁的高分条目更高。"""
    counts: dict[str, int] = {}
    best: dict[str, float] = {}
    for entry in topic.items:
        tag = str(entry.get("tag") or "")
        counts[tag] = counts.get(tag, 0) + 1
        best[tag] = max(best.get(tag, 0.0), float(entry["score"]))
    if not counts:
        return ""
    return max(counts, key=lambda tag: (counts[tag], best[tag]))


def topic_dimension(topic: events.Event) -> str:
    """对外暴露：重打标后需要重新计算主线维度。"""
    return _topic_dimension(topic)


def _pick_theme(
    found: list[events.Event],
    rotation,
    band: float,
    theme_pref: dict | None = None,
    blocked: frozenset[str] | set[str] | None = None,
) -> events.Event | None:
    """先看头名，再在 85% 分带内按主线赤字挑一个：不为了轮转去推明显冷门的话题。

    `blocked` 里是事件 key：被模型否决、或确认是重复回放的事件不再参与挑选。
    """
    blocked = blocked or set()
    # 单篇事件可以展示，但不能当主线：主线必须真的是「多篇在讲同一件事」。
    strong = [
        topic for topic in found if topic.key not in blocked and len(topic.items) >= 2
    ]
    if not strong:
        return None
    # 用户用自然语言指定了主题：只在命中的主线里挑，没有命中再退回常规逻辑。
    if theme_pref:
        matched = [topic for topic in strong if _topic_matches(topic, theme_pref)]
        if matched:
            strong = matched
    best = strong[0]
    if rotation is None or not rotation.active:
        return best
    floor = best.score * band
    in_band = [topic for topic in strong if topic.score >= floor]
    return max(
        in_band,
        key=lambda topic: (rotation.theme_weight(_topic_dimension(topic)), topic.score),
    )


def _topic_matches(topic: events.Event, theme_pref: dict) -> bool:
    dimension = theme_pref.get("dimension")
    if dimension and _topic_dimension(topic) == dimension:
        return True
    words = [
        str(word).casefold() for word in theme_pref.get("keywords") or [] if str(word).strip()
    ]
    if not words:
        return False
    for entry in topic.items:
        title = str(entry["doc"]["title"] or "").casefold()
        if any(word in title for word in words):
            return True
    return False


def theme_pref_matches(topic: events.Event, theme_pref: dict | None) -> bool:
    """对外暴露：这条主线是否来自用户设定的临时主题。"""
    return bool(theme_pref) and _topic_matches(topic, theme_pref or {})


def _duplicate_of(entry: dict, others: list[dict], vectors: dict[int, list[float]]) -> bool:
    """同一条新闻被多个号转发：标题高度重叠，或语义几乎一致。"""
    title = str(entry["doc"]["title"])
    vector = vectors.get(_doc_id(entry))
    for other in others:
        other_title = str(other["doc"]["title"])
        # 同一个产品的两件事（发布 vs 越狱、GPT-5 vs GPT-5.1）不是转载，别折叠。
        if titles_conflict(title, other_title):
            continue
        # 中文标题没有空格，token 级比较失效，用字符级的 partial + 整体比例双闸门。
        if (
            fuzz.partial_ratio(title, other_title) >= 75
            and fuzz.ratio(title, other_title) >= 50
        ):
            return True
        other_vector = vectors.get(_doc_id(other))
        if vector and other_vector and embedding.cosine(vector, other_vector) >= 0.95:
            return True
    return False


def compose_daily(
    conn,
    config,
    scored: list[dict],
    *,
    now=None,
    size: int = 12,
    rotation=None,
    theme_pref: dict | None = None,
    blocked_keys: set[str] | frozenset[str] | None = None,
    allow_theme: bool = True,
    found_events: list[events.Event] | None = None,
    progress_events: int = 2,
    sources_map: dict[int, set[str]] | None = None,
) -> Composition:
    """编排日报：主线排第一，其余事件按分数补齐到 `size` 个。

    `found_events` 由调用方传入（模型切分 + 单篇事件）；不传时按实体粗分组兜底，
    保证单测和离线环境仍能产出事件列表，不会因为少一次模型调用就空手。
    """
    now = now or utcnow()
    weights = config.weights
    cfg = weights.get("topics") or {}
    rotation_cfg = weights.get("rotation") or {}

    pool = sorted(scored, key=lambda item: float(item["score"]), reverse=True)
    topic_pool = pool[: int(cfg.get("pool", 180))]
    if found_events is None:
        buckets = events.group_buckets(
            topic_pool,
            sources_map=sources_map,
            authority_weights=weights.get("authority"),
            now=now,
            min_members=int(cfg.get("min_members", 2)),
            entity_max_docs=int(cfg.get("max_docs", 12)),
        )
        found = events.fallback_events(
            buckets,
            sources_map=sources_map,
            authority_weights=weights.get("authority"),
            now=now,
        )
        used = {int(item["doc"]["id"]) for event in found for item in event.items}
        found.extend(
            events.standalone_events(
                topic_pool,
                used_ids=used,
                sources_map=sources_map,
                authority_weights=weights.get("authority"),
                now=now,
                limit=int(cfg.get("standalone", 40)),
                min_score=float(cfg.get("standalone_min_score", 0.5)),
            )
        )
        found.sort(key=lambda event: event.score, reverse=True)
    else:
        found = list(found_events)

    theme = (
        _pick_theme(
            found,
            rotation,
            float(rotation_cfg.get("theme_band", 0.85)),
            theme_pref,
            blocked=blocked_keys,
        )
        if allow_theme
        else None
    )

    ordered: list[events.Event] = []
    for event in found:
        if len(ordered) >= size:
            break
        if not event.items:
            continue
        ordered.append(event)
    # 主线排第一，其余按事件分排序；单篇事件分数天然低，自然落在末尾。
    ordered.sort(key=lambda event: (event is not theme, -event.score))

    displayed: list[events.Event] = []
    for event in ordered:
        event.items = event.items[:EVENT_MAX_ITEMS]
        if event.items:
            displayed.append(event)
    if theme is not None and theme not in displayed:
        theme = None
    progress = (
        [event for event in displayed if event is not theme][:progress_events]
        if theme is not None
        else []
    )

    return Composition(
        kind="daily",
        events=displayed,
        theme=theme,
        theme_dimension=_topic_dimension(theme) if theme is not None else "",
        progress=progress,
    )


def _weekly_score(entry: dict, conn, sources_map: dict[int, set[str]]) -> float:
    """周报看「发酵后的热度」：热度权重最高，时效只作微调。"""
    document_id = _doc_id(entry)
    # 周报要选「真的热过」的：没有信号的文档记 0，而不是白拿中性分 0.5。
    heat = signals.heat_score(conn, document_id, _kind(entry), neutral=0.0)
    authority = 1.0 if str(entry["doc"]["authority"]) in ("S", "A") else 0.0
    sources = len(sources_map.get(document_id) or ())
    return 0.55 * heat + 0.25 * authority + 0.10 * min(1.0, sources / 3) + 0.10 * float(entry["score"])


def compose_weekly(
    conn,
    config,
    scored: list[dict],
    *,
    now=None,
    paper_limit: int = 3,
    repo_limit: int = 2,
    window_days: int = 10,
) -> Composition:
    now = now or utcnow()
    sources_map = repo.document_sources_map(conn)

    papers = [
        entry
        for entry in scored
        if _kind(entry) == "paper" and _age_hours(entry, now) <= window_days * 24
    ]
    papers.sort(key=lambda entry: _weekly_score(entry, conn, sources_map), reverse=True)
    paper_picks = papers[:paper_limit]

    repos = [entry for entry in scored if _kind(entry) == "repo"]
    repos.sort(
        key=lambda entry: signals.repo_trend(conn, _doc_id(entry))["stars_per_day"],
        reverse=True,
    )
    repo_picks = repos[:repo_limit]

    blocks: list[Block] = []
    if paper_picks:
        blocks.append(Block(section="weekly_paper", title="本周论文", items=paper_picks))
    if repo_picks:
        blocks.append(Block(section="weekly_repo", title="本周仓库", items=repo_picks))
    return Composition(kind="weekly", blocks=blocks)
