#!/usr/bin/env python3
"""生成「账号台账」Excel：一行一个邀请码账号（2026-10-20 立，当天改过一版口径）

## 这是什么

手工服务期的**反馈台账**。三个账号（你 + 两个朋友）一人一行，把**能自动算的**算出来
（状态 / 创建日 / 最近登录 / 记账笔数 / 最近一笔），把**只能人来判断的**留空给你手填
（反馈 / 卡在哪一步 / 他想要什么 / 我改了什么 / 是否付费 / 备注）。

## 为什么不记金额（2026-10-20 David 拍板，当天就改了）

原话：「这个台账里的金额和我其实没有什么关系，他们赚多赚少和我也没有关系，
我只是把账号给他们用，然后听他们的反馈，对工具进行修改。」

所以收入 / 支出 / 净额三列**整列去掉**，理由有三条：
① 这几个数回答不了任何一个我要回答的问题 —— 我要知道的是「他用没用、卡在哪、他提了什么」；
② 客户自己赚多少是**他的事**，不因为借了个账号就该被算一遍；
③ 少存一份别人的经营数据，就少一份要解释的东西。

留下的「记账笔数」不是金额，是**他有没有真的用**的唯一行为信号。

## 三条纪律

1. **只读**。只读 `tenants.json` 与各租户的 `finance_events.jsonl`，不写、不建、不改
   任何数据文件 —— 台账是观察工具，不是操作工具。
2. **口径与工作台一致**：作废的事件不算；同一租户内**同号发票只算一次**。
3. **不含邀请码**（服务器上只存哈希）。码由 `--codes` 指定的本地文件填，那张表别整份发人。

## 用法

    # 服务器上（真实数据）
    CASH_DATA_DIR=/var/lib/cashlens python3 scripts/make_tenant_ledger.py --out ~/账号台账.xlsx

    # 本机（用拷下来的副本）
    python3 scripts/make_tenant_ledger.py --data-dir /tmp/cl_tenants --out 账号台账.xlsx
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import sys

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services.state_engine.ledger import EventLedger  # noqa: E402

SHEET = "账号台账"
NOTE = "说明"
_HEAD_FILL = PatternFill("solid", fgColor="EEF1F6")
_MANUAL_FILL = PatternFill("solid", fgColor="FFF8E6")   # 手填列：浅黄，一眼能分开

# (表头, 宽度, auto|manual|code) —— code 列由 --codes 文件填（服务器上没有明文码）
HEADERS = [
    ("账号", 16, "auto"),
    ("邀请码", 22, "code"),
    ("状态", 8, "auto"),
    ("创建日", 12, "auto"),
    ("最近登录", 12, "auto"),
    ("记账笔数", 10, "auto"),
    ("最近一笔", 12, "auto"),
    ("反馈 / 用得怎么样", 36, "manual"),
    ("卡在哪一步", 24, "manual"),
    ("他想要什么", 24, "manual"),
    ("我改了什么", 24, "manual"),
    ("是否付费", 11, "manual"),
    ("备注", 22, "manual"),
]

COL = {h: i for i, (h, _, _) in enumerate(HEADERS)}          # 表头 → 列下标（0 基）
MANUAL = [h for h, _, kind in HEADERS if kind == "manual"]


def _load_tenants(data_dir: Path) -> list[dict]:
    p = data_dir / "tenants.json"
    if not p.exists():
        raise SystemExit(f"✗ 找不到 {p}（--data-dir 指到含 tenants.json 的目录）")
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"✗ tenants.json 解析失败：{e}") from e
    return [r for r in rows if isinstance(r, dict) and r.get("slug")]


def _read_events(path: Path) -> list[dict]:
    """读一个租户的账本。

    ⚠️ 必须走 `EventLedger.load()`，不能自己逐行 json.loads —— 那个函数会
    ① 滤掉被 `type=void` 作废的事件、② 按 dedupe_key 只留先写者。
    自己读会把**已作废的账算进笔数**（2026-10-20 实测：一次 void + 重记就会多算一笔）。
    """
    if not path.exists():
        return []
    return EventLedger(path).load()


def _invoice_no(ev: dict) -> str:
    extra = ev.get("extra") if isinstance(ev.get("extra"), dict) else {}
    return str(ev.get("invoice_no") or extra.get("invoice_no") or "").strip()


def _stats(events: list[dict]) -> dict:
    """一个租户的用量概况。口径与工作台一致：作废的不算，同号发票只算一次。"""
    count = 0
    latest = ""
    seen: set[str] = set()
    for ev in events:
        typ = str(ev.get("type") or "")
        ts = str(ev.get("ts") or "")
        if ts > latest:
            latest = ts
        if typ in ("income", "refund"):
            count += 1
        elif typ == "expense":
            no = _invoice_no(ev)
            if no and no in seen:
                continue            # 同号重复票不计入（明细里仍留在账本）
            if no:
                seen.add(no)
            count += 1
    return {"count": count, "latest": latest[:10]}


def _load_manual(out_path: Path) -> dict[str, dict[str, str]]:
    """读上一次生成的台账，把**手填列**按账号名 + 表头名带过来。

    不这么做的话，「重新生成」会把几周的反馈一次抹掉 —— 而台账的价值恰好在手填那几列。
    按**文件里的表头**逐列找（不是按固定下标）：旧文件可能少一列或多一列，
    用下标读老文件会越界（2026-10-20 真机：加列后重生成直接 IndexError）。
    认得出的列名就带过来，认不出的（比如已删掉的金额列）直接跳过。
    """
    if not out_path.exists():
        return {}
    try:
        wb = load_workbook(out_path)
        ws = wb[SHEET] if SHEET in wb.sheetnames else wb.active
    except Exception:  # noqa: BLE001  文件坏了就当没有，别因此生不出新台账
        # ⚠️ 这条宽 except 曾把「未 import load_workbook」的 NameError 也吞掉，
        # 表现为「手填列莫名其妙没保留」——所以读取逻辑必须有测试兜着（见 tests）。
        return {}
    head = [str(c.value or "") for c in ws[1]]
    idx = {k: head.index(k) for k in MANUAL if k in head}
    if not idx:
        return {}
    keep: dict[str, dict[str, str]] = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        name = str(row[0] or "").strip()
        if not name or name == "合计":
            continue
        keep[name] = {k: (str(row[i] or "") if i < len(row) else "") for k, i in idx.items()}
    return keep


def build(data_dir: Path, out_path: Path, codes: dict[str, str] | None = None,
          source_note: str = "") -> Path:
    as_of = datetime.now()
    tenants = _load_tenants(data_dir)
    manual = _load_manual(out_path)
    codes = codes or {}
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET

    ws.append([h for h, _, _ in HEADERS])
    for i, (_, width, kind) in enumerate(HEADERS, 1):
        ws.column_dimensions[get_column_letter(i)].width = width
        cell = ws.cell(row=1, column=i)
        cell.font = Font(bold=True)
        cell.fill = _MANUAL_FILL if kind == "manual" else _HEAD_FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"

    total = 0
    for r in sorted(tenants, key=lambda x: str(x.get("created_at") or "")):
        st = _stats(_read_events(data_dir / "tenants" / str(r["slug"]) / "finance_events.jsonl"))
        label = str(r.get("label") or "（未命名）")
        filled = manual.get(label, {})
        ws.append([
            label,
            codes.get(label) or codes.get(str(r["slug"])) or "",
            "在用" if r.get("active") else "停用",
            str(r.get("created_at") or "")[:10],
            str(r.get("last_seen") or "—")[:10],
            st["count"],
            st["latest"] or "—",
            *[filled.get(k, "") for k in MANUAL],
        ])
        total += st["count"]

    last = ws.max_row + 1
    ws.cell(row=last, column=COL["账号"] + 1, value="合计").font = Font(bold=True)
    ws.cell(row=last, column=COL["记账笔数"] + 1, value=total).font = Font(bold=True)

    # ── 说明页：口径与「哪些列是自动的」写清楚，免得下次自己都分不清 ──
    ws2 = wb.create_sheet(NOTE)
    ws2.column_dimensions["A"].width = 22
    ws2.column_dimensions["B"].width = 100
    lines = [
        ("生成时间", f"{as_of:%Y-%m-%d %H:%M}"),
        ("数据来源", source_note or str(data_dir)),
        ("账号数", f"{len(tenants)} 个（在用 {sum(1 for r in tenants if r.get('active'))}）"),
        ("", ""),
        ("自动列（灰）", "账号 / 邀请码 / 状态 / 创建日 / 最近登录 / 记账笔数 / 最近一笔"),
        ("手填列（黄）", "反馈 / 卡在哪一步 / 他想要什么 / 我改了什么 / 是否付费 / 备注"
                        " —— 重新生成时按账号名 + 表头保留旧值（脚本只写自动列）"),
        ("", ""),
        ("「最近登录」口径", "打点在**用邀请码换会话**那一刻。⚠️ 建码后立刻自己换一次也会打点，"
                            "所以「创建日」与「最近登录」是同一分钟、或同一秒 —— 那是我在试，不是他用过"
                            "（2026-10-20 实际踩到：朋友A 建码 9 秒后有一次打点，差点被当成「他登进来过」）"),
        ("「记账笔数」口径", "真正入账的笔数：作废的不算、同号重复票只算一次。"
                            "**这是「他有没有真的用」的唯一行为信号** —— 肯交一张真票才算数，口头说好用不算"),
        ("为什么不记金额", "2026-10-20 David 拍板：「他们赚多赚少和我没有关系，我只是把账号给他们用，"
                          "然后听他们的反馈」。收入/支出/净额三列已整列去掉 —— "
                          "它们回答不了「他用没用、卡在哪、提了什么」，还多存了一份别人的经营数据"),
        ("邀请码列", "来自 --codes 指定的本地文件（服务器上只存哈希，取不回来）。"
                    "⚠️ 本表因此含**有效凭据**：不要整份发人，只撕自己那行；别放同步盘/公开仓库"),
        ("重生成", "python3 scripts/make_tenant_ledger.py --data-dir <含 tenants.json 的目录> --out <文件名>.xlsx"),
    ]
    for k, v in lines:
        ws2.append([k, v])
    for row in ws2.iter_rows(min_row=1, max_row=ws2.max_row):
        row[0].font = Font(bold=True)
        row[1].alignment = Alignment(wrap_text=True, vertical="top")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser(description="生成邀请码账号台账（xlsx）")
    ap.add_argument("--data-dir", default="", help="含 tenants.json 的目录（默认取 CASH_DATA_DIR 或仓库 data/）")
    ap.add_argument("--out", default="", help="输出文件名（默认 账号台账-YYYYMMDD.xlsx）")
    ap.add_argument("--codes", default="", help='邀请码表（JSON：{"账号名": "CL-XXXX-XXXX-XXXX"}）')
    ap.add_argument("--source-note", default="", help="写在「说明」页的数据来源（默认用目录路径）")
    args = ap.parse_args()

    if args.data_dir:
        data_dir = Path(args.data_dir).expanduser().resolve()
    else:
        import os
        env = os.environ.get("CASH_DATA_DIR")
        data_dir = Path(env).expanduser().resolve() if env else \
            (Path(__file__).resolve().parents[1] / "data")
    out = Path(args.out).expanduser() if args.out else \
        Path.cwd() / f"账号台账-{datetime.now():%Y%m%d}.xlsx"

    codes: dict[str, str] = {}
    if args.codes:
        cp = Path(args.codes).expanduser()
        if cp.exists():
            codes = json.loads(cp.read_text(encoding="utf-8"))
            print(f"✓ 邀请码：读入 {len(codes)} 条（{cp.name}）")
        else:
            print(f"⚠ 找不到邀请码文件 {cp}，邀请码列留空")

    path = build(data_dir, out, codes=codes, source_note=args.source_note)
    print(f"✓ 已生成：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
