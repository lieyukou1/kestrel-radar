from __future__ import annotations

from pathlib import Path

import pytest

from ai_radar.adapters.sources.base import RawItem
from ai_radar.config import PROJECT_ROOT, load_config
from ai_radar.domain.profile import DomainError, load_profile
from ai_radar.pipeline import tagging


def _item(title: str, kind: str = "blog") -> RawItem:
    return RawItem(source_id="rss", external_id=title, url="https://example.com", title=title, kind=kind)


def test_ai_pack_carries_taxonomy_and_defaults() -> None:
    profile = load_profile(PROJECT_ROOT, "ai")
    assert profile.id == "ai"
    assert profile.tags == [
        "1 模型与架构",
        "2 训练与对齐",
        "3 推理与加速",
        "4 Agent 与应用",
        "5 多模态与生成",
        "6 系统与基础设施",
        "7 理论与可解释性",
        "8 数据与评测",
    ]
    assert profile.defaults == {
        "paper": "1 模型与架构",
        "forum": "4 Agent 与应用",
        "blog": "4 Agent 与应用",
        "repo": "6 系统与基础设施",
    }


def test_ai_pack_actions_keep_priority_order() -> None:
    profile = load_profile(PROJECT_ROOT, "ai")
    assert profile.action_of("某数据中心发生故障，服务宕机") == "incident"
    assert profile.action_of("公司完成 B 轮融资") == "funding"
    assert profile.action_of("新模型在 benchmark 上 beats 对手") == "benchmark"
    assert profile.action_of("两家公司宣布合作") == "partnership"
    assert profile.action_of("OpenAI 发布新模型") == "release"
    assert profile.action_of("今天天气不错") == "other"


def test_profile_drives_tagging(tmp_path: Path) -> None:
    pack = tmp_path / "config" / "domains" / "toy"
    pack.mkdir(parents=True)
    (pack / "domain.yaml").write_text(
        "id: toy\n"
        "name: 测试领域\n"
        "taxonomy:\n"
        "  - name: 政治\n"
        "    keywords: [选举]\n"
        "  - name: 经济\n"
        "    keywords: [通胀]\n"
        "defaults:\n"
        "  blog: 经济\n"
        "actions:\n"
        "  - name: vote\n"
        "    pattern: 选举\n",
        encoding="utf-8",
    )
    toy = load_profile(tmp_path, "toy")
    assert tagging.classify(_item("某国选举结果出炉"), profile=toy)[0] == "政治"
    assert tagging.classify(_item("完全无关的标题"), profile=toy)[0] == "经济"
    # 同一个标题在 AI 包里不会落到「政治」，说明分类确实跟着领域包走。
    ai = load_profile(PROJECT_ROOT, "ai")
    assert tagging.classify(_item("某国选举结果出炉"), profile=ai)[0] != "政治"


def test_config_attaches_domain_profile() -> None:
    config = load_config(PROJECT_ROOT)
    assert config.domain is not None
    assert config.domain.id == "ai"
    assert "cadence" in config.weights


def test_broken_pack_fails_fast(tmp_path: Path) -> None:
    config_dir = tmp_path / "config" / "domains" / "broken"
    config_dir.mkdir(parents=True)
    (config_dir / "domain.yaml").write_text(
        "id: broken\ntaxonomy:\n  - name: 分类\n    keywords: [x]\ndefaults:\n  blog: 不存在的分类\n",
        encoding="utf-8",
    )
    with pytest.raises(DomainError):
        load_profile(tmp_path, "broken")
