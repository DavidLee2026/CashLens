#!/usr/bin/env python3
"""生成「合成小微经营账」——可入库、可复现的演示/回归数据。

## 为什么需要它（2026-09-28 立）

真实数据不能用：`data/bill_out/ledger.jsonl` 那份 90 笔是**个人家庭账单**
（59 笔有效里 55 笔支出、4 笔收入共 ¥1,019.99，**零报销、零经营收入**）。
把它导入产品后实测出三个后果：
  1. 预测变成"必死演示"——只有支出节奏，没有收入节奏，30 天中位 −¥16,686；
  2. 「健康度」被压到 0%（该指标就是收入笔数占比）；
  3. 决策引擎答「算不出」（它要「带工时的**收入**记录」，家庭账单一条都没有）。

**公开数据集也补不上**：带标签的多为模板合成且无时间戳；有跨年时间的是 INR/英文、
无报销关系。所以这条数据自己造，但要**按产品口径造对**。

## 四条硬条件（缺一条这批数据就没有演示价值）

| # | 条件 | 为什么 |
|:--|:--|:--|
| ① | **双边**：有经营收入也有成本 | 预测才有"节奏"可言 |
| ② | 跨 ≥2 个月、具体到日 | 90 天区间要足够多"有记录的日子"（外推门槛是 5 天） |
| ③ | **有垫付/报销关系** | 产品差异化的那一段，家庭账单演示不出来 |
| ④ | 中文 + 小微经营商户名与分类 | 分类词表、平台映射才有东西可测 |

## 两条刻意设计（都是为了"能解释"，不是为了让数字好看）

1. **收入带 `hours`**：决策引擎靠它反推历史时薪（`decision.historical_hourly`）。
   不给工时，问「这单接不接」只会得到「算不出」——那就演示不了决策。
2. **只放"相符"的假设检验**：`calc._has_misconception` 一旦发现 `actual != expected`
   就把标签永久钉在「财务误区」（**当前没有消除机制**，见下方已知边界）。
   为了让演示标签不被锁死，本数据只生成一组相符的 assumption/resolution，
   用来证明事件类型走得通；不符的那组留到有清除机制之后再演示。

## 已知边界（如实标注，别在材料里回避）

- **`misconception` 标签不可消除**：任何一组不符的假设检验都会让状态标签永久停在
  「财务误区」，产品里没有"这条已经纠正了"的出口。本生成器因此不产不符的样本。
- 本数据是**合成**的：每条事件的证据里带 `synthetic: true`、note 带「合成样本」，
  `projects.json` 里项目备注也写明。**任何材料引用它都必须标注为合成**。

## 用法

  # 生成（默认写到 samples/synthetic_small_business/）
  python3 scripts/make_synthetic_business_ledger.py

  # 自定义时间窗口（默认今天往前 8 周）
  python3 scripts/make_synthetic_business_ledger.py --weeks 10 --out /tmp/x

产出：
  finance_events.jsonl —— 规范事件（schema_version=3），产品直接可读（文件名即产品口径）
  projects.json  —— 与之配套的项目表
  README.md      —— 数据说明（本文件顶部摘要）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # 09-代码/
sys.path.insert(0, str(ROOT / "backend"))

from app.services.state_engine import make_event    # noqa: E402
from app.services.state_engine.spec import SCHEMA_VERSION  # noqa: E402

DEFAULT_OUT = ROOT / "samples" / "synthetic_small_business" / "data"
# ⚠️ 产出目录本身就是一个可用的 **数据目录**：直接
#   CASH_DATA_DIR=samples/synthetic_small_business/data python3 -m uvicorn app.main:app
# 就能让产品跑在这批合成数据上，不必碰 data/（那是用户真实数据）。

# ── 主体设定：一人设计/内容工作室（与产品目标用户同型）──────────────
# 项目 id 与显示名分开：账本存 id，显示名在 projects.json（与线上口径一致）
PROJECTS = [
    ("project_syn_a", "安馨商贸 · 门店视觉"),
    ("project_syn_b", "禾野食品 · 月度内容"),
    ("project_syn_c", "启明教育 · 训练营"),
]

# 经营支出（可开票 / 有经营属性）
BIZ_EXPENSES = [
    # (分类, 对手方, 金额区间(分), 说明, 权重)
    ("经营", "阿里云", (6000, 24000), "云服务器续费", 3),
    ("经营", "DeepSeek API", (3000, 15000), "模型调用充值", 4),
    ("经营", "稿定设计", (2900, 8900), "会员续费", 2),
    ("经营", "顺丰速运", (1500, 6800), "寄送打样物料", 3),
    ("经营", "小马外包", (80000, 260000), "外包插画结算", 2),
    ("经营", "商标局", (30000, 30000), "商标注册费", 1),
    ("交通", "高德打车", (2600, 9800), "客户现场往返", 5),
    ("交通", "滴滴出行", (2200, 8600), "客户现场往返", 4),
    ("交通", "中国铁路", (12000, 56000), "出差高铁票", 2),
    ("住宿", "汉庭酒店", (18000, 42000), "外地项目住宿", 2),
    ("餐饮", "瑞幸咖啡", (1500, 3800), "见客户", 4),
    ("餐饮", "老乡鸡", (2800, 6800), "工作餐", 3),
    ("通讯", "中国联通", (9900, 12900), "手机套餐", 1),
]

# 个人支出（演示"经营 vs 个人"的边界，不该进经营成本）
PERSONAL_EXPENSES = [
    ("居住", "房东", (350000, 350000), "房租", 1),
    ("餐饮", "美团外卖", (1800, 5800), "晚饭", 6),
    ("购物", "山姆会员店", (15000, 46000), "家里日用", 3),
    ("娱乐", "哔哩哔哩", (1500, 2500), "会员", 1),
]

# 收入：带工时（决策引擎靠它反推历史时薪）
# (项目序, 分类, 金额区间(分), 工时区间, 说明, 回款延迟天数区间)
INCOMES = [
    (0, "接单", (1200000, 1800000), (30, 45), "门店视觉首期款", (2, 5)),
    (0, "接单", (900000, 1400000), (25, 40), "门店视觉尾款", (5, 14)),
    (1, "接单", (600000, 900000), (18, 28), "月度内容服务费", (1, 4)),
    (1, "接单", (600000, 900000), (18, 28), "月度内容服务费", (2, 6)),
    (2, "接单", (1500000, 2200000), (40, 60), "训练营视觉全案", (7, 18)),
    (2, "接单", (400000, 700000), (12, 20), "训练营物料加急", (3, 9)),
]

# 垫付 → 报销（产品差异化那一段：员工/自己先买单，回头走报销）
ADVANCES = [
    ("经营", "打印店", (12000, 36000), "项目物料打印，先垫付后报销"),
    ("交通", "高德打车", (4800, 12800), "客户现场往返，垫付待报"),
    ("餐饮", "海底捞", (28000, 56000), "项目庆功，垫付待报"),
    ("住宿", "亚朵酒店", (36000, 68000), "出差住宿，垫付待报"),
]


def _at(day: datetime, hh: int, mm: int) -> str:
    return day.replace(hour=hh, minute=mm, second=random.randint(0, 59)).isoformat(timespec="seconds")


def _cents(lo_hi: tuple[int, int]) -> int:
    return random.randrange(lo_hi[0], lo_hi[1] + 1, 100)   # 取整到元，像人花的钱


def build_events(as_of: datetime, weeks: int, seed: int) -> list[dict]:
    random.seed(seed)
    start = (as_of - timedelta(weeks=weeks)).replace(hour=9, minute=0, second=0, microsecond=0)
    events: list[dict] = []

    # ── 收入：分散到窗口内，回款日与到账日分开 ──
    inc_days = [start + timedelta(days=round(i * weeks * 7 / (len(INCOMES) + 1))) for i in range(1, len(INCOMES) + 1)]
    for (proj_i, cat, amt, hrs, memo, delay), base in zip(INCOMES, inc_days):
        pid, pname = PROJECTS[proj_i]
        hours = random.randint(*hrs)
        ts = _at(base, random.choice([10, 11, 14, 15, 16]), random.choice([5, 18, 26, 40, 55]))
        events.append(make_event(
            ts=ts, event_type="income", amount_cents=_cents(amt), evidence_kind="flow",
            channel=random.choice(["wechat", "alipay", "bank"]),
            category=cat, counterparty=f"{pname.split(' · ')[0]}（客户）",
            note=f"{pname} · {memo}（合成样本）",
            confirmed=True, project=pid,
            extra={"hours": hours,  # 决策引擎靠它反推历史时薪
                   "due_days": random.randint(*delay)},
            dedupe_unique=True,
        ))

    # ── 经营与个人支出：全程散布 ──
    span = (as_of - start).days
    for cat, cp, amt, memo, weight in BIZ_EXPENSES + PERSONAL_EXPENSES:
        for _ in range(weight * max(1, weeks // 4)):
            d = start + timedelta(days=random.randint(0, span))
            proj = random.choice([p for p in PROJECTS if cat in ("交通", "住宿", "餐饮", "经营")] or PROJECTS)
            biz = cat in ("经营", "通讯") or cp in ("高德打车", "滴滴出行", "中国铁路", "汉庭酒店")
            events.append(make_event(
                ts=_at(d, random.randint(8, 21), random.choice([3, 12, 27, 41, 58])),
                event_type="expense", amount_cents=_cents(amt), evidence_kind="receipt",
                channel=random.choice(["wechat", "alipay", "receipt"]),
                category=cat, counterparty=cp, note=f"{memo}（合成样本）",
                confirmed=True, project=proj[0] if biz else "",
                dedupe_unique=True,
            ))

    # ── 垫付待报销：note 里写明状态，供产品认列「垫付 → 报销」──
    for cat, cp, amt, memo in ADVANCES:
        d = start + timedelta(days=random.randint(0, span))
        events.append(make_event(
            ts=_at(d, random.randint(9, 19), random.choice([7, 22, 35, 49])),
            event_type="expense", amount_cents=_cents(amt), evidence_kind="receipt",
            channel="receipt", category=cat, counterparty=cp,
            note=f"{memo}（合成样本）",
            confirmed=True, project=random.choice(PROJECTS)[0],
            dedupe_unique=True,
        ))

    # ── 假设检验：一组**相符**的（刻意不相符的会让标签永久停在「财务误区」）──
    a_ts = _at(start + timedelta(days=7), 10, 30)
    assumption = {
        "schema_version": SCHEMA_VERSION,
        "event_id": uuid.uuid4().hex,
        "ts": a_ts,
        "recorded_at": a_ts,
        "type": "assumption",
        "amount_cents": 0,
        "channel": "manual",
        "category": "",
        "counterparty": "",
        "note": "报价时假设：安馨商贸尾款 5 天内回（合成样本）",
        "project": PROJECTS[0][0],
        "assumption": {"kind": "payment_days", "expected": 5},
        "evidence": {"kind": "self_report", "weight": 0.10, "confirmed": True},
    }
    r_ts = _at(start + timedelta(days=12), 15, 10)
    resolution = {
        "schema_version": SCHEMA_VERSION,
        "event_id": uuid.uuid4().hex,
        "ts": r_ts,
        "recorded_at": r_ts,
        "type": "resolution",
        "amount_cents": 0,
        "channel": "manual",
        "category": "",
        "counterparty": "",
        "note": "实际 5 天回款，与假设相符（合成样本）",
        "project": PROJECTS[0][0],
        "assumption_ref": assumption["event_id"],
        "actual": 5,
        "evidence": {"kind": "flow", "weight": 1.00, "confirmed": True},
    }
    # 手工事件也要有 dedupe_key：整个账本的 load() 都按它折叠
    for rec in (assumption, resolution):
        raw = "|".join([rec["project"], rec["channel"], "0", rec["ts"][:10], "", "", rec["event_id"]])
        rec["dedupe_key"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    events += [assumption, resolution]

    events.sort(key=lambda e: e["ts"])
    return events


def build_projects(as_of: datetime) -> list[dict]:
    return [
        {"id": pid, "name": name, "named": True,
         "created": (as_of - timedelta(weeks=8)).isoformat(timespec="seconds"),
         "note": "合成样本项目（samples/synthetic_small_business）"}
        for pid, name in PROJECTS
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description="生成合成小微经营账（双边 + 垫付报销）")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--weeks", type=int, default=8, help="时间窗口（周），默认 8")
    ap.add_argument("--seed", type=int, default=20260928, help="随机种子（固定以保证可复现）")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    as_of = datetime.now().replace(microsecond=0)
    events = build_events(as_of, args.weeks, args.seed)

    # 文件名用产品口径（finance_events.jsonl），使产出目录可直接当 CASH_DATA_DIR 用
    ledger = out / "finance_events.jsonl"
    with open(ledger, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    (out / "projects.json").write_text(
        json.dumps(build_projects(as_of), ensure_ascii=False, indent=2), encoding="utf-8")

    inc = [e for e in events if e["type"] == "income"]
    exp = [e for e in events if e["type"] == "expense"]
    days = len({e["ts"][:10] for e in events})
    si = sum(e["amount_cents"] for e in inc) / 100
    se = sum(e["amount_cents"] for e in exp) / 100
    print(f"✅ 合成经营账 → {ledger}")
    print(f"   事件 {len(events)} 笔（收入 {len(inc)} / 支出 {len(exp)} / 假设检验 2）")
    print(f"   跨度 {events[0]['ts'][:10]} → {events[-1]['ts'][:10]}（{days} 个有记录的日子）")
    print(f"   收入 ¥{si:,.2f} ｜ 支出 ¥{se:,.2f} ｜ 净 ¥{si-se:,.2f}")
    adv = [e for e in exp if "垫付" in e.get("note", "")]
    print(f"   垫付待报销 {len(adv)} 笔（¥{sum(e['amount_cents'] for e in adv)/100:,.2f}）")
    print(f"   带工时收入 {sum(1 for e in inc if e.get('hours'))} 笔（决策引擎需要）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
