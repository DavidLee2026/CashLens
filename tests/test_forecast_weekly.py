"""现金流预测：按周聚合（2026-10-19 立）

## 为什么立这一组

改之前用「日净流入」算：均值 ± 1.65×标准差，**再乘 30 天**。三个毛病叠在一起：
① 只统计有记录的日子（没收入的日子当不存在）→ 趋势被抬高；
② 日粒度噪音极大 → 标准差被放大；
③ 标准差乘 30 而不是乘 √30 → 又放大 5.5 倍。

实测后果：106 笔的演示样本算出 **−¥134,915 ~ ¥260,622**（宽 ¥39.5 万），
界面上必然打「范围太宽」，等于告诉用户这个功能没用。

现在：90 天切整周桶（**没记录的周记 0 也算**），周净流入当增量，
带宽 = z × 周标准差 × √周数。同一样本变成 **¥34,823 ~ ¥69,311**（宽 ¥3.4 万）。

下面四条钉住的就是这四件事，任何一条回退都会变红。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services.state_engine.calc import forecast_cashflow  # noqa: E402

AS_OF = datetime(2026, 10, 19, 12, 0)


def _ev(days_ago: int, cents: int, typ: str = "income", tag: str = "") -> dict:
    return {
        "event_id": f"ev-{days_ago}-{cents}-{tag}",
        "type": typ,
        "amount_cents": cents,
        "ts": (AS_OF - timedelta(days=days_ago)).isoformat(),
    }


def _four_week_flow() -> list[dict]:
    """12 周里只有 4 周有进账（其余周为 0）—— 用来钉「零周要计入」。"""
    evs = []
    for week in (0, 3, 6, 9):                  # 摊在 12 周里，隔 3 周有一次（避免全挤在最后几桶）
        base = week * 7 + 3
        evs.append(_ev(base, 5000, tag=f"a{week}"))
        evs.append(_ev(base + 1, 5000, tag=f"b{week}"))
    return evs


def test_empty_ledger_does_not_pretend_a_forecast():
    """空账本必须明说「推不出来」，且必须带 extrapolated=False。

    2026-10-19 的真实 bug：空数据分支漏了这个字段，前端判 `extrapolated === false` 判不出来，
    于是把三个 0 当成 30 天预测摆出来 —— 看起来像「预测你剩 0 元」。
    """
    fc = forecast_cashflow([], as_of=AS_OF, horizon_days=30)
    assert fc["insufficient"] is True
    assert fc["extrapolated"] is False
    assert fc["median_balance_cents"] == 0


def test_band_brackets_the_median():
    fc = forecast_cashflow(_four_week_flow(), as_of=AS_OF, horizon_days=30)
    assert fc["extrapolated"] is True
    assert fc["band90_low_cents"] < fc["median_balance_cents"] < fc["band90_high_cents"]


def test_zero_weeks_are_counted_in_the_average():
    """没有记录的周要按 0 计入均值，否则趋势会被抬高。

    4 周各 +10,000（合计 40,000），其余 8 周为 0：
      正确：周均 40,000/12 ≈ 3,333 → 30 天后 ≈ 40,000 + 14,286 ≈ 54,300
      错误：只按 4 个有记录的周平均（10,000/周）→ ≈ 40,000 + 42,900 ≈ 82,900
    """
    fc = forecast_cashflow(_four_week_flow(), as_of=AS_OF, horizon_days=30)
    assert fc["median_balance_cents"] < 60_000, "均值把零周漏掉了（趋势被抬高）"
    assert "周有记录" in fc["reason"]


def test_band_grows_with_sqrt_of_horizon_not_linearly():
    """带宽要按 √周数 长，不是按周数线性长。

    60 天是 30 天的两倍；√2 ≈ 1.41。旧算法（标准差 × 天数）会是 2.0。
    """
    evs = _four_week_flow()
    fc30 = forecast_cashflow(evs, as_of=AS_OF, horizon_days=30)
    fc60 = forecast_cashflow(evs, as_of=AS_OF, horizon_days=60)
    w30 = fc30["band90_high_cents"] - fc30["band90_low_cents"]
    w60 = fc60["band90_high_cents"] - fc60["band90_low_cents"]
    assert w30 > 0 and w60 > 0
    ratio = w60 / w30
    assert 1.2 < ratio < 1.6, f"带宽按天数线性放大了（ratio={ratio:.2f}，应≈1.41）"


def test_noisy_daily_data_no_longer_explodes_the_band():
    """一天收 3 万、隔天花 2 万的抖动数据：区间不能被放大到不可用。

    旧算法在这个数据上给出几十万的宽度（日标准差 × 30）；周聚合后应该是个窄带。
    """
    evs = []
    for d in range(0, 90, 3):
        evs.append(_ev(d, 30_000, "income", tag=f"i{d}"))
        evs.append(_ev(d + 1, 20_000, "expense", tag=f"o{d}"))
    fc = forecast_cashflow(evs, as_of=AS_OF, horizon_days=30)
    width = fc["band90_high_cents"] - fc["band90_low_cents"]
    assert fc["median_balance_cents"] > 0
    assert width < 0.5 * fc["median_balance_cents"], (
        f"区间还是太宽：宽 {width/100:,.0f} 元 vs 最可能 {fc['median_balance_cents']/100:,.0f} 元"
    )


def test_too_few_active_weeks_stays_honest():
    """只有 3 周有进出 → 不外推（宁可说数据不足，也不给假区间）。"""
    evs = [_ev(w * 7 + 2, 8000, tag=f"w{w}") for w in range(3)]
    fc = forecast_cashflow(evs, as_of=AS_OF, horizon_days=30)
    assert fc["insufficient"] is True
    assert fc["extrapolated"] is False
