from __future__ import annotations

import html as html_lib
import io
import re

import trafilatura
from pypdf import PdfReader
from readability import Document

MIN_CONTENT_CHARS = 800

SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")
FIGCAPTION_RE = re.compile(r"<figcaption[^>]*>(.*?)</figcaption>", re.S | re.I)
ARXIV_ABS_RE = re.compile(r"^https?://(?:www\.)?arxiv\.org/abs/([^?#]+)")
ARXIV_HTML_RE = re.compile(r"^https?://(?:www\.)?arxiv\.org/html/([^?#]+)")
ARXIV_PDF_RE = re.compile(r"^https?://(?:www\.)?arxiv\.org/pdf/([^?#]+)")


class CleanError(RuntimeError):
    pass


def arxiv_html_url(url: str) -> str:
    """arXiv abs 页换成 HTML 全文页；其他 URL 原样返回。"""
    match = ARXIV_ABS_RE.match(url)
    if match:
        return f"https://arxiv.org/html/{match.group(1)}"
    return url


def arxiv_pdf_url(url: str) -> str:
    """arXiv abs/html 页换成 PDF 地址；其他 URL 原样返回。"""
    for pattern in (ARXIV_ABS_RE, ARXIV_HTML_RE, ARXIV_PDF_RE):
        match = pattern.match(url)
        if match:
            return f"https://arxiv.org/pdf/{match.group(1)}"
    return url


def _plain_text(html: str) -> str:
    text = SCRIPT_STYLE_RE.sub(" ", html)
    text = TAG_RE.sub(" ", text)
    return " ".join(html_lib.unescape(text).split())


def _trafilatura(html: str, url: str) -> str:
    return (
        trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_links=True,
            include_tables=True,
            include_formatting=True,
            include_comments=False,
            favor_recall=True,
        )
        or ""
    )


def _readability(html: str, url: str) -> str:
    article = Document(html).summary(html_partial=True) or ""
    return _trafilatura(article, url)


def extract_figcaptions(html: str) -> list[str]:
    captions: list[str] = []
    for raw in FIGCAPTION_RE.findall(html):
        text = _plain_text(raw)
        if len(text) < 12:
            continue
        if text not in captions:
            captions.append(text)
    return captions


def clean_html(html: str, url: str, *, kind: str = "blog") -> str:
    """HTML → Markdown。trafilatura 为主，readability 回退，论文补图注。"""
    text = _trafilatura(html, url)
    if len(text) < MIN_CONTENT_CHARS:
        fallback = _readability(html, url)
        if len(fallback) > len(text):
            text = fallback
    if len(text) < MIN_CONTENT_CHARS:
        raise CleanError(
            f"cleaned content too short ({len(text)} chars): {url}"
        )

    if kind == "paper":
        captions = extract_figcaptions(html)
        missing = [caption for caption in captions if caption[:40] not in text]
        if missing:
            text += "\n\n## 图表说明\n" + "\n".join(f"- {caption}" for caption in missing)

    return text.strip() + "\n"


def pdf_to_text(data: bytes) -> str:
    """PDF 二进制 → 纯文本。按页拼接，压缩多余空行。"""
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:
        raise CleanError(f"pdf parse failed: {type(exc).__name__}: {exc}") from exc

    text = "\n\n".join(pages)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < MIN_CONTENT_CHARS:
        raise CleanError(f"pdf text too short ({len(text)} chars)")
    return text + "\n"
