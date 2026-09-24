"""工时 / 工分表导入与工资参考表汇总

输入：项目工时 Excel（.xlsx）。各项目表格式不统一，列名与列序都可能不同。
输出：列映射建议 + 按人汇总的工资参考表（金额以分计）。

设计约束：
1. 纯标准库。xlsx 本质是 zip + XML，不需要 openpyxl / pandas。
   读取算法与 scripts/parse_bill_csv.py 的 read_xlsx_rows 同源；
   该脚本属开发工具层，后端不反向依赖它，故此处独立实现。
2. 不做推断。列名识别不出来就标 needs_confirm，交用户确认，不猜列。
3. 只出「参考表」，不出「应发工资」。产品帮用户自己看清，不替用户做薪酬决策。
"""
from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

# xlsx 内部 XML 命名空间（Office Open XML）
_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# 列角色与列名关键词（命中即建议该角色，可被调用方显式指定覆盖）
_COLUMN_HINTS: dict[str, tuple[str, ...]] = {
    "name": ("姓名", "员工", "名字", "人员", "负责人", "人名"),
    "hours": ("工时", "工分", "小时", "时长", "出勤", "点数", "数量"),
    "rate": ("单价", "时薪", "费率", "元/时", "元每小时", "工资标准"),
    "project": ("项目", "工程", "任务", "工地", "门店"),
}
COLUMN_ROLES = ("name", "hours", "rate", "project")

# 工分与工时是两种口径：前者是点数，后者是小时
_POINTS_HINTS = ("工分", "点数")


def read_xlsx_rows(source) -> list[list[str]]:
    """读 .xlsx 首个工作表为二维表（纯标准库）。

    source 可以是文件路径、bytes 或二进制流。
    关键坑：xlsx 会省略空单元格，必须用 c/@r 解析出的列号回填，
    否则整行列位会错乱（这是自研 xlsx 读取器最常见的 bug）。
    """
    if isinstance(source, (bytes, bytearray)):
        zf = zipfile.ZipFile(io.BytesIO(source))
    else:
        zf = zipfile.ZipFile(source)

    with zf:
        names = zf.namelist()
        # 共享字符串表（xlsx 把重复文本存这里，单元格只存索引）
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.iter(_XLSX_NS + "si"):
                shared.append("".join(t.text or "" for t in si.iter(_XLSX_NS + "t")))
        # 取第一个工作表
        sheets = sorted(n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        if not sheets:
            return []
        rows: list[list[str]] = []
        for row in ET.fromstring(zf.read(sheets[0])).iter(_XLSX_NS + "row"):
            cells: dict[int, str] = {}
            for c in row.iter(_XLSX_NS + "c"):
                m = re.match(r"[A-Z]+", c.get("r", ""))
                idx = 0
                for ch in (m.group(0) if m else "A"):
                    idx = idx * 26 + (ord(ch) - 64)  # A→1, Z→26, AA→27
                ctype, v = c.get("t"), c.find(_XLSX_NS + "v")
                if ctype == "s" and v is not None:
                    i = int(v.text) if v.text else -1
                    val = shared[i] if 0 <= i < len(shared) else ""
                elif ctype == "inlineStr":
                    ist = c.find(_XLSX_NS + "is")
                    val = ("".join(x.text or "" for x in ist.iter(_XLSX_NS + "t"))
                           if ist is not None else "")
                else:
                    val = v.text if v is not None else ""
                cells[idx - 1] = val
            width = (max(cells) + 1) if cells else 0
            rows.append([cells.get(j, "") for j in range(width)])
        return rows


def find_header_row(rows: list[list[str]], max_scan: int = 10) -> int:
    """在前若干行里找表头行（命中列名关键词最多的那一行）。找不到返回 -1。"""
    best_i, best_hits = -1, 0
    for i, row in enumerate(rows[:max_scan]):
        hits = 0
        for cell in row:
            text = str(cell).strip()
            if any(w in text for w in _all_hint_words()):
                hits += 1
        if hits > best_hits:
            best_i, best_hits = i, hits
    return best_i if best_hits > 0 else -1


def _all_hint_words() -> tuple[str, ...]:
    return tuple(w for words in _COLUMN_HINTS.values() for w in words)


def suggest_mapping(header: list[str]) -> dict[str, int]:
    """按列名关键词建议列映射，返回 {角色: 列序号（0 起）}。

    一列只归一个角色；识别不出的角色不出现在结果里，由调用方标 needs_confirm。
    """
    mapping: dict[str, int] = {}
    for idx, cell in enumerate(header):
        text = str(cell).strip()
        if not text:
            continue
        for role in COLUMN_ROLES:
            if role in mapping:
                continue
            if any(w in text for w in _COLUMN_HINTS[role]):
                mapping[role] = idx
                break
    return mapping


def _cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx < 0 or idx >= len(row):
        return ""
    return str(row[idx]).strip()


def _to_number(text: str) -> float | None:
    """从单元格文本里抽数字（容忍 '40 小时'、'¥30'、'1,200' 这类写法）。"""
    if not text:
        return None
    cleaned = str(text).replace(",", "").replace("，", "")
    m = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    return float(m.group(0)) if m else None


def analyze(source, overrides: dict[str, int] | None = None,
            header_row: int | None = None) -> dict:
    """解析工时表 → 列映射 + 按人汇总的工资参考表。

    overrides 形如 {"name": 0, "hours": 2, "rate": 3}，用于用户确认或纠正列映射。
    """
    rows = read_xlsx_rows(source)
    if not rows:
        return {"ok": False, "error": "工作表为空或不是有效的 xlsx"}

    detected_header = find_header_row(rows)
    if header_row is None:
        header_row = detected_header if detected_header >= 0 else 0
    header = [str(c).strip() for c in rows[header_row]] if rows else []

    mapping = suggest_mapping(header)
    for role, idx in (overrides or {}).items():
        if role in COLUMN_ROLES and idx is not None and int(idx) >= 0:
            mapping[role] = int(idx)

    confirmed = overrides is not None and "name" in overrides and "hours" in overrides
    needs_confirm = [r for r in ("name", "hours") if r not in mapping]
    if "rate" not in mapping:
        needs_confirm.append("rate")
    # 用户已显式确认列映射时，表头行不再是疑问（指定列号本身已隐含数据起始行）
    if detected_header < 0 and not confirmed:
        needs_confirm.append("header_row")

    # 工分还是工时：看表头口径，仅作标注，不影响汇总算法（都是 数量 × 单价）
    unit = "hours"
    for cell in header:
        if any(w in str(cell) for w in _POINTS_HINTS):
            unit = "points"
            break

    data_start = header_row + 1
    people: dict[str, dict] = {}
    issues: list[str] = []

    for row in rows[data_start:]:
        name = _cell(row, mapping.get("name"))
        if not name:
            continue
        if name in header:
            continue  # 重复表头行
        hours = _to_number(_cell(row, mapping.get("hours")))
        rate = _to_number(_cell(row, mapping.get("rate")))
        project = _cell(row, mapping.get("project"))

        rec = people.setdefault(name, {"name": name, "hours": 0.0, "rows": 0,
                                       "rates": [], "projects": []})
        rec["rows"] += 1
        if hours is not None:
            rec["hours"] += hours
        if rate is not None and rate not in rec["rates"]:
            rec["rates"].append(rate)
        if project and project not in rec["projects"]:
            rec["projects"].append(project)
        if hours is None and mapping.get("hours") is not None:
            issues.append(f"{name}：工时/工分列读不出数字，已按 0 计，请核对")

    employees = []
    total_pay_cents = 0
    for rec in people.values():
        rates = rec["rates"]
        rate_yuan = rates[0] if rates else 0.0
        if len(rates) > 1:
            issues.append(f"{rec['name']}：表内出现 {len(rates)} 个不同单价 {rates}，"
                          f"参考值按 {rate_yuan:g} 计算，请确认")
        pay_cents = int(round(rec["hours"] * rate_yuan * 100))
        total_pay_cents += pay_cents
        employees.append({
            "name": rec["name"],
            "hours": round(rec["hours"], 2),
            "rate_cents": int(round(rate_yuan * 100)),
            "pay_cents": pay_cents,
            "row_count": rec["rows"],
            "projects": rec["projects"],
            "rate_conflict": len(rates) > 1,
        })

    employees.sort(key=lambda e: e["pay_cents"], reverse=True)

    return {
        "ok": True,
        "header_row": header_row,
        "columns": header,
        "mapping": mapping,
        "mapping_confirmed": confirmed,
        "needs_confirm": needs_confirm,
        "unit": unit,
        "row_count": len(rows),
        "data_rows": sum(e["row_count"] for e in employees),
        "employees": employees,
        "total_pay_cents": total_pay_cents,
        "issues": issues,
        "disclaimer": "工资参考表由导入的工时数据汇总得出，仅供参考；"
                      "实际发放金额与个税、社保等事项请以正式核算为准。",
    }
