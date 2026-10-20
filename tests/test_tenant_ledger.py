"""账号台账（xlsx）：一行一个邀请码账号（2026-10-20 立，当天改口径）

手工服务期用它看「三个账号各自用成什么样」。这组用例钉五条，任何一条回退都会让台账说谎：

1. **作废的事件不算** —— 曾出现的错：自己逐行 json.loads 读账本，把 void 掉的账
   也算成笔数；
2. **同号发票只算一次** —— 与工作台同一口径；
3. **重新生成不抹掉手填列** —— 反馈/卡点/是否付费是几周攒下来的，刷新一次就没了等于白记；
4. **金额列不许回来** —— 2026-10-20 David 拍板去掉收入/支出/净额（「他们赚多赚少和我没有关系」），
   这条钉住它别复活；
5. **老台账里带金额列也能读** —— 换口径之后重生成，旧表里已经填好的反馈必须带过来。
"""
import json
import sys
from pathlib import Path

from openpyxl import Workbook, load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import make_tenant_ledger as ledger  # noqa: E402


def _mk(root: Path, slug: str, acts: list[dict]) -> None:
    d = root / "tenants" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "finance_events.jsonl").write_text(
        "\n".join(json.dumps(a, ensure_ascii=False) for a in acts) + "\n", encoding="utf-8")


def _env(tmp_path: Path) -> Path:
    rows = [
        {"slug": "aaaaaaaa", "label": "账号一", "active": True,
         "created_at": "2026-10-19T10:00:00", "last_seen": "2026-10-20T09:00:00"},
        {"slug": "bbbbbbbb", "label": "账号二", "active": False,
         "created_at": "2026-10-19T11:00:00", "last_seen": ""},
    ]
    (tmp_path / "tenants.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    _mk(tmp_path, "aaaaaaaa", [
        {"event_id": "e1", "type": "income", "amount_cents": 1000000, "ts": "2026-10-20T10:00:00",
         "category": "接单", "dedupe_key": "k1"},
        {"event_id": "e2", "type": "expense", "amount_cents": 96200, "ts": "2026-10-20T10:05:00",
         "category": "住宿", "dedupe_key": "k2", "extra": {"invoice_no": "INV1"}},
        {"event_id": "v1", "type": "void", "amount_cents": 0, "ts": "2026-10-20T10:10:00",
         "targets": ["e2"], "dedupe_key": "k-void"},
        {"event_id": "e3", "type": "expense", "amount_cents": 96200, "ts": "2026-10-20T10:11:00",
         "category": "住宿", "dedupe_key": "k3", "extra": {"invoice_no": "INV1"}},
        {"event_id": "e4", "type": "expense", "amount_cents": 96200, "ts": "2026-10-20T10:12:00",
         "category": "住宿", "dedupe_key": "k4", "extra": {"invoice_no": "INV1"}},
    ])
    return tmp_path


def _row(path: Path, name: str) -> dict:
    """按表头取一行的字典 —— 列序会变（后来加入了「邀请码」列），用下标会脆。"""
    wb = load_workbook(path)
    ws = wb["账号台账"]
    head = [c.value for c in ws[1]]
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[0] == name:
            return dict(zip(head, row))
    raise AssertionError(f"台账里没有 {name}")


def test_voided_event_is_not_counted(tmp_path):
    root = _env(tmp_path)
    out = ledger.build(root, tmp_path / "ledger.xlsx")
    row = _row(out, "账号一")
    # 收入 1 笔 + 支出 1 笔（被 void 的那笔不算，重复发票也只算一次）
    assert row["记账笔数"] == 2
    assert row["最近一笔"] == "2026-10-20"
    assert row["最近登录"] == "2026-10-20"


def test_duplicate_invoice_counted_once(tmp_path):
    root = _env(tmp_path)
    out = ledger.build(root, tmp_path / "ledger.xlsx")
    assert _row(out, "账号一")["记账笔数"] == 2      # INV1 三笔只算一笔


def test_inactive_account_is_marked_and_empty(tmp_path):
    root = _env(tmp_path)
    out = ledger.build(root, tmp_path / "ledger.xlsx", include_inactive=True)
    row = _row(out, "账号二")
    assert row["状态"] == "停用"
    assert row["记账笔数"] == 0
    assert row["最近登录"] == "—"                    # 从没换过码，别写成空
    assert row["最近一笔"] == "—"


def test_no_money_columns(tmp_path):
    """金额列不许回来：David 2026-10-20 拍板「他们赚多赚少和我没有关系」。"""
    heads = [h for h, _, _ in ledger.HEADERS]
    for bad in ("收入(元)", "支出(元)", "净额(元)"):
        assert bad not in heads, f"{bad} 应已整列去掉"
    root = _env(tmp_path)
    out = ledger.build(root, tmp_path / "ledger.xlsx")
    ws = load_workbook(out)["账号台账"]
    assert not any("元)" in str(c.value or "") for c in ws[1])


def test_regenerate_keeps_manual_columns(tmp_path):
    """重新生成时按账号名 + 表头保留手填的反馈/卡点/是否付费/备注。"""
    root = _env(tmp_path)
    out = tmp_path / "ledger.xlsx"
    ledger.build(root, out)

    wb = load_workbook(out)
    ws = wb["账号台账"]
    head = [c.value for c in ws[1]]
    fill = {"反馈 / 用得怎么样": "用了一周，说对账比原来快",
            "卡在哪一步": "不知道草稿要不要点确认",
            "他想要什么": "想直接导出给会计",
            "我改了什么": "批量确认",
            "是否付费": "已付 99", "备注": "下月回访"}
    for row in ws.iter_rows(min_row=2):
        if row[0].value == "账号一":
            for k, v in fill.items():
                row[head.index(k)].value = v
    wb.save(out)

    ledger.build(root, out)          # 再生成一次
    row = _row(out, "账号一")
    for k, v in fill.items():
        assert row[k] == v, f"手填列 {k} 被重新生成抹掉了"
    assert row["记账笔数"] == 2       # 自动列仍然是最新的


def test_old_ledger_with_money_columns_still_migrates(tmp_path):
    """换口径（删掉金额列）之后重生成，旧表里已填好的反馈必须带过来、金额列直接忽略。"""
    root = _env(tmp_path)
    out = tmp_path / "ledger.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "账号台账"
    ws.append(["账号", "邀请码", "状态", "创建日", "最近使用",
               "账本笔数", "收入(元)", "支出(元)", "净额(元)", "近30天笔数", "最近一笔",
               "反馈 / 用得怎么样", "是否付费", "备注"])
    ws.append(["账号一", "CL-AAAA-BBBB-CCCC", "在用", "2026-10-19", "2026-10-20",
               2, 10000, 962, 9038, 2, "2026-10-20",
               "旧表里填过的反馈", "", ""])
    wb.save(out)

    ledger.build(root, out)
    row = _row(out, "账号一")
    assert row["反馈 / 用得怎么样"] == "旧表里填过的反馈"
    assert row["最近登录"] == "2026-10-20"           # 新列名从 tenants.json 取，不抄旧表的值
    assert row["邀请码"] in (None, "")               # 旧表的码不回填（新表由 --codes 给）


def test_invite_code_column_comes_from_the_codes_file(tmp_path):
    """邀请码列：服务器上没有明文码，只能由 --codes 指定的本地文件填。"""
    root = _env(tmp_path)
    out = tmp_path / "ledger.xlsx"
    ledger.build(root, out, codes={"账号一": "CL-AAAA-BBBB-CCCC"}, include_inactive=True)
    assert _row(out, "账号一")["邀请码"] == "CL-AAAA-BBBB-CCCC"
    assert _row(out, "账号二")["邀请码"] in (None, "")   # 没给的留空，不编（空串读回来是 None）


def test_rename_and_codes_survive_regeneration(tmp_path):
    """你在表里改过的显示名 + 手填反馈，重新生成时都不许被覆盖（2026-10-20 真机）。

    真机经过：客户把「朋友A」改成真名、并在反馈列写了东西；如果按名字对，重新生成会
    把名字覆盖回「朋友A」、而且那一行的反馈全丢。所以**按邀请码**对，显示名以表里为准。
    """
    root = _env(tmp_path)
    out = tmp_path / "ledger.xlsx"
    ledger.build(root, out, codes={"账号一": "CL-AAAA-BBBB-CCCC"})

    wb = load_workbook(out)
    ws = wb["账号台账"]
    head = [c.value for c in ws[1]]
    for row in ws.iter_rows(min_row=2):
        if row[0].value == "账号一":
            row[head.index("账号")].value = "张三（改过的名字）"
            row[head.index("反馈 / 用得怎么样")].value = "他说能省半小时"
    wb.save(out)

    ledger.build(root, out, codes={"账号一": "CL-AAAA-BBBB-CCCC"})
    row = _row(out, "张三（改过的名字）")
    assert row["反馈 / 用得怎么样"] == "他说能省半小时"
    assert row["邀请码"] == "CL-AAAA-BBBB-CCCC"
    assert row["记账笔数"] == 2          # 自动列照旧刷新


def test_inactive_accounts_are_left_out_by_default(tmp_path):
    """停用账号（自测临时建的）默认不进台账（2026-10-20 真机：客户看到「自检临时1..5」问这是什么）。"""
    root = _env(tmp_path)
    out = tmp_path / "ledger.xlsx"
    ledger.build(root, out)
    wb = load_workbook(out)["账号台账"]
    names = [r[0] for r in wb.iter_rows(min_row=2, values_only=True) if r[0] and r[0] != "合计"]
    assert names == ["账号一"], names
