"""「每行一个条目 + 末尾一个金额」的清单解析（2026-10-20 立，确定性、不依赖模型）

## 起因（第一个真人客户 · 真机）

他把 16 行成本清单发进来（`3个n6三脚架  100` / `大监1  80` / … / `灯光1200w 四套  1000`，
末尾还有几行没标价的配置项，最后一句「帮我算个总价」）：

- **第一次**只发清单（配一句「帮我算个总价」）→ 模型只挑了其中一行（「10块v口电池」读成 ¥10.00），
  其余 12 行全丢；
- **第二次**他补上一句「**每一行空格之后的数字是需要合计的费用 请帮我合计**」→ 模型这才按行拆出 12 笔。

也就是说：**这个结构靠人解释才成立**。而「名称 + 末尾数字」这种写法在中国人的报价单 / 成本清单里
是最常见的一种 —— 用户没有理由先教我们一遍。这条逻辑必须由代码认，不能指望模型每次开窍。

## 口径（三条，全部确定性）

1. **一行一笔**：行尾的数字就是这一行的金额（`元` 可省）。
2. **行尾不是数字的行不是一笔**：「音频线4根」「准备6根40米的sdi信号线…」「hdmi线根据电视来配」
   这类配置项没有价格 —— 不猜、不编，只在回复里如实说「另有 N 行没标金额，没计入合计」。
3. **合计由这里加**：`total_cents` 是代码算的；对话里的合计一律引用它，模型不复述。

## 什么时候算「清单」

- 至少 **3 行**带金额（一句话里顺带一个数不算清单）；
- 而且这些行要占**非空行的一半以上**（防止把一段带数字的闲聊当清单）；
- 每行的数字在**行尾**（「10块v口电池」的 10 在行中，是量词不是钱，天然被排除）。
"""

from __future__ import annotations

import re

from . import categories

# 行尾金额：`名称 …… 100` / `名称 …… 100 元` / `名称 …… 1,000.00 块`
_TAIL_AMOUNT_RE = re.compile(
    r"^(?P<label>.*?)[\s:：\-—–、,，=]*"
    r"(?P<num>\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
    r"\s*(?P<unit>亿元|万元|万|元|块钱|块)?\s*$"
)
# 只是一串数字/日期的行（「2026-10-20」「12:30」「3」）：不是条目，别当金额
_BARE_RE = re.compile(r"^[\d\s\-/.:：年月日]+$")
# 用户写在清单里的「要求」句（不是条目，也别进"没标金额"的统计）
_INSTRUCTION_RE = re.compile(r"(帮我|帮忙|请|合计|总计|一共|总共|多少|算一|算个|报价|核对|谢谢|看看)")
# 「合计 9980」「小计：3000」这类行是**合计行**，不是条目 —— 收进去就是重复记账
_TOTAL_LABEL_RE = re.compile(r"^(合计|小计|总计|共计|一共|总共|总额|合计金额|总价)\s*[:：]?$")
# 收入方向（清单也可能是"客户回款清单"）：只在**条目名**里出现这些词才改方向，不猜
_INCOME_HINTS = ("收到", "收款", "回款", "进账", "尾款", "付给我们", "付款给我", "到账")
_MAX_CENTS = 1_000_000_00  # 单条上限 ¥100 万：超过基本是把日期/编号读成了金额


def _to_cents(num: str, unit: str = "") -> int:
    try:
        value = float(num.replace(",", ""))
    except (TypeError, ValueError):
        return 0
    if unit.startswith("万"):
        value *= 10000
    elif unit.startswith("亿"):
        value *= 100000000
    return int(round(value * 100))


def _direction(label: str) -> str:
    return "income" if any(h in label for h in _INCOME_HINTS) else "expense"


def parse(text_lines) -> dict:
    """把「清单文本」拆成 [{label, amount_cents, direction}] + 没标金额的行。

    返回：
        {"items": [...], "skipped": [...], "is_list": bool, "total_cents": int,
         "expense_cents": int, "income_cents": int}
    `skipped` 只放**看起来像条目但没标金额**的行（要求句、空行不算）。
    """
    if isinstance(text_lines, str):
        raw_lines = text_lines.splitlines()
    else:
        raw_lines = list(text_lines or [])

    items: list[dict] = []
    skipped: list[str] = []
    lines = 0
    formula = False  # 出现「=」的算式行 → 这一整段归 v3.8 的算式逻辑，不按清单拆
    for raw in raw_lines:
        line = str(raw or "").strip()
        if not line:
            continue
        lines += 1
        if "=" in line or "＝" in line:
            formula = True
            continue
        m = _TAIL_AMOUNT_RE.match(line)
        if m:
            label = (m.group("label") or "").strip(" \t:：-—–、,，=")
            cents = _to_cents(m.group("num"), m.group("unit") or "")
            if (label and not _BARE_RE.match(label) and not _TOTAL_LABEL_RE.match(label)
                    and 0 < cents <= _MAX_CENTS):
                items.append({"label": label, "amount_cents": cents,
                              "direction": _direction(label)})
                continue
            # 形如「2026-10-20」「12:30」「合计 3000」：不是条目，也不算"没标金额"
            if not label or _BARE_RE.match(label) or _TOTAL_LABEL_RE.match(label):
                continue
        if not _INSTRUCTION_RE.search(line):
            skipped.append(line)

    is_list = (not formula) and len(items) >= 3 and len(items) * 2 >= max(lines, 1)
    if is_list:
        _assign_categories(items)
    expense = sum(i["amount_cents"] for i in items if i["direction"] == "expense")
    income = sum(i["amount_cents"] for i in items if i["direction"] == "income")
    return {"items": items, "skipped": skipped, "is_list": is_list,
            "total_cents": expense + income, "expense_cents": expense,
            "income_cents": income,
            "context_filled": sum(1 for i in items if i.get("category_from") == "context")}


def _assign_categories(items: list[dict]) -> None:
    """逐条按条目名判分类；判不出的**在同组有证据时才跟着走**。

    为什么允许「跟着走」（2026-10-20）：清单里「大监1」「1个场务」这类行话词表永远追不全，
    而「待确认」在确认入账时会被收成「其他」—— 一笔项目成本就这么散掉了。
    判据是**同组证据**：这份清单里至少 2 条靠词表判成了「经营」（三脚架 / 调音台 / 摄像 /
    场务…），说明它整体就是一份项目成本清单，那剩下的行按同一口径归「经营」，
    并在 `category_from` 里标记 `context` —— 回复里会明说「整体按项目成本归类，不对可以在
    待确认里改」，不假装每条都是逐条判出来的。
    """
    named = [category_of(i["label"]) for i in items]
    business = sum(1 for c in named if c == categories.BUSINESS_CATEGORY)
    for item, cat in zip(items, named):
        if cat == categories.UNCONFIRMED_CATEGORY and business >= 2:
            item["category"] = categories.BUSINESS_CATEGORY
            item["category_from"] = "context"
        else:
            item["category"] = cat
            item["category_from"] = "label"


def category_of(label: str) -> str:
    """条目名 → 分类（判不出就落「待确认」，不瞎猜；口径与全系统同一张表）。"""
    return categories.match_category(label)
