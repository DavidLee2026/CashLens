"""项目账户归属（2026-09-26 欠账 E 组 18）：默认值、当前项目、确认时改归属、查询范围标注。

为什么单独一个测试文件：这一组行为直接决定「用户的钱落到哪个账户」，而它出错的形态是
**静默的**（账悄悄进到某个项目里，界面不报错）。所以每条优先级都要有断言钉住：
  归属优先级 = 动作里显式说的项目 > 前端当前选中项目 > 未归项目（总账户）
  查询口径   = 始终全部项目（总账户），选中项目只影响"新账默认记到哪"
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import app.main as main  # noqa: E402
from app.services import pending, projects  # noqa: E402
from app.services.state_engine import EventLedger  # noqa: E402


@pytest.fixture()
def temp_data(tmp_path, monkeypatch):
    """把 main 的数据目录指到临时目录，避免碰真实账本/映射表。"""
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "LEDGER_PATH", tmp_path / "finance_events.jsonl")
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "proj.db")
    return tmp_path


def _append_recorder():
    """假的写账本回调：只记录收到的 project，不真写。"""
    seen = []

    def fn(direction, amount_cents, category, channel, note, counterparty, project):
        seen.append({"project": project, "amount_cents": amount_cents, "direction": direction})
        return {"event_id": "x", "project": project}, True

    return seen, fn


# ─── ① 默认值：未归项目（总账户），不是"表里第一个项目" ─────────────

def test_draft_without_project_goes_to_unassigned(temp_data):
    """没指定项目 → 草稿的 project 为空（未归项目），且提示说清确认时能选。"""
    projects.create(temp_data, "121213")            # 占住"第一个项目"的位置
    d = main._draft_one("expense", 2800, "交通", "manual", "打车 28")
    assert d["project"] == ""
    assert "未归项目" in d.get("project_hint", "")
    assert "121213" not in d.get("project_hint", "")


# ─── ② 当前选中的项目＝新账默认归属 ────────────────────────────

def test_current_project_becomes_default_for_drafts(temp_data):
    row = projects.create(temp_data, "919 昆明项目")
    d = main._draft_one("expense", 2800, "交通", "manual", "打车 28", project=row["id"])
    assert d["project"] == row["id"]


def test_run_actions_prefers_explicit_project_over_current(temp_data):
    """一句话里显式说了别的项目，要能覆盖"当前选中项目"（归属是逐笔属性，不是会话属性）。"""
    cur = projects.create(temp_data, "本月报销")
    other = projects.create(temp_data, "三个月后回款")
    parsed = {"actions": [{"kind": "record", "direction": "income", "amount_cents": 300000,
                           "category": "接单", "channel": "manual", "project": "三个月后回款"}]}
    run = main._run_actions(parsed, "三个月后回款 3000", current_project=cur["id"])
    assert run["drafts"][0]["project"] == other["id"]


def test_run_actions_falls_back_to_unassigned(temp_data):
    """既没说项目、也没有当前选中项目 → 未归项目（不能落到某个既有项目上）。"""
    projects.create(temp_data, "121213")
    parsed = {"actions": [{"kind": "record", "direction": "expense", "amount_cents": 2800,
                           "category": "交通", "channel": "manual"}]}
    run = main._run_actions(parsed, "打车 28", current_project="")
    assert run["drafts"][0]["project"] == ""


# ─── ③ 确认环节能改归属（这是"用户不可能每次都放对"的兜底）─────────

def test_accept_applies_project_override(temp_data):
    d = pending.create(temp_data, direction="expense", amount_cents=2800,
                       category="交通", channel="manual", note="打车", project="")
    p = projects.create(temp_data, "919 昆明项目")
    seen, fn = _append_recorder()
    out = pending.accept(temp_data, d["id"], fn, project_override=p["id"])
    assert [x["project"] for x in seen] == [p["id"]]
    assert out["draft"]["project"] == ""            # 草稿原值不动，只是入账时被覆盖


def test_accept_without_override_keeps_draft_project(temp_data):
    d = pending.create(temp_data, direction="expense", amount_cents=2800,
                       category="交通", channel="manual", note="打车", project="")
    seen, fn = _append_recorder()
    pending.accept(temp_data, d["id"], fn)
    assert [x["project"] for x in seen] == [""]


def test_accept_override_empty_string_means_explicitly_unassigned(temp_data):
    """confirm 时选「未归项目」＝传空串，必须与"不传"区分开（不传才沿用草稿）。"""
    p = projects.create(temp_data, "919 昆明项目")
    d = pending.create(temp_data, direction="expense", amount_cents=2800,
                       category="交通", channel="manual", note="打车", project=p["id"])
    seen, fn = _append_recorder()
    pending.accept(temp_data, d["id"], fn, project_override="")
    assert [x["project"] for x in seen] == [""]


def test_accept_override_accepts_project_name(temp_data):
    """`pending.accept` 是**透传**层：它不解析名字（解析在端点那层做，见下一个测试）。"""
    d = pending.create(temp_data, direction="expense", amount_cents=2800,
                       category="交通", channel="manual", note="打车", project="")
    seen, fn = _append_recorder()
    pending.accept(temp_data, d["id"], fn, project_override="云南出差")
    assert [x["project"] for x in seen] == ["云南出差"]


def test_accept_endpoint_resolves_name_to_id(temp_data):
    """确认时用**项目名**覆盖 → 账本里必须存 id（名字只是给人看的）。

    真实踩坑（2026-09-26 当场抓到）：覆盖值直接透传给 `_append_event` 时，账本会存进一个
    不存在 id 的幽灵值 —— 回执里项目名会显示成「未归项目」，项目卡片里那笔也不计入，
    而账本看起来"有值"。所以端点这层必须先 `resolve_incoming` 成 id 再写。
    """
    p = projects.create(temp_data, "云南出差")
    d = pending.create(temp_data, direction="expense", amount_cents=2800,
                       category="交通", channel="manual", note="打车", project="")
    out = main.pending_accept(d["id"], main.PendingAcceptIn(project="云南出差"))
    assert out["appended"] is True
    assert out["project_name"] == "云南出差"
    events = EventLedger(main.LEDGER_PATH).load()
    assert events[-1]["project"] == p["id"]            # 存 id，不是名字


def test_accept_endpoint_empty_override_means_unassigned(temp_data):
    """确认时选「未归项目」＝传空串：账本里 project 为空，而不是落到某个项目。"""
    projects.create(temp_data, "121213")
    d = pending.create(temp_data, direction="expense", amount_cents=2800,
                       category="交通", channel="manual", note="打车", project="")
    out = main.pending_accept(d["id"], main.PendingAcceptIn(project=""))
    assert out["project_name"] == "未归项目"
    assert EventLedger(main.LEDGER_PATH).load()[-1]["project"] == ""


# ─── ④ 查询口径：全账本 + 标注范围 ──────────────────────────

def test_scope_note_empty_when_no_project_selected(temp_data):
    assert main._scope_note("") == ""


def test_scope_note_says_scope_is_all_projects(temp_data):
    p = projects.create(temp_data, "919 昆明项目")
    note = main._scope_note(p["id"])
    assert "全部项目" in note and "总账户" in note and "919 昆明项目" in note


def test_rule_reply_marks_scope_on_query(temp_data):
    """规则兜底的查询答复也要带口径提示（否则用户以为选了项目就只算它）。"""
    p = projects.create(temp_data, "919 昆明项目")
    r = main._rule_reply("现金流怎么样", p["id"])
    assert "全部项目" in r["text"]


def test_rule_reply_draft_uses_current_project(temp_data):
    p = projects.create(temp_data, "919 昆明项目")
    r = main._rule_reply("打车 28", p["id"])
    assert r["pending"][0]["project"] == p["id"]
