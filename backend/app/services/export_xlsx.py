"""导出账目为 Excel（2026-10-19 立）

## 为什么要它

戚老师要用它**对账**——对账的交付物是一张能打开、能加批注、能发给财务的表，
而不是网页上一屏字。此前 27 个端点里**一个导出都没有**，账只能看不能带走。

## 三条硬约束

1. **数字口径必须与界面一致**：直接复用 `projects.summary()`（同一套项目归集与
   发票去重），不另算一套 —— 否则「界面 9,019、导出 9,219」这种差异会毁掉信任。
2. **金额写成数字，不写成字符串**：客户要在 Excel 里自己求和、排序、透视。
   单元格格式 `#,##0.00`，值＝元（不是分）且四舍五入到两位。
3. **重复票要看得见、但不重复计**：同项目内同号发票只计一次（沿用 summary 口径），
   被剔除的那几行在明细里标「重复·未计入合计」，汇总里单列一行说明 ——
   不许悄悄删掉，否则客户数不出来就会怀疑整张表。
"""
from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from . import projects as projects_svc

SHEET_ROWS = "账目明细"
SHEET_SUM = "汇总"

_HEAD_FILL = PatternFill("solid", fgColor="EEF1F6")
_MONEY_FMT = "#,##0.00"
_KIND_CN = {"income": "收入", "refund": "退款", "expense": "支出"}
_CHANNEL_CN = {"wechat": "微信", "alipay": "支付宝", "bank": "银行卡",
               "cash": "现金", "manual": "手动", "voice": "语音", "receipt": "票据"}


def _yuan(cents: int | None) -> float:
    return round(int(cents or 0) / 100.0, 2)


def _safe_name(s: str) -> str:
    """文件名里不能有路径分隔符与引号。"""
    return re.sub(r'[\\/:*?"<>|]+', "", (s or "").strip())[:60] or "全部项目"


def _invoice_no(ev: dict) -> str:
    extra = ev.get("extra") if isinstance(ev.get("extra"), dict) else {}
    return str(ev.get("invoice_no") or extra.get("invoice_no") or "").strip()


def _style_header(ws, widths: list[int]) -> None:
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = _HEAD_FILL
        c.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"


def build_workbook(data_dir: str | Path, events: list[dict], project: str = "") -> tuple[bytes, str]:
    """生成 xlsx，返回 (内容, 建议文件名)。project 为空＝全部项目。"""
    data_dir = Path(data_dir)
    sm = projects_svc.summary(data_dir, events, project=project or None)
    if not sm.get("ok", True):
        raise ValueError(str(sm.get("error") or "项目不存在"))

    rows = {str(r.get("id")): r for r in projects_svc.visible_rows(data_dir)}
    dup_ids = {str(d.get("event_id")): d for d in
               [x for g in sm.get("projects", []) for x in g.get("duplicates", [])]}

    target = str(project or "").strip()
    target_id = None
    if target:
        target_id = (projects_svc.resolve(data_dir, target)
                     or (projects_svc.UNASSIGNED_PROJECT if target in
                         (projects_svc.UNASSIGNED_LABEL, "unassigned", "-") else None))

    # ── 明细页 ──────────────────────────────────────────
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_ROWS
    headers = ["日期", "项目", "收支", "分类", "商户 / 对手方", "金额(元)",
               "来源", "备注", "发票号", "计入合计"]
    ws.append(headers)
    _style_header(ws, [12, 16, 6, 10, 20, 12, 8, 32, 22, 12])

    kept = 0
    for ev in sorted(events or [], key=lambda e: str(e.get("ts") or "")):
        pid = str(ev.get("project") or "")
        if target_id is not None and pid != target_id:
            continue
        if str(ev.get("type")) not in ("income", "expense", "refund"):
            continue
        name = str((rows.get(pid) or {}).get("name") or "")
        if not name:
            name = projects_svc.UNASSIGNED_LABEL
        dup = dup_ids.get(str(ev.get("event_id") or ""))
        ws.append([
            str(ev.get("ts") or "")[:10],
            name,
            _KIND_CN.get(str(ev.get("type")), str(ev.get("type"))),
            str(ev.get("category") or "") or "未分类",
            str(ev.get("counterparty") or ""),
            _yuan(ev.get("amount_cents")),
            _CHANNEL_CN.get(str(ev.get("channel") or ""), str(ev.get("channel") or "")),
            str(ev.get("note") or ""),
            _invoice_no(ev),
            "重复·未计入" if dup else "计入",
        ])
        if dup:
            ws.cell(row=ws.max_row, column=6).font = Font(color="B26B00")
        else:
            kept += 1
    for r in range(2, ws.max_row + 1):
        ws.cell(row=r, column=6).number_format = _MONEY_FMT

    # ── 汇总页 ──────────────────────────────────────────
    ws2 = wb.create_sheet(SHEET_SUM)
    ws2.append(["项目", "收入(元)", "支出(元)", "净额(元)", "笔数", "备注"])
    _style_header(ws2, [20, 14, 14, 14, 8, 40])

    total_income = total_expense = total_count = total_dup = 0
    for g in sm.get("projects", []):
        dup_n = len(g.get("duplicates", []))
        ws2.append([
            g.get("name") or projects_svc.UNASSIGNED_LABEL,
            _yuan(g.get("income_cents")),
            _yuan(g.get("expense_cents")),
            _yuan(g.get("net_cents")),
            int(g.get("income_count", 0)) + int(g.get("expense_count", 0)),
            f"含 {dup_n} 笔同号重复发票（已从合计剔除）" if dup_n else "",
        ])
        total_income += int(g.get("income_cents", 0))
        total_expense += int(g.get("expense_cents", 0))
        total_count += int(g.get("income_count", 0)) + int(g.get("expense_count", 0))
        total_dup += dup_n

    for r in range(2, ws2.max_row + 1):
        for col in (2, 3, 4):
            ws2.cell(row=r, column=col).number_format = _MONEY_FMT
    ws2.append(["合计", _yuan(total_income), _yuan(total_expense),
                _yuan(total_income - total_expense), total_count, ""])
    for c in ws2[ws2.max_row]:
        c.font = Font(bold=True)
    for col in (2, 3, 4):
        ws2.cell(row=ws2.max_row, column=col).number_format = _MONEY_FMT

    # 分类小计（客户最常问的第二件事：钱花在哪类上）
    ws2.append([])
    ws2.append(["支出分类", "金额(元)", "笔数"])
    for c in ws2[ws2.max_row]:
        c.font = Font(bold=True)
        c.fill = _HEAD_FILL
    merged: dict[str, dict] = {}
    for g in sm.get("projects", []):
        for c in g.get("by_category", []):
            k = str(c.get("category") or "未分类")
            m = merged.setdefault(k, {"cents": 0, "count": 0})
            m["cents"] += int(c.get("amount_cents", 0))
            m["count"] += int(c.get("count", 0))
    for k, v in sorted(merged.items(), key=lambda kv: -kv[1]["cents"]):
        ws2.append([k, _yuan(v["cents"]), v["count"]])
    for r in range(ws2.max_row - len(merged), ws2.max_row + 1):
        if r > 0:
            ws2.cell(row=r, column=2).number_format = _MONEY_FMT

    ws2.append([])
    note = ws2.cell(row=ws2.max_row + 1, column=1,
                    value=(f"导出时间：{datetime.now():%Y-%m-%d %H:%M}　"
                           f"口径：与工作台一致（同项目内同号发票只计一次）"
                           + (f"；本表含 {total_dup} 笔重复票，已在明细里标注" if total_dup else "")
                           + f"；明细共 {kept} 笔计入合计"))
    note.font = Font(color="5A6B82", size=9)

    buf = io.BytesIO()
    wb.save(buf)
    scope = "全部项目" if not target else (str(sm["projects"][0]["name"]) if sm.get("projects")
                                           else target)
    filename = f"CashLens-账目-{_safe_name(scope)}-{datetime.now():%Y%m%d}.xlsx"
    return buf.getvalue(), filename
