"""「文字里说的工时/工分」能记下来（外包按小时）—— 2026-09-28

用户口径：「客户这边的人员有 2 种，一种是外包性质的，一种是合同工，先增加计算外包性质，
按小时算，这个中比较好计算，因为不涉及交金的部分，只是按照时间算钱」。

这一版只做**外包**：数量（小时/工分）× 单价 = 应付劳务费，不涉及社保公积金；
**合同工明确不接**（涉及交金，规则不同），如实说明、不按外包口径硬算。
⚠️ 底线：工时/工分**不是钱** —— 只写 `data/timesheet.jsonl`，**不写账本**。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services import timesheet_records as tr  # noqa: E402


def _isolate(tmp_path, monkeypatch):
    """把账本 / 投影 / 数据目录**三个路径一起**指到临时目录。

    2026-09-28 的教训：只 monkeypatch `DATA_DIR` 时，`LEDGER_PATH` 仍指向真实账本 ——
    端点级测试因此写坏过真实数据。这里三个一起改。
    """
    from app import main

    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "LEDGER_PATH", tmp_path / "finance_events.jsonl")
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "proj.db")
    return main


# ─── 文字解析（确定性，不依赖模型）────────────────────────────

def test_parses_people_hours_and_rate():
    out = tr.parse_text("阿明今天 8 小时，时薪 50")
    assert out["contract"] is False
    assert out["entries"] == [{"person": "阿明", "qty": 8.0, "unit": "hours",
                               "rate_cents": 5000, "project": ""}]


def test_one_rate_applies_to_everyone_but_multiple_rates_stay_with_their_own_person():
    """只提到一个单价 → 发给所有还没单价的人；提到多个 → 各归各的，**不猜**。"""
    both = tr.parse_text("阿明 8 小时、小周 6 小时，时薪都是 50")["entries"]
    assert [e["rate_cents"] for e in both] == [5000, 5000]

    own = tr.parse_text("阿明 8 小时时薪 50，小周 6 小时时薪 60")["entries"]
    assert [(e["person"], e["rate_cents"]) for e in own] == [("阿明", 5000), ("小周", 6000)]

    head = tr.parse_text("外包时薪 60：阿明 8 小时，小周 5.5 小时")["entries"]
    assert [e["rate_cents"] for e in head] == [6000, 6000]


def test_points_and_hours_are_two_different_units():
    """工分（点数）与工时是两种口径 —— 数量一样、标签必须不同。"""
    p = tr.parse_text("小周 120 工分，单价 5")["entries"][0]
    assert p["unit"] == "points" and p["qty"] == 120 and p["rate_cents"] == 500
    h = tr.parse_text("小周 120 小时，时薪 5")["entries"][0]
    assert h["unit"] == "hours"


def test_contract_workers_are_refused_not_guessed():
    """合同工涉及交金 → **不接**（不按外包口径硬算）。"""
    for text in ("合同工 老李 8 小时，时薪 50", "老李 8 小时，要交社保公积金"):
        out = tr.parse_text(text)
        assert out["contract"] is True and out["entries"] == []


def test_money_talk_is_not_hijacked_by_the_timesheet_parser():
    """别抢记账：带钱的句子不该被当成工时（否则「打车 28」会被记成工时）。"""
    for text in ("今天花了 28 打车", "打车 28 元", "全程 10.73 公里，33 分钟", "客户有五份晚饭"):
        assert tr.parse_text(text)["entries"] == [], text


# ─── 记下来 + 汇总 ───────────────────────────────────────────

def test_summarize_groups_by_person_and_flags_missing_rate(tmp_path):
    """按人汇总；**没有单价的人不算钱**，进 issues 让用户补（不拿别人的单价顶上去）。"""
    tr.append(tmp_path, person="阿明", qty=8, unit="hours", rate_cents=5000)
    tr.append(tmp_path, person="阿明", qty=2, unit="hours", rate_cents=5000)
    tr.append(tmp_path, person="小周", qty=6, unit="hours")
    out = tr.summarize(tr.list_all(tmp_path))
    assert out["unit"] == "hours" and out["data_rows"] == 3
    by = {e["name"]: e for e in out["employees"]}
    assert by["阿明"]["hours"] == 10 and by["阿明"]["pay_cents"] == 50000
    assert by["小周"]["rate_missing"] is True and by["小周"]["pay_cents"] == 0
    assert out["total_pay_cents"] == 50000, "没有单价的那份不进合计"
    assert any("小周" in x and "没有单价" in x for x in out["issues"])
    # 同一个人出现两个单价 → 标出来（不静默取一个）
    tr.append(tmp_path, person="阿明", qty=1, unit="hours", rate_cents=6000)
    out2 = tr.summarize(tr.list_all(tmp_path))
    assert {e["name"]: e for e in out2["employees"]}["阿明"]["rate_conflict"] is True


def test_store_is_append_only_and_filterable_by_project(tmp_path):
    tr.append(tmp_path, person="阿明", qty=8, unit="hours", rate_cents=5000, project="919")
    tr.append(tmp_path, person="小周", qty=6, unit="hours", rate_cents=5000, project="924")
    lines = (tmp_path / "timesheet.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2 and all(json.loads(x)["id"] for x in lines), "只追加，每条带 id"
    assert [r["person"] for r in tr.list_all(tmp_path, project="919")] == ["阿明"]


# ─── 端点：聊天里说一句就记下来（无 LLM 的规则兜底也要能走）──────

def test_chat_records_timesheet_without_llm_and_never_touches_the_ledger(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    main = _isolate(tmp_path, monkeypatch)
    client = TestClient(main.app)
    res = client.post("/api/chat", json={"text": "阿明今天 8 小时，时薪 50"})
    body = res.json()
    assert res.status_code == 200 and body["ok"] is True
    assert body["timesheet"]["unit"] == "hours"
    emp = body["timesheet"]["employees"][0]
    assert emp["name"] == "阿明" and emp["hours"] == 8 and emp["pay_cents"] == 40000
    assert body["timesheet"]["total_pay_cents"] == 40000
    assert "工时表" in body["text"] and "不入账" in body["text"]
    # 记进工时表，但**账本一个字都没写**
    assert tr.list_all(tmp_path)[0]["person"] == "阿明"
    assert not (tmp_path / "finance_events.jsonl").exists(), "工时不是钱，不许写账本"


def test_chat_refuses_contract_workers_and_says_why(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    main = _isolate(tmp_path, monkeypatch)
    res = TestClient(main.app).post("/api/chat", json={"text": "合同工 老李 8 小时，时薪 50"})
    text = res.json()["text"]
    assert "合同工" in text and ("社保" in text or "交金" in text), "要说清为什么接不了"
    assert "外包" in text, "并给出能接的口径"
    assert tr.list_all(tmp_path) == [], "不接就不许偷偷记一条"


def test_chat_still_records_money_normally(tmp_path, monkeypatch):
    """回归：加了工时解析之后，「打车 28」还是要走记账（别被工时那条路抢走）。"""
    from fastapi.testclient import TestClient

    main = _isolate(tmp_path, monkeypatch)
    # 这条要验的是**记账规则**，不是模型；而且上面那次实测说明真模型会拖慢测试、结果还看它脸色
    monkeypatch.setattr(main.llm_skill, "is_configured", lambda: False)
    body = TestClient(main.app).post("/api/chat", json={"text": "打车 28"}).json()
    assert body.get("timesheet") is None
    assert body["pending"] and body["pending"][0]["amount_cents"] == 2800
