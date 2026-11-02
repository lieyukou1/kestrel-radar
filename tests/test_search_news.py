from __future__ import annotations

from types import SimpleNamespace

from ai_radar.adapters import search_news as search_news_mod


RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Jev - Google News</title>
    <item>
      <title>TypeSafe AI Launches Jev Model</title>
      <pubDate>Thu, 17 Sep 2026 07:00:00 GMT</pubDate>
      <source url="https://example.com">SuaraGarut</source>
    </item>
    <item>
      <title>Jev full public launch</title>
      <pubDate>Sun, 20 Sep 2026 07:00:00 GMT</pubDate>
      <source url="https://example.com">36k</source>
    </item>
  </channel>
</rss>
"""


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


class FakeClient:
    status_code = 200

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def get(self, url, params=None):
        return FakeResponse(RSS)


def test_search_news_parses_rss(monkeypatch) -> None:
    monkeypatch.setattr("ai_radar.adapters.search_news.httpx.Client", FakeClient)

    items = search_news_mod.search_news("Jev release")

    assert [item["published"] for item in items] == ["2026-09-17", "2026-09-20"]
    assert items[0]["source"] == "SuaraGarut"
    assert items[0]["title"] == "TypeSafe AI Launches Jev Model"


def test_search_news_empty_query_returns_empty() -> None:
    assert search_news_mod.search_news("   ") == []


def test_search_news_returns_empty_on_http_error(monkeypatch) -> None:
    class ErrorClient(FakeClient):
        def get(self, url, params=None):
            return FakeResponse("", status_code=500)

    monkeypatch.setattr("ai_radar.adapters.search_news.httpx.Client", ErrorClient)
    assert search_news_mod.search_news("Jev") == []


def test_search_news_returns_empty_on_exception(monkeypatch) -> None:
    class BoomClient(FakeClient):
        def get(self, url, params=None):
            raise RuntimeError("network down")

    monkeypatch.setattr("ai_radar.adapters.search_news.httpx.Client", BoomClient)
    assert search_news_mod.search_news("Jev", proxy="http://127.0.0.1:7897") == []
