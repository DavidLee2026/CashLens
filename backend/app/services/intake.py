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
import hashlib
import io
import json
import os
import re
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
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


# ─── 嵌入图核对的提速设施（2026-09-28）──────────────────────────
# 起因：一张报表 10 行各嵌一张图，核对是**串行**调模型，实测 18 秒/张 → 整批 3 分钟。
# 而读表本身是 0.00 秒（纯标准库）——慢的从来不是"读取"。
# 两条提速，互不冲突：
#   1) 并发：模型调用是 I/O 等待，并行不占 CPU
#   2) 缓存：图片内容 + 模型指纹都没变，就没必要重算（用户会反复导入同一份表）
_RECOG_WORKERS = 4                 # 并发路数：别开太大，注意模型服务侧的限流
_RECOG_CACHE_FILE = "_recog_cache.json"
_RECOG_CACHE_MAX = 500             # 只留最近这么多条，别让缓存无限长大


def _model_fingerprint() -> str:
    """当前档位 + 型号 + 端点 —— 换模型就让缓存自动失效，不拿旧模型的结果糊弄。"""
    try:
        from . import model_config  # noqa: PLC0415 局部导入：只有走缓存时才需要
        cfg = model_config.active()
        return f"{cfg.get('tier')}|{cfg.get('model')}|{cfg.get('base_url')}"
    except Exception:  # noqa: BLE001 取不到指纹就不缓存（不因为缓存把导入搞挂）
        return ""


def _cache_path(data_dir) -> Path:
    return Path(data_dir) / _RECOG_CACHE_FILE


def _cache_load(data_dir) -> dict:
    p = _cache_path(data_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 缓存坏了就当没有，不影响导入
        return {}


def _cache_save(data_dir, cache: dict) -> None:
    try:
        if len(cache) > _RECOG_CACHE_MAX:
            cache = dict(list(cache.items())[-_RECOG_CACHE_MAX:])
        _cache_path(data_dir).write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001 写不进去也不该让导入失败
        pass


# 只缓存核对用得上的字段：识别结果里那些本机路径、渠道标记不进缓存
_CACHE_FIELDS = ("amount", "date", "merchant", "invoice_no")


def _recognize_cached(img_path: Path, data_dir, cache: dict, fingerprint: str) -> dict:
    """识别一张嵌入图，命中缓存就完全不调模型。"""
    try:
        key = f"{fingerprint}|{hashlib.sha256(img_path.read_bytes()).hexdigest()}"
    except Exception:  # noqa: BLE001 读不到文件就走原路（让 recognize_file 去报错）
        return recognize_file(img_path)
    hit = cache.get(key)
    if isinstance(hit, dict):
        return {**hit, "_from_cache": True}
    res = recognize_file(img_path)
    if not res.get("error") and res.get("amount"):
        cache[key] = {k: res.get(k) for k in _CACHE_FIELDS}
    return res


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


def _merchant_by_category(candidates: list[str], category: str) -> str:
    """用分类反查商户：只在**唯一命中**时采纳，否则留空交人工。

    为什么不能按顺序取名字：文本流里买方与卖方的名字可能挤在同一行，顺序**不等于**
    视觉左右分栏（引擎层已写明这条纪律）。但卖方有一个独立可核的特征 ——
    它的名字**应当与票面项目名目的分类一致**（住宿票的卖方是酒店，餐饮票的是餐厅）。
    所以这里做的是**交叉核对**而不是猜：
      · 候选来自引擎层读到的原文（公司 / 酒店 / 宾馆 / 民宿 / 客栈 / 店 …）
      · 只保留"分类能对上"的；恰好一个才算数
      · 命中 0 个或 ≥2 个 → 返回空串，交人工（与原来一样，不推断）

    实弹（2026-09-28）：一张住宿数电票的候选是「上海简乐文化传播有限公司」（买方，
    分类对不上）与「昆明和美酒店管理有限公司」（卖方，对上「住宿」）→ 唯一命中，采纳卖方。
    """
    if not category or category == categories.UNCONFIRMED_CATEGORY:
        return ""
    hits = [w for w in candidates if categories.match_category(w) == category]
    return hits[0] if len(hits) == 1 else ""


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
        # 分类看「商户 + 模型给的分类 + **票面项目名目**」，**不传 note**。
        # 踩过的坑（2026-09-24 实弹）：PDF 本机回退说明里含 "API HTTP 400"，
        # 把 "api" 当成了经营成本关键词，一张酒店发票被判成「经营」。
        # note 里可能是错误说明或解释文字，不适合参与分类。
        # （2026-09-28 补票面名目）数电票项目名目是「*大类*具体名目」这种**自包含 token**，
        # 不依赖版式分栏，所以本机文本层也拿得到。此前只看 merchant+category，
        # 而这两样在文本层路径上都是空的 → 一张写着「*生产生活服务*住宿费」的
        # 住宿发票被判成「待确认」。**读票面是引擎层的事，判分类是这里的事。**
        # ⚠️ 变量名别叫 items —— 本函数的 items 是"逐文件结果"累加器。
        item_names = res.get("items") or []
        cat = categories.match_category(res.get("merchant", ""), res.get("category", ""), *item_names)
        # 商户：文本层路径下引擎只给候选（买卖方名字顺序不可靠，不能按顺序取），
        # 这里用"分类能否对上"做交叉核对后再定；对不上就留空交人工。
        merchant = res.get("merchant", "") or _merchant_by_category(
            res.get("merchant_candidates") or [], cat)
        note = res.get("note") or f["name"]
        if not merchant and (res.get("merchant_candidates") or []):
            note += "（票面主体名称不唯一，商户请人工核对）"
        d = _draft(data_dir, pid, direction=res.get("type") or "expense",
                   amount_cents=cents, category=cat,
                   note=note, counterparty=merchant,
                   date=res.get("date", ""), source="识别", image_name=f["name"])
        if d:
            drafts.append(d)
        items.append({"file": f["name"], "ok": True, "amount_cents": cents,
                      "date": res.get("date", ""), "merchant": merchant,
                      "category": cat, "invoice_no": res.get("invoice_no", ""),
                      "cloud_uploaded": bool(res.get("_cloud_uploaded")),
                      "confidence": res.get("confidence")})
    return {"drafts": drafts, "items": items, "errors": errors}


def _ingest_pdf(data_dir, pid: str, files: list[dict], work_dir: Path) -> dict:
    """PDF：走同一条识别通道（receipt_mcp 内部对带文本层的数电发票有本机直读路径）。"""
    return _ingest_images(data_dir, pid, files, work_dir)


def _ingest_sheet(data_dir, pid: str, files: list[dict], work_dir: Path,
                  image_dir: Path, reconcile: bool = True, progress=None) -> dict:
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
        #    ⚠️ 这是整条链路最慢的一步（串行时 10 张 ≈ 3 分钟，实测 18 秒/张）。
        #    两处提速：**并发**（I/O 等待，并行不占 CPU）+ **按内容与模型指纹缓存**。
        #    `reconcile=False` 时整步跳过 —— 表内数字本来就是权威，核对只是用来发现
        #    表和票对不上；着急时可以不要（用户 2026-09-28 要求给个逃生门）。
        checks: list[dict] = []
        todo_rows = [r for r in sheet["rows"] if r["image_path"]] if reconcile else []
        if todo_rows:
            cache = _cache_load(data_dir)
            fingerprint = _model_fingerprint()
            results: list[dict] = [{} for _ in todo_rows]
            done = 0
            with ThreadPoolExecutor(max_workers=min(_RECOG_WORKERS, len(todo_rows))) as pool:
                futs = {pool.submit(_recognize_cached, Path(r["image_path"]), data_dir,
                                    cache, fingerprint): i
                        for i, r in enumerate(todo_rows)}
                for fut in as_completed(futs):
                    idx = futs[fut]
                    try:
                        results[idx] = fut.result() or {}
                    except Exception as e:  # noqa: BLE001 单张失败不影响其余
                        results[idx] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
                    done += 1
                    if progress:
                        progress(done, len(todo_rows), "核对表内嵌入图")
            _cache_save(data_dir, cache)
            for row, res in zip(todo_rows, results):
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
                    "from_cache": bool(res.get("_from_cache")),
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
            "reconcile_skipped": not reconcile,
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
           project_ref: str | None = None, reconcile: bool = True,
           progress=None) -> dict:
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
            "declared_total_cents": 0, "reconcile": None, "reconcile_skipped": False,
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
            r = _ingest_sheet(data_dir, pid, buckets["sheet"], work_dir, image_dir,
                             reconcile=reconcile, progress=progress)
            batch["files"] += r["items"]
            batch["errors"] += r["errors"]
            draft_total += sum(d["amount_cents"] for d in r["drafts"])
            draft_count += len(r["drafts"])
            sheet_draft_total = sum(d["amount_cents"] for d in r["drafts"])
            for it in r["items"]:
                if it.get("kind") == "sheet":
                    batch["declared_total_cents"] += it.get("declared_total_cents", 0)
                    batch["reconcile"] = it.get("reconcile")
                    # 只要有一张表跳过了核对，这批的汇总就得说"未核对"
                    batch["reconcile_skipped"] = bool(
                        batch.get("reconcile_skipped") or it.get("reconcile_skipped"))
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
