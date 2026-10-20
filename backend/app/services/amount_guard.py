"""回复里的数字闸 —— 模型写的那句话，金额必须出自本地引擎（2026-10-20 立）

## 起因（第一个真人客户 · 戚老师，真机）

他把 12 行成本清单发进来（`3个n6三脚架 100` / `大监1 80` / … / `灯光1200w 四套 1000`），
第二句说「**每一行空格之后的数字是需要合计的费用 请帮我合计**」。

系统生成的 **12 条待确认草稿每一笔都对**，前端把草稿金额相加得到 **¥9,980.00**（确定性代码算的）。
可同一屏上那句话写的是「你报的这些待确认的经营类支出合计总金额是 **8980 元**」——
这句是模型自己写的：它把 12 个数自己加了一遍，少算了一个 1000。
用户问的正是这个：「为什么是 8980 元，和确认框里的价格不一样？」

**账算对了，话说错了。** 而「话说错了」比算错更伤信任 —— 同一屏两个数，用户不知道该信哪个。

## 口径（既有红线，不是本轮新立的）

「金额 / 合计 / 差额一律由本地引擎算，模型只做措辞」——见 AGENTS.md「编排式智能体」与
决策那条路（`ask_decision` 的文案直接用引擎产出的那句，不经过 `compose_reply`）。
本模块把这条红线**变成可执行、可测的闸门**：模型写完的话先过一遍，不过就整条作废。

## 两道闸

1. **金额出处**：带货币标记的数（`¥` / `元` / `块`）必须在「系统给过的金额」集合里。
2. **合计一致**：说了合计（合计 / 一共 / 总共 / 共计 / 总计 / 加起来 / 加总）就必须等于
   系统算出的某一个合计（支出合计 / 收入合计 / 两者之和）。

任一不满足 → 调用方改用确定性文案（`_run_actions` 的 parts），不把这句话给用户。
"""

from __future__ import annotations

import re

_NUM = r"[0-9][0-9,]*(?:\.[0-9]+)?"
# 「¥9,980.00」「9980 元」「10块钱」「1.2 万」；
# `块` 只有在后面不是汉字/字母时才算钱 —— 「10块v口电池」「5块硬盘」里的「块」是量词
# （2026-10-20 同一张图里的真教训：那句「10块v口电池」被读成了 ¥10.00 的一笔支出）。
_MONEY_RE = re.compile(
    rf"[¥￥]\s*({_NUM})\s*(万)?"
    rf"|({_NUM})\s*(万元|万块|万|元|块钱|块(?![0-9\u4e00-\u9fa5A-Za-z]))"
)
# 「说了个合计」的关键词。宁可多认几个，也别让一句「总共 8980」溜过去。
_TOTAL_KEY_RE = re.compile(r"(合计|一共|总共|共计|总计|加起来|加总|总金额|合计金额)")
_TOTAL_NUM_RE = re.compile(rf"({_NUM})")
# 「合计 9,980.00 元」「一共 1.2 万」：合计词后面跟的单位也要认，否则把「1.2 万」读成 1.2 元
_UNIT_RE = re.compile(r"\s*(万元|万块|万|元|块钱|块)")
# 「合计 12 笔」「一共 3 项」：量词后面的数是笔数，不是钱 —— 不能当成合计去对。
_COUNTERS = "笔条项个张件份行次天"
# 「N 笔」这类笔数主张。只在**系统知道真数**的场合核对（刚记一批草稿 / 问待确认合计）：
# 那一刻说「11 笔」而草稿是 12 笔，就是同屏两个数对不上（真机第一次试就撞上了：
# 回复写「标注了金额的 11 笔」，而待确认卡里是 12 笔）。查询类回答不核对笔数 ——
# 「最近 5 笔」里的 5 是模型自己的措辞，不是账本里算出来的真数。
_BARE_COUNT_RE = re.compile(r"(?<!第)(\d+)\s*笔")
_ANY_COUNT_RE = _BARE_COUNT_RE


def _cents(num: str, unit: str = "") -> int:
    """把「9,980.00 元 / 1.2 万」换算成分（整数），只做字面换算，不做任何业务推断。"""
    try:
        value = float(num.replace(",", ""))
    except (TypeError, ValueError):
        return 0
    if unit.startswith("万"):
        value *= 10000
    return int(round(value * 100))


def money_claims(text: str) -> list[int]:
    """回复里所有**带货币标记**的金额（分）。"""
    out: list[int] = []
    for m in _MONEY_RE.finditer(text or ""):
        if m.group(1):
            out.append(_cents(m.group(1), f"{m.group(2) or ''}元"))
        else:
            out.append(_cents(m.group(3), m.group(4) or ""))
    return out


def claimed_totals(text: str) -> list[int]:
    """回复里「说了合计」的那些数（分）。

    只看合计词后面那一小段（16 字以内）的第一个数；紧跟量词（笔/条/项…）的跳过 ——
    「合计 12 笔」说的是笔数，不是金额。
    """
    out: list[int] = []
    text = text or ""
    for key in _TOTAL_KEY_RE.finditer(text):
        window = text[key.end(): key.end() + 16]
        for nm in _TOTAL_NUM_RE.finditer(window):
            rest = window[nm.end():]
            if rest.lstrip()[:1] in _COUNTERS:
                continue
            unit = _UNIT_RE.match(rest)
            out.append(_cents(nm.group(1), unit.group(1) if unit else ""))
            break
    return out


def allowed_sets(parts_text: str = "", drafts=(), totals: dict | None = None,
                 user_text: str = "") -> dict:
    """「系统给过的数」的集合 —— 闸门只放行这些。

    - `parts_text`：确定性文案（引擎自己写的那几句，含单笔金额与合计）；
    - `drafts`：这批草稿的金额（文案被截断时也还认得出）；
    - `totals`：`{count, expense_cents, income_cents}`（代码算的合计，含两者之和）；
    - `user_text`：用户原话里的数 —— 引用用户自己的数放行（「打车 28」→「¥28.00」不算编）。
      这里**故意放宽到不带货币单位的裸数字**：用户原话本来就长这样，收紧只会误杀正确复述；
      真正的闸门在「合计」那条 —— 说了合计就必须等于代码算出的合计。
    """
    cents = set(money_claims(parts_text)) | set(money_claims(user_text))
    for m in re.finditer(_NUM, user_text or ""):
        value = _cents(m.group(0))
        if value:
            cents.add(value)
    for d in drafts or ():
        try:
            cents.add(int(d.get("amount_cents") or 0))
        except (TypeError, ValueError):
            continue
    totals_set = set(claimed_totals(parts_text))
    if totals:
        exp = int(totals.get("expense_cents") or 0)
        inc = int(totals.get("income_cents") or 0)
        totals_set.update({exp, inc, exp + inc})
    counts = {int(m.group(1)) for m in _ANY_COUNT_RE.finditer(parts_text or "")}
    if totals and totals.get("count"):
        counts.add(int(totals["count"]))
    counts.add(len(drafts or ()))
    counts.discard(0)
    return {"cents": cents, "totals": totals_set, "counts": counts}


def check(reply: str, allowed_cents=(), allowed_totals=(), allowed_counts=()) -> dict:
    """闸门：返回 {"ok": bool, "reason": str}。reason 只用于日志/测试，不给用户看。"""
    cents = {int(c) for c in allowed_cents}
    totals = {int(c) for c in allowed_totals}
    for c in money_claims(reply):
        if c not in cents and c not in totals:
            return {"ok": False, "reason": f"金额 {c / 100:.2f} 不在系统给出的数里"}
    for c in claimed_totals(reply):
        if c not in totals:
            return {"ok": False, "reason": f"合计 {c / 100:.2f} 与系统算出的合计不一致"}
    counts = {int(c) for c in allowed_counts}
    if counts:  # 空集合 ＝ 这一轮系统没有可信笔数（查询类），不核对笔数
        for n in _BARE_COUNT_RE.findall(reply or ""):
            if int(n) not in counts:
                return {"ok": False, "reason": f"笔数 {n} 与系统算出的 {sorted(counts)} 不符"}
    return {"ok": True, "reason": ""}
