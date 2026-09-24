#!/usr/bin/env python3
"""账单解析器：支付宝 / 微信导出账单（CSV / xlsx）→ CashLens 统一账本草稿

用途（家庭场景首个真实数据入口）：
  把支付宝/微信导出的账单 CSV 解析成统一账本草稿，并回答「这个月各渠道一共花了多少」。
  产出供后续现金流预测与状态引擎使用。

隐私铁律（本项目强制，见 09-代码/.gitignore）：
  1. 本脚本**纯本地运行，无任何网络调用**（不 import requests / 不调任何 API）
  2. 真实账单请放 user-data/（已被 .gitignore 忽略，绝不入库）
  3. 摘要默认对「交易对方」完全脱敏——只统计笔数与金额，不出现名称
  4. 需要看对手方时显式加 --show-counterparty，且产物只留本地

输出结构沿用 backend/app/services/parser_rule.py 的约定：
  金额用「分」(amount_cents)、direction ∈ income/expense/neutral、
  channel ∈ alipay/wechat/..., category 为中文分类名 —— 保证对话入口与账单入口同构。

用法：
  # 1. 先诊断格式（第一次拿到真实账单时建议先跑这个）
  python3 parse_bill_csv.py ../user-data/支付宝账单.csv --diagnose

  # 2. 正式解析（可一次传多个文件，自动合并）
  python3 parse_bill_csv.py ../user-data/*.csv --out ../data/bill_out

  # 微信「用于个人对账」导出的是 .xlsx（不是 CSV），直接支持，无需转换
  python3 parse_bill_csv.py ../user-data/微信支付账单流水文件.xlsx

  # 3. 需要看对手方（默认脱敏）
  python3 parse_bill_csv.py ../user-data/微信账单.csv --show-counterparty
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

# ── 编码候选（按命中优先级；支付宝导出通常是 gb18030）──
_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "big5")

# ── 列名模糊匹配：内部字段 → 关键词（列名含任一关键词即命中）──
_COL_RULES = (
    ("time", ("交易时间", "交易日期", "记账时间", "时间")),
    ("counterparty", ("交易对方", "对方")),
    ("direction", ("收/支", "收支")),
    ("amount", ("金额",)),
    ("bill_category", ("交易分类", "交易类型")),
    ("description", ("商品说明", "商品", "备注")),
    ("status", ("交易状态", "当前状态")),
    ("pay_method", ("收/付款方式", "支付方式", "付款方式")),
    ("order_id", ("交易订单号", "交易单号", "订单号")),
)

# ═══════════════════════════════════════════════════════════════
# 跨渠道分类归一化（纯规则，零模型调用）
#
# 要解决的问题（真实数据实测）：
#   **两家平台都不给可用的消费分类。**
#     微信「交易类型」= 通道类型（商户消费 / 扫码 / 转账 —— 这是**怎么付的**）
#     支付宝「交易分类」= **该列为空**（实测 55/55 笔为空，见下 _PLATFORM_MAP 注释）
#   没有一个字段回答「钱花在哪了」。直接拼接只会得到
#   「商户消费 29.6% + 其他 45.7%」这种无法分析的表。
#
# 三级归一化 → 统一分类：
#   一级 平台分类映射（质量最高）：支付宝「餐饮美食」→「餐饮」
#   二级 关键词下沉（微信没有类别信息时）：靠「商户名 + 商品说明」判
#   三级 兜底「待确认」（不再叫「其他」—— 它是要人来确认的池子，不是一个类别）
#
# 同时标注两个新维度（见 02-脑暴/支出三层结构-类别性质归属-v0.1.md）：
#   性质 nature：rigid 刚性（不可压缩）/ flexible 灵活（可压缩）
#   归属 attribution：business 经营 / personal 个人
# ═══════════════════════════════════════════════════════════════

# 统一分类体系（支出侧）
_CATEGORIES = ("餐饮", "交通", "购物", "居住", "教育", "医疗", "娱乐", "通讯",
               "人情", "经营", "社保税费", "待确认", "不计收支")

# ── 一级：平台分类精确映射（值是 None 表示「该值不含类别信息，需下沉」）──
# ⚠️ 实测（2026-09-13）：支付宝「账户余额 → 下载查询结果」导出的 CSV 里，
#    「交易分类」列**全部为空**（55/55 笔）—— 该导出方式不提供分类。
#    故支付宝分支的映射当前**不会命中**，保留作兼容（换导出方式若带分类即可生效）。
#    微信「交易类型」列有值，但那是通道类型，不是消费分类。
_PLATFORM_MAP = {
    # 支付宝「交易分类」
    "餐饮美食": "餐饮",
    "交通出行": "交通",
    "日用百货": "购物", "服饰装扮": "购物", "数码电器": "购物",
    "美容美发": "购物", "母婴亲子": "购物", "运动户外": "购物",
    "住房物业": "居住", "生活服务": "居住",
    "教育培训": "教育",
    "医疗健康": "医疗",
    "文化休闲": "娱乐",
    "通讯物流": "通讯",
    "转账红包": "人情", "亲友代付": "人情",
    "公共缴费": "社保税费",
    "投资理财": None, "信用借还": None, "保险": None,   # → direction 判 neutral
    # 微信「交易类型」（前四项是通道类型，无类别信息 → 下沉）
    "商户消费": None, "扫二维码付款": None, "二维码收款": None, "商户消费-退款": None,
    "微信红包（单发）": "人情", "微信红包（群发）": "人情", "微信红包-退款": None,
    "转账": "人情", "转账-退款": None, "群收款": "人情",
}

# ── 二级：关键词下沉规则（顺序敏感：特定的放前面，避免被宽泛规则抢走）──
_CATEGORY_RULES = (
    ("社保税费", ("社保", "税务局", "税务", "国税", "地税", "完税", "公积金", "医保缴费")),
    ("经营", ("deepseek", "openai", "api", "阿里云", "腾讯云", "服务器", "域名",
              "github", "figma", "adobe", "notion", "vercel", "saas", "云服务",
              "开发者", "素材", "字体", "插件", "商标", "注册费")),
    ("交通", ("打车", "滴滴", "地铁", "公交", "加油", "高铁", "机票", "停车", "单车",
              "铁路", "航空", "地图", "出行", "快车", "专车", "泊车")),
    ("餐饮", ("餐", "吃", "外卖", "咖啡", "奶茶", "饭店", "早餐", "午餐", "晚餐",
              "美团", "饿了么", "肯德基", "麦当劳", "星巴克", "瑞幸", "面馆", "小吃", "烘焙")),
    ("购物", ("超市", "购物", "淘", "拼多多", "京东", "唯品会", "服饰", "日用", "百货",
              "商场", "便利店", "711", "7-11", "全家", "罗森", "便利蜂", "美宜佳", "苏宁")),
    ("居住", ("房租", "租金", "物业", "水费", "电费", "燃气", "宽带")),
    ("教育", ("教育", "网课", "培训", "学费", "书籍", "图书", "考试", "考试院")),
    ("医疗", ("医院", "药", "诊所", "体检", "医疗")),
    ("娱乐", ("电影", "游戏", "视频", "音乐", "ktv", "旅游", "剪映", "会员")),
    ("通讯", ("话费", "流量", "通讯", "移动", "联通", "电信")),
    ("人情", ("红包", "转账", "礼", "人情")),
)

# ── 性质：刚性支出词表（不可压缩 / 有法定义务 / 按月固定）──
_RIGID_WORDS = ("社保", "税务局", "税务", "国税", "地税", "完税", "公积金",
                "房租", "租金", "物业", "电费", "水费", "燃气", "宽带",
                "话费", "保险", "还款", "贷款", "月租", "年检")

# ── 归属：经营成本词表（与经营直接相关、可能取得票据）──
_BUSINESS_WORDS = ("deepseek", "openai", "api", "阿里云", "腾讯云", "服务器", "域名",
                   "github", "figma", "adobe", "notion", "vercel", "saas", "云服务",
                   "开发者", "素材", "字体", "插件", "商标", "注册费", "办公")

# 「不计收支」类值：这些不是收入也不是支出，纳入统计会虚高
_NEUTRAL_VALUES = ("不计收支", "/", "-", "", "中性")

_DATE_RE = re.compile(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})")
# 紧凑数字日期：微信「用于个人对账」的 xlsx 把时间存成数字单元格，
# 值形如 20260813123000.0（YYYYMMDDHHMMSS），不含任何分隔符
_COMPACT_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(?:(\d{2})(\d{2})(\d{2}))?")
_AMOUNT_CLEAN_RE = re.compile(r"[¥￥,\s\"']")


# ═══════════════════════════════════════════════════════════════
# 读取与结构探测
# ═══════════════════════════════════════════════════════════════

def read_text(path: Path) -> tuple[str, str]:
    """按候选编码依次尝试解码，返回 (文本, 命中的编码名)。"""
    raw = path.read_bytes()
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    # 全部失败：用 gb18030 + 替换，保证不中断
    return raw.decode("gb18030", errors="replace"), "gb18030(replace)"


# xlsx 内部 XML 命名空间（Office Open XML）
_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def read_xlsx_rows(path: Path) -> list[list[str]]:
    """读 .xlsx（微信「用于个人对账」导出即此格式）。

    纯标准库实现：xlsx 本质是 zip + XML，不需要 openpyxl / pandas。
    关键坑：xlsx 会**省略空单元格**，必须用 c/@r 解析出的列号回填，
    否则整行列位会错乱（这是自研 xlsx 读取器最常见的 bug）。
    """
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        # 共享字符串表（xlsx 把重复文本存这里，单元格只存索引）
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.iter(_XLSX_NS + "si"):
                shared.append("".join(t.text or "" for t in si.iter(_XLSX_NS + "t")))
        # 取第一个工作表
        sheets = sorted(n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        if not sheets:
            return []
        rows: list[list[str]] = []
        for row in ET.fromstring(z.read(sheets[0])).iter(_XLSX_NS + "row"):
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
                    val = "".join(x.text or "" for x in ist.iter(_XLSX_NS + "t")) if ist is not None else ""
                else:
                    val = v.text if v is not None else ""
                cells[idx - 1] = val
            width = (max(cells) + 1) if cells else 0
            rows.append([cells.get(j, "") for j in range(width)])
        return rows


def read_rows(path: Path) -> tuple[list[list[str]], str]:
    """读成二维表，返回 (行列表, 编码名或格式名)。

    分派：.xlsx → 内置 XML 解析（微信「用于个人对账」导出格式）；
          其余   → 按编码探测读文本 CSV（支付宝导出格式，通常 gb18030）。
    """
    if path.suffix.lower() == ".xlsx":
        return read_xlsx_rows(path), "xlsx(内置XML)"
    text, enc = read_text(path)
    rows = [r for r in csv.reader(text.splitlines())]
    return rows, enc


def score_header(row: list[str]) -> int:
    """给一行打分：命中多少个已知列名关键词。"""
    score = 0
    for keywords in (k for _, k in _COL_RULES):
        if any(any(kw in (cell or "") for kw in keywords) for cell in row):
            score += 1
    return score


def find_header(rows: list[list[str]], scan: int = 40) -> int:
    """定位表头行（真实导出前若干行是账号/日期范围说明，末尾是统计页脚）。"""
    best_idx, best_score = -1, 0
    for i, row in enumerate(rows[:scan]):
        s = score_header(row)
        if s > best_score:
            best_idx, best_score = i, s
    # 至少命中 4 个已知列才认账，否则视为没找到
    return best_idx if best_score >= 4 else -1


def map_columns(header: list[str]) -> dict[str, int]:
    """列名 → 下标；每个内部字段只占一列（先到先得）。"""
    mapping: dict[str, int] = {}
    used: set[int] = set()
    for field, keywords in _COL_RULES:
        for idx, cell in enumerate(header):
            if idx in used:
                continue
            if any(kw in (cell or "") for kw in keywords):
                mapping[field] = idx
                used.add(idx)
                break
    return mapping


def detect_source(header: list[str], filename: str) -> str:
    """判断账单来源：alipay / wechat / unknown。"""
    joined = " ".join(header)
    name = filename.lower()
    if "对方账号" in joined or "交易分类" in joined or "alipay" in name or "支付宝" in filename:
        return "alipay"
    if "当前状态" in joined or "金额(元)" in joined or "商户单号" in joined or "微信" in filename:
        return "wechat"
    return "unknown"


# ═══════════════════════════════════════════════════════════════
# 字段解析
# ═══════════════════════════════════════════════════════════════

def cell(row: list[str], mapping: dict[str, int], field: str) -> str:
    """安全取单元格（列缺失或行短时返回空串）。"""
    idx = mapping.get(field)
    if idx is None or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


def parse_amount_cents(raw: str) -> int:
    """金额 → 分。处理 ¥、千分位、引号、正负号；解析失败返回 0。"""
    if not raw:
        return 0
    s = _AMOUNT_CLEAN_RE.sub("", raw)
    negative = s.startswith("-")
    s = s.lstrip("+-")
    try:
        return int(round(float(s) * 100)) * (-1 if negative else 1)
    except ValueError:
        return 0


def parse_direction(raw: str, amount_cents: int) -> str:
    """方向判定：优先看收/支列，缺失时退化为金额正负。"""
    v = (raw or "").strip()
    if v in _NEUTRAL_VALUES:
        return "neutral"
    if "不计" in v or v == "/":
        return "neutral"
    if "收入" in v or v == "收":
        return "income"
    if "支出" in v or v == "支":
        return "expense"
    if amount_cents > 0:
        return "income"
    if amount_cents < 0:
        return "expense"
    return "neutral"


def normalize_date(raw: str) -> str:
    """统一时间表示 → 'YYYY-MM-DD HH:MM:SS'（或 'YYYY-MM-DD'）。

    为什么要做这层：不同导出格式给的时间**类型完全不同**，本项目实测踩过两次坑——
      1. 微信「用于个人对账」的 xlsx，时间列是 **Excel 日期序列号**（数字单元格），
         值形如 46276.49626157407 —— 直接拿 'YYYY-MM-DD' 正则匹配会全部失配，
         结果每一行都被误判成说明行，整份账单解析为 0 笔；
      2. 部分导出用紧凑数字 20260813123000（无分隔符）。
    """
    s = (raw or "").strip()
    # 分支一：紧凑数字 YYYYMMDD[HHMMSS]
    m = _COMPACT_DATE_RE.match(s)
    if m and len(s) >= 8:
        y, mo, d, hh, mi, ss = m.groups()
        if 1900 <= int(y) <= 2100 and 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
            head = f"{y}-{mo}-{d}"
            return f"{head} {hh}:{mi}:{ss}" if (hh and mi and ss) else head
    # 分支二：Excel 日期序列号（基准 1899-12-30，该基准已吸收 Excel 的 1900 闰年 bug）
    try:
        serial = float(s)
        if 25000 <= serial <= 80000:  # 约 1968 ~ 2119 年
            return (datetime(1899, 12, 30) + timedelta(days=serial)).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
    return s


def parse_month(raw: str) -> str:
    """从时间串提取 YYYY-MM；失败返回空串（该行不计入月度统计）。"""
    m = _DATE_RE.search(normalize_date(raw) or "")
    if not m:
        return ""
    year, month = m.group(1), int(m.group(2))
    if not 1 <= month <= 12:
        return ""
    return f"{year}-{month:02d}"


def normalize_record(bill_category: str, counterparty: str, description: str,
                     direction: str) -> dict:
    """跨渠道归一化 → {category, nature, attribution, norm_source}。

    norm_source 记录判定来源，便于质量追溯：
      platform = 平台给了真分类（最可信）
      keyword  = 靠「商户名 + 商品说明」关键词判
      fallback = 落「待确认」，等用户一句话（不叫「其他」——那是池子，不是类别）
    """
    bc = (bill_category or "").strip()
    # 关键词检索文本 = 商品说明 + 商户名
    # 商户名很关键：微信的「商户消费」不含任何类别信息，只能靠商户名判
    text = f"{description or ''} {counterparty or ''}"

    # 一级：平台分类精确映射
    category, norm_source = None, "fallback"
    if bc in _PLATFORM_MAP:
        category = _PLATFORM_MAP[bc]
        if category:
            norm_source = "platform"

    # 二级：关键词下沉
    if not category:
        low = text.lower()
        for name, words in _CATEGORY_RULES:
            if any(w.lower() in low for w in words):
                category, norm_source = name, "keyword"
                break

    # 三级：兜底
    if not category:
        category = "不计收支" if direction == "neutral" else "待确认"

    nature = "rigid" if any(w in text for w in _RIGID_WORDS) else "flexible"
    attribution = ("business" if any(w.lower() in text.lower() for w in _BUSINESS_WORDS)
                   else "personal")
    return {"category": category, "nature": nature,
            "attribution": attribution, "norm_source": norm_source}


def parse_file(path: Path) -> tuple[list[dict], dict]:
    """解析单个账单文件，返回 (记录列表, 诊断信息)。"""
    rows, enc = read_rows(path)
    header_idx = find_header(rows)
    diag = {
        "file": path.name,
        "encoding": enc,
        "total_rows": len(rows),
        "header_index": header_idx,
        "source": "unknown",
        "columns": {},
        "skipped_rows": 0,
        "header_raw": [],
    }
    if header_idx < 0:
        diag["header_raw"] = rows[:10]
        return [], diag

    header = rows[header_idx]
    mapping = map_columns(header)
    source = detect_source(header, path.name)
    diag["source"] = source
    diag["columns"] = {f: header[i] for f, i in mapping.items()}
    diag["header_raw"] = [header]

    records: list[dict] = []
    for row in rows[header_idx + 1:]:
        if not any((c or "").strip() for c in row):
            continue
        date_raw = cell(row, mapping, "time")
        date_norm = normalize_date(date_raw)
        month = parse_month(date_norm)
        if not month:
            diag["skipped_rows"] += 1  # 说明行 / 页脚 / 空行
            continue
        cents = abs(parse_amount_cents(cell(row, mapping, "amount")))
        direction = parse_direction(cell(row, mapping, "direction"), parse_amount_cents(cell(row, mapping, "amount")))
        desc = cell(row, mapping, "description")
        records.append({
            "date": date_norm,
            "month": month,
            "amount_cents": cents,
            "direction": direction,
            "channel": source,
            **normalize_record(cell(row, mapping, "bill_category"),
                               cell(row, mapping, "counterparty"), desc, direction),
            "counterparty": cell(row, mapping, "counterparty"),
            "description": desc,
            "status": cell(row, mapping, "status"),
            "pay_method": cell(row, mapping, "pay_method"),
            "order_id": cell(row, mapping, "order_id"),
            "source_file": path.name,
        })
    return records, diag


# ═══════════════════════════════════════════════════════════════
# 输出
# ═══════════════════════════════════════════════════════════════

def yuan(cents: int) -> str:
    return f"{cents / 100:,.2f}"


def write_ledger(records: list[dict], outdir: Path) -> Path:
    """结构化账本草稿（本地文件，含对手方；勿提交）。"""
    path = outdir / "ledger.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def write_summary(records: list[dict], diags: list[dict], outdir: Path,
                  show_counterparty: bool) -> Path:
    """脱敏摘要（这一份可以贴给人看 / 进对话）。"""
    path = outdir / "summary.md"
    lines: list[str] = ["# 账单解析摘要", ""]
    lines.append(f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M} ｜ 全部本地计算，无网络调用")
    lines.append("")

    # ── 解析概况 ──
    lines.append("## 一、解析概况")
    lines.append("")
    lines.append("| 文件 | 编码 | 来源 | 识别行数 | 有效笔数 | 跳过行 |")
    lines.append("|:--|:--|:--|:--|:--|:--|")
    for d in diags:
        n = sum(1 for r in records if r["source_file"] == d["file"])
        src = {"alipay": "支付宝", "wechat": "微信"}.get(d["source"], d["source"])
        lines.append(f"| {d['file']} | {d['encoding']} | {src} | {d['total_rows']} | {n} | {d['skipped_rows']} |")
    lines.append("")

    effective = [r for r in records if r["direction"] in ("income", "expense")]
    neutral = [r for r in records if r["direction"] == "neutral"]
    income = [r for r in effective if r["direction"] == "income"]
    expense = [r for r in effective if r["direction"] == "expense"]

    # ── 核心回答：各渠道一共花了多少 ──
    lines.append("## 二、各渠道支出汇总（回答「这个月几个渠道一共花了多少」）")
    lines.append("")
    by_channel: dict[str, int] = defaultdict(int)
    cnt_channel: Counter = Counter()
    for r in expense:
        by_channel[r["channel"]] += r["amount_cents"]
        cnt_channel[r["channel"]] += 1
    if by_channel:
        lines.append("| 渠道 | 支出笔数 | 支出合计 |")
        lines.append("|:--|--:|--:|")
        for ch, total in sorted(by_channel.items(), key=lambda kv: -kv[1]):
            name = {"alipay": "支付宝", "wechat": "微信"}.get(ch, ch)
            lines.append(f"| {name} | {cnt_channel[ch]} | ¥{yuan(total)} |")
        lines.append(f"| **合计** | **{len(expense)}** | **¥{yuan(sum(by_channel.values()))}** |")
    else:
        lines.append("_无支出记录_")
    lines.append("")

    # ── 月度 ──
    lines.append("## 三、按月汇总")
    lines.append("")
    by_month: dict[str, dict[str, int]] = defaultdict(lambda: {"income": 0, "expense": 0})
    for r in effective:
        by_month[r["month"]][r["direction"]] += r["amount_cents"]
    if by_month:
        lines.append("| 月份 | 收入 | 支出 | 净额 |")
        lines.append("|:--|--:|--:|--:|")
        for month in sorted(by_month):
            inc = by_month[month]["income"]
            exp = by_month[month]["expense"]
            lines.append(f"| {month} | ¥{yuan(inc)} | ¥{yuan(exp)} | ¥{yuan(inc - exp)} |")
    else:
        lines.append("_无有效记录_")
    lines.append("")

    # ── 分类（跨渠道归一化后）──
    lines.append("## 四、支出分类（跨渠道归一化后）")
    lines.append("")
    lines.append("> **两家平台都不提供可用的消费分类**：微信「交易类型」是支付通道"
                 "（商户消费/扫码），支付宝「交易分类」列**为空**。")
    lines.append("> 下表由**规则归一化**得出（平台枚举映射 + 商户名/商品说明关键词）；"
                 "命中不了的进「待确认」，等用户一句话补全。")
    lines.append("")
    by_cat: dict[str, int] = defaultdict(int)
    cnt_cat: Counter = Counter()
    for r in expense:
        by_cat[r["category"]] += r["amount_cents"]
        cnt_cat[r["category"]] += 1
    if by_cat:
        lines.append("| 分类 | 笔数 | 金额 | 占比 |")
        lines.append("|:--|--:|--:|--:|")
        total_exp = sum(by_cat.values()) or 1
        for cat, amt in sorted(by_cat.items(), key=lambda kv: -kv[1])[:12]:
            lines.append(f"| {cat} | {cnt_cat[cat]} | ¥{yuan(amt)} | {amt / total_exp:.1%} |")
    else:
        lines.append("_无支出记录_")
    lines.append("")
    src_cnt: Counter = Counter()
    for r in expense:
        src_cnt[r.get("norm_source", "fallback")] += 1
    src_name = {"platform": "平台枚举映射", "keyword": "关键词下沉", "fallback": "待确认"}
    lines.append("判定来源：" + "　".join(f"{src_name.get(k, k)} {v} 笔"
                                    for k, v in src_cnt.most_common()))
    lines.append("")

    # ── 性质：刚性 vs 灵活（现金流预测的基数）──
    lines.append("## 五、刚性 vs 灵活（现金流预测的保底基数）")
    lines.append("")
    n_amt: dict[str, int] = defaultdict(int)
    n_cnt: Counter = Counter()
    for r in expense:
        n_amt[r.get("nature", "flexible")] += r["amount_cents"]
        n_cnt[r.get("nature", "flexible")] += 1
    total_n = sum(n_amt.values()) or 1
    lines.append("| 性质 | 含义 | 笔数 | 金额 | 占比 |")
    lines.append("|:--|:--|--:|--:|--:|")
    for k, desc in (("rigid", "刚性 · 不可压缩"), ("flexible", "灵活 · 可压缩")):
        lines.append(f"| {k} | {desc} | {n_cnt[k]} | ¥{yuan(n_amt[k])} | {n_amt[k] / total_n:.1%} |")
    lines.append("")
    lines.append(f"> **刚性支出 ¥{yuan(n_amt['rigid'])} = 每月保底基数**——"
                 "预测「钱还能撑多久」要用它，而不是用「上月总支出」。")
    lines.append("")

    # ── 归属：个人 / 经营 ──
    lines.append("## 六、归属（个人 / 经营）")
    lines.append("")
    b_amt: dict[str, int] = defaultdict(int)
    b_cnt: Counter = Counter()
    for r in expense:
        b_amt[r.get("attribution", "personal")] += r["amount_cents"]
        b_cnt[r.get("attribution", "personal")] += 1
    lines.append("| 归属 | 笔数 | 金额 |")
    lines.append("|:--|--:|--:|")
    for k, desc in (("business", "经营"), ("personal", "个人")):
        lines.append(f"| {desc} | {b_cnt[k]} | ¥{yuan(b_amt[k])} |")
    lines.append("")
    if b_amt["business"]:
        lines.append(f"> 疑似经营成本 ¥{yuan(b_amt['business'])}——**建议留存票据**。"
                     "（本工具只做标注与提醒，不判断可否税前扣除。）")
        lines.append("")

    # ── 不计收支（重要：转账/理财不计会虚高）──
    lines.append("## 七、不计收支记录（转账/理财等，**未计入上面统计**）")
    lines.append("")
    if neutral:
        lines.append(f"- 笔数：{len(neutral)}　金额合计：¥{yuan(sum(r['amount_cents'] for r in neutral))}")
        lines.append("- 说明：这类记录不是收入也不是支出（如余额转入/转出、信用卡还款），")
        lines.append("  若混入收支统计会让「这个月花了多少」虚高，故单列。")
    else:
        lines.append("_无_")
    lines.append("")

    # ── 对手方（默认脱敏）──
    lines.append("## 八、交易对手方")
    lines.append("")
    if show_counterparty:
        cp: dict[str, int] = defaultdict(int)
        for r in effective:
            if r["counterparty"]:
                cp[r["counterparty"]] += r["amount_cents"]
        lines.append("> ⚠️ 已按 `--show-counterparty` 显式开启，以下含真实对手方名称，仅留本地。")
        lines.append("")
        lines.append("| 对手方 | 金额合计 |")
        lines.append("|:--|--:|")
        for name, amt in sorted(cp.items(), key=lambda kv: -kv[1])[:20]:
            lines.append(f"| {name} | ¥{yuan(amt)} |")
    else:
        distinct = len({r["counterparty"] for r in effective if r["counterparty"]})
        lines.append(f"- 涉及不同对手方 **{distinct}** 个（名称已脱敏，未输出）")
        lines.append("- 需要查看请加 `--show-counterparty`（产物仅留本地，勿外传）")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("*本摘要由 parse_bill_csv.py 本地生成 · 无网络调用 · 明细见同目录 ledger.jsonl*")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ═══════════════════════════════════════════════════════════════
# 生存仪表（A 方案）· 辞职前覆盖率（C 方案）
#
# 依据：02-脑暴/财务生存工具-三层方案与合规判断-20260913.md
#
# 设计铁律 —— **账单能证明的我们算，只有用户知道的他自己填**：
#   ✅ 从账单算：月刚性支出 / 外部收入 / 收入稳定性 / 覆盖区间
#   ⚠️ 用户提供：现金余额（账单是流水，不含余额）
#   ⚠️ 用户自填：资源清单
#
# 合规：只陈述事实与依据；**不给建议、不承诺结果**（合规文档 §一）
# ═══════════════════════════════════════════════════════════════

# 「不算外部收入」的词：退款 / 退货是把钱退回来，不是新的收入
_NON_REVENUE_WORDS = ("退款", "退还", "退货")


def is_external_revenue(record: dict) -> bool:
    """一笔 income 是否属于「外部收入」（客户 / 市场给的钱）。"""
    if record.get("direction") != "income":
        return False
    text = f"{record.get('description', '')} {record.get('category', '')}"
    return not any(w in text for w in _NON_REVENUE_WORDS)


def _date_of(record: dict):
    """取出记录日期（datetime）；失败返回 None。"""
    try:
        return datetime.strptime(record["date"][:10], "%Y-%m-%d")
    except (ValueError, KeyError, TypeError):
        return None


def write_survival(records: list[dict], cash_cents: int, outdir: Path,
                   extra_fixed_month_cents: int = 0) -> Path:
    """生成 survival.md —— 生存指数（A）+ 辞职前覆盖率（C）。

    生存指数 = 现金余额 ÷ 日刚性支出
    覆盖率   = 月均外部收入 ÷ 月均刚性支出

    extra_fixed_month_cents：**用户补充的月固定支出**（账单没识别到的，如房租、水电）。
    依据「账单能证明的我们算、只有他知道的他自填」——**识别不全时不靠猜，靠用户补**
    （2026-09-14 David 要求）。
    """
    dates = [d for d in (_date_of(r) for r in records) if d]
    lines: list[str] = ["# 生存仪表（Survival Gauge）", ""]
    lines.append(f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M} ｜ "
                 f"现金余额（**由你提供**）：¥{yuan(cash_cents)}")
    lines.append("> **本页只陈述账单能证明的事实；不构成建议，不承诺结果。**")
    lines.append("")

    path = outdir / "survival.md"
    if not dates:
        lines.append("_无有效日期，无法计算。_")
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    first, last = min(dates), max(dates)
    span_days = max((last - first).days + 1, 1)
    today = datetime.now()

    # ── 账单能算的两件事：刚性支出 / 外部收入 ──
    rigid = [r for r in records
             if r.get("direction") == "expense" and r.get("nature") == "rigid"]
    revenue = [r for r in records if is_external_revenue(r)]
    rigid_from_bill = sum(r["amount_cents"] for r in rigid) / span_days * 30
    rigid_month = rigid_from_bill + extra_fixed_month_cents   # 账单识别 + 用户补充
    rigid_daily = rigid_month / 30
    revenue_daily = sum(r["amount_cents"] for r in revenue) / span_days
    revenue_month = revenue_daily * 30

    # ── 一、生存指数 ──
    lines.append("## 一、生存指数")
    lines.append("")
    survive_days = None
    if rigid_daily > 0:
        survive_days = int(cash_cents / rigid_daily)
        lines.append(f"### **{survive_days} 天**")
        lines.append("")
        lines.append(f"按你的日刚性支出 ¥{yuan(int(rigid_daily))}/天计算"
                     "（社保 / 房租 / 税费等**不可压缩**项）。")
        if extra_fixed_month_cents:
            lines.append("")
            lines.append(f"　— 其中 **账单识别 ¥{yuan(int(rigid_from_bill))}/月**"
                         f" ＋ **你补充 ¥{yuan(extra_fixed_month_cents)}/月**")
    else:
        lines.append("_账单里没有识别到「刚性支出」，无法计算生存指数。_")
        lines.append("")
        lines.append("（刚性支出识别依据：社保、房租、税费、宽带、话费等不可压缩项。）")
    lines.append("")

    # ── 二、三层判据 ──
    lines.append("## 二、三层判据")
    lines.append("")
    lines.append("| 层 | 判据 | 你的结果 |")
    lines.append("|:--|:--|:--|")
    if revenue:
        last_rev = max(d for d in (_date_of(r) for r in revenue) if d)
        l1 = f"最近一次 **{(today - last_rev).days} 天前**"
    else:
        l1 = "**覆盖期内没有外部收入**"
    lines.append(f"| 1 | 有没有外部收入 | {l1} |")
    months_with = {r["month"] for r in revenue if r.get("month")}
    all_months = {r["month"] for r in records if r.get("month")}
    l2 = (f"**{len(months_with)} / {len(all_months)} 个月**有外部收入"
          if all_months else "—")
    lines.append(f"| 2 | 稳不稳 | {l2} |")
    lines.append(f"| 3 | 还能撑多久 | "
                 f"{f'**{survive_days} 天**' if survive_days is not None else '—'} |")
    lines.append("")
    lines.append(f"（覆盖区间：{first:%Y-%m-%d} ~ {last:%Y-%m-%d}，共 {span_days} 天；"
                 "月均值按日均 × 30 折算。）")
    lines.append("")

    # ── 三、辞职前覆盖率（C 方案）──
    lines.append("## 三、辞职前覆盖率（C 方案）")
    lines.append("")
    lines.append("| 项 | 金额 |")
    lines.append("|:--|--:|")
    lines.append(f"| 月均外部收入 | ¥{yuan(int(revenue_month))} |")
    if extra_fixed_month_cents:
        lines.append(f"| 月均刚性支出 | ¥{yuan(int(rigid_month))}"
                     f"（账单 ¥{yuan(int(rigid_from_bill))} + 补充 ¥{yuan(extra_fixed_month_cents)}） |")
    else:
        lines.append(f"| 月均刚性支出 | ¥{yuan(int(rigid_month))} |")
    if rigid_month > 0:
        lines.append(f"| **覆盖率** | **{revenue_month / rigid_month * 100:.0f}%** |")
        lines.append("")
        lines.append("> **覆盖率低于 100% = 现有收入尚不足以覆盖刚性支出。**")
        lines.append("> 这句话只描述账单里的事实，不评价你该怎么做。")
    else:
        lines.append("| **覆盖率** | 无法计算（未识别到刚性支出） |")
    lines.append("")

    # ── 四、资源清点（用户自填）──
    lines.append("## 四、资源清点（**这一栏由你自己填**）")
    lines.append("")
    lines.append("账单证明不了这些，只有你知道：")
    lines.append("")
    lines.append("- [ ] 我的技能可以接的零散活（设计 / 开发 / 写作…）")
    lines.append("- [ ] 我认识但最近没联系的人（可能带来订单）")
    lines.append("- [ ] 我每天能投入在找客户上的小时数：__________")
    lines.append("- [ ] 其他：__________________________")
    lines.append("")

    # ── 五、本页不做什么 ──
    lines.append("## 五、本页**不做**什么")
    lines.append("")
    lines.append("- **不给你建议**——不判断你该做什么、先做哪一件")
    lines.append("- **不承诺结果**——不保证能提高生存概率")
    lines.append("- 只把账单能证明的事，**摆在你自己面前**")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("*本页由 parse_bill_csv.py 本地生成 · 无网络调用 · 不含建议与预测*")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════

def run_diagnose(paths: list[Path]) -> int:
    """只诊断不解析：第一次拿到真实账单时用，看清真实字段结构。"""
    for path in paths:
        print(f"\n{'=' * 60}\n诊断：{path.name}\n{'=' * 60}")
        try:
            rows, enc = read_rows(path)
        except OSError as e:
            print(f"  ❌ 读取失败：{e}")
            continue
        print(f"  编码：{enc}　总行数：{len(rows)}")
        hi = find_header(rows)
        if hi < 0:
            print("  ⚠️ 未识别到表头（前 15 行原文如下，请把这段发我）")
            for i, row in enumerate(rows[:15]):
                print(f"    [{i}] {row}")
            continue
        header = rows[hi]
        mapping = map_columns(header)
        print(f"  表头在第 {hi} 行，来源判定：{detect_source(header, path.name)}")
        print(f"  列映射（{len(mapping)}/{len(_COL_RULES)} 个字段命中）：")
        for field, idx in mapping.items():
            print(f"    {field:<15} ← 第 {idx} 列：{header[idx]}")
        missing = [f for f, _ in _COL_RULES if f not in mapping]
        if missing:
            print(f"  ⚠️ 未命中的字段：{', '.join(missing)}")
        print(f"  表头原文：{header}")
        for row in rows[hi + 1:hi + 4]:
            print(f"  样例行：{row}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="账单 CSV 解析器（支付宝/微信）→ CashLens 账本草稿 + 脱敏摘要",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("files", nargs="+", help="账单 CSV 路径（可多个）")
    ap.add_argument("--out", default="data/bill_out", help="输出目录（默认 data/bill_out，已被 gitignore）")
    ap.add_argument("--diagnose", action="store_true", help="只诊断格式，不解析")
    ap.add_argument("--show-counterparty", action="store_true",
                    help="摘要中显示交易对手方（默认脱敏）")
    ap.add_argument("--cash", type=float, default=None,
                    help="当前现金余额（元）。给了它就额外生成 survival.md —— "
                         "账单是流水、不含余额，所以这个数只能由你提供")
    ap.add_argument("--fixed-cost", type=float, default=0.0,
                    help="账单未识别到的**月固定支出**（元），如房租 3000；"
                         "会补进刚性支出，与 --cash 配合使用")
    args = ap.parse_args()

    paths = [Path(p) for p in args.files]
    missing = [p for p in paths if not p.exists()]
    if missing:
        for p in missing:
            print(f"❌ 文件不存在：{p}", file=sys.stderr)
        return 2

    if args.diagnose:
        return run_diagnose(paths)

    all_records: list[dict] = []
    all_diags: list[dict] = []
    for path in paths:
        records, diag = parse_file(path)
        if diag["header_index"] < 0:
            print(f"⚠️ {path.name}：未识别到表头，已跳过。请先跑 --diagnose 看真实结构。")
        all_records.extend(records)
        all_diags.append(diag)

    if not all_records:
        print("❌ 没有解析出任何记录。请先跑 --diagnose 诊断格式。", file=sys.stderr)
        return 1

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    ledger = write_ledger(all_records, outdir)
    summary = write_summary(all_records, all_diags, outdir, args.show_counterparty)

    expense = [r for r in all_records if r["direction"] == "expense"]
    total = sum(r["amount_cents"] for r in expense)
    print(f"✅ 解析完成：{len(all_records)} 笔（其中支出 {len(expense)} 笔）")
    print(f"   支出合计：¥{yuan(total)}")
    print(f"   账本：{ledger}")
    print(f"   摘要：{summary}")

    # 生存仪表（A 方案）+ 辞职前覆盖率（C 方案）—— 需要用户提供现金余额
    if args.cash is not None:
        survival = write_survival(all_records, int(round(args.cash * 100)), outdir,
                                  int(round((args.fixed_cost or 0) * 100)))
        print(f"   生存仪表：{survival}")
    else:
        print("   （提示：加 --cash <现金余额> 可生成生存仪表 survival.md）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
