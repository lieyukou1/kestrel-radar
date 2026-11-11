from __future__ import annotations

import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, wait
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
from . import composer, discussion, entities, events, rank, recall, rotation, signals, topics
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
# 入选内容要写摘要：摘要太短说明只有标题，摘要前并发补抓一次正文。
BODY_MIN_ABSTRACT = 400
BODY_FETCH_WORKERS = 4
BODY_FETCH_TIMEOUT = 8.0
# 整批正文抓取的总预算：到点就用手上已有的，没回来的放弃，不拖住推送。
BODY_FETCH_DEADLINE = 8.0
BODY_CHARS = 4000
# 企业公关稿的标题特征：签约、揭牌、启动仪式这类，不是技术内容。
PR_TITLE_RE = re.compile(
    r"战略合作|签约|揭牌|启动仪式|框架协议|合作协议|项目启动|正式启动|开工|奠基|获批|中标"
)
# 推送前的候选闸门：只看分数最高的一批，剔除企业软文与 AI 无关内容。
SCREEN_TOP_N = 30
# 一次判掉大半说明模型跑偏；整批放行，宁可推一条普通内容，也不能清空日报。
SCREEN_MAX_DROP_RATIO = 0.5
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
        # 企业通稿：标题写着战略合作/签约/揭牌，内容通常是公关话术，不是技术进展。
        if PR_TITLE_RE.search(str(document["title"] or "")):
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


def _fetch_html(
    url: str, *, proxy: str | None = None, timeout: float = THEME_FETCH_TIMEOUT
) -> str:
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
                timeout=timeout,
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


def _fetch_body(
    url: str,
    *,
    proxy: str | None = None,
    max_chars: int = THEME_BODY_CHARS,
    timeout: float = THEME_FETCH_TIMEOUT,
) -> str:
    """抓正文；失败静默返回空，不影响推送。"""
    text = _fetch_html(url, proxy=proxy, timeout=timeout)
    if not text:
        return ""
    try:
        return clean_html(text, url, kind="blog")[:max_chars]
    except Exception:
        return ""


def _needs_body(document: sqlite3.Row) -> bool:
    """论文摘要是作者写的，够用；其余类型摘要太短说明抓到的只是空壳。"""
    if document["kind"] == "paper":
        return False
    abstract = " ".join(str(document["abstract"] or "").split())
    return len(abstract) < BODY_MIN_ABSTRACT


def _prefetch_bodies(config: AppConfig, entries: list[dict]) -> dict[int, str]:
    """并发补抓入选内容的正文，给摘要当输入。

    只抓摘要过短的那些；整批受 BODY_FETCH_DEADLINE 约束，抓不回来就退回原摘要。
    """
    targets = [
        (int(entry["doc"]["id"]), str(entry["doc"]["url"] or ""))
        for entry in entries
        if _needs_body(entry["doc"]) and str(entry["doc"]["url"] or "")
    ]
    if not targets:
        return {}
    network = getattr(config, "network", None)
    proxy = network.proxy if getattr(network, "mode", "") == "overseas-only" else None
    bodies: dict[int, str] = {}
    pool = ThreadPoolExecutor(max_workers=BODY_FETCH_WORKERS)
    try:
        futures = {
            pool.submit(
                _fetch_body,
                url,
                proxy=proxy,
                max_chars=BODY_CHARS,
                timeout=BODY_FETCH_TIMEOUT,
            ): document_id
            for document_id, url in targets
        }
        wait(futures, timeout=BODY_FETCH_DEADLINE)
        for future, document_id in futures.items():
            if not future.done() or future.cancelled():
                continue
            try:
                body = future.result()
            except Exception:
                continue
            if body:
                bodies[document_id] = body
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return bodies


def _with_body(document: sqlite3.Row, body: str) -> dict:
    """把正文当成摘要喂给模型；Row 不可变，拷一份 dict。"""
    data = {key: document[key] for key in document.keys()}
    data["abstract"] = body
    return data


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
    llm: DeepSeekLLM | None,
    conn: sqlite3.Connection,
    label: str,
    *,
    proxy: str | None = None,
) -> tuple[str, str]:
    """材料里没有事件日期时，用新闻检索反查最早报道时间。

    返回 (日期, 来源说明)；搜不到或判断不了就返回空，绝不用材料日期顶替。
    """
    # 缓存命中不需要模型；没有可用模型时也不做外网检索，省一次调用。
    cached = _cached_event_date(conn, label)
    if cached is not None:
        return cached
    if llm is None or not hasattr(llm, "chat"):
        return "", ""
    found: list[dict] = []
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


def _screen_candidates(
    llm: DeepSeekLLM | None,
    conn: sqlite3.Connection,
    scored: list[dict],
    config: AppConfig,
) -> tuple[list[dict], dict[int, str], dict]:
    """推送前筛掉企业软文与 AI 无关内容，顺带定好维度。

    返回 (保留的候选, {document_id: dimension}, 统计)。只判分数最高的一批，
    后面编排照常在剩余候选里挑；模型不可用、调用失败或剔除比例异常时全部放行。
    """
    cfg = config.weights.get("screen") or {}
    enabled = bool(cfg.get("enabled", True))
    stats: dict = {"enabled": enabled, "dropped": []}
    screen = getattr(llm, "screen_candidates", None)
    if not enabled or llm is None or not callable(screen) or not scored:
        return scored, {}, stats

    head = scored[: int(cfg.get("top_n", SCREEN_TOP_N))]
    payload = [
        {
            "id": int(entry["doc"]["id"]),
            "title": str(entry["doc"]["title"] or ""),
            "kind": str(entry["doc"]["kind"] or ""),
            "abstract": str(entry["doc"]["abstract"] or ""),
        }
        for entry in head
    ]
    try:
        verdicts = screen(conn, payload)
    except (BudgetExceeded, LLMError) as exc:
        print(f"[digest] screen skipped: {type(exc).__name__}: {exc}", flush=True)
        return scored, {}, stats
    if not verdicts:
        # 模型输出没法解析时也会走到这里；留一行日志，否则闸门失效只能靠翻 stats 发现。
        print("[digest] screen skipped: no verdicts", flush=True)
        return scored, {}, stats

    dropped = {
        document_id
        for document_id, verdict in verdicts.items()
        if not verdict.get("keep", True)
    }
    max_drop = int(len(head) * float(cfg.get("max_drop_ratio", SCREEN_MAX_DROP_RATIO)))
    if len(dropped) > max_drop:
        print(f"[digest] screen ignored: dropped {len(dropped)}/{len(head)}", flush=True)
        return scored, {}, stats

    titles = {int(entry["doc"]["id"]): str(entry["doc"]["title"] or "") for entry in head}
    stats["dropped"] = [
        {
            "id": document_id,
            "title": titles.get(document_id, ""),
            "reason": verdicts[document_id].get("reason", ""),
        }
        for document_id in sorted(dropped)
    ]
    dimensions = {
        document_id: verdict["dimension"]
        for document_id, verdict in verdicts.items()
        if verdict.get("dimension")
    }
    kept = [entry for entry in scored if int(entry["doc"]["id"]) not in dropped]
    return kept, dimensions, stats


def _retag_selected(
    llm: DeepSeekLLM | None,
    conn: sqlite3.Connection,
    entries: list[dict],
    *,
    known: dict[int, str] | None = None,
) -> dict[int, str]:
    """给真正入选推送的少量内容做一次 LLM 重打标。

    采集时的关键词启发式会把「Agent 数据工程」归到 Agent、把集合名归到模型，
    只修正用户要看的那几条；调用失败或结果非法时沿用原标签，不影响推送。
    `known` 是候选闸门已经给过的维度，命中就不再重复问模型。
    """
    known = known or {}
    mapping: dict[int, str] = {
        int(entry["doc"]["id"]): known[int(entry["doc"]["id"])]
        for entry in entries
        if int(entry["doc"]["id"]) in known
    }
    missing = [entry for entry in entries if int(entry["doc"]["id"]) not in known]
    classify = getattr(llm, "classify_dimensions", None)
    if llm is not None and missing and callable(classify):
        payload = [
            {
                "id": int(entry["doc"]["id"]),
                "title": str(entry["doc"]["title"] or ""),
                "kind": str(entry["doc"]["kind"] or ""),
                "abstract": str(entry["doc"]["abstract"] or ""),
            }
            for entry in missing
        ]
        try:
            mapping.update(classify(conn, payload))
        except (BudgetExceeded, LLMError) as exc:
            print(f"[digest] retag skipped: {type(exc).__name__}: {exc}", flush=True)
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
    sourced, note = _infer_event_date(theme, llm, conn, config)
    return ThemeLabel(label, why, sourced, note, is_update, change)


def _infer_event_date(
    theme, llm: DeepSeekLLM | None, conn: sqlite3.Connection, config: AppConfig | None
) -> tuple[str, str]:
    """给事件补一个「事情本身发生的时间」；拿不到就返回空，不拿发布日期充数。"""
    proxy = getattr(getattr(config, "network", None), "proxy", None)
    # 先看原文页面：它离事件最近，新闻转载的日期通常比事件本身晚。
    sourced, source_note = _source_event_date(theme, proxy=proxy)
    if sourced:
        _store_event_date(conn, theme.label, sourced, source_note)
        return sourced, source_note
    # 原文页面读不到日期，再退回外网检索：用话题实体检索，别用模型起的中文标题。
    searched, note = _search_event_date(llm, conn, theme.label, proxy=proxy)
    if searched:
        return searched, note
    # 三者都失败时若写空，回声判定会退化成「上次推送距今 ≤7 天」，旧事超过窗口就会被重推；
    # 退到材料最早报道日（上界），保证新写入的主线一定有事件时间。
    material_day, material_note = _earliest_material_day(theme)
    if material_day:
        return material_day, material_note
    return "", ""


def _fresh_entries(scored: list[dict], *, now: datetime, hours: float = 72.0) -> list[dict]:
    """事件池：近 72 小时的内容，不受分数排名截断。"""
    fresh: list[dict] = []
    for entry in scored:
        document = entry["doc"]
        published = parse_iso(document["published_at"]) or parse_iso(
            document["first_seen_at"]
        )
        if published is None:
            continue
        if (now - published).total_seconds() / 3600 <= hours:
            fresh.append(entry)
    return fresh


def _found_events(
    llm: DeepSeekLLM | None,
    conn: sqlite3.Connection,
    scored: list[dict],
    config: AppConfig,
    *,
    now: datetime,
) -> tuple[list[events.Event], dict]:
    """粗分组 → 模型切事件；模型不可用就退回「一个实体桶一件事」。"""
    weights = config.weights
    cfg = weights.get("topics") or {}
    sources_map = repo.document_sources_map(conn)
    # 事件池 = 近 72h 全量（不被分数截断）∪ 分数最高的一批（兜住跨天仍在发酵的事件）。
    fresh = _fresh_entries(scored, now=now)
    seen = {int(entry["doc"]["id"]) for entry in fresh}
    head = scored[: int(cfg.get("pool", 180))]
    pool_entries = fresh + [
        entry for entry in head if int(entry["doc"]["id"]) not in seen
    ]
    buckets = events.group_buckets(
        pool_entries,
        sources_map=sources_map,
        authority_weights=weights.get("authority"),
        now=now,
        min_members=int(cfg.get("min_members", 2)),
        entity_max_docs=int(cfg.get("max_docs", 12)),
    )
    stats = {"buckets": len(buckets), "events": 0, "source": "fallback"}
    found: list[events.Event] = []
    segment = getattr(llm, "segment_events", None)
    if llm is not None and buckets and callable(segment):
        try:
            assignments = segment(conn, events.bucket_payload(buckets))
        except (BudgetExceeded, LLMError) as exc:
            print(f"[digest] segment skipped: {type(exc).__name__}: {exc}", flush=True)
            assignments = []
        if assignments:
            found = events.build_events(
                buckets,
                assignments,
                sources_map=sources_map,
                authority_weights=weights.get("authority"),
                now=now,
            )
            if found:
                stats["source"] = "model"
    if not found:
        found = events.fallback_events(
            buckets,
            sources_map=sources_map,
            authority_weights=weights.get("authority"),
            now=now,
        )
    stats["events"] = len(found)
    return found, stats


def _event_continuation(
    conn: sqlite3.Connection, event: events.Event, today: date
) -> tuple[int, int, str]:
    """同一事件键 7 天内出现过就是续跟：返回 (第几天, 今天新增条数, 首次日期)。"""
    since = (today - timedelta(days=7)).isoformat()
    previous = repo.last_topic_by_key(conn, event.key, since, today.isoformat())
    if previous is None:
        return 1, len(event.items), today.isoformat()
    seen = repo.topic_document_ids(conn, int(previous["id"]))
    new_count = sum(
        1 for item in event.items if int(item["doc"]["id"]) not in seen
    )
    return (
        int(previous["day_index"] or 1) + 1,
        new_count,
        str(previous["first_date"] or previous["topic_date"]),
    )


def _label_event(
    llm: DeepSeekLLM | None,
    conn: sqlite3.Connection,
    composition: composer.Composition,
    *,
    config: AppConfig | None = None,
    today: date | None = None,
) -> ThemeLabel:
    """模型切分过的事件已经自带名字；兜底事件仍走旧的命名链路。

    兜底事件（没有 why / when）意味着模型没有给这件事下过判断，
    这时才需要 label_theme：它能识别新版本，并补上事件发生时间。
    """
    event = composition.theme
    if event is None:
        return ThemeLabel(why="今日没有清晰的跨来源主线，只推送时效性内容。")
    if not event.why and not event.when:
        return _apply_theme_label(
            llm,
            conn,
            composition,
            config=config,
            history=(
                _theme_history(conn, event.entity, today)
                if today is not None
                else ""
            ),
        )
    label = str(event.label or "").strip()
    if topics.is_bucket_label(label) or entities.is_venue(label):
        label = ""
    if not label:
        return ThemeLabel(why="今日没有清晰的跨来源主线，只推送时效性内容。")
    why = event.why or f"围绕「{label}」的讨论同时在多个来源出现。"
    when = event.when
    note = "模型推断" if when else ""
    if not when:
        when, note = _infer_event_date(event, llm, conn, config)
    return ThemeLabel(label, why, when, note, event.is_update, event.change)


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
    candidate_count = len(scored)
    llm = DeepSeekLLM(config.llm) if config.llm else None
    # 推送前先过闸门：企业软文与 AI 无关内容在编排前剔掉，空出的席位自动补位。
    scored, screened_dimensions, screen_stats = _screen_candidates(
        llm, conn, scored, config
    )
    theme_pref = repo.get_theme_preference(conn)
    sources_map = repo.document_sources_map(conn)
    # 事件优先：先用近 72h 全量内容粗分组，再由模型切成「一件事」。
    found_events: list[events.Event] = []
    segment_stats: dict = {"buckets": 0, "events": 0, "source": "none"}
    if kind == "daily":
        found_events, segment_stats = _found_events(llm, conn, scored, config, now=now)
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
            found_events=found_events or None,
            sources_map=sources_map,
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

    labeled = ThemeLabel(why="今日没有清晰的跨来源主线，只推送时效性内容。")
    echo = None
    day_index, new_count, first_date = 1, 0, date
    if kind == "daily":
        # 一个事件被否掉只换下一个，不能让当天的主线跟着一起没了。
        blocked: set[str] = set()
        for _ in range(3):
            theme = composition.theme
            if theme is None:
                break
            day_index, new_count, first_date = _event_continuation(
                conn, theme, now.date()
            )
            if new_count <= 0:
                # 同一事件 7 天内推过、今天又没有新增：不再占主线席位。
                blocked.add(theme.key)
                composition = _compose_daily(blocked_keys=blocked)
                continue
            labeled = _label_event(
                llm, conn, composition, config=config, today=now.date()
            )
            if not labeled.label:
                blocked.add(theme.key)
                composition = _compose_daily(blocked_keys=blocked)
                continue
            # 续跟事件按事件键认亲，不再走实体回声判定；新事件仍要防媒体回声。
            if day_index == 1:
                found_echo = _echo_of(
                    conn,
                    theme.entity,
                    labeled.when,
                    now.date(),
                    is_update=labeled.is_update,
                    change=labeled.change,
                )
                if found_echo is not None:
                    echo = found_echo
                    blocked.add(theme.key)
                    composition = _compose_daily(blocked_keys=blocked)
                    continue
            break
        if composition.theme is None or not labeled.label:
            composition = _compose_daily(blocked_keys=blocked, allow_theme=False)
            labeled = ThemeLabel(why="今日没有清晰的跨来源主线，只推送时效性内容。")
            day_index, new_count, first_date = 1, 0, date
    else:
        labeled = _apply_theme_label(llm, conn, composition, config=config)

    theme_label, theme_reason = labeled.label, labeled.why
    theme_when, theme_when_note = labeled.when, labeled.when_note
    if composition.theme is not None and day_index > 1:
        theme_reason = (
            f"{theme_reason} 该事件已持续第 {day_index} 天，今天新增 {new_count} 条。"
        ).strip()
    if (
        composition.theme is not None
        and composer.theme_pref_matches(composition.theme, repo.get_theme_preference(conn))
    ):
        theme_reason = theme_reason or "按你设定的临时主题优先挑选"
    # 卡片上的「围绕此话题找」用实体名，比模型起的事件标题更适合当检索词。
    theme_query = composition.theme.entity if composition.theme is not None else ""

    # 闸门已经给过高分候选的维度，这里直接沿用；漏掉的才补一次模型调用。
    retagged = _retag_selected(
        llm, conn, composition.items, known=screened_dimensions
    )
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
            entity=composition.theme.entity,
            dimension=composition.theme_dimension or None,
            score=composition.theme.score,
            item_count=len(composition.theme.items),
            event_date=theme_when,
            event_key=composition.theme.key,
            first_date=first_date,
            day_index=day_index,
            new_count=new_count,
        )
        for index, entry in enumerate(composition.theme.items, start=1):
            repo.add_topic_item(
                conn,
                topic_id=topic_id,
                document_id=int(entry["doc"]["id"]),
                angle=str(entry["doc"]["kind"]),
                rank=index,
            )

    # 其他进展也入库：跨天跟踪靠 topic_items 判断「今天有没有新增」。
    for event in composition.progress:
        p_day, p_new, p_first = _event_continuation(conn, event, now.date())
        progress_topic_id = repo.add_topic(
            conn,
            digest_id=digest_id,
            topic_date=date,
            kind=kind,
            label=event.label,
            summary=event.why,
            entity="",
            score=event.score,
            item_count=len(event.items),
            event_key=event.key,
            first_date=p_first,
            day_index=p_day,
            new_count=p_new,
        )
        for index, entry in enumerate(event.items, start=1):
            repo.add_topic_item(
                conn,
                topic_id=progress_topic_id,
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
    # 摘要前先补抓正文：只有标题的条目模型无从下手，写出来就是「未知」。
    bodies = _prefetch_bodies(config, entries) if llm is not None else {}
    for block in composition.blocks:
        for entry in block.items:
            index += 1
            document = entry["doc"]
            reason = signals.heat_label(conn, int(document["id"]))
            target = repo.discussion_target(conn, int(document["id"]))
            summary: dict = {"abstract": (document["abstract"] or "")[:280]}
            if llm is not None:
                body = bodies.get(int(document["id"]))
                try:
                    generated = llm.summarize(
                        conn, _with_body(document, body) if body else document
                    )
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
            "theme_day": day_index,
            "theme_new": new_count,
            "events": segment_stats,
            "progress": [event.key for event in composition.progress],
            "outbox": location,
            "candidates": candidate_count,
            "screened": screen_stats,
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
        "candidates": candidate_count,
        "screened": screen_stats,
        "summaries": summaries,
        "summary_errors": summary_errors,
        "outbox": location,
    }
