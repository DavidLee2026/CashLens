"""对话规则解析（服务端兜底层）

对应 02-脑暴/金额语义层分工规格-20260905.md：
规则层 = 确定性兜底 + 测试基准；此处把 demo 已验证的句级语义规则移植为服务端实现，
供 /api/chat 真链路使用（SKILL/LLM 主解析后续接入后本层退化为兜底）。
规则与 demo（docs/index.html）同源：金额用分，方向/渠道词表一致。
"""

from __future__ import annotations

import re

# ── 数字表 ──
_CN_D = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_U = {"十": 10, "百": 100, "千": 1000}
_CN_B = {"万": 10000, "亿": 100000000}

# ── 金钱语境信号 ──
_MONEY_VERBS = ["支付", "收到", "收款", "进账", "入账", "到账", "转账", "付款", "退款", "报销", "充值",
                "扣款", "结算", "消费", "花费", "花", "付", "收", "买", "卖", "转", "赚", "借", "还",
                "缴", "报", "工资", "房租", "货款", "尾款", "报酬", "稿费", "分红", "押金", "定金",
                "价格", "报价", "多少钱", "记录", "记账"]
_CHANNELS = {"微信": "wechat", "支付宝": "alipay", "现金": "cash", "银行卡": "bank", "银行": "bank", "零钱": "cash"}
_COUNTER_AFTER = "个名位次天点号分秒里台件杯份张岁层月周年季刻钟条辆双只朵页本集回人部下节课"
_ASK_WORDS = ["现金流", "缺钱", "够不够", "下个月", "会不会", "状态", "健康", "预测", "撑住", "断粮"]
_INCOME_WORDS = ["客户支付", "收到", "收款", "回款", "收", "尾款", "工资", "入账", "进账", "退款", "红包", "报酬", "项目款", "货款", "结算"]
_EXPENSE_WORDS = ["买", "花", "付", "充值", "打车", "咖啡", "奶茶", "餐", "吃", "超市", "房租", "水电", "电影", "机票", "酒店", "报销"]
_CAT_RULES = [
    ("餐饮", ["咖啡", "奶茶", "餐", "吃", "外卖", "饭店", "早餐", "午餐", "晚餐", "吃喝"]),
    ("交通", ["打车", "地铁", "公交", "加油", "高铁", "机票", "停车"]),
    ("购物", ["买", "超市", "购物", "淘", "拼多多", "京东", "衣服", "裤子"]),
    ("房租", ["房租", "租金"]),
    ("娱乐", ["电影", "游戏", "会员"]),
]
_IN_CAT_RULES = [
    ("接单", ["尾款", "项目", "接单", "报酬", "货款", "结算", "套餐", "模板", "设计"]),
    ("工资", ["工资"]),
    ("退款", ["退款", "退货"]),
]


def cn_to_amount(s: str) -> float:
    """中文金额 → 数值（单位制 + 无单位连续数字十进制展开）。"""
    dot = s.find("点")
    frac, fdiv = 0.0, 1
    if dot > -1:
        for ch in s[dot + 1:]:
            d = _CN_D.get(ch)
            if d is None:
                break
            fdiv *= 10
            frac += d / fdiv
        s = s[:dot]
    if not any(ch in _CN_U or ch in _CN_B for ch in s):
        acc = 0
        for ch in s:
            d = _CN_D.get(ch)
            if d is not None:
                acc = acc * 10 + d
        return round(acc + frac, 2)
    result = section = num = lit = 0
    zero_pending = False
    last_u = last_b = 0
    for ch in s:
        if ch in ("零", "〇"):
            zero_pending = True
            continue
        if ch in _CN_D:
            if zero_pending:
                lit = lit * 10 + _CN_D[ch]
                zero_pending = False
            else:
                num = _CN_D[ch]
            continue
        if ch in _CN_U:
            section += (lit if lit else num or 1) * _CN_U[ch]
            last_u, last_b = _CN_U[ch], 0
            num = lit = 0
            zero_pending = False
            continue
        if ch in _CN_B:
            section += lit if lit else num
            section = (section or 1) * _CN_B[ch]
            result += section
            section = 0
            last_b, last_u = _CN_B[ch], 0
            num = lit = 0
            zero_pending = False
    tail = 0.0
    if lit:
        tail = lit
    elif num:
        if last_b:
            tail = num * (last_b / 10)
        elif last_u:
            tail = num * (last_u / 10)
        else:
            tail = num
    return round(result + section + tail + frac, 2)


def _is_money(text: str, start: int, end: int) -> bool:
    """句级判定：该数字候选是否为金额（紧跟货币单位/句尾加分；量词/序数减分）。"""
    prev = text[start - 1] if start > 0 else ""
    if prev in ("周", "期", "拜", "第"):
        return False
    after = re.sub(r"^[\s,，。.．、!！?？:：;；'\"”’「」『』()（）]*", "", text[end:])
    if re.match(r"^(?:元|块|块钱|万元|人民币)", after):
        return True
    if after == "":
        return True
    if after.startswith(("客户", "朋友", "小时", "分钟")):
        return False
    if not after.startswith("套餐") and after[0] in _COUNTER_AFTER:
        return False
    if after.startswith("套餐"):
        return True
    near = text[max(0, start - 4):min(len(text), end + 4)]
    return any(v in near for v in _MONEY_VERBS)


_NUM_RE = re.compile(r"[零〇一二两三四五六七八九十百千万亿]+(?:点[零〇一二三四五六七八九]+)?|\d+(?:\.\d+)?")


def extract_amount(text: str) -> float:
    """枚举全部数字候选，取证据最高的金额（0 = 不是金额）。"""
    best, best_score = 0.0, -99
    for m in _NUM_RE.finditer(text):
        seg = m.group(0)
        if not _is_money(text, m.start(), m.end()):
            continue
        value = float(seg) if seg[:1].isdigit() else cn_to_amount(seg)
        if value <= 0:
            continue
        score = 0
        after = re.sub(r"^[\s,，。.．、!！?？:：;；'\"”’「」『』()（）]*", "", text[m.end():])
        if re.match(r"^(?:元|块|块钱|万元|人民币)", after):
            score += 3
        if after == "":
            score += 1
        if after.startswith("套餐"):
            score += 1
        if not after.startswith("套餐") and after and after[0] in _COUNTER_AFTER:
            score -= 2
        near = text[max(0, m.start() - 4):min(len(text), m.end() + 4)]
        if not after.startswith("套餐") and any(v in near for v in _MONEY_VERBS):
            score += 2
        if value >= 10000 or "万" in seg or "亿" in seg:
            score += 1
        if score > best_score:
            best, best_score = value, score
    has_money_signal = any(v in text for v in _MONEY_VERBS) or any(c in text for c in _CHANNELS) or bool(re.search(r"(?:元|块|块钱|万元|人民币)", text))
    if not has_money_signal and best_score < 1:
        return 0.0
    if has_money_signal and best_score < 1:
        return 0.0
    return best


def analyze(text: str) -> dict:
    """把一句话解析成结构化草稿（供 /api/chat 与规则测试）。"""
    text = (text or "").strip()
    amount = extract_amount(text)
    ask = any(w in text for w in _ASK_WORDS) and amount == 0
    if ask:
        return {"kind": "ask_cashflow", "text": text}
    if amount <= 0:
        return {"kind": "fallback", "text": text}
    is_income = any(w in text for w in _INCOME_WORDS)
    # 无收入词命中 → 默认按支出记录（与 demo 规则口径一致）
    rules = _IN_CAT_RULES if is_income else _CAT_RULES
    cat = "其他"
    for cname, words in rules:
        if any(w in text for w in words):
            cat = cname
            break
    channel = "manual"
    for cname, ch in _CHANNELS.items():
        if cname in text:
            channel = ch
            break
    return {
        "kind": "record",
        "text": text,
        "amount_cents": round(amount * 100),
        "direction": "income" if is_income else "expense",
        "channel": channel,
        "category": cat,
        "note": text,
        "confidence": 0.75,
    }
