"""去重键必须带上**条目名** —— 2026-10-20 真机「识别 12 笔、只入账 8 笔」

## 现场

客户传了一张成本清单截图：12 笔、合计 ¥9,980 全部识别对了，对话里的合计也对。
但「全部记上」只入了 **8 笔 ¥6,480**，另外 4 笔被判「与账本里已有记录完全相同，没有重复入账」，
草稿留在待确认里、反复点也没用。

## 根因

去重键 = `project|channel|amount|day|category|counterparty`（**不含 note**）。
那份清单里有 **4 笔 ¥1,000**（2个摄像助理 / 一个音控师带笔记本 / 灯光助理2位 / 灯光1200w 四套）
和 **2 笔 ¥500**。它们金额、分类（经营）、日期、项目、渠道全都一样，只有名字不同 ——
键就撞在一起：`append()` 写不进去，更要命的是 `load()` 也会按这个键**把后来者折叠掉**。
少掉的正好是 3×1000 + 1×500 = ¥3,500，与界面上的落差一分不差。

## 结论

**同额不同名 = 两笔不同的账**。note（去掉空白后）必须进键；
同一张图再导一次（名字与金额都一样）仍然要被挡住 —— 这两条都要有断言。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services import pending, projects  # noqa: E402
from app.services.state_engine import EventLedger  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CASH_DATA_DIR", str(tmp_path))
    from fastapi.testclient import TestClient
    from app import main
    return main, TestClient(main.app), tmp_path


def _mk_drafts(tmp, pid, rows):
    """造「文件识别」来源的草稿（source=识别 → 入账走严格去重，和真机同一条路）。"""
    ids = []
    for note, cents in rows:
        d = pending.create(tmp, direction="expense", amount_cents=cents, category="经营",
                           channel="receipt", note=note, project=pid)
        ids.append(d["id"])
    p = Path(tmp) / "pending.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    for r in data:
        r["source"] = "识别"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return ids


def _accept(client, ids, pid):
    return [client.post(f"/api/pending/{i}/accept", json={"project": pid}).json().get("appended")
            for i in ids]


def test_same_amount_different_name_are_two_entries(env):
    """同额、同分类、同日期、同项目，只是名字不同 → 两笔都要能入账。"""
    main, client, tmp = env
    pid = projects.create(tmp, "项目 A")["id"]
    ids = _mk_drafts(tmp, pid, [("2个摄像助理", 100000), ("灯光助理2位", 100000)])
    assert _accept(client, ids, pid) == [True, True]
    assert len(EventLedger(main.ledger_path()).load()) == 2


def test_reimporting_the_same_receipt_is_still_deduped(env):
    """同一份东西再导一次（名字与金额都一样）→ 必须被挡住，不许记两遍。"""
    main, client, tmp = env
    pid = projects.create(tmp, "项目 A")["id"]
    first = _mk_drafts(tmp, pid, [("4个摄像", 400000), ("2个摄像助理", 100000)])
    assert _accept(client, first, pid) == [True, True]
    second = _mk_drafts(tmp, pid, [("4个摄像", 400000), ("2个摄像助理", 100000)])
    assert _accept(client, second, pid) == [False, False]
    assert len(EventLedger(main.ledger_path()).load()) == 2


def test_note_whitespace_does_not_create_a_new_entry(env):
    """名字里多一个空格不该变成"另一笔"（OCR / 粘贴常抖）。"""
    main, client, tmp = env
    pid = projects.create(tmp, "项目 A")["id"]
    first = _mk_drafts(tmp, pid, [("灯光 1200w 四套", 100000)])
    assert _accept(client, first, pid) == [True]
    second = _mk_drafts(tmp, pid, [("灯光1200w 四套", 100000)])   # 空格不同
    assert _accept(client, second, pid) == [False]


def test_the_real_12_line_list_now_imports_all_12(env):
    """真机那份清单：4 笔 ¥1,000 + 2 笔 ¥500，12 笔必须全部入账、合计 ¥9,980。

    这条是这次事故的完整回归：以前只入账 8 笔 ¥6,480（少 3×1000 + 1×500 = ¥3,500）。
    """
    main, client, tmp = env
    from app.services import expense_list
    pid = projects.create(tmp, "我的项目 10 月 20 日")["id"]
    text = """3个n6三脚架 100
大监1 80
Fx3+16-35 一套配背夹 250
一套调音台配4手持话筒 500
1台电视机和移动支架配1个电小二 150
4个摄像 4000
2个摄像助理 1000
1个场务 400
一个音控师带笔记本 1000
音频助理一位 500
灯光助理2位 1000
灯光1200w 四套 1000"""
    rows = [(i["label"], i["amount_cents"]) for i in expense_list.parse(text)["items"]]
    ids = _mk_drafts(tmp, pid, rows)
    assert _accept(client, ids, pid) == [True] * 12
    events = EventLedger(main.ledger_path()).load()
    assert len(events) == 12
    assert sum(e["amount_cents"] for e in events) == 998000
