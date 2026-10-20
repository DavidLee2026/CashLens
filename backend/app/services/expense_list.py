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

# 行尾金额：`名称 …… 100` / `名称，100` / `名称: 1K` / `名称 1万5`
# 金额的写法（真人怎么写就怎么认，见 2026-10-20 客户口述）：
#   100 / 1,000.00 / 100元 / 100块 / 1k / 1K5 / 1.5k / 3千 / 1万 / 1万5 / 2w
_NUM = r"\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?"
# 单位必须**紧跟在数字后面**：`1k` / `1k5` / `1万5` / `100元`；裸数字没有尾数这一说
# （否则「大监1  80」会被拆成 1 + 尾数 80 —— 实测踩到过）
_AMOUNT_TAIL = (r"(?:(?P<unit>亿元|万元|万|千|k|K|w|W)(?P<tail>\d{1,2})?"
                r"|(?P<unit_simple>元|块钱|块))?")
_TAIL_AMOUNT_RE = re.compile(
    rf"^(?P<label>.*?)(?P<sep>[\s:：\-—–、,，=]*)(?P<num>{_NUM})\s*{_AMOUNT_TAIL}\s*$"
)
# 「金额」前面有没有一个真正的分隔（空格 / 逗号 / 冒号）——「Fx3+16-35」这种型号里的
# 「-35」不是金额，只是编号；没有分隔且名称以字母数字结尾时按名称行处理。
_SEP_RE = re.compile(r"[\s,，:：、]")
# 整行就是一个金额（「100」/「1K5」/「¥500」）：配给**上一行**的名称 —— 真人复制粘贴时
# 常常「名称一行、价格一行」（客户原话：「还会比如说会有换行的格式」）
_AMOUNT_ONLY_RE = re.compile(rf"^[¥￥]?\s*(?P<num>{_NUM})\s*{_AMOUNT_TAIL}$")


def _unit_of(m) -> tuple[str, str]:
    """取出 (单位, 尾数)：`unit` 是 k/万 那一档，`unit_simple` 是元/块。"""
    return (m.groupdict().get("unit") or m.groupdict().get("unit_simple") or "",
            m.groupdict().get("tail") or "")
# 只是一串数字/日期的行（「2026-10-20」「12:30」「3」）：不是条目，别当金额
_BARE_RE = re.compile(r"^[\d\s\-/.:：年月日]+$")
# 用户写在清单里的「要求」句（不是条目，也别进"没标金额"的统计）
_INSTRUCTION_RE = re.compile(r"(帮我|帮忙|请|合计|总计|一共|总共|多少|算一|算个|报价|核对|谢谢|看看)")
# 「合计 9980」「小计：3000」这类行是**合计行**，不是条目 —— 收进去就是重复记账
_TOTAL_LABEL_RE = re.compile(r"^(合计|小计|总计|共计|一共|总共|总额|合计金额|总价)\s*[:：]?$")
# 名称没收干净（以连接符/加号结尾）→ 它其实是"半截名称"，交给换行配对，别当条目
_LABEL_TAIL_JUNK = "+-—–/／:：、,，"
# 收入方向（清单也可能是"客户回款清单"）：只在**条目名**里出现这些词才改方向，不猜
_INCOME_HINTS = ("收到", "收款", "回款", "进账", "尾款", "付给我们", "付款给我", "到账")
_MAX_CENTS = 1_000_000_00  # 单条上限 ¥100 万：超过基本是把日期/编号读成了金额


def _amount_cents(num: str, unit: str = "", tail: str = "") -> int:
    """把「金额写法」换算成分。

    支持真人习惯：`1k`=1000、`1K5`=1500、`1.5k`=1500、`3千`=3000、`1万`=10000、
    `1万5`=15000、`2w`=20000（w 只认 1–2 位整数，`1200w` 是**瓦数**不是 2 亿）。
    `tail` 是「1k5 / 1万5」里那个尾数：按位数缩小后加到主数上（1k5 = 1000 + 5×100）。
    """
    raw = (num or "").replace(",", "").strip()
    if not raw:
        return 0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0
    u = (unit or "").strip()
    low = u.lower()
    if low == "w" and len(raw.split(".")[0]) > 2:
        return 0          # 「1200w 四套」是灯光瓦数，不是钱
    scale = {
        "亿": 100_000_000, "亿元": 100_000_000,
        "万": 10_000, "万元": 10_000, "w": 10_000,
        "千": 1_000, "k": 1_000,
    }.get(low) or {
        "亿": 100_000_000, "亿元": 100_000_000,
        "万": 10_000, "万元": 10_000,
        "千": 1_000,
    }.get(u, 0)
    if tail and not scale:
        return 0          # 「100元5」这种不是金额
    if scale:
        value *= scale
        if tail:
            value += int(tail) * scale / (10 ** len(tail))
    return int(round(value * 100))


def _valid_label(label: str) -> bool:
    """名称得像个人写的条目：「3个n6三脚架」是，「2026-10-」不是。"""
    if not label:
        return False
    if _BARE_RE.match(label) or _TOTAL_LABEL_RE.match(label):
        return False
    return label[-1] not in _LABEL_TAIL_JUNK


def _direction(label: str) -> str:
    return "income" if any(h in label for h in _INCOME_HINTS) else "expense"


def parse(text_lines) -> dict:
    """把「清单文本」拆成 [{label, amount_cents, direction}] + 没标金额的行。

    三种真人写法都认（客户 2026-10-20 口述的真实格式）：
        ① 一行一笔：`名称 + 分隔 + 金额`（空格 / 逗号 / 冒号 / 制表符都行）
        ② 名称一行、金额一行（复制粘贴与截图 OCR 常见的换行格式）→ 配对
        ③ 金额用口语写：`1k` / `1K5` / `1.5k` / `3千` / `1万5` / `2w`

    返回：
        {"items": [...], "skipped": [...], "is_list": bool, "total_cents": int,
         "expense_cents": int, "income_cents": int, "pair_lines": int}
    `skipped` 只放**看起来像条目但没标金额**的行（要求句、空行不算）。
    """
    if isinstance(text_lines, str):
        raw_lines = text_lines.splitlines()
    else:
        raw_lines = list(text_lines or [])

    items: list[dict] = []
    skipped: list[str] = []
    lines = 0
    pairs = 0         # 通过「名称一行 / 金额一行」配对出来的条数
    formula = False   # 出现「=」的算式行 → 这一整段归 v3.8 的算式逻辑，不按清单拆
    pending = ""      # 上一行的名称：等着下一行的纯金额

    def push(label: str, cents: int) -> None:
        items.append({"label": label, "amount_cents": cents, "direction": _direction(label)})

    for raw in raw_lines:
        line = str(raw or "").strip()
        if not line:
            continue
        lines += 1
        if "=" in line or "＝" in line:
            formula = True
            pending = ""
            continue

        # ② 整行就是一个金额：配给上一行的名称（名称已经被前面某行拿走了就不配）
        only = _AMOUNT_ONLY_RE.match(line)
        if only:
            _u, _t = _unit_of(only)
            cents = _amount_cents(only.group("num"), _u, _t)
            if pending and 0 < cents <= _MAX_CENTS:
                # 名称行可能以冒号/逗号收尾（「2个摄像助理：」）—— 存成标签时切掉
                push(pending.strip(" \t:：-—–、,，=") or pending, cents)
                pairs += 1
                pending = ""
                continue
            if pending:            # 上一行没等到金额（这行是坏数字），把它记成"没标金额"
                skipped.append(pending)
                pending = ""
            continue               # 孤零零的金额行：可能是合计/无关数字，不当条目

        m = _TAIL_AMOUNT_RE.match(line)
        if m:
            label = (m.group("label") or "").strip(" \t:：-—–、,，=")
            _u, _t = _unit_of(m)
            cents = _amount_cents(m.group("num"), _u, _t)
            sep = m.group("sep") or ""
            # 「大监1」「Fx3+16-35」这种以数字收尾的名称：**中间没有空格/逗号/冒号**、
            # 又没有单位、数字还很短时，末尾那位是名称的一部分（1 号机 / 型号 35），不是金额。
            # 判据保守到只影响「粘连 + 短数字 + 无单位」这一种写法；带分隔或带单位的照旧算钱。
            num_digits = m.group("num").replace(",", "").split(".")[0]
            no_sep = not _SEP_RE.search(sep)
            no_unit = not (_unit_of(m)[0] or "")
            glued_model = bool(label) and no_sep and no_unit and (
                label[-1].isdigit() or len(num_digits) <= 2)
            if _valid_label(label) and 0 < cents <= _MAX_CENTS and not glued_model:
                push(label, cents)
                if pending:
                    skipped.append(pending)
                pending = ""
                continue
            # 形如「2026-10-20」「12:30」「合计 3000」：不是条目，也不算"没标金额"
            if not label or _BARE_RE.match(label) or _TOTAL_LABEL_RE.match(label):
                pending = ""
                continue

        # ③ 名称行（这一行没有金额）：先记住，下一行如果是纯金额就配成一条
        if _INSTRUCTION_RE.search(line):
            continue
        if pending:
            skipped.append(pending)
        pending = line

    if pending:
        skipped.append(pending)

    is_list = (not formula) and (
        # ① 常见清单：≥3 笔，且金额行占非空行一半以上
        (len(items) >= 3 and len(items) * 2 >= max(lines, 1))
        # ② 每一行都是一笔（2 行起）：「打车 28 / 午饭 45」这种也按清单走，
        #    结果与模型路径一样是"一行一条待确认"，但金额与合计由代码给
        or (len(items) >= 2 and lines == len(items))
        # ③ 「名称一行 / 金额一行」交替到底（客户的换行格式）：2 组起
        or (len(items) >= 2 and pairs == len(items) and lines == 2 * len(items))
    )
    if is_list:
        _assign_categories(items)
    expense = sum(i["amount_cents"] for i in items if i["direction"] == "expense")
    income = sum(i["amount_cents"] for i in items if i["direction"] == "income")
    return {"items": items, "skipped": skipped, "is_list": is_list,
            "total_cents": expense + income, "expense_cents": expense,
            "income_cents": income, "pair_lines": pairs,
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
