"""测试默认关闭真实 embedding 与正文抓取，避免拖慢用例或触发下载。"""

from __future__ import annotations

import os

import pytest

os.environ["AI_RADAR_EMBED"] = "off"


@pytest.fixture(autouse=True)
def _offline_body_fetch(monkeypatch):
    """正文补抓会走真实网络，测试里默认返回空；需要正文的用例自己打桩。"""
    from ai_radar.pipeline import digest

    monkeypatch.setattr(digest, "_fetch_body", lambda *args, **kwargs: "")
