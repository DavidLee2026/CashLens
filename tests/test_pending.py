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


# ─── 端点层：草稿列表的项目名在读取时解析（2026-09-28 补）────────────

def test_pending_endpoint_resolves_project_name(tmp_path, monkeypatch):
    """/api/pending 必须返回**当前**项目名，而不是草稿生成时的快照、也不是 None。

    起因：真机上 17 条草稿在界面上看不到归哪个项目 —— 草稿记录里只存 `project` id，
    连 `project_name` 字段都没有。正确的做法是读取时从映射表解析（与账本同一条纪律：
    只存稳定 id，显示名走 projects.json），这样改名立刻生效、删项目如实回落。
    """
    from fastapi.testclient import TestClient

    from app import main
    from app.services import projects

    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    pid = projects.create(tmp_path, "919 昆明项目")["id"]
    pending.create(tmp_path, direction="expense", amount_cents=4050, category="购物",
                   channel="receipt", note="便利店", project=pid)
    client = TestClient(main.app)

    row = client.get("/api/pending").json()["pending"][0]
    assert row["project"] == pid, "草稿里存的应该是稳定 id"
    assert row["project_name"] == "919 昆明项目"

    # 改名之后草稿显示的名字要跟着变（不能在草稿里存名字快照）
    projects.rename(tmp_path, pid, "919 昆明差旅")
    assert client.get("/api/pending").json()["pending"][0]["project_name"] == "919 昆明差旅"

    # 项目被删掉 → 如实回落「未归项目」，与账本事件的回落口径保持一致
    projects.mark_deleted(tmp_path, pid)
    assert client.get("/api/pending").json()["pending"][0]["project_name"] == projects.UNASSIGNED_LABEL


def test_pending_endpoint_keeps_panel_fields(tmp_path, monkeypatch):
    """左栏「待确认」面板要显示金额/分类/日期/商户，端点必须原样带出来。

    `date` 由识别通道写入草稿（intake 在 create 之后补 `date` / `source` / `image`），
    所以这里直接落一份带 date 的草稿文件，验证端点原样透传、不做裁剪。
    """
    import json

    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    (tmp_path / "pending.json").write_text(json.dumps([{
        "id": "abc123", "direction": "expense", "amount_cents": 4050, "category": "购物",
        "channel": "receipt", "note": "", "counterparty": "云南强林乐家连锁便利店有限公司",
        "project": "", "created": "2026-09-28T11:19:14", "date": "2026-09-18",
        "source": "识别", "image": "微信图片.jpg",
    }], ensure_ascii=False), encoding="utf-8")

    row = TestClient(main.app).get("/api/pending").json()["pending"][0]
    assert row["amount_cents"] == 4050 and row["category"] == "购物"
    assert row["date"] == "2026-09-18"
    assert row["counterparty"] == "云南强林乐家连锁便利店有限公司"
    assert row["project_name"] == "未归项目", "未归项目的草稿也要有可显示的名字"
