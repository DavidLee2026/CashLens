"""待确认草稿（识别→确认→入账）单测 —— 用临时目录，不碰真实数据。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services import pending  # noqa: E402


def test_create_list_decline(tmp_path):
    d = pending.create(tmp_path, direction="expense", amount_cents=2800, category="交通",
                       channel="manual", note="打车报销垫付")
    assert d["id"]
    assert len(pending.list_all(tmp_path)) == 1
    assert pending.get(tmp_path, d["id"])["amount_cents"] == 2800
    assert pending.decline(tmp_path, d["id"]) is True
    assert pending.list_all(tmp_path) == []
    assert pending.decline(tmp_path, "不存在") is False


def test_accept_invokes_append(tmp_path):
    d = pending.create(tmp_path, direction="income", amount_cents=29900, category="接单",
                       channel="alipay", note="二九九套餐", project="project_a")
    captured = {}

    def append_fn(direction, amount_cents, category, channel, note, counterparty, project):
        captured.update(direction=direction, amount=amount_cents, category=category,
                        channel=channel, project=project)
        return {"event_id": "x"}, True

    out = pending.accept(tmp_path, d["id"], append_fn)
    assert out["appended"] is True
    assert captured["amount"] == 29900 and captured["direction"] == "income"
    assert captured["project"] == "project_a"  # 项目维度透传到写账本回调
    assert pending.list_all(tmp_path) == []  # 弹出后草稿消失


def test_accept_tolerates_legacy_draft_without_project(tmp_path):
    """9/24 之前生成的旧草稿没有 project 字段，accept 不能因此报错。"""
    import json
    fp = tmp_path / "pending.json"
    fp.write_text(json.dumps([{
        "id": "legacy1", "direction": "expense", "amount_cents": 4713, "category": "交通",
        "channel": "manual", "note": "旧草稿", "counterparty": "",
        "created": "2026-09-01T10:00:00",
    }], ensure_ascii=False), encoding="utf-8")

    captured = {}

    def append_fn(direction, amount_cents, category, channel, note, counterparty, project):
        captured.update(project=project)
        return {"event_id": "y"}, True

    out = pending.accept(tmp_path, "legacy1", append_fn)
    assert out["appended"] is True
    assert captured["project"] == ""
