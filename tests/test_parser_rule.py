"""parser_rule（服务端规则层）测试 —— 与 demo JS 规则同源的关键用例。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services.parser_rule import analyze, extract_amount  # noqa: E402


def test_extract_amount_cases():
    assert extract_amount("二九九") == 299
    assert extract_amount("昨天微信收了三千元尾款") == 3000
    assert extract_amount("打车花了 28") == 28
    assert extract_amount("咖啡三十八") == 38
    assert extract_amount("我付了三千给客户") == 3000
    assert extract_amount("今天进账其实二九九") == 299
    # 防误判：不是钱
    assert extract_amount("今天是和十个客户接触了，但是他们都不想聊") == 0
    assert extract_amount("花了10分钟") == 0
    assert extract_amount("十点开会") == 0
    assert extract_amount("周三") == 0


def test_analyze_record_direction_channel():
    r = analyze("昨天微信收了 3000 尾款")
    assert r["kind"] == "record" and r["direction"] == "income" and r["amount_cents"] == 300000
    assert r["channel"] == "wechat" and r["category"] == "接单"

    r2 = analyze("打车花了 28")
    assert r2["direction"] == "expense" and r2["amount_cents"] == 2800 and r2["category"] == "交通"

    r3 = analyze("嗯，我客户这边二九九套餐愿意支付，呃，客户支付的是支付宝，你记录一下")
    assert r3["direction"] == "income" and r3["amount_cents"] == 29900 and r3["channel"] == "alipay"


def test_analyze_kinds():
    assert analyze("我下个月现金流怎么样？")["kind"] == "ask_cashflow"
    assert analyze("会不会缺钱？")["kind"] == "ask_cashflow"
    assert analyze("记录一下")["kind"] == "fallback"
    assert analyze("今天天气不错")["kind"] == "fallback"
