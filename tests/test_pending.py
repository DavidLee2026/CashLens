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
                       channel="alipay", note="二九九套餐")
    captured = {}

    def append_fn(direction, amount_cents, category, channel, note, counterparty):
        captured.update(direction=direction, amount=amount_cents, category=category, channel=channel)
        return {"event_id": "x"}, True

    out = pending.accept(tmp_path, d["id"], append_fn)
    assert out["appended"] is True
    assert captured["amount"] == 29900 and captured["direction"] == "income"
    assert pending.list_all(tmp_path) == []  # 弹出后草稿消失
