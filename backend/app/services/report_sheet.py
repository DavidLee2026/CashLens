"""报销表（xlsx）通道：读表 + 认列 + 提取嵌入图

为什么要它（2026-09-24 实测）：
  只吃图片和 PDF 会**整批漏账**。简乐广告 919 昆明那批材料里，
  10 笔凭证全部嵌在 xlsx 内部（`xl/media/image1..10.jpeg`），
  文件夹里另有的 9 张 jpg 是**另一批图**，两者不重合。
  没有这条通道，表里那 10 笔、¥384.35 一分都进不来。

它比截图更可靠的原因：Excel 里同时有「数字」和「凭证图」，
两者可以互相校验（表里写 47.13，图里识别出 47.13 = 双源确认）。

对外只暴露 `analyze()`：xlsx 二进制 → 结构化行 + 嵌入图 + 表内总计。
"""

from __future__ import annotations

import html
import io
import re
import zipfile
from pathlib import Path

from . import categories
from .timesheet import read_xlsx_rows

# 列名关键词（长的优先匹配，避免「小计」被「计」抢走）
_COLUMN_HINTS: dict[str, tuple[str, ...]] = {
    "owner": ("报销人", "归属", "编号", "人员", "团队", "谁"),
    "date": ("日期", "时间", "发生"),
    "category": ("费用类别", "类别", "科目", "类型", "项目", "费用项"),
    "amount": ("金额", "费用", "价格", "单价"),
    "image": ("截图", "图片", "凭证", "票据", "附件"),
    "note": ("备注", "说明", "事由", "用途", "摘要"),
    "total": ("总计", "合计", "总额"),
}
COLUMN_ROLES = ("owner", "date", "category", "amount", "image", "note", "total")

# Excel 序列号纪元（1900 系统含著名的闰年 bug，故用 1899-12-30 作基准）
_EXCEL_EPOCH_DAYS_OFFSET = 25569  # 1970-01-01 对应的 Excel 序列号
_DISPIMG_RE = re.compile(r'DISPIMG\(\s*"?(ID_[0-9A-Fa-f]+)"?', re.I)
_MONEY_RE = re.compile(r"-?[\d,]+(?:\.\d+)?")


def suggest_mapping(header: list[str]) -> dict[str, int]:
    """按列名关键词建议列映射，返回 {角色: 列序号（0 起）}。一列只归一个角色。"""
    mapping: dict[str, int] = {}
    for idx, cell in enumerate(header):
        text = str(cell or "").strip()
        if not text:
            continue
        best, best_len = "", 0
        for role, words in _COLUMN_HINTS.items():
            for w in words:
                if w in text and len(w) > best_len:
                    best, best_len = role, len(w)
        if best and best not in mapping:
            mapping[best] = idx
    return mapping


def to_date(raw) -> str:
    """单元格值 → YYYY-MM-DD。支持 Excel 序列号、常见字符串格式；认不出返回空串。"""
    s = str(raw or "").strip()
    if not s:
        return ""
    # Excel 序列号（如 46283）
    if re.fullmatch(r"\d{5}(?:\.\d+)?", s):
        import datetime as _dt
        try:
            days = int(float(s))
            d = _dt.date(1970, 1, 1) + _dt.timedelta(days=days - _EXCEL_EPOCH_DAYS_OFFSET)
            return d.isoformat()
        except (ValueError, OverflowError):
            return ""
    m = re.search(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})", s)
    if m:
        y, mo, da = (int(x) for x in m.groups())
        return f"{y:04d}-{mo:02d}-{da:02d}"
    return ""


def to_cents(raw) -> int:
    """单元格值 → 金额（分）。认不出返回 0。"""
    s = str(raw or "").replace("¥", "").replace("￥", "").replace(",", "").strip()
    m = _MONEY_RE.search(s)
    if not m:
        return 0
    try:
        return int(round(float(m.group(0)) * 100))
    except ValueError:
        return 0


def _read_cellimages(zf: zipfile.ZipFile) -> dict[str, str]:
    """DISPIMG 的 ID → 图片文件名（WPS 的嵌入图存在 xl/media 下）。

    WPS 把「=DISPIMG("ID_xxx",1)」这种嵌入图登记在 xl/cellimages.xml，
    图片实体在 xl/media/，两者靠 xl/_rels/cellimages.xml.rels 里的 rId 关联。
    """
    rels_name = "xl/_rels/cellimages.xml.rels"
    ci_name = "xl/cellimages.xml"
    if rels_name not in zf.namelist() or ci_name not in zf.namelist():
        return {}
    rels = zf.read(rels_name).decode("utf-8", "ignore")
    rel_map = dict(re.findall(r'Id="([^"]+)"[^>]*Target="(?:\.\./)?media/([^"]+)"', rels))
    ci = zf.read(ci_name).decode("utf-8", "ignore")
    out: dict[str, str] = {}
    for name, rid in re.findall(r'name="(ID_[0-9A-Fa-f]+)".*?r:embed="([^"]+)"', ci, re.S):
        img = rel_map.get(rid)
        if img:
            out[name] = img
    return out


def extract_embedded_images(source) -> dict[str, bytes]:
    """取出 xlsx 里全部嵌入图，返回 {文件名: bytes}。"""
    zf = (zipfile.ZipFile(io.BytesIO(source)) if isinstance(source, (bytes, bytearray))
          else zipfile.ZipFile(source))
    with zf:
        return {n.split("/")[-1]: zf.read(n)
                for n in zf.namelist()
                if n.startswith("xl/media/") and not n.endswith("/")}


def _dispimg_id(cell_value: str) -> str:
    """从单元格值里抠出 DISPIMG 的图片 ID。"""
    m = _DISPIMG_RE.search(str(cell_value or ""))
    return m.group(1) if m else ""


def analyze(source, image_dir: str | Path | None = None) -> dict:
    """报销表 xlsx → 结构化结果。

    image_dir 给了就把嵌入图落盘（识别通道要文件路径），并回填每行的图片路径。
    """
    try:
        rows = read_xlsx_rows(source)
    except Exception as e:  # noqa: BLE001 不是 zip / 损坏的 xlsx 一律如实报错
        return {"ok": False, "error": f"不是有效的 xlsx：{type(e).__name__}"}
    if not rows:
        return {"ok": False, "error": "工作表为空或不是有效的 xlsx"}

    # 找表头行：命中列名关键词最多的那一行
    header_i, best_hits = -1, 0
    for i, row in enumerate(rows[:10]):
        hits = sum(1 for cell in row
                   if any(w in str(cell or "") for words in _COLUMN_HINTS.values() for w in words))
        if hits > best_hits:
            header_i, best_hits = i, hits
    if header_i < 0:
        return {"ok": False, "error": "认不出表头行：前 10 行里没有任何已知列名"}

    header = [str(c or "").strip() for c in rows[header_i]]
    mapping = suggest_mapping(header)
    data_rows = [r for r in rows[header_i + 1:] if any(str(c or "").strip() for c in r)]

    id2img = _read_cellimages(
        zipfile.ZipFile(io.BytesIO(source)) if isinstance(source, (bytes, bytearray))
        else zipfile.ZipFile(source))
    images = extract_embedded_images(source)

    if image_dir:
        d = Path(image_dir)
        d.mkdir(parents=True, exist_ok=True)
        for name, blob in images.items():
            (d / name).write_bytes(blob)

    def cell(row: list[str], role: str) -> str:
        i = mapping.get(role)
        return str(row[i]).strip() if i is not None and i < len(row) else ""

    # 「项目」列到底是费用类别还是真项目？看值：能匹配已知分类的多 → 是费用类别
    cat_i = mapping.get("category")
    cat_values = [str(r[cat_i]).strip() for r in data_rows
                  if cat_i is not None and cat_i < len(r) and str(r[cat_i]).strip()]
    matched = sum(1 for v in cat_values
                  if categories.match_category(v) != categories.UNCONFIRMED_CATEGORY)
    cat_semantics = ("expense_category"
                     if cat_values and matched * 2 >= len(cat_values) else "project")

    out_rows: list[dict] = []
    for r in data_rows:
        raw_cat = cell(r, "category")
        if cat_semantics == "expense_category":
            category = categories.match_category(raw_cat, cell(r, "note"))
            project_hint = ""
        else:
            category = categories.match_category(cell(r, "note"))
            project_hint = raw_cat
        img_id = _dispimg_id(cell(r, "image"))
        img_name = id2img.get(img_id, "")
        out_rows.append({
            "owner": cell(r, "owner"),
            "date": to_date(cell(r, "date")),
            "category_raw": raw_cat,
            "category": category,
            "amount_cents": to_cents(cell(r, "amount")),
            "note": cell(r, "note"),
            "project_hint": project_hint,
            "image_id": img_id,
            "image_name": img_name,
            "image_path": str(Path(image_dir) / img_name) if (image_dir and img_name) else "",
        })

    totals_cents = [to_cents(cell(r, "total")) for r in data_rows]
    totals_cents = [t for t in totals_cents if t > 0]

    return {
        "ok": True,
        "header_row": header_i,
        "columns": header,
        "mapping": mapping,
        "category_column_semantics": cat_semantics,
        "row_count": len(out_rows),
        "rows": out_rows,
        "embedded_image_count": len(images),
        "embedded_image_names": sorted(images.keys()),
        "rows_missing_image": [i for i, r in enumerate(out_rows) if not r["image_name"]],
        "declared_total_cents": max(totals_cents) if totals_cents else 0,
        "sum_amount_cents": sum(r["amount_cents"] for r in out_rows),
        "needs_confirm": [role for role in ("amount", "date") if role not in mapping],
        "disclaimer": "报销表解析为纯本机处理，无网络调用；表内数字与嵌入图仅作归集与核对，"
                      "不构成记账或税务结论。",
    }
