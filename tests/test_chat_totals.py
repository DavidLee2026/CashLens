"""对话里的「合计」必须由代码算（2026-10-20 · 真机 bug 回归）

## 那次真机发生了什么

戚老师把 12 行成本清单发进来（`3个n6三脚架 100` … `灯光1200w 四套 1000`），
再问「**每一行空格之后的数字是需要合计的费用 请帮我合计**」。

- 系统生成的 **12 条待确认草稿每一笔都对**，前端把草稿相加 = **¥9,980.00**（确定性）；
- 可对话里那句话写着「…合计总金额是 **8980 元**」—— 模型自己把 12 个数加了一遍，少了一个 1000；
- 用户看到的是一屏两个数：「文字 8980」vs「确认框 9,980.00」，他直接问「为什么是 8980」。

**账算对了，话说错了。** 这里的测试锁住三件事：
1. 一批草稿的合计由 `_draft_block` 算，模型只说一遍「照抄」；
2. 模型如果在话里给了别的数（8980 / 编的金额），这句话**整条作废**，换成确定性文案；
3. 用户问「合计多少」有专门的动作 `ask_pending_total`，数字只从草稿加出来。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services import amount_guard, pending  # noqa: E402

# 用户原话里那 12 行（金额用分）
_LINES = [
    ("3个n6三脚架", 10000), ("大监1", 8000), ("Fx3+16-35 一套配背夹", 25000),
    ("一套调音台配4手持话筒", 50000), ("1台电视机和移动支架配1个电小二", 15000),
    ("4个摄像", 400000), ("2个摄像助理", 100000), ("1个场务", 40000),
    ("一个音控师带笔记本", 100000), ("音频助理一位", 50000),
    ("灯光助理2位", 100000), ("灯光1200w 四套", 100000),
]
_TOTAL_CENTS = 998000  # ¥9,980.00（真机确认框里那个数）
_MODEL_SAID = ("记好了，你报的这些待确认的经营类支出合计总金额是 8980元。"
               "先按你说的记下，回头有流水/票据发我，我帮你核实升级。")


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CASH_DATA_DIR", str(tmp_path))
    from app import main
    return main


def _fake_llm(main, monkeypatch, actions, reply="_compose_text_"):
    """把模型两条路都换掉：解析固定回 actions，润色固定回 reply（或指定的那句话）。"""
    monkeypatch.setattr(main.llm_skill, "is_configured", lambda: True)
    monkeypatch.setattr(main.llm_skill, "parse_accounting",
                        lambda text, ctx="": {"actions": actions, "reply": ""})
    if callable(reply):
        monkeypatch.setattr(main.llm_skill, "compose_reply", reply)
    else:
        monkeypatch.setattr(main.llm_skill, "compose_reply",
                            lambda text, results, timeout=30, context="": reply)


def _record_actions():
    return [{"kind": "record", "direction": "expense", "amount_cents": cents,
             "channel": "manual", "category": "经营", "note": note} for note, cents in _LINES]


# ─── 闸门本身（不依赖模型）────────────────────────────

def test_guard_rejects_the_number_the_model_got_wrong():
    """真机那句话必须被拦下来（8980 ≠ 9980）。"""
    allowed = amount_guard.allowed_sets(
        "合计 支出 ¥9,980.00（12 笔）。",
        totals={"count": 12, "expense_cents": _TOTAL_CENTS, "income_cents": 0})
    verdict = amount_guard.check(_MODEL_SAID, allowed["cents"], allowed["totals"])
    assert verdict["ok"] is False
    assert "8980" in verdict["reason"]


def test_guard_allows_the_code_computed_total_and_the_right_details():
    allowed = amount_guard.allowed_sets(
        "· 3个n6三脚架 ¥100.00（经营）\n合计 支出 ¥9,980.00（12 笔）。",
        totals={"count": 12, "expense_cents": _TOTAL_CENTS, "income_cents": 0})
    ok = ("记好了：12 笔都放进「待确认」，合计 ¥9,980.00，逐笔确认后才进账本。"
          "第一笔是 3个n6三脚架 ¥100.00。")
    assert amount_guard.check(ok, allowed["cents"], allowed["totals"])["ok"] is True


def test_count_is_not_a_money_claim():
    """「合计 12 笔」说的是笔数，别把它当金额去对（否则正确的回复会被误杀）。"""
    allowed = amount_guard.allowed_sets("", totals={"count": 12, "expense_cents": _TOTAL_CENTS,
                                                    "income_cents": 0})
    reply = "一共 12 笔待确认，合计 9,980 元。"
    assert amount_guard.check(reply, allowed["cents"], allowed["totals"],
                              allowed["counts"])["ok"] is True


def test_hallucinated_line_amount_is_rejected():
    allowed = amount_guard.allowed_sets("合计 支出 ¥9,980.00。",
                                       totals={"count": 2, "expense_cents": _TOTAL_CENTS,
                                               "income_cents": 0})
    reply = "其中灯光 ¥1,500.00 是大头。"
    assert amount_guard.check(reply, allowed["cents"], allowed["totals"])["ok"] is False


def test_users_own_numbers_can_be_quoted():
    """引用用户原话里的金额不算编（他说的「打车 28」）。"""
    allowed = amount_guard.allowed_sets("", user_text="打车 28，午饭 45")
    assert amount_guard.check("打车 ¥28.00 那笔记下了。", allowed["cents"], allowed["totals"])["ok"]


def test_block_as_a_measure_word_is_not_money():
    """「10块v口电池」里的「块」是量词 —— 真机里它被读成了 ¥10.00 的一笔支出。"""
    assert amount_guard.money_claims("准备 10块v口电池") == []
    assert amount_guard.money_claims("打车 10块") == [1000]
    assert amount_guard.money_claims("一共 10块钱") == [1000]


# ─── 端点级：一批草稿 + 模型写错话 ────────────────────

def test_batch_total_is_computed_by_code_and_bad_sentence_is_dropped(tmp_path, monkeypatch):
    """12 行清单：合计由代码算，模型那句 8980 不许发给用户。"""
    from fastapi.testclient import TestClient

    main = _isolate(tmp_path, monkeypatch)
    _fake_llm(main, monkeypatch, _record_actions(), reply=_MODEL_SAID)
    body = TestClient(main.app).post("/api/chat", json={"text": "每一行空格之后的数字是需要合计的费用 请帮我合计"}).json()

    text = body["text"]
    assert "8980" not in text, "模型自己加出来的数不许出现在用户面前"
    assert "9,980.00" in text, "合计要由代码算出来（12 笔 ¥9,980.00）"
    assert len(body["pending"]) == 12
    assert sum(d["amount_cents"] for d in body["pending"]) == _TOTAL_CENTS
    # 草稿是真落盘的（这一步只写草稿，不入账）
    assert len(pending.list_all(tmp_path)) == 12


def test_good_sentence_survives_the_guard(tmp_path, monkeypatch):
    """说对了就保留模型的口语化说法 —— 闸门不是"一律不用模型"。"""
    from fastapi.testclient import TestClient

    main = _isolate(tmp_path, monkeypatch)
    good = "记好了：12 笔都先进「待确认」，合计 ¥9,980.00，等你有票据我再帮你核。"
    _fake_llm(main, monkeypatch, _record_actions(), reply=good)
    body = TestClient(main.app).post("/api/chat", json={"text": "帮我合计"}).json()
    assert body["text"] == good


def test_single_record_still_uses_the_old_one_line_copy(tmp_path, monkeypatch):
    """回归：单笔记账的文案不变（别为了多笔把单笔也改成长列表）。"""
    from fastapi.testclient import TestClient

    main = _isolate(tmp_path, monkeypatch)
    actions = [{"kind": "record", "direction": "expense", "amount_cents": 2800,
                "channel": "manual", "category": "交通", "note": "打车"}]

    def boom(*a, **kw):
        raise RuntimeError("模型挂了 —— 这里要验的是确定性兜底文案")

    _fake_llm(main, monkeypatch, actions, reply=boom)
    body = TestClient(main.app).post("/api/chat", json={"text": "打车 28"}).json()
    assert "识别到支出 ¥28.00" in body["text"]
    assert "请确认后入账" in body["text"]


def test_ask_pending_total_sums_the_drafts_not_the_ledger(tmp_path, monkeypatch):
    """「待确认里一共多少钱」→ 只加草稿，不碰账本；数字与笔数由代码给。"""
    from fastapi.testclient import TestClient

    main = _isolate(tmp_path, monkeypatch)
    for note, cents in _LINES[:3]:
        pending.create(tmp_path, direction="expense", amount_cents=cents,
                       category="经营", channel="manual", note=note)
    _fake_llm(main, monkeypatch, [{"kind": "ask_pending_total"}],
              reply="加起来一共 1 块钱吧。")   # 模型乱说 → 闸门必须换成真数
    body = TestClient(main.app).post("/api/chat", json={"text": "待确认里一共多少钱"}).json()

    assert "1 块钱" not in body["text"]
    assert "3 笔" in body["text"] and "¥430.00" in body["text"]   # 100+80+250
    # 只写草稿：账本里一笔都没有
    assert not (tmp_path / "finance_events.jsonl").exists()


def test_ask_pending_total_says_so_when_empty(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    main = _isolate(tmp_path, monkeypatch)
    _fake_llm(main, monkeypatch, [{"kind": "ask_pending_total"}], reply="")
    body = TestClient(main.app).post("/api/chat", json={"text": "待确认一共多少"}).json()
    assert "一笔都没有" in body["text"]


def test_wrong_count_is_rejected_too():
    """笔数说错也是「两个数对不上」：真机那句要是写成「共 10 笔」同样不过闸。"""
    allowed = amount_guard.allowed_sets("识别到 12 笔支出（合计 ¥9,980.00）。",
                                       totals={"count": 12, "expense_cents": _TOTAL_CENTS,
                                               "income_cents": 0})
    assert allowed["counts"] >= {12}
    assert amount_guard.check("记好了，共 12 笔，合计 9,980 元。", allowed["cents"],
                              allowed["totals"], allowed["counts"])["ok"] is True
    assert amount_guard.check("记好了，共 10 笔，合计 9,980 元。", allowed["cents"],
                              allowed["totals"], allowed["counts"])["ok"] is False


def test_batch_count_slip_is_rejected():
    """真机第二次实弹：草稿 12 笔，回复写「标注了金额的 11 笔」—— 同样是同屏两个数对不上。"""
    allowed = amount_guard.allowed_sets("识别到 12 笔支出（合计 ¥9,980.00）。",
                                       totals={"count": 12, "expense_cents": _TOTAL_CENTS,
                                               "income_cents": 0})
    slip = "合计好了，目前你标注了金额的11笔经营类支出总金额是 9980 元。"
    assert amount_guard.check(slip, allowed["cents"], allowed["totals"],
                              allowed["counts"])["ok"] is False


def test_query_turn_does_not_police_the_word_笔():
    """查询类回答没有「可信笔数」这一说：「最近 5 笔」里的 5 是措辞，不该被拦。"""
    allowed = amount_guard.allowed_sets("最近流水：支出 ¥28.00（交通 · 现金 · 2026-10-20）。")
    assert allowed["counts"] == set()
    assert amount_guard.check("最近 5 笔里最大的一笔是 ¥28.00。", allowed["cents"],
                              allowed["totals"], allowed["counts"])["ok"] is True
