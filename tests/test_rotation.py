from __future__ import annotations

from pathlib import Path

from ai_radar.pipeline.rotation import format_rotation, rotation_state
from ai_radar.pipeline.tagging import TAGS
from ai_radar.store import repo
from ai_radar.store.db import connect, init_db

DIM_1 = "1 模型与架构"
DIM_8 = "8 数据与评测"


class Config:
    def __init__(self, *, learning_days: int = 2, enabled: bool = True) -> None:
        self.weights = {
            "rotation": {
                "enabled": enabled,
                "item_window": 30,
                "theme_window": 10,
                "theme_band": 0.85,
                "max_adjust": 0.2,
                "learning_days": learning_days,
            }
        }


def add_document(conn, title: str, tag: str) -> int:
    return repo.insert_document(
        conn,
        canonical_id=f"test:{title}",
        kind="blog",
        title=title,
        abstract="abstract",
        url=f"https://example.com/{title}",
        published_at="2026-10-20T00:00:00+00:00",
        authority="A",
        primary_tag=tag,
        tags=[tag],
    )


def push(conn, date: str, document_ids: list[int], *, kind: str = "daily") -> int:
    digest_id = repo.create_digest(conn, date, kind=kind)
    for index, document_id in enumerate(document_ids, start=1):
        repo.add_digest_item(
            conn,
            digest_id=digest_id,
            document_id=document_id,
            rank=index,
            score=0.5,
            reason="",
            summary={},
            section="brief",
        )
    repo.mark_digest_sent(conn, digest_id, {"count": len(document_ids)})
    return digest_id


def test_learning_mode_keeps_weights_neutral(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document = add_document(conn, "only model news", DIM_1)
    push(conn, "2026-10-20", [document])

    state = rotation_state(conn, Config(learning_days=14))

    assert state.active is False
    assert state.recorded_pushes == 1
    assert all(stat.weight == 1.0 for stat in state.stats.values())
    # 只记录也要把赤字算出来，方便在 /prefs 里观察。
    assert state.stats[DIM_8].item_deficit > 0


def test_deficit_turns_into_capped_weights(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    model_doc = add_document(conn, "model news", DIM_1)
    push(conn, "2026-10-20", [model_doc, model_doc])
    push(conn, "2026-10-21", [model_doc, model_doc])

    state = rotation_state(conn, Config(learning_days=2))

    assert state.active is True
    assert state.stats[DIM_1].item_deficit == -1.0
    assert state.stats[DIM_8].item_deficit == 1.0
    assert state.stats[DIM_1].weight == 0.8
    assert state.stats[DIM_8].weight == 1.2
    # 没有 topics 记录时主线赤字为 0，主线乘子保持中性。
    assert state.theme_weight(DIM_8) == 1.0


def test_paused_dimension_leaves_the_denominator(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    doc_1 = add_document(conn, "model news", DIM_1)
    doc_8 = add_document(conn, "eval news", DIM_8)
    push(conn, "2026-10-20", [doc_1, doc_8])
    push(conn, "2026-10-21", [doc_1, doc_8])
    repo.set_preference(conn, f"paused:dimension:{DIM_8}", 1.0)

    state = rotation_state(conn, Config(learning_days=2))

    assert state.stats[DIM_8].paused is True
    assert state.stats[DIM_8].weight == 1.0
    assert state.stats[DIM_8].item_deficit == 0.0
    # 暂停维度退出期望份额：剩下 7 个维度平分 2 个席位，没曝光的维度赤字打满。
    assert state.stats["3 推理与加速"].item_deficit == 1.0


def test_engagement_shifts_weight_but_within_cap(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    model_docs = [add_document(conn, f"model {index}", DIM_1) for index in range(4)]
    eval_docs = [add_document(conn, f"eval {index}", DIM_8) for index in range(4)]
    digest_id = push(conn, "2026-10-20", model_docs + eval_docs)
    push(conn, "2026-10-21", model_docs + eval_docs)
    for document_id in eval_docs:
        repo.add_interaction(
            conn, document_id=document_id, digest_id=digest_id, action="save"
        )

    state = rotation_state(conn, Config(learning_days=2))

    # 两边赤字相同，只有互动不同：收藏多的维度乘子更高，但都受限在 ±20%。
    assert state.stats[DIM_8].weight > state.stats[DIM_1].weight
    for stat in state.stats.values():
        assert 0.8 <= stat.weight <= 1.2


def test_theme_deficit_uses_topic_dimension(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    doc_1 = add_document(conn, "model news", DIM_1)
    doc_8 = add_document(conn, "eval news", DIM_8)
    digest_id = push(conn, "2026-10-20", [doc_1, doc_8])
    push(conn, "2026-10-21", [doc_1, doc_8])
    repo.add_topic(
        conn,
        digest_id=digest_id,
        topic_date="2026-10-20",
        kind="daily",
        label="模型主线",
        summary="",
        entity="model",
        dimension=DIM_1,
        score=1.0,
        item_count=2,
    )

    state = rotation_state(conn, Config(learning_days=2))

    assert state.stats[DIM_1].theme_deficit < 0
    assert state.stats[DIM_8].theme_deficit > 0
    assert state.theme_weight(DIM_8) == 1.2
    assert state.theme_weight(DIM_1) == 0.8


def test_weekly_digests_do_not_count_for_theme_rotation(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    doc = add_document(conn, "paper", DIM_1)
    digest_id = push(conn, "2026-10-19", [doc, doc], kind="weekly")
    repo.add_topic(
        conn,
        digest_id=digest_id,
        topic_date="2026-10-19",
        kind="weekly",
        label="weekly",
        summary="",
        entity="paper",
        dimension=DIM_8,
        score=1.0,
        item_count=2,
    )
    push(conn, "2026-10-20", [doc])

    state = rotation_state(conn, Config(learning_days=1))

    assert state.recorded_pushes == 1  # 周报不算冷启动样本
    assert state.stats[DIM_8].theme_deficit == 0.0  # 周报话题不进主线窗口


def test_format_preferences_includes_rotation(tmp_path: Path) -> None:
    from ai_radar.pipeline.preferences import format_preferences

    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document = add_document(conn, "model news", DIM_1)
    push(conn, "2026-10-20", [document])

    text = format_preferences(conn, Config(learning_days=14))

    assert "偏好设置" in text
    assert "维度轮转" in text
    assert DIM_1 in text


def test_format_rotation_lists_all_dimensions(tmp_path: Path) -> None:
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    document = add_document(conn, "model news", DIM_1)
    push(conn, "2026-10-20", [document])

    text = format_rotation(rotation_state(conn, Config(learning_days=14)))

    assert "只记录" in text
    for tag in TAGS:
        assert tag in text
