"""左栏「你的账现在什么样」要用的字段（2026-10-19 立）

起因：David 看着「记账证据 17% / 推算靠不靠谱 100%」问了一句——
「这两个数字是用户想看到的吗」。核查发现：
① 17% 不告诉他下一步做什么；
② 100% 其实是「你今天刚有进账」（0.9^0 = 1），却和下面「还推不出 30 天后」自相矛盾。

现在换成三行钱（进来多少 / 出去多少 / 净多少）+ 一句「还差几天」。
这组用例钉的是**支撑这些展示的字段**别被改坏。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services import pending  # noqa: E402
from app.services.state_engine import calc  # noqa: E402

AS_OF = datetime(2026, 10, 19, 12, 0)


def _ev(days_ago: int, cents: int, typ: str) -> dict:
    return {"event_id": f"e{days_ago}{typ}{cents}", "type": typ, "amount_cents": cents,
            "ts": (AS_OF - timedelta(days=days_ago)).isoformat()}


def test_state_splits_income_and_expense_over_30_days():
    evs = [
        _ev(1, 10_000, "income"),
        _ev(10, 2_000, "expense"),
        _ev(45, 99_000, "income"),      # 超出 30 天窗口，不该被算进去
    ]
    st = calc.compute(evs, as_of=AS_OF)
    assert st["income_30d_cents"] == 10_000
    assert st["expense_30d_cents"] == 2_000


def test_forecast_says_how_many_days_are_missing():
    """「再记 N 天」必须说得出具体数字，不能只有一个百分比。"""
    fc = calc.forecast_cashflow([_ev(1, 5000, "income")], as_of=AS_OF, horizon_days=30)
    assert fc["insufficient"] is True
    assert fc["need_days"] == 4          # 已有 1 天，门槛 5 天
    assert fc["need_weeks"] >= 1


def test_chat_draft_carries_its_own_date(tmp_path):
    """聊天记的草稿要带当天日期 —— 否则面板只能显示「日期待补」，可它明明是今天记的。"""
    d = pending.create(tmp_path, direction="expense", amount_cents=100, category="经营",
                       channel="manual", note="打印", occurred_at="2026-10-19T10:00:00")
    assert d["occurred_at"] == "2026-10-19T10:00:00"
    # 不传也要落当天，不能留空
    d2 = pending.create(tmp_path, direction="expense", amount_cents=200, category="交通",
                        channel="manual", note="打车")
    assert d2["occurred_at"]
