#!/usr/bin/env python3
"""CashLens 账单 CSV 导入解析器（采集模块 · 通道 A）
支持：支付宝官方导出 CSV / 微信支付官方导出 CSV
输出：统一交易模型列表（对齐事件账本 schema）
"""
import csv
import hashlib
import io
import re
import sys


# ─── 统一交易模型 ────────────────────────────────────────
def norm_name(name):
    """对方名归一化：去空格/符号/常见后缀，用于跨渠道指纹"""
    if not name:
        return ""
    n = re.sub(r"[\s*\-_（）()]+", "", str(name))
    for suffix in ["有限公司", "有限责任公司", "股份有限公司", "官方旗舰店", "旗舰店"]:
        if n.endswith(suffix):
            n = n[: -len(suffix)]
            break
    return n


def make_txn(date, txn_type, amount, counterparty, category, channel, note="", txn_id=""):
    """构造统一交易 + 双指纹：
    - dedupe_key：渠道内防重复写入（含 txn_id）
    - merge_fingerprint：跨渠道对账去重（金额 + 日期 + 对方名归一化，不含 txn_id）
    """
    amount = round(float(amount), 2)
    dedupe_src = f"{date[:10]}|{txn_type}|{amount:.2f}|{counterparty}|{txn_id}"
    dedupe_key = hashlib.md5(dedupe_src.encode("utf-8")).hexdigest()[:16]
    merge_src = f"{date[:10]}|{txn_type}|{amount:.2f}|{norm_name(counterparty)}"
    merge_fingerprint = hashlib.md5(merge_src.encode("utf-8")).hexdigest()[:16]
    return {
        "date": date,
        "type": txn_type,
        "amount": amount,
        "counterparty": counterparty,
        "category": category,
        "channel": channel,
        "note": note,
        "dedupe_key": dedupe_key,
        "merge_fingerprint": merge_fingerprint,
        "evidence": "recognition"
    }


def find_duplicates(txns):
    """对账去重：按 merge_fingerprint 聚类，找出跨渠道重复交易（返回重复组）"""
    groups = {}
    for i, t in enumerate(txns):
        groups.setdefault(t["merge_fingerprint"], []).append(i)
    return {fp: idxs for fp, idxs in groups.items() if len(idxs) > 1}


def detect_channel(rows):
    """根据表头判断是支付宝还是微信 CSV"""
    header = rows[0] if rows else []
    joined = "|".join(str(h) for h in header)
    if "交易分类" in joined or "交易订单号" in joined:
        return "alipay"
    if "交易单号" in joined and "商户单号" in joined:
        return "wechat"
    return "unknown"


# ─── 支付宝 CSV 解析 ─────────────────────────────────────
ALIPAY_COLS = ["交易时间", "交易分类", "交易对方", "对方账号", "商品说明", "收/支",
               "金额", "收/付款方式", "交易状态", "交易订单号", "商家订单号", "备注"]


def parse_alipay(rows):
    txns = []
    header = rows[0]
    idx = {name: header.index(name) for name in ALIPAY_COLS if name in header}
    for row in rows[1:]:
        if len(row) < len(header):
            continue
        get = lambda name: row[idx[name]].strip() if name in idx else ""
        direction = get("收/支")
        if direction == "不计收支":
            continue
        txn_type = "income" if direction == "收入" else "expense"
        amount = re.sub(r"[^\d.]", "", get("金额"))
        if not amount:
            continue
        txns.append(make_txn(
            date=get("交易时间"),
            txn_type=txn_type,
            amount=amount,
            counterparty=get("交易对方"),
            category=map_category(get("交易分类"), get("商品说明")),
            channel="alipay",
            note=get("备注") or get("商品说明"),
            txn_id=get("交易订单号"),
        ))
    return txns


# ─── 微信 CSV 解析 ───────────────────────────────────────
WECHAT_COLS = ["交易时间", "交易类型", "交易对方", "商品", "收/支", "金额(元)",
               "支付方式", "当前状态", "交易单号", "商户单号", "备注"]


def parse_wechat(rows):
    txns = []
    header = rows[0]
    idx = {name: header.index(name) for name in WECHAT_COLS if name in header}
    for row in rows[1:]:
        if len(row) < len(header):
            continue
        get = lambda name: row[idx[name]].strip() if name in idx else ""
        direction = get("收/支")
        if direction == "/":
            continue
        txn_type = "income" if direction == "收入" else "expense"
        amount = re.sub(r"[^\d.]", "", get("金额(元)"))
        if not amount:
            continue
        txns.append(make_txn(
            date=get("交易时间"),
            txn_type=txn_type,
            amount=amount,
            counterparty=get("交易对方"),
            category=map_category(get("交易类型"), get("商品")),
            channel="wechat",
            note=get("备注") or get("商品"),
            txn_id=get("交易单号"),
        ))
    return txns


# ─── 分类映射（启发式，后续可接智能分类 SKILL）───────────
def map_category(*keywords):
    text = " ".join(str(k) for k in keywords if k)
    rules = [
        ("餐饮", ["餐饮", "美食", "面馆", "咖啡", "外卖", "饭店", "餐"]),
        ("交通", ["交通", "滴滴", "打车", "地铁", "加油", "公交"]),
        ("购物", ["购物", "淘宝", "拼多多", "京东", "超市", "百货", "商城"]),
        ("房租", ["房租", "物业", "水电"]),
        ("收入", ["工资", "转账", "收款", "退款", "尾款", "接单"]),
    ]
    for cat, keys in rules:
        if any(k in text for k in keys):
            return cat
    return "其他"


# ─── 入口 ────────────────────────────────────────────────
def parse_csv_file(path):
    """读 CSV 文件（支持 UTF-8 / GBK），自动识别渠道并解析"""
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "gbk", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(c.strip() for c in r)]
    if len(rows) < 2:
        return {"channel": "unknown", "txns": [], "error": "空文件或格式不支持"}
    channel = detect_channel(rows)
    if channel == "alipay":
        txns = parse_alipay(rows)
    elif channel == "wechat":
        txns = parse_wechat(rows)
    else:
        return {"channel": "unknown", "txns": [], "error": "无法识别账单渠道（请用支付宝/微信官方导出的 CSV）"}
    return {"channel": channel, "txns": txns, "count": len(txns)}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 csv_import.py <账单CSV路径>")
        sys.exit(1)
    result = parse_csv_file(sys.argv[1])
    import json
    print(json.dumps(result, ensure_ascii=False, indent=2)[:2000])
