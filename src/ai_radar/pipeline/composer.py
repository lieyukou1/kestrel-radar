"""日报 / 周报的编排。

日报（每个工作日）：
  1. 从已切好的事件里取分数最高的 1 个当主线（事件由 events.py + 模型切分，
     「跨来源」已从成立条件降级为打分项，同一来源连发多条也能成题）；
  2. 主线内挑 2-3 条不同角度的内容，论文/仓库各最多 1 条充当背景；
  3. 其余事件各挑 1 条组成「其他进展」，只呈现、不抢主线；
  4. 补若干「快讯」，只取中文媒体与论坛，保证当天热度；有爆款则单独成块；
  5. 只有模型明确判定「这些内容不是一件事」时才降级成「无主线」，不硬凑。

周报（周一）：
  论文 3 篇 + 仓库 2 个，按「发酵后的热度」而不是发布新鲜度排序。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz

from ..store import repo
from ..util import parse_iso, utcnow
from . import embedding, events, rank, signals, topics
from .entity_conflict import titles_conflict


@dataclass(slots=True)
class Block:
    section: str
    title: str
    items: list[dict] = field(default_factory=list)


@dataclass(slots=True)
class Composition:
    kind: str
    blocks: list[Block]
    theme: topics.Topic | None = None
    theme_reason: str = ""
    theme_dimension: str = ""
    # 主线之外的事件：用于跨天跟踪与「其他进展」分区
    progress: list[events.Event] = field(default_factory=list)

    @property
    def items(self) -> list[dict]:
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


def _is_blockbuster(conn, entry: dict, config: dict) -> bool:
    gates = config.get("blockbuster") or {}
    kind = _kind(entry)
    if kind == "repo":
        trend = signals.repo_trend(conn, _doc_id(entry))
        return trend["stars_per_day"] >= float(gates.get("repo_stars_per_day", 500))
    rows = repo.latest_signals(conn, _doc_id(entry))
    if not rows:
        return False
    row = rows[0]
    source = str(row["source_id"])
    if source == "hackernews":
        return int(row["points"] or 0) >= int(gates.get("hn_points", 300))
    if source == "hf_daily":
        return int(row["points"] or 0) >= int(gates.get("hf_upvotes", 120))
    return False


def _topic_dimension(topic: topics.Topic) -> str:
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


def topic_dimension(topic: topics.Topic) -> str:
    """对外暴露：重打标后需要重新计算主线维度。"""
    return _topic_dimension(topic)


def _pick_theme(
    found: list[topics.Topic],
    rotation,
    band: float,
    theme_pref: dict | None = None,
    blocked: frozenset[str] | set[str] | None = None,
) -> topics.Topic | None:
    """先看头名，再在 85% 分带内按主线赤字挑一个：不为了轮转去推明显冷门的话题。

    `blocked` 里是事件 key：被模型否决、或确认是重复回放的事件不再参与挑选。
    """
    blocked = blocked or set()
    strong = [topic for topic in found if topic.key not in blocked]
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


def _topic_matches(topic: topics.Topic, theme_pref: dict) -> bool:
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


def theme_pref_matches(topic: topics.Topic, theme_pref: dict | None) -> bool:
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


def _theme_items(
    topic: topics.Topic, *, limit: int, vectors: dict[int, list[float]] | None = None
) -> list[dict]:
    """主线内选条目：论文 / 仓库各最多 1 条，其余留给媒体与讨论。

    同一件事常被多个公众号重复报道，标题几乎一样；这类重复不该占主线席位，
    否则「3 条主线」看着热闹，其实是同一条新闻。
    """
    vectors = vectors or {}
    picked: list[dict] = []
    paper_used = repo_used = False
    for entry in topics.pick_theme_items(topic, max_items=limit * 3):
        if _duplicate_of(entry, picked, vectors):
            continue
        kind = _kind(entry)
        if kind == "paper" and paper_used:
            continue
        if kind == "repo" and repo_used:
            continue
        picked.append(entry)
        paper_used = paper_used or kind == "paper"
        repo_used = repo_used or kind == "repo"
        if len(picked) >= limit:
            break
    return picked


def compose_daily(
    conn,
    config,
    scored: list[dict],
    *,
    now=None,
    size: int = 5,
    theme_limit: int = 3,
    rotation=None,
    theme_pref: dict | None = None,
    blocked_keys: set[str] | frozenset[str] | None = None,
    allow_theme: bool = True,
    found_events: list[events.Event] | None = None,
    progress_events: int = 2,
    sources_map: dict[int, set[str]] | None = None,
) -> Composition:
    """编排日报：一个事件当主线，另有 0-2 个事件当「其他进展」，其余进快讯。

    `found_events` 由调用方传入（模型切分后的结果）；不传时按实体粗分组兜底，
    这样单测和离线环境仍然能跑，不会因为少一次模型调用就完全没主线。
    """
    now = now or utcnow()
    weights = config.weights
    cfg = weights.get("topics") or {}
    rotation_cfg = weights.get("rotation") or {}
    max_per_tag = int((weights.get("selection") or {}).get("max_per_tag", 3))

    pool = sorted(scored, key=lambda item: float(item["score"]), reverse=True)
    topic_pool = pool[: int(cfg.get("pool", 180))]
    vectors = embedding.ensure_vectors(conn, [_doc_id(entry) for entry in topic_pool])
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
    else:
        found = list(found_events)
    # allow_theme=False 用于「模型判定不成主线」后的降级：条目直接回流到突发/快讯池。
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

    blocks: list[Block] = []
    chosen: list[dict] = []
    progress: list[events.Event] = []
    if theme is not None:
        theme_items = _theme_items(theme, limit=theme_limit, vectors=vectors)
        blocks.append(Block(section="theme", title="今日主线", items=theme_items))
        chosen.extend(theme_items)

    # 其他进展：每个次级事件只放一条代表，保证「有关联但不抢主线」。
    if theme is not None:
        blocked = set(blocked_keys or set())
        progress_items: list[dict] = []
        for event in found:
            if event is theme or event.key in blocked or not event.items:
                continue
            best = max(event.items, key=lambda item: float(item["score"]))
            if any(best is picked for picked in chosen):
                continue
            if _duplicate_of(best, chosen, vectors):
                continue
            progress.append(event)
            progress_items.append(best)
            chosen.append(best)
            if len(progress) >= progress_events:
                break
        if progress_items:
            blocks.append(Block(section="progress", title="其他进展", items=progress_items))

    # 爆款插队：从剩下的条目里找一条，单独成块
    breaking: list[dict] = []
    for entry in pool:
        if any(entry is picked for picked in chosen):
            continue
        if _is_blockbuster(conn, entry, weights):
            breaking.append(entry)
            break
    if breaking:
        blocks.append(Block(section="breaking", title="突发", items=breaking))
        chosen.extend(breaking)

    # 快讯只取中文媒体与论坛，不塞论文和仓库；
    # 已经在主线/突发里出现过的同一件事，也不能换个位置再出现一次。
    brief_pool = [
        entry
        for entry in pool
        if _kind(entry) in ("blog", "forum")
        and not any(entry is picked for picked in chosen)
        and not _duplicate_of(entry, chosen, vectors)
    ]
    room = max(0, size - len(chosen))
    tag_weights = (
        {tag: stat.weight for tag, stat in rotation.stats.items()}
        if rotation is not None and rotation.active
        else None
    )
    briefs = rank._select(
        brief_pool,
        size=room,
        max_per_tag=max_per_tag,
        mmr_lambda=float((weights.get("selection") or {}).get("mmr_lambda", 0.7)),
        base=chosen,
        tag_weights=tag_weights,
    )
    if briefs:
        blocks.append(Block(section="brief", title="快讯", items=briefs))

    return Composition(
        kind="daily",
        blocks=blocks,
        theme=theme,
        theme_reason="" if theme is None else "跨来源讨论形成的当日主线",
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
