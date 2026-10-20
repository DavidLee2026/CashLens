"""改归属（追加式订正）与「未归项目」一键收编 —— 2026-10-20

## 为什么有这东西

真机：客户先确认了一批草稿（那时项目还没建），之后才建项目 —— 12 笔全落在「未归项目」，
他建的项目一笔都没有。逐笔改 12 次太麻烦，所以「未归项目」那一行给一个批量出口。

## 口径（改的时候别破坏）

- **不重写历史**：改归属 = 追加一条 `void` + 按新项目重记一笔。账本行数变多，金额总数不变。
- **交易时间不变**：`ts` 沿用原事件，否则改一次归属这笔钱就从"上周"跳到"今天"，月度统计跟着变。
- **投影要重建**：作废不改变 MODEL_VERSION，不重建的话投影里还留着旧行。
- **目标必须是真项目**：收编到空串（未归）毫无意义，直接 400。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services import projects  # noqa: E402
from app.services.state_engine import EventLedger  # noqa: E402


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CASH_DATA_DIR", str(tmp_path))
    from fastapi.testclient import TestClient
    from app import main
    return main, TestClient(main.app), tmp_path


def _seed_two_unassigned(main):
    main._append_event("expense", 400000, "经营", "manual", "4个摄像", project="",
                       strict_dedupe=False)
    main._append_event("expense", 100000, "经营", "manual", "2个摄像助理", project="",
                       strict_dedupe=False)


def test_absorb_moves_every_unassigned_entry_without_losing_money(app_env):
    main, client, tmp = app_env
    pid = projects.create(tmp, "示例项目 A")["id"]
    _seed_two_unassigned(main)
    before = EventLedger(main.ledger_path()).load()
    before_ts = [e["ts"] for e in before]

    res = client.post("/api/projects/unassigned/absorb", json={"project": pid}).json()
    assert res["ok"] is True
    assert res["moved"] == 2 and res["total_cents"] == 500000
    assert res["project_name"] == "示例项目 A"

    after = EventLedger(main.ledger_path()).load()
    assert [(e["amount_cents"], e["project"]) for e in after] == [
        (400000, pid), (100000, pid)]
    # **交易时间不许变**：改归属不是"今天又花了一次"
    assert [e["ts"] for e in after] == before_ts
    # 分类 / 备注也原样保留
    assert [e["note"] for e in after] == ["4个摄像", "2个摄像助理"]
    assert {e["category"] for e in after} == {"经营"}
    # 账本只增不删：2 条原始 + 2 条作废 + 2 条重记
    raw = [l for l in Path(main.ledger_path()).read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(raw) == 6
    voids = [l for l in raw if '"type": "void"' in l]
    assert len(voids) == 2
    assert "改归属" in voids[0] and "未归项目" in voids[0]


def test_absorb_keeps_the_account_total_unchanged(app_env):
    """收编只是换个归属，**总额一分不能变**（后端汇总口径）。"""
    main, client, tmp = app_env
    pid = projects.create(tmp, "示例项目 A")["id"]
    _seed_two_unassigned(main)
    before = client.get("/api/projects").json()
    before_total = before["unassigned"]["expense_cents"] + sum(
        p["expense_cents"] for p in before["projects"])

    client.post("/api/projects/unassigned/absorb", json={"project": pid})
    after = client.get("/api/projects").json()
    after_total = after["unassigned"]["expense_cents"] + sum(
        p["expense_cents"] for p in after["projects"])
    assert after_total == before_total == 500000
    assert after["unassigned"]["count"] == 0
    assert after["projects"][0]["count"] == 2


def test_absorb_needs_a_target_and_creates_it_when_named(app_env):
    """空目标是硬拒；给一个还没建过的名字 → 按既定语义**顺手建出这个项目**再收编。"""
    main, client, tmp = app_env
    _seed_two_unassigned(main)
    assert client.post("/api/projects/unassigned/absorb", json={"project": ""}).status_code == 400
    # 空目标被拒之后账本一动没动
    assert len(EventLedger(main.ledger_path()).load()) == 2

    res = client.post("/api/projects/unassigned/absorb", json={"project": "新项目 Z"}).json()
    assert res["moved"] == 2 and res["project_name"] == "新项目 Z"
    assert any(p["name"] == "新项目 Z" for p in client.get("/api/projects").json()["projects"])


def test_absorb_is_a_no_op_when_unassigned_is_empty(app_env):
    main, client, tmp = app_env
    pid = projects.create(tmp, "示例项目 A")["id"]
    res = client.post("/api/projects/unassigned/absorb", json={"project": pid}).json()
    assert res["moved"] == 0 and "没有账" in res["note"]


def test_single_event_reassign_moves_only_that_one(app_env):
    main, client, tmp = app_env
    pid = projects.create(tmp, "示例项目 A")["id"]
    _seed_two_unassigned(main)
    events = EventLedger(main.ledger_path()).load()
    one = events[0]["event_id"]

    res = client.post("/api/events/reassign", json={"ids": [one], "project": pid}).json()
    assert res["moved"] == 1 and res["total_cents"] == 400000

    after = EventLedger(main.ledger_path()).load()
    assert sorted((e["amount_cents"], e["project"]) for e in after) == [
        (100000, ""), (400000, pid)]


def test_reassign_unknown_event_is_reported_not_faked(app_env):
    main, client, tmp = app_env
    res = client.post("/api/events/reassign", json={"ids": ["deadbeef"], "project": ""})
    assert res.status_code == 404
    assert client.post("/api/events/reassign", json={"ids": [], "project": ""}).status_code == 400


# ─── 只删账、保留项目（2026-10-20 David 要求）──────────────────────

def test_bills_only_voids_the_ledger_but_keeps_the_project(app_env):
    """「只认账不认项目」的第三种清场：作废名下账单，项目行留在清单里还能继续记账。

    起因：原来只有「只删项目（账回落未归）」与「连账单一起删」，想"项目继续用、旧账不算了"
    只能删了重建 —— 项目名与归属一起丢。
    """
    main, client, tmp = app_env
    pid = projects.create(tmp, "示例项目 A")["id"]
    for amount, note in ((400000, "4个摄像"), (100000, "2个摄像助理")):
        main._append_event("expense", amount, "经营", "manual", note, project=pid,
                           strict_dedupe=False)

    res = client.delete(f"/api/projects/{pid}?bills_only=true").json()
    assert res["action"] == "voided_bills"
    assert res["bill_count"] == 2 and res["voided_count"] == 2
    assert "项目本身留着" in res["note"]

    # 项目还在，账归零；账本是一串追加记录（2 原 + 1 void），有效事件 0
    rows = client.get("/api/projects").json()["projects"]
    assert [(r["name"], r["count"], r["expense_cents"]) for r in rows] == [("示例项目 A", 0, 0)]
    assert EventLedger(main.ledger_path()).load() == []
    raw = [l for l in Path(main.ledger_path()).read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(raw) == 3
    assert any('"type": "void"' in l and "项目保留" in l for l in raw)

    # 幂等：再点一次什么都不动，也不报错
    again = client.delete(f"/api/projects/{pid}?bills_only=true").json()
    assert again["bill_count"] == 0 and again["voided_count"] == 0
    assert "本来就" in again["note"]


def test_bills_only_still_supports_record_into_that_project(app_env):
    """清空之后还能继续往这个项目里记账（这是它区别于"删项目"的意义）。"""
    main, client, tmp = app_env
    pid = projects.create(tmp, "示例项目 A")["id"]
    main._append_event("expense", 100000, "经营", "manual", "旧账", project=pid,
                       strict_dedupe=False)
    client.delete(f"/api/projects/{pid}?bills_only=true")
    main._append_event("expense", 200000, "经营", "manual", "新账", project=pid,
                       strict_dedupe=False)
    rows = client.get("/api/projects").json()["projects"]
    assert rows[0]["count"] == 1 and rows[0]["expense_cents"] == 200000


# ─── 默认项目「同名复用」（2026-10-20：导入前替他建默认项目）────────────

def test_default_project_creation_can_reuse_by_name(app_env):
    """`reuse=true`：同名项目已存在就返回那一个 —— 同一天导两批不该出现两个「默认项目 10-20」。

    默认（reuse=false）仍然是"新建就是新建"：用户明确要两个同名项目时不该被拦。
    """
    main, client, tmp = app_env
    a = client.post("/api/projects", json={"name": "默认项目 10-20", "reuse": True}).json()
    b = client.post("/api/projects", json={"name": "默认项目 10-20", "reuse": True}).json()
    assert a["action"] == "created" and b["action"] == "reused"
    assert a["project"]["id"] == b["project"]["id"]
    assert len(client.get("/api/projects").json()["projects"]) == 1

    # 不带 reuse 还是照旧新建（两个同名项目是允许的）
    client.post("/api/projects", json={"name": "默认项目 10-20"})
    assert len(client.get("/api/projects").json()["projects"]) == 2
