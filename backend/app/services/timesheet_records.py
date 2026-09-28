"""工时 / 工分记录（**文字里说的也能记**）—— 2026-09-28

用户口径（原话）：「客户这边的人员有 2 种，一种是**外包性质的**，一种是**合同工**，
先增加计算外包性质，按小时算，这个中比较好计算，**因为不涉及交金的部分，只是按照时间算钱**」。

所以这一版：
  · 只做**外包**：按小时（或工分点数）× 单价 = 应付劳务费，**不涉及社保公积金**；
  · **合同工明确不接**（涉及交金，规则完全不同）—— 如实说明「暂未支持」，不猜数字、不按外包口径硬算；
  · 文字里说的（「阿明今天 8 小时，时薪 50」）与 Excel 导入的，**汇到同一张汇总卡**。

存储：`data/timesheet.jsonl`（追加式，与账本同一套纪律：只追加、带 id 与来源，可追溯）。
⚠️ **它不是账本**：工时/工分不是钱。这里只记数量、单价与来源；应付金额是**参考值**，
   要入账必须用户确认 —— 本模块不写任何账本事件。
"""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

_FILE = "timesheet.jsonl"

# 口径：hours = 小时（外包按小时），points = 工分/点数
_UNITS = ("hours", "points")
# 人员性质（这一版只支持 outsourced）
KIND_LABELS = {"outsourced": "外包", "contract": "合同工"}

# 数量：8 小时 / 120 工分 / 8h / 3.5 小时
# ⚠️ 负向断言只排掉「分钟 / 分钱」这类**同头不同义**的写法，不能写成「后面不许跟汉字」——
# 那会把「8 小时时薪 50」这种再自然不过的写法整句挡掉（写的时候真踩到了）。
_QTY = re.compile(r"(\d+(?:\.\d+)?)\s*(个?小时|小时|工时|工分|点数|点|分|h)(?!钟|钱)", re.I)
_UNIT_HOURS = ("小时", "工时", "h", "个时")
# 单价：时薪 50 / 单价 5 / 50 元/小时 / 每分 5 毛…（只认到"元"这一档，毛角不做换算）
# 「时薪 50」「时薪都是 50」「单价按 5 块算」都要认（中间允许 都是/都/统一/一律/按/为/是）
_RATE_A = re.compile(r"(?:时薪|单价|每(?:个)?小时|每分|每点)\s*(?:都是|都|统一|一律|按|为|是)?\s*[：:]?\s*(\d+(?:\.\d+)?)")
_RATE_B = re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块)\s*/?\s*(?:个?小时|分|点)")
_NAME_TAIL = re.compile(r"([\u4e00-\u9fa5]{2,4})\s*(?:今天|昨天|前天|本周|这周|上周|这个月|本月)?\s*$")
_NOISE_WORDS = ("今天", "昨天", "前天", "本周", "这周", "上周", "这个月", "本月",
                "一共", "总共", "合计", "外包", "合同工", "帮", "给")
# 合同工（涉及交金）—— 明确不接，别按外包口径硬算
_CONTRACT_HINTS = ("合同工", "正式工", "交金", "社保", "公积金", "五险")


def _path(data_dir: str | Path) -> Path:
    return Path(data_dir) / _FILE


def _find_rate(seg: str) -> int | None:
    m = _RATE_A.search(seg) or _RATE_B.search(seg)
    if not m:
        return None
    try:
        return int(round(float(m.group(1)) * 100))
    except (TypeError, ValueError):
        return None


def _person_before(seg: str, pos: int) -> str:
    m = _NAME_TAIL.search(seg[:pos])
    if not m:
        return ""
    name = m.group(1)
    for w in _NOISE_WORDS:
        name = name.replace(w, "")
    return name.strip()


def parse_text(text: str) -> dict:
    """把一句话解析成若干条工时/工分记录（**确定性**，不调模型）。

    「阿明今天 8 小时，时薪 50」→ 一条；「阿明 8 小时、小周 6 小时，时薪都是 50」→ 两条 + 一个默认单价。
    返回 `{"entries": [...], "contract": bool}`；`contract=True` 表示这句话讲的是合同工，
    调用方要**如实说明暂未支持**，不能按外包口径算。
    """
    raw = str(text or "")
    if any(w in raw for w in _CONTRACT_HINTS):
        return {"entries": [], "contract": True}

    entries: list[dict] = []
    rates: list[int] = []
    for seg in [x for x in re.split(r"[，,；;。\n]", raw) if x.strip()]:
        rate = _find_rate(seg)
        hits = list(_QTY.finditer(seg))
        if rate is not None:
            rates.append(rate)
        for m in hits:
            unit = "hours" if any(u in m.group(2).lower() for u in _UNIT_HOURS) else "points"
            entries.append({
                "person": _person_before(seg, m.start()),
                "qty": float(m.group(1)),
                "unit": unit,
                "rate_cents": None,
                "project": "",
            })
        if rate is not None and hits:
            # 同一句里既有人又有单价（「阿明 8 小时 时薪 50」）→ 归这一句最后那个人
            entries[-1]["rate_cents"] = rate
        elif rate is not None and entries:
            # 「，时薪 50」这种补充句 → 归上一条
            entries[-1]["rate_cents"] = rate

    # 单价分配：整句**只提到一个单价**（「时薪都是 50」「外包时薪 60：…」）→ 发给所有还没单价的人；
    # 提到多个（「阿明时薪 50，小周时薪 60」）→ 按出现顺序各归各的，剩下的留空（不猜）。
    rest = [r for r in rates if r]
    if len(rest) == 1:
        for e in entries:
            if e["rate_cents"] is None:
                e["rate_cents"] = rest[0]
    elif len(rest) > 1:
        queue = list(rest)
        for e in entries:
            if e["rate_cents"] is None and queue:
                e["rate_cents"] = queue.pop(0)
    return {"entries": [e for e in entries if e["qty"] > 0], "contract": False}


def append(data_dir: str | Path, *, person: str, qty: float, unit: str = "hours",
           rate_cents: int | None = None, kind: str = "outsourced", project: str = "",
           date: str = "", source: str = "chat", note: str = "") -> dict:
    """追加一条工时/工分记录（**只追加，不改已有行**）。"""
    rec = {
        "id": uuid.uuid4().hex[:12],
        "person": str(person or "").strip(),
        "qty": round(float(qty or 0), 2),
        "unit": unit if unit in _UNITS else "hours",
        "rate_cents": int(rate_cents) if rate_cents else None,
        "kind": kind if kind in KIND_LABELS else "outsourced",
        "project": str(project or ""),
        "date": str(date or ""),
        "source": str(source or "chat"),
        "note": str(note or "")[:200],
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    fp = _path(data_dir)
    fp.parent.mkdir(parents=True, exist_ok=True)
    with fp.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def list_all(data_dir: str | Path, *, project: str | None = None) -> list[dict]:
    fp = _path(data_dir)
    if not fp.exists():
        return []
    out: list[dict] = []
    for line in fp.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue                      # 坏行跳过，不让一行把整表带崩
        if project and str(rec.get("project") or "") != project:
            continue
        out.append(rec)
    return out


def summarize(records: list[dict]) -> dict:
    """按人汇总成一张参考表 —— **结构与 `timesheet.analyze()` 同型**，前端用同一张卡渲染。

    单价从缺的人**不算钱**（应付留空并进 issues），不替他猜、也不按别人的单价顶上去。
    """
    by_person: dict[str, dict] = {}
    units: Counter = Counter()
    issues: list[str] = []
    for r in records:
        person = str(r.get("person") or "").strip() or "（没写名字）"
        units[str(r.get("unit") or "hours")] += 1
        e = by_person.setdefault(person, {
            "name": person, "kind": str(r.get("kind") or "outsourced"),
            "hours": 0.0, "rate_cents": None, "pay_cents": 0, "row_count": 0,
            "projects": [], "rate_conflict": False, "rate_missing": False,
        })
        e["hours"] = round(e["hours"] + float(r.get("qty") or 0), 2)
        e["row_count"] += 1
        proj = str(r.get("project") or "")
        if proj and proj not in e["projects"]:
            e["projects"].append(proj)
        rate = r.get("rate_cents")
        if rate:
            if e["rate_cents"] is None:
                e["rate_cents"] = int(rate)
            elif e["rate_cents"] != int(rate):
                e["rate_conflict"] = True

    total = 0
    for e in by_person.values():
        if e["rate_cents"]:
            e["pay_cents"] = int(round(e["hours"] * e["rate_cents"]))
            total += e["pay_cents"]
        else:
            e["rate_missing"] = True
            issues.append(f"{e['name']}：没有单价，应付算不出（补一句「时薪 X」就行）")

    unit = "points" if units["points"] > units["hours"] else "hours"
    if units["points"] and units["hours"]:
        issues.append("这批里工时和工分混着，已按数量分开显示，口径请核对")
    return {
        "ok": True,
        "unit": unit,
        "source": "chat",
        "kind": "outsourced",
        "mapping_confirmed": True,
        "needs_confirm": [],
        "row_count": len(records),
        "data_rows": len(records),
        "employees": list(by_person.values()),
        "total_pay_cents": total,
        "issues": issues,
    }
