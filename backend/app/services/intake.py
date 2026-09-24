"""统一采集入口：把用户拖进来的东西分流处理，变成待确认草稿

支持四类文件 + 文件夹整批投喂：

  1. 图片 JPG / PNG / WebP / GIF  → 视觉模型识别
  2. PDF                          → 有文本层走本机直读，无文本层走模型
  3. Excel xlsx                   → 报销表：读表 + 提取嵌入图 + 双源对账
  4. CSV                          → 微信 / 支付宝账单归一化
  文件夹整批                      → 第一层目录名即项目名

两条设计原则（重要，别改）：

  A. **Excel 里的数字优先于识别结果**。用户已经人工核对过的表，入账就用他写的数；
     识别嵌入图只用来**核对**（表里 47.13，图里也识别出 47.13 = 双源确认；
     对不上就报出来让人查）。机器不覆盖人写的数。
  B. **一律只生成待确认草稿，绝不直接入账**。对齐合规红线
     「用户确认环节不得为体验而取消」。
"""

from __future__ import annotations

import csv as _csv
import io
import os
import re
import shutil
import sys
from pathlib import Path

from . import categories, pending, projects, report_sheet

REPO_ROOT = Path(__file__).resolve().parents[3]
_ENGINE_MCP = REPO_ROOT / "engine" / "mcp"
_SCRIPTS = REPO_ROOT / "scripts"

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic")
SHEET_EXT = (".xlsx", ".xlsm")
CSV_EXT = (".csv",)
PDF_EXT = (".pdf",)
# .xls（老格式）与 .numbers 不装第三方库读不了，如实拒绝，不静默丢弃
UNSUPPORTED_EXT = (".xls", ".numbers", ".et")


def classify(name: str) -> str:
    """按扩展名判文件类型。"""
    ext = Path(str(name or "")).suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in PDF_EXT:
        return "pdf"
    if ext in SHEET_EXT:
        return "sheet"
    if ext in CSV_EXT:
        return "csv"
    if ext in UNSUPPORTED_EXT:
        return "unsupported"
    return "unknown"


def _load_receipt_mcp():
    """按需加载引擎层的票据识别模块（不 import 后端包，故单独加 sys.path）。"""
    if str(_ENGINE_MCP) not in sys.path:
        sys.path.insert(0, str(_ENGINE_MCP))
    import receipt_mcp  # noqa: PLC0415 按需加载：避免后端启动时依赖引擎层
    return receipt_mcp


def recognize_file(path: str | Path) -> dict:
    """调票据识别。图片与无文本层 PDF 走模型；有文本层 PDF 由 receipt_mcp 内部走本机。"""
    try:
        rc = _load_receipt_mcp()
        return rc.recognize_receipt(str(path))
    except Exception as e:  # noqa: BLE001 识别失败不该中断整批
        return {"error": f"{type(e).__name__}: {str(e)[:160]}"}


def merge_folder(files: list[dict]) -> list[dict]:
    """按相对路径的第一层目录名分组（文件夹整批投喂时的项目归属）。

    没有相对路径或只有一层文件的，归到 `None` 组（不建项目，用当前默认项目）。
    """
    groups: dict[str | None, list[dict]] = {}
    for f in files:
        rel = str(f.get("rel_path") or f.get("name") or "").replace("\\", "/").strip("/")
        parts = [p for p in rel.split("/") if p]
        folder = parts[0] if len(parts) > 1 else None
        groups.setdefault(folder, []).append(f)
    return [{"folder": k, "files": v} for k, v in groups.items()]


def _draft(data_dir, pid: str, *, direction: str, amount_cents: int, category: str,
           note: str, counterparty: str = "", date: str = "", source: str = "",
           image_name: str = "") -> dict | None:
    """建一条待确认草稿。金额为 0 的不建（没金额就不是一笔账）。"""
    if amount_cents <= 0:
        return None
    d = pending.create(data_dir, direction=direction, amount_cents=amount_cents,
                       category=category or categories.UNCONFIRMED_CATEGORY,
                       channel="receipt", note=note, counterparty=counterparty,
                       project=pid)
    extra = {}
    if date:
        extra["date"] = date
    if source:
        extra["source"] = source
    if image_name:
        extra["image"] = image_name
    if extra:
        pending.update(data_dir, d["id"], **extra)
        d.update(extra)
    return d


def _ingest_images(data_dir, pid: str, files: list[dict], work_dir: Path) -> dict:
    """图片：逐张识别 → 草稿，并记录识别明细供人工核对。"""
    drafts, items, errors = [], [], []
    for f in files:
        p = work_dir / f["name"]
        p.write_bytes(f["content"])
        res = recognize_file(p)
        if res.get("error"):
            errors.append({"file": f["name"], "error": res["error"]})
            items.append({"file": f["name"], "ok": False, "error": res["error"]})
            continue
        cents = int(round(float(res.get("amount") or 0) * 100))
        # 分类只看「商户 + 模型给的分类」，**不传 note**。
        # 踩过的坑（2026-09-24 实弹）：PDF 本机回退说明里含 "API HTTP 400"，
        # 把 "api" 当成了经营成本关键词，一张酒店发票被判成「经营」。
        # note 里可能是错误说明或解释文字，不适合参与分类。
        cat = categories.match_category(res.get("merchant", ""), res.get("category", ""))
        d = _draft(data_dir, pid, direction=res.get("type") or "expense",
                   amount_cents=cents, category=cat,
                   note=res.get("note") or f["name"], counterparty=res.get("merchant", ""),
                   date=res.get("date", ""), source="识别", image_name=f["name"])
        if d:
            drafts.append(d)
        items.append({"file": f["name"], "ok": True, "amount_cents": cents,
                      "date": res.get("date", ""), "merchant": res.get("merchant", ""),
                      "category": cat, "invoice_no": res.get("invoice_no", ""),
                      "cloud_uploaded": bool(res.get("_cloud_uploaded")),
                      "confidence": res.get("confidence")})
    return {"drafts": drafts, "items": items, "errors": errors}


def _ingest_pdf(data_dir, pid: str, files: list[dict], work_dir: Path) -> dict:
    """PDF：走同一条识别通道（receipt_mcp 内部对带文本层的数电发票有本机直读路径）。"""
    return _ingest_images(data_dir, pid, files, work_dir)


def _ingest_sheet(data_dir, pid: str, files: list[dict], work_dir: Path,
                  image_dir: Path) -> dict:
    """Excel 报销表：表内数字入账（优先），嵌入图识别做核对（双源）。"""
    drafts, items, errors = [], [], []
    for f in files:
        body = f["content"]
        p = work_dir / f["name"]
        p.write_bytes(body)
        sub = image_dir / Path(f["name"]).stem
        try:
            sheet = report_sheet.analyze(body, image_dir=sub)
        except Exception as e:  # noqa: BLE001
            errors.append({"file": f["name"], "error": f"{type(e).__name__}: {str(e)[:160]}"})
            continue
        if not sheet.get("ok"):
            errors.append({"file": f["name"], "error": sheet.get("error", "解析失败")})
            continue

        # 1) 表内数字 → 草稿（用户已核对过，以他写的为准）
        for row in sheet["rows"]:
            d = _draft(data_dir, pid, direction="expense",
                       amount_cents=row["amount_cents"], category=row["category"],
                       note=(row["note"] or row["category_raw"] or f["name"]),
                       counterparty=row["owner"], date=row["date"],
                       source="报销表", image_name=row["image_name"])
            if d:
                drafts.append(d)

        # 2) 嵌入图识别 → 与表内金额核对（机器不覆盖人写的数）
        checks = []
        for row in sheet["rows"]:
            if not row["image_path"]:
                continue
            res = recognize_file(row["image_path"])
            if res.get("error"):
                checks.append({"image": row["image_name"], "declared_cents": row["amount_cents"],
                               "recognized_cents": None, "match": None,
                               "error": res["error"]})
                continue
            got = int(round(float(res.get("amount") or 0) * 100))
            checks.append({
                "image": row["image_name"],
                "declared_cents": row["amount_cents"],
                "recognized_cents": got,
                "match": got == row["amount_cents"],
                "merchant": res.get("merchant", ""),
                "date": res.get("date", ""),
            })

        matched = sum(1 for c in checks if c.get("match"))
        items.append({
            "file": f["name"],
            "ok": True,
            "kind": "sheet",
            "row_count": sheet["row_count"],
            "embedded_image_count": sheet["embedded_image_count"],
            "category_column_semantics": sheet["category_column_semantics"],
            "declared_total_cents": sheet["declared_total_cents"],
            "sum_amount_cents": sheet["sum_amount_cents"],
            "reconcile": {
                "checked": len(checks),
                "matched": matched,
                "mismatched": [c for c in checks if c.get("match") is False],
                "unreadable": [c for c in checks if c.get("match") is None],
            },
            "checks": checks,
            "needs_confirm": sheet["needs_confirm"],
        })
    return {"drafts": drafts, "items": items, "errors": errors}


def _ingest_csv(data_dir, pid: str, files: list[dict], work_dir: Path) -> dict:
    """CSV 账单：复用 scripts/parse_bill_csv.py 的归一化逻辑（分类/性质/归属同源）。"""
    drafts, items, errors = [], [], []
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    try:
        import parse_bill_csv as pbc  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        return {"drafts": [], "items": [], "errors": [{"file": "-", "error": f"账单解析模块不可用：{e}"}]}

    for f in files:
        p = work_dir / f["name"]
        p.write_bytes(f["content"])
        try:
            records, diag = pbc.parse_file(p)
        except Exception as e:  # noqa: BLE001
            errors.append({"file": f["name"], "error": f"{type(e).__name__}: {str(e)[:160]}"})
            continue
        kept = 0
        for rec in records:
            direction = rec.get("direction") or "expense"
            if direction not in ("income", "expense"):
                continue  # 不计收支的（转账还款、投资理财）不入账
            d = _draft(data_dir, pid, direction=direction,
                       amount_cents=int(rec.get("amount_cents") or 0),
                       category=rec.get("category") or "",
                       note=rec.get("note") or rec.get("description") or "",
                       counterparty=rec.get("counterparty") or "",
                       date=rec.get("date") or "", source="账单")
            if d:
                drafts.append(d)
                kept += 1
        items.append({"file": f["name"], "ok": True, "kind": "csv",
                      "records": len(records), "drafted": kept,
                      "source": diag.get("source") if isinstance(diag, dict) else ""})
    return {"drafts": drafts, "items": items, "errors": errors}


def ingest(data_dir, files: list[dict], work_root: str | Path | None = None,
           project_ref: str | None = None) -> dict:
    """主入口。

    files: [{name, rel_path?, content: bytes}]
    project_ref: 用户显式指定的项目（留空则按文件夹名建项目；再没有就用当前默认项目）
    """
    data_dir = Path(data_dir)
    work_root = Path(work_root or (data_dir / "_intake"))
    shutil.rmtree(work_root, ignore_errors=True)
    work_root.mkdir(parents=True, exist_ok=True)

    batches, all_errors = [], []
    created_projects: list[dict] = []

    for group in merge_folder(files):
        folder = group["folder"]
        # 项目归属：显式指定 > 文件夹名 > 当前默认项目
        hint = ""
        if project_ref:
            pid, hint = projects.resolve_incoming(data_dir, project_ref)
            pname = projects.display_name(projects._load(data_dir), pid)
        elif folder:
            pid, _ = projects.resolve_incoming(data_dir, folder)
            pname = projects.display_name(projects._load(data_dir), pid)
            created_projects.append({"id": pid, "name": pname, "from_folder": folder})
            # 按文件夹名建的项目，导入完要提示改名（David 2026-09-24 定的交互）
            hint = (f"已按文件夹名建项目「{pname}」。要改个更清楚的名称吗？"
                    f"在左侧项目卡片点「改名」即可，账本不受影响。")
        else:
            pid, hint = projects.resolve_incoming(data_dir, None)
            pname = projects.display_name(projects._load(data_dir), pid)

        batch = {
            "source_folder": folder or "",
            "project_id": pid,
            "project_name": pname,
            "project_hint": hint or (
                f"已按文件夹名建项目「{pname}」。要改个更清楚的名称吗？"
                f"在项目卡片点「改名」即可，账本不受影响。" if folder else ""),
            "files": [], "errors": [],
            "draft_count": 0, "identified_total_cents": 0,
            "declared_total_cents": 0, "reconcile": None,
        }

        # 同一批里不同类型分开处理（图片和 PDF 共用识别通道）
        buckets: dict[str, list[dict]] = {}
        for f in group["files"]:
            kind = classify(f.get("name", ""))
            if kind in ("unsupported", "unknown"):
                batch["errors"].append({
                    "file": f.get("name"), "error": f"暂不支持的文件类型（{kind}）"})
                continue
            buckets.setdefault(kind, []).append(f)

        work_dir = work_root / (folder or "_root")
        work_dir.mkdir(parents=True, exist_ok=True)
        image_dir = work_dir / "_images"
        draft_total = 0
        draft_count = 0
        # Excel 那部分的草稿合计，**只用它**与表内「总计」对账。
        # 不能用整批合计去减：一批里可能同时有图片、PDF 和 Excel，
        # 拿整批合计减 Excel 表内总计得到的差额没有意义（实弹踩过）。
        sheet_draft_total = 0

        if "image" in buckets or "pdf" in buckets:
            mixed = buckets.get("image", []) + buckets.get("pdf", [])
            r = _ingest_images(data_dir, pid, mixed, work_dir)
            batch["files"] += r["items"]
            batch["errors"] += r["errors"]
            draft_total += sum(d["amount_cents"] for d in r["drafts"])
            draft_count += len(r["drafts"])
        if "sheet" in buckets:
            r = _ingest_sheet(data_dir, pid, buckets["sheet"], work_dir, image_dir)
            batch["files"] += r["items"]
            batch["errors"] += r["errors"]
            draft_total += sum(d["amount_cents"] for d in r["drafts"])
            draft_count += len(r["drafts"])
            sheet_draft_total = sum(d["amount_cents"] for d in r["drafts"])
            for it in r["items"]:
                if it.get("kind") == "sheet":
                    batch["declared_total_cents"] += it.get("declared_total_cents", 0)
                    batch["reconcile"] = it.get("reconcile")
        if "csv" in buckets:
            r = _ingest_csv(data_dir, pid, buckets["csv"], work_dir)
            batch["files"] += r["items"]
            batch["errors"] += r["errors"]
            draft_total += sum(d["amount_cents"] for d in r["drafts"])
            draft_count += len(r["drafts"])

        batch["draft_count"] = draft_count
        batch["identified_total_cents"] = draft_total
        if batch["declared_total_cents"]:
            # 与表内「总计」对账时，只拿 Excel 那部分的合计比（口径要一致）
            batch["sheet_draft_total_cents"] = sheet_draft_total
            batch["reconcile_diff_cents"] = (
                batch["declared_total_cents"] - sheet_draft_total)
        batches.append(batch)
        all_errors += batch["errors"]

    return {
        "ok": not all_errors or any(b["draft_count"] for b in batches),
        "batches": batches,
        "projects": created_projects,
        "errors": all_errors,
        "draft_total_cents": sum(b["identified_total_cents"] for b in batches),
        "disclaimer": "导入结果一律先落成「待确认草稿」，需你确认后才入账；"
                      "报销表以表内数字为准，识别结果只用于核对。",
    }
