from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import httpx

from ..adapters.notify.base import DigestItemView, Notifier
from ..adapters.search_news import search_news
from ..adapters.url_safety import is_public_http_url
from ..config import AppConfig
from ..llm.client import BudgetExceeded, DeepSeekLLM, LLMError
from ..store import repo
from ..util import iso, parse_iso, utcnow
from . import composer, discussion, entities, rank, recall, rotation, signals, topics
from .cleaning import clean_html
from .preferences import paused_dimensions, paused_sources

# 社区观点最多给两条内容做，控制评论抓取与模型调用成本。
MAX_DISCUSSION_ITEMS = 2

# 主线最多抓 3 篇正文，用来找「事件本身发生的时间」；拿不到就算了，不能拖住推送。
THEME_FETCH_LIMIT = 3
THEME_FETCH_TIMEOUT = 6.0
THEME_BODY_CHARS = 1200
# 摘要短于这个长度基本是「点击查看原文」之类的空壳，需要抓正文。
THEME_MIN_ABSTRACT = 80
# 材料里找不到事件日期时，先去原文页面读发布日期；读不到才搜外网。
_SEARCH_DATE = re.compile(r"20\d{2}-\d{2}(?:-\d{2})?")
# 原文页面的发布日期：结构化字段优先，正文里的日期不可靠，不碰。
_META_DATE = re.compile(
    r"""<meta[^>]+(?:property|name|itemprop)=["']"""
    r"""(?:article:published_time|article:published|og:published_time|datepublished|"""
    r"""date|pubdate|publishdate|publish_date|pub_date|dc\.date(?:\.issued)?|"""
    r"""sailthru\.date|parsely-pub-date|firstpublished)["'][^>]*?content=["']([^"']{6,40})["']""",
    re.IGNORECASE,
)
_META_DATE_CONTENT_FIRST = re.compile(
    r"""<meta[^>]+content=["']([^"']{6,40})["'][^>]*?(?:property|name|itemprop)=["']"""
    r"""(?:article:published_time|article:published|og:published_time|datepublished|"""
    r"""date|pubdate|publishdate|publish_date|pub_date|dc\.date(?:\.issued)?)["']""",
    re.IGNORECASE,
)
_JSON_DATE = re.compile(r'"datePublished"\s*:\s*"([^"]{6,40})"', re.IGNORECASE)
_TIME_TAG_DATE = re.compile(r"""<time[^>]+datetime=["']([^"']{6,40})["']""", re.IGNORECASE)
# 页面原样 HTML 太大时截断，元信息都在 <head> 里，不影响取值。
PAGE_HTML_CHARS = 400_000
# 页面日期超过这个年头基本是站点模板里的假数据。
PAGE_DATE_MAX_AGE_DAYS = 3650
SEARCH_DATE_SYSTEM = """你是事实核查助手。用户会给你一个话题名和一批带日期的新闻报道。
请判断「这件事本身」最早发生或发布的时间，只输出日期，格式 YYYY-MM-DD 或 YYYY-MM。
如果这些报道只能说明它被讨论的时间、无法确定发布或发生时间，就输出空字符串。
不要输出任何解释。"""
# 检索结果会抖动：同一查询有时只回 8 条、有时回 30 条。查到一次就缓存，避免下次又变空。
EVENT_CACHE_PREFIX = "event_date:"
EVENT_CACHE_TTL = timedelta(days=7)
EVENT_SEARCH_LIMIT = 30
# 同一件事的媒体回声：事件时间没往前走还重复开主线，只会变成「又是这条」。
ECHO_MIN_DAYS = 3
# 事件时间拿不全时，同实体在这段时间内开过主线，先按回声处理。
ECHO_WINDOW_DAYS = 7


def _cached_event_date(conn: sqlite3.Connection, label: str) -> tuple[str, str] | None:
    raw = repo.get_text_preference(conn, f"{EVENT_CACHE_PREFIX}{label.strip().lower()}")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    date = str(data.get("date") or "").strip()
    if not date:
        return None
    at = parse_iso(data.get("at"))
    if at is not None and utcnow() - at > EVENT_CACHE_TTL:
        return None
    return date, str(data.get("note") or "")


def _store_event_date(conn: sqlite3.Connection, label: str, when: str, note: str) -> None:
    repo.set_text_preference(
        conn,
        f"{EVENT_CACHE_PREFIX}{label.strip().lower()}",
        json.dumps({"date": when, "note": note, "at": iso(utcnow())}, ensure_ascii=False),
    )


def _deliver(
    conn: sqlite3.Connection,
    notifier: Notifier,
    digest_id: int,
    date: str,
    items: list[DigestItemView],
    *,
    kind: str,
    theme_label: str,
    theme_reason: str,
    theme_query: str,
    theme_when: str = "",
    theme_when_note: str = "",
    preview: bool = False,
) -> str:
    try:
        return notifier.send_digest(
            date,
            items,
            kind=kind,
            theme_label=theme_label,
            theme_reason=theme_reason,
            theme_query=theme_query,
            theme_when=theme_when,
            theme_when_note=theme_when_note,
            preview=preview,
        )
    except Exception as exc:
        repo.mark_digest_failed(conn, digest_id, f"{type(exc).__name__}: {exc}")
        raise


def collect_candidates(
    conn: sqlite3.Connection,
    config: AppConfig,
    *,
    now: datetime,
) -> list[dict]:
    """按闸门筛出候选并打分：暂停、GitHub 双层校验、成熟窗口、去重。"""
    weights = config.weights
    maturity = weights.get("maturity_hours") or {}
    selection_cfg = weights.get("selection") or {}
    novelty_days = float(selection_cfg.get("novelty_days", 30))
    recent_titles = repo.recent_pushed_titles(conn, iso(now - timedelta(days=novelty_days)))
    # 只要最近确实推过，就不再进候选：状态被手动回退、或采集/重放把文档改回
    # observing 时，也不会把同一篇再推一遍。
    recent_pushed_ids = repo.pushed_document_ids(
        conn, iso(now - timedelta(days=novelty_days))
    )
    preferences = repo.get_preference_weights(conn)
    theme_pref = repo.get_theme_preference(conn)
    paused = paused_dimensions(conn)
    paused_src = paused_sources(conn)
    doc_sources = repo.document_sources_map(conn) if paused_src else {}

    scored: list[dict] = []
    for document in repo.list_candidates(conn):
        if int(document["id"]) in recent_pushed_ids:
            continue
        if document["primary_tag"] in paused:
            continue
        if paused_src:
            sources = doc_sources.get(int(document["id"])) or set()
            if sources and sources <= paused_src:
                continue
        if document["kind"] == "repo" and not signals.repo_gate_ok(
            conn, int(document["id"]), weights.get("repo_gate")
        ):
            continue
        published = parse_iso(document["published_at"]) or parse_iso(document["first_seen_at"])
        if published is None:
            continue
        # 个别源会把本地时间标成 UTC，历史数据可能落在未来；按发现时间兜底。
        if published > now:
            published = parse_iso(document["first_seen_at"]) or now
        if (now - published).total_seconds() / 3600 < float(maturity.get(document["kind"], 24)):
            continue
        scored.append(
            rank.score_document(
                document,
                heat=signals.heat_score(conn, int(document["id"]), document["kind"], weights),
                recent_titles=recent_titles,
                weights=weights,
                preferences=preferences,
                now=now,
                theme=theme_pref,
            )
        )
    return scored


def _fetch_html(url: str, *, proxy: str | None = None) -> str:
    """抓原文页面原始 HTML；失败静默返回空，不影响推送。"""
    if not url or not is_public_http_url(url):
        return ""
    # 代理可能没开：先按配置走，失败再直连。
    attempts = [proxy] if proxy else []
    attempts.append(None)
    for attempt_proxy in attempts:
        try:
            with httpx.Client(
                proxy=attempt_proxy,
                timeout=THEME_FETCH_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": "ai-radar/0.1 (personal research; contact: local)"},
            ) as client:
                response = client.get(url)
            if response.status_code >= 400 or not is_public_http_url(str(response.url)):
                continue
            return response.text[:PAGE_HTML_CHARS]
        except Exception:
            continue
    return ""


def _fetch_body(url: str) -> str:
    """抓正文用于判断事件时间；失败静默返回空，不影响推送。"""
    text = _fetch_html(url)
    if not text:
        return ""
    try:
        return clean_html(text, url, kind="blog")[:THEME_BODY_CHARS]
    except Exception:
        return ""


def _normalize_date(raw: str) -> str:
    """把 2026-09-15T08:00:00Z / 2026/9/15 / 2026年9月15日 统一成 YYYY-MM-DD。"""
    match = re.search(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})", str(raw))
    if not match:
        return ""
    year, month, day = (int(part) for part in match.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return f"{year:04d}-{month:02d}-{day:02d}"


def _page_date(page: str) -> str:
    """从原文页面读发布日期：只认结构化字段，正文里随手出现的日期不采信。"""
    if not page:
        return ""
    for pattern in (_META_DATE, _META_DATE_CONTENT_FIRST, _JSON_DATE, _TIME_TAG_DATE):
        for raw in pattern.findall(page):
            when = _normalize_date(raw)
            if when:
                return when
    return ""


def _host(url: str) -> str:
    try:
        host = str(httpx.URL(url).host or "")
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _source_event_date(theme, *, proxy: str | None = None) -> tuple[str, str]:
    """优先用主线成员的原文页面日期：它是事件自己的时间，不是转载时间。

    只采信「早于这条材料自己发布日期」的页面日期：
      - 一手公告页写的是事件当天，通常早于我们采集到它的时间 → 采用；
      - 转述文章页面写的就是它自己的发稿日，等于材料日期 → 没有新信息，跳过；
      - 混进来的同名论文/无关页面日期也贴着材料日期 → 一并跳过，不会篡位。
    仓库不参与：它的创建时间没有新闻意义上的「发布」。
    """
    today = utcnow().date()
    found: list[tuple[str, str]] = []
    attempts = 0
    for entry in theme.items:
        if attempts >= THEME_FETCH_LIMIT:
            break
        document = entry["doc"]
        if str(document["kind"]) == "repo":
            continue
        url = str(document["url"] or "")
        if not url:
            continue
        attempts += 1
        when = _page_date(_fetch_html(url, proxy=proxy))
        if not when:
            continue
        parsed = parse_iso(f"{when}T00:00:00+00:00")
        if parsed is None:
            continue
        page_day = parsed.date()
        # 未来日期和十年前的模板日期都不算数，宁可退回检索。
        if page_day > today or (today - page_day).days > PAGE_DATE_MAX_AGE_DAYS:
            continue
        material = None
        try:
            material = parse_iso(document["published_at"])
        except (KeyError, IndexError):
            material = None
        if material is not None and page_day >= material.date():
            continue
        found.append((when, url))
    if not found:
        return "", ""
    when, url = min(found, key=lambda item: item[0])
    return when, f"原文页面 · {_host(url)}"


def _theme_material_lines(theme) -> list[str]:
    """给起标题的模型喂标题 + 摘要；摘要为空壳时抓正文。

    事件发生时间常常只写在正文里（例如「9 月 23 日发布」），而材料的发布日期
    是另一件事，这里只提供文本，不让模型用发布日期充数。
    """
    lines: list[str] = []
    for entry in theme.items[:5]:
        document = entry["doc"]
        title = str(document["title"])
        abstract = " ".join(str(document["abstract"] or "").split())
        placeholder = len(abstract) < THEME_MIN_ABSTRACT or "点击查看原文" in abstract
        if placeholder and len(lines) < THEME_FETCH_LIMIT:
            body = _fetch_body(str(document["url"] or ""))
            if body:
                lines.append(f"- {title}\n  正文节选：{body}")
                continue
        if placeholder:
            lines.append(f"- {title}")
        else:
            lines.append(f"- {title}\n  摘要：{abstract[:160]}")
    return lines


def _search_queries(label: str) -> list[str]:
    """中英都查：中文报道的标题常写「正式开放/上线」，英文报道则更接近发布当天。"""
    return [
        f"{label} 发布 OR 上线 OR 推出 OR 正式开放",
        f"{label} release OR launch OR announced",
    ]


def _earliest_material_day(theme) -> tuple[str, str]:
    """最后的兜底：模型、原文页面、外网检索都拿不到事件时间时，用材料的最早发布日。

    媒体只会晚于事件报道，所以它是事件时间的「上界」，不是精确值——说明文字会带进卡片，
    避免以后被当成模型判断出来的事件时间。仓库类不参与：它的创建时间没有新闻意义。
    """
    days: list[str] = []
    for entry in theme.items:
        document = entry["doc"]
        try:
            if str(document["kind"]) == "repo":
                continue
        except (KeyError, IndexError):
            continue
        for key in ("published_at", "first_seen_at"):
            try:
                value = document[key]
            except (KeyError, IndexError):
                value = None
            parsed = parse_iso(value)
            if parsed is not None:
                days.append(parsed.date().isoformat())
                break
    if not days:
        return "", ""
    return min(days), "材料最早报道日（上界）"


def _search_event_date(
    llm: DeepSeekLLM,
    conn: sqlite3.Connection,
    label: str,
    *,
    proxy: str | None = None,
) -> tuple[str, str]:
    """材料里没有事件日期时，用新闻检索反查最早报道时间。

    返回 (日期, 来源说明)；搜不到或判断不了就返回空，绝不用材料日期顶替。
    """
    found: list[dict] = []
    cached = _cached_event_date(conn, label)
    if cached is not None:
        return cached
    for query in _search_queries(label):
        found.extend(search_news(query, proxy=proxy, limit=EVENT_SEARCH_LIMIT))
    if not found:
        return "", ""
    seen: set[str] = set()
    results: list[dict] = []
    for item in sorted(found, key=lambda entry: entry["published"] or "9999-99-99"):
        title = str(item["title"])
        if title in seen:
            continue
        seen.add(title)
        results.append(item)
    results = results[:8]
    lines = "\n".join(
        f"- {item['published'] or '日期未知'} {item['source']}: {item['title'][:90]}"
        for item in results
    )
    try:
        text, _ = llm.chat(
            conn,
            stage="theme_date",
            system=SEARCH_DATE_SYSTEM,
            user=f"话题：{label}\n\n检索到的报道：\n{lines}",
            # 思考模型会先花掉一批 token 再输出；60 会被截断成空内容，
            # 统一按 label_theme 的口径取 800，避免推理吃掉全部额度。
            max_tokens=800,
        )
    except (BudgetExceeded, LLMError):
        return "", ""
    match = _SEARCH_DATE.search(text or "")
    if match:
        when, note = match.group(0), f"据 {len(results)} 条检索报道"
        _store_event_date(conn, label, when, note)
        return when, note
    # 模型判断不出时，退到「最早报道日期」：它是事件时间的上界，且明确标注依据。
    dated = [item["published"] for item in results if item.get("published")]
    if not dated:
        return "", ""
    when, note = min(dated), f"据 {len(results)} 条检索报道 · 最早报道"
    _store_event_date(conn, label, when, note)
    return when, note


def _event_day(when: str) -> date | None:
    """事件时间可能是 YYYY-MM，按月初算；解析不了就返回 None。"""
    text = str(when or "").strip()
    if len(text) == 7:
        text += "-01"
    if len(text) < 10:
        return None
    parsed = parse_iso(f"{text[:10]}T00:00:00+00:00")
    return parsed.date() if parsed else None


def _echo_of(
    conn: sqlite3.Connection,
    entity: str,
    when: str,
    today: date,
    *,
    is_update: bool | None = None,
    change: str = "",
) -> sqlite3.Row | None:
    """同一实体上一次开主线：事件时间没推进，这次的报道就是回声。

    判定只用事件时间，不用材料日期——媒体发稿晚是常态，不代表事情又发生了一次。
    模型读了「上次推送」后给的 is_update 作为补充信号：
      - 明确说没变化 → 回声；
      - 说是更新、并且说得出变化内容 → 放行（实测里「上线新评分接口」这类功能更新
        没有版本号，关键词判断会误杀，所以只要求 change 非空）；
      - 其余情况仍按事件时间判。
    """
    previous = repo.last_topic_for_entity(conn, entity, today.isoformat())
    if previous is None:
        return None
    if is_update is False:
        return previous
    if is_update and change.strip():
        return None
    current, last = _event_day(when), _event_day(previous["event_date"])
    if current is not None and last is not None:
        # 事件时间往前走了（新版、新阶段），才允许再开一次主线。
        return None if (current - last).days >= ECHO_MIN_DAYS else previous
    # 事件时间拿不全：宁可当天少一条主线，也别把旧事重推一遍。
    pushed = _event_day(previous["topic_date"])
    if pushed is None or (today - pushed).days > ECHO_WINDOW_DAYS:
        return None
    return previous


def _echo_note(previous: sqlite3.Row, entity: str, when: str) -> str:
    label = str(previous["label"] or entity)
    pushed = previous["topic_date"]
    last = str(previous["event_date"] or "")
    if when and last:
        return (
            f"「{label}」这条 {pushed} 推过，事件时间仍是 {last}，"
            "这次只有后续报道，不再单开主线。"
        )
    if when:
        # 上次推送的记录里没有事件时间（老数据），只能拿这次的判断：它就是旧事件。
        return f"「{label}」这条 {pushed} 推过，这次报道的事件时间仍是 {when}，先不重复开主线。"
    return f"「{label}」这条 {pushed} 推过，这次没取到新的事件时间，先不重复开主线。"


@dataclass(slots=True)
class ThemeLabel:
    """主线的一次判定结果：名字、原因、事件时间，以及「这是不是同一件事的新阶段」。"""

    label: str = ""
    why: str = ""
    when: str = ""
    when_note: str = ""
    is_update: bool | None = None
    change: str = ""


def _theme_history(conn: sqlite3.Connection, entity: str, today: date) -> str:
    """把该实体上次推送的情况写给模型，它才知道今天这波是「又一轮」还是「更新」。"""
    previous = repo.last_topic_for_entity(conn, entity, today.isoformat())
    if previous is None:
        return ""
    event = str(previous["event_date"] or "") or "未知"
    return (
        f"\n\n该话题上次推送于 {previous['topic_date']}，标题「{previous['label']}」，"
        f"当时判断的事件时间是 {event}。"
    )


def _retag_selected(
    llm: DeepSeekLLM | None, conn: sqlite3.Connection, entries: list[dict]
) -> dict[int, str]:
    """给真正入选推送的少量内容做一次 LLM 重打标。

    采集时的关键词启发式会把「Agent 数据工程」归到 Agent、把集合名归到模型，
    只修正用户要看的那几条；调用失败或结果非法时沿用原标签，不影响推送。
    """
    classify = getattr(llm, "classify_dimensions", None)
    if llm is None or not entries or not callable(classify):
        return {}
    payload = [
        {
            "id": int(entry["doc"]["id"]),
            "title": str(entry["doc"]["title"] or ""),
            "kind": str(entry["doc"]["kind"] or ""),
            "abstract": str(entry["doc"]["abstract"] or ""),
        }
        for entry in entries
    ]
    try:
        mapping = classify(conn, payload)
    except (BudgetExceeded, LLMError) as exc:
        print(f"[digest] retag skipped: {type(exc).__name__}: {exc}", flush=True)
        return {}
    for entry in entries:
        document_id = int(entry["doc"]["id"])
        tag = mapping.get(document_id)
        if not tag or tag == entry.get("tag"):
            continue
        entry["tag"] = tag
        repo.update_primary_tag(conn, document_id, tag)
    return mapping


def _apply_theme_label(
    llm: DeepSeekLLM | None,
    conn: sqlite3.Connection,
    composition: composer.Composition,
    *,
    config: AppConfig | None = None,
    history: str = "",
) -> ThemeLabel:
    """让模型给主线起名、说明原因，并尽量给出事件时间。"""
    theme = composition.theme
    if theme is None:
        # 只有日报会降级成纯快讯；周报没有主线是正常结构，不该带这句话。
        note = (
            "今日没有跨来源的强主线，只推送时效性内容。"
            if composition.kind == "daily"
            else ""
        )
        return ThemeLabel(why=note)
    titles = _theme_material_lines(theme)
    fallback = (theme.label, f"围绕「{theme.label}」的讨论同时在多个来源出现。")
    if llm is None:
        return ThemeLabel(label=fallback[0], why=fallback[1])
    try:
        data = llm.label_theme(conn, titles, history=history)
    except (BudgetExceeded, LLMError):
        return ThemeLabel(label=fallback[0], why=fallback[1])
    label = str(data.get("label") or "").strip()
    why = str(data.get("why") or "").strip() or fallback[1]
    when = str(data.get("when") or "").strip()
    is_update = data.get("is_update")
    change = str(data.get("change") or "").strip()
    # 模型明确说「没有共同事件」时，不能再退回到实体名硬凑一个主线。
    if not label:
        return ThemeLabel(why="今日没有清晰的跨来源主线，只推送时效性内容。")
    # 主线必须是一件事：会议/期刊名和「N 篇论文」这类集合名都不算。
    if topics.is_bucket_label(label) or entities.is_venue(label):
        label = fallback[0]
    if not label.strip() or topics.is_bucket_label(label) or entities.is_venue(label):
        return ThemeLabel(why="今日没有清晰的跨来源主线，只推送时效性内容。")
    if when:
        return ThemeLabel(label, why, when, "模型推断", is_update, change)
    proxy = getattr(getattr(config, "network", None), "proxy", None)
    # 先看原文页面：它离事件最近，新闻转载的日期通常比事件本身晚。
    sourced, source_note = _source_event_date(theme, proxy=proxy)
    if sourced:
        _store_event_date(conn, theme.label, sourced, source_note)
        return ThemeLabel(label, why, sourced, source_note, is_update, change)
    # 原文页面读不到日期，再退回外网检索：用话题实体检索，别用模型起的中文标题。
    searched, note = _search_event_date(llm, conn, theme.label, proxy=proxy)
    if searched:
        return ThemeLabel(label, why, searched, note, is_update, change)
    # 三者都失败时若写空，回声判定会退化成「上次推送距今 ≤7 天」，旧事超过窗口就会被重推；
    # 退到材料最早报道日（上界），保证新写入的主线一定有事件时间。
    material_day, material_note = _earliest_material_day(theme)
    if material_day:
        return ThemeLabel(label, why, material_day, material_note, is_update, change)
    return ThemeLabel(label, why, "", "", is_update, change)


def build_digest(
    conn: sqlite3.Connection,
    config: AppConfig,
    *,
    notifier: Notifier,
    digest_date: str | None = None,
    size: int | None = None,
    kind: str = "daily",
    now: datetime | None = None,
    preview: bool = False,
) -> dict:
    now = now or utcnow()
    date = digest_date or now.date().isoformat()

    existing = repo.get_digest(conn, date, kind)
    if existing is not None:
        if existing["status"] != "failed":
            return {
                "date": date,
                "status": existing["status"],
                "skipped": True,
                "reason": "digest already exists",
            }
        # 上次发送失败：清掉重建，避免幂等键把任务永久卡死。
        repo.reset_digest(conn, int(existing["id"]))

    weights = config.weights
    cadence = weights.get("cadence") or {}
    scored = collect_candidates(conn, config, now=now)
    theme_pref = repo.get_theme_preference(conn)
    turn = None

    def _compose_daily(**overrides) -> composer.Composition:
        return composer.compose_daily(
            conn,
            config,
            scored,
            now=now,
            size=size or int(cadence.get("daily_size", 5)),
            theme_limit=int(cadence.get("daily_theme_items", 3)),
            rotation=turn,
            theme_pref=theme_pref,
            **overrides,
        )

    if kind == "weekly":
        composition = composer.compose_weekly(
            conn,
            config,
            scored,
            now=now,
            paper_limit=int(cadence.get("weekly_papers", 3)),
            repo_limit=int(cadence.get("weekly_repos", 2)),
        )
    else:
        # 轮转冷启动期间乘子恒为 1.0，这里传进去只是让统计随每次推送累积。
        turn = rotation.rotation_state(conn, config, now=now)
        composition = _compose_daily()

    digest_id = repo.create_digest(conn, date, kind=kind)
    llm = DeepSeekLLM(config.llm) if config.llm else None
    labeled = _apply_theme_label(
        llm,
        conn,
        composition,
        config=config,
        history=(
            _theme_history(conn, composition.theme.label, now.date())
            if composition.theme is not None
            else ""
        ),
    )
    # 模型判定「这些内容不是一件事」时，不能硬发一条没有名字的主线：
    # 先换一条强主线；换不出来就彻底不发主线，让条目回到突发/快讯池。
    if kind == "daily" and composition.theme is not None and not labeled.label:
        rejected = {composition.theme.label}
        composition = _compose_daily(blocked_entities=rejected)
        if composition.theme is not None:
            labeled = _apply_theme_label(
                llm,
                conn,
                composition,
                config=config,
                history=_theme_history(conn, composition.theme.label, now.date()),
            )
        if composition.theme is not None and not labeled.label:
            composition = _compose_daily(blocked_entities=rejected, allow_theme=False)
            labeled = ThemeLabel(why="今日没有清晰的跨来源主线，只推送时效性内容。")
    # 同一件事的跟进报道不该再占一次主线：能换出别的强主线就照常开，换不出就只发快讯。
    echo = None
    if composition.theme is not None:
        echo = _echo_of(
            conn,
            composition.theme.label,
            labeled.when,
            now.date(),
            is_update=labeled.is_update,
            change=labeled.change,
        )
    if echo is not None:
        note = _echo_note(echo, composition.theme.label, labeled.when)
        composition = _compose_daily(blocked_entities={echo["entity"]})
        if composition.theme is not None:
            labeled = _apply_theme_label(
                llm,
                conn,
                composition,
                config=config,
                history=_theme_history(conn, composition.theme.label, now.date()),
            )
        else:
            labeled = ThemeLabel(why=note)
    theme_label, theme_reason = labeled.label, labeled.why
    theme_when, theme_when_note = labeled.when, labeled.when_note
    if (
        composition.theme is not None
        and composer.theme_pref_matches(composition.theme, repo.get_theme_preference(conn))
    ):
        theme_reason = theme_reason or "按你设定的临时主题优先挑选"
    # 卡片上的「围绕此话题找」优先用实体名，它比模型起的标题更适合当检索词。
    theme_query = composition.theme.label if composition.theme is not None else ""

    # 只在入选之后重打标：只影响要看的内容和维度轮转，不额外给全库分类花钱。
    retagged = _retag_selected(llm, conn, composition.items)
    if retagged and composition.theme is not None:
        composition.theme_dimension = composer.topic_dimension(composition.theme)

    if composition.theme is not None:
        topic_id = repo.add_topic(
            conn,
            digest_id=digest_id,
            topic_date=date,
            kind=kind,
            label=theme_label,
            summary=theme_reason,
            entity=composition.theme.label,
            dimension=composition.theme_dimension or None,
            score=composition.theme.score,
            item_count=len(composition.theme.items),
            event_date=theme_when,
        )
        for index, entry in enumerate(composition.theme.items, start=1):
            repo.add_topic_item(
                conn,
                topic_id=topic_id,
                document_id=int(entry["doc"]["id"]),
                angle=str(entry["doc"]["kind"]),
                rank=index,
            )

    entries = composition.items
    items: list[DigestItemView] = []
    summaries = 0
    summary_errors: list[str] = []
    discussion_used = 0
    index = 0
    for block in composition.blocks:
        for entry in block.items:
            index += 1
            document = entry["doc"]
            reason = signals.heat_label(conn, int(document["id"]))
            target = repo.discussion_target(conn, int(document["id"]))
            summary: dict = {"abstract": (document["abstract"] or "")[:280]}
            if llm is not None:
                try:
                    generated = llm.summarize(conn, document)
                    if generated:
                        summary = generated
                        summaries += 1
                except (BudgetExceeded, LLMError) as exc:
                    summary_errors.append(f"{document['id']}: {exc}")
            if (
                llm is not None
                and document["kind"] == "forum"
                and discussion_used < MAX_DISCUSSION_ITEMS
            ):
                discussion_used += 1
                proxy = config.network.proxy if config.network.mode == "overseas-only" else None
                try:
                    summary = discussion.enrich(
                        conn, document, summary, llm=llm, proxy=proxy
                    )
                except Exception as exc:  # 社区观点是可选增强，任何异常都不能中断推送
                    summary_errors.append(f"{document['id']}: discussion {exc}")
            repo.add_digest_item(
                conn,
                digest_id=digest_id,
                document_id=int(document["id"]),
                rank=index,
                score=float(entry["score"]),
                reason=reason,
                summary=summary,
                section=block.section,
            )
            items.append(
                DigestItemView(
                    rank=index,
                    document_id=int(document["id"]),
                    title=document["title"],
                    url=document["url"],
                    tag=entry["tag"],
                    source=document["kind"],
                    reason=reason,
                    score=float(entry["score"]),
                    discussion_url=target[0] if target else None,
                    discussion_label=target[1] if target else "",
                    section=block.section,
                    published_at=document["published_at"],
                )
            )

    # 反刍：日报末尾补一条「值得回看」。预览与失败不写 interactions，
    # 否则会污染「60 天间隔 / 累计 2 次」的状态。
    recall_candidate = None
    if kind == "daily" and not preview:
        candidate = recall.pick_recall(conn, now)
        current_ids = {item.document_id for item in items}
        if candidate is not None and candidate.document_id not in current_ids:
            document = repo.get_document(conn, candidate.document_id)
            if document is not None:
                recall_candidate = candidate
                index += 1
                repo.add_digest_item(
                    conn,
                    digest_id=digest_id,
                    document_id=candidate.document_id,
                    rank=index,
                    score=0.0,
                    reason="值得回看",
                    summary={"recall": candidate.reason},
                    section="recall",
                )
                items.append(
                    DigestItemView(
                        rank=index,
                        document_id=candidate.document_id,
                        title=candidate.title,
                        url=candidate.url,
                        tag=candidate.primary_tag or "",
                        source=str(document["kind"]),
                        reason="值得回看",
                        score=0.0,
                        section="recall",
                        note=candidate.reason,
                        published_at=document["published_at"],
                    )
                )

    if not items:
        location = _deliver(
            conn,
            notifier,
            digest_id,
            date,
            [],
            kind=kind,
            theme_label=theme_label,
            theme_reason=theme_reason,
            theme_query=theme_query,
            theme_when=theme_when,
            theme_when_note=theme_when_note,
            preview=preview,
        )
        repo.mark_digest_sent(conn, digest_id, {"count": 0, "kind": kind, "outbox": location})
        return {"date": date, "status": "sent", "count": 0, "kind": kind, "outbox": location}

    location = _deliver(
        conn,
        notifier,
        digest_id,
        date,
        items,
        kind=kind,
        theme_label=theme_label,
        theme_reason=theme_reason,
        theme_query=theme_query,
        theme_when=theme_when,
        theme_when_note=theme_when_note,
        preview=preview,
    )
    repo.mark_status(conn, [int(entry["doc"]["id"]) for entry in entries], "pushed")
    repo.mark_digest_sent(
        conn,
        digest_id,
        {
            "count": len(items),
            "kind": kind,
            "theme": theme_label,
            "theme_when": theme_when,
            "theme_when_note": theme_when_note,
            "theme_echo": "" if echo is None else str(echo["entity"]),
            "theme_dimension": composition.theme_dimension,
            "outbox": location,
            "candidates": len(scored),
            "summaries": summaries,
            "summary_errors": summary_errors,
            "recall": None if recall_candidate is None else recall_candidate.document_id,
        },
    )
    # 只有真正投递成功才记一次「回看」，保证 60 天间隔与两次上限可信。
    if recall_candidate is not None:
        repo.add_interaction(
            conn,
            document_id=recall_candidate.document_id,
            digest_id=digest_id,
            action="recall",
            reason=recall_candidate.reason,
        )
    return {
        "date": date,
        "status": "sent",
        "kind": kind,
        "count": len(items),
        "theme": theme_label,
        "candidates": len(scored),
        "summaries": summaries,
        "summary_errors": summary_errors,
        "outbox": location,
    }
