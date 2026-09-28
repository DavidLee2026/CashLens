"""流式导入（/api/intake/stream）的纯函数单测：批次合并 + 「这个文件读到了什么」。

为什么单测这两个：它们是流式汇总口径的所在地。
合并写错会出现「一个项目被拆成两行」或「金额被重复累加」这类不容易一眼看出的问题；
而 `_file_detail` 是 0 条草稿时唯一告诉用户「为什么没读到」的地方（信息透明度的落点）。
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
_ROOT = Path(__file__).resolve().parents[1]
from app.main import (_file_stages, _intake_stream_events, _match_list_orders,  # noqa: E402
                      _merge_batches, _parse_ts, _tick_note)


def _batch(pid, name, **kw):
    b = {
        "source_folder": name, "project_id": pid, "project_name": name, "project_hint": "",
        "files": [], "errors": [], "draft_count": 0, "identified_total_cents": 0,
        "declared_total_cents": 0, "reconcile": None,
    }
    b.update(kw)
    return b


# ─── 批次合并：一个项目必须合成一行，金额不能重复累加 ──────────

def test_merge_same_project_collapses_to_one_row():
    """同一个项目的多个文件（流式是逐个文件跑的）必须合成一行，金额相加。"""
    a = _batch("project_a", "919 昆明项目", draft_count=1, identified_total_cents=96200,
               files=[{"file": "发票.pdf"}])
    b = _batch("project_a", "919 昆明项目", draft_count=3, identified_total_cents=12862,
               files=[{"file": "打车.xlsx"}], declared_total_cents=38435,
               reconcile={"checked": 10, "matched": 10, "mismatched": [], "unreadable": []})
    out = _merge_batches([a, b])
    assert len(out) == 1
    assert out[0]["draft_count"] == 4
    assert out[0]["identified_total_cents"] == 109062          # 96200 + 12862
    assert out[0]["declared_total_cents"] == 38435
    assert [f["file"] for f in out[0]["files"]] == ["发票.pdf", "打车.xlsx"]


def test_merge_different_projects_stay_separate():
    """不同项目不能混成一行（否则归集会把两个项目的钱加到一起）。"""
    out = _merge_batches([
        _batch("project_a", "919 昆明项目", draft_count=1, identified_total_cents=100),
        _batch("project_b", "另一个项目", draft_count=2, identified_total_cents=200),
    ])
    assert len(out) == 2
    assert {b["project_id"] for b in out} == {"project_a", "project_b"}


def test_merge_accumulates_errors_and_reconcile():
    """错误要全带上（不能因为合并就吞掉），双源核对计数要相加。"""
    a = _batch("project_a", "P", errors=[{"file": "a.txt", "error": "不支持"}],
               reconcile={"checked": 3, "matched": 2, "mismatched": [{"x": 1}], "unreadable": []})
    b = _batch("project_a", "P", errors=[{"file": "b.txt", "error": "读不到金额"}],
               reconcile={"checked": 4, "matched": 4, "mismatched": [], "unreadable": [{"y": 1}]})
    out = _merge_batches([a, b])[0]
    assert [e["file"] for e in out["errors"]] == ["a.txt", "b.txt"]
    assert out["reconcile"]["checked"] == 7
    assert out["reconcile"]["matched"] == 6
    assert len(out["reconcile"]["mismatched"]) == 1
    assert len(out["reconcile"]["unreadable"]) == 1


def test_merge_keeps_first_project_hint():
    out = _merge_batches([
        _batch("project_a", "P", project_hint="已按文件夹名建项目「P」"),
        _batch("project_a", "P", project_hint="另一个提示"),
    ])[0]
    assert out["project_hint"] == "已按文件夹名建项目「P」"


# ─── 每个文件的处理过程（四段：读取 / 识别 / 处理 / 方式）────────

def test_file_stages_sheet_csv_image():
    """四段各自要有实话：读取读了什么、识别认出了什么、处理生成了什么、方式依据什么。"""
    sheet = _file_stages(
        "报销表.xlsx",
        [{"ok": True, "kind": "sheet", "row_count": 11, "embedded_image_count": 10,
          "reconcile": {"checked": 10, "matched": 10}}], [], 3, 12862)
    assert set(sheet) == {"read", "recognized", "processed", "how"}
    assert "11 行" in sheet["read"] and "10 张嵌入图" in sheet["read"]
    assert "10/10" in sheet["recognized"]
    assert "3 条" in sheet["processed"] and "128.62" in sheet["processed"]   # 金额已格式化
    assert "以表内数字为准" in sheet["how"]

    csv = _file_stages(
        "账单.csv",
        [{"ok": True, "kind": "csv", "records": 12, "drafted": 5, "source": "微信"}], [], 5, 1000)
    assert "12 行" in csv["read"] and "微信" in csv["read"]
    assert "5 行" in csv["recognized"]

    img = _file_stages(
        "发票.jpg",
        [{"ok": True, "amount_cents": 96200, "merchant": "上海简乐文化传播有限公司",
          "date": "2026-09-20", "invoice_no": "26537", "cloud_uploaded": True}], [], 1, 96200)
    assert img["read"] == "1 张图片"
    assert "上海简乐" in img["recognized"] and "962.00" in img["recognized"]
    # 2026-09-28 David 定：方式栏只说「这张票怎么处理、结果由谁点头」，
    # 不叙述用哪一档模型、也不叙述供应商（写了会让人以为经营信息被别人看到）。
    assert "待确认清单" in img["how"] and "确认后才入账" in img["how"]
    assert "模型服务商" not in img["how"]


def test_recognition_prompt_asks_for_verbatim_text_lines():
    """引擎层必须真的让模型把聊天/文字截图里的字**原样抄回来**，且不许它替用户挑金额。

    这条护栏的意义：提示词是这个能力的唯一入口 —— 少了一句指令，模型就会像 2026-09-28
    那次一样答「无法识别」，用户看到的是「未生成草稿」，而图里明明写着
    「打车 186.79+闪送 110=296.79」。提示词是纯**追加**字段，票据路径不受影响。
    """
    rp = _ROOT / "engine" / "mcp" / "receipt_mcp.py"
    src = rp.read_text(encoding="utf-8")
    assert "text_lines" in src, "提示词要有一个字段装「图里的文字，一行一条」"
    assert "逐行原样" in src, "必须要求逐行原样抄，不许它合并/改写"
    assert "不要替用户挑金额" in src and "不要算合计" in src, (
        "机器只抄原文；挑哪几笔、算不算合计是用户的事（不碰机器不猜这条线）"
    )
    assert "text_lines 填空数组" in src, "发票/小票那档要明确填空，避免多出一堆噪音行"


def test_recognition_prompt_classifies_page_type_and_reads_refunds():
    """**先判「这张图是什么页面」**（2026-09-28 真机：919 昆明项目九张图暴露的三件事）。

    ① 付款凭证（账单详情 / 支付成功 / 行程已结束）要当票据：amount = **实付**，
       优惠 / 里程 / 时长不是钱（图 3 就是被认成 5 个金额）。
    ② **没付款的不许当付款凭证**：待支付 / 预订单 / 行程预览 → amount 0（钱还没出去）。
    ③ **订单列表不是付款凭证**：同一笔钱的另一个视角，两张都记就是记两遍
       （实弹：「我的订单」里的 78 元 = 另一张账单详情里的 78 元）。
    ④ **退款要单独读出来**，净额由后端减（用户口径：账单 -78.00 + 已退款 ¥6.00 → 入账 -72.00）——
       不许让模型自己算净额，它只要把两个数读准。
    ⑤ 商品说明之外，**页面上出现的商家名 / 小程序名也要抄进 items**
       （否则「爱萝卜打印」这四个字进不了分类，一笔打印费会落进「待确认」）。
    """
    src = (Path(__file__).resolve().parents[1] / "engine" / "mcp" / "receipt_mcp.py").read_text(encoding="utf-8")
    assert '"doc_kind"' in src and '"refund_amount"' in src, "两个字段都要在 JSON 结构里"
    for kind in ("receipt", "payment", "unpaid", "list", "chat"):
        assert f"`{kind}`" in src, f"doc_kind 要有这一档：{kind}"
    assert "实际支付的那个金额" in src, "付款凭证取的是**实付**"
    assert "原价、优惠、节省、折扣、里程、时长、积分、余额、优惠券" in src, (
        "这些都不是金额（图 3 那 5 个「金额」就是优惠/里程/时长）"
    )
    assert "退款金额" in src and "净额由后端减" in src, "退款只读出来，别让模型算净额"
    assert "商家名 / 小程序名 / 服务名" in src, "商家/小程序名要抄进 items，分类才认得出来"
    assert "收款方全称" in src, "商户取收款方"


def test_file_stages_explains_text_amount_candidates():
    """聊天截图那一档的四段文案：说清"不是票面、读到了几个金额、记哪几笔由你决定"。"""
    st = _file_stages("微信图片_1.jpg",
                      [{"ok": True, "amount_cents": 0, "text_amounts": 5, "draft_count": 5,
                        "confidence": 0}], [], 5, 145439)
    assert "不是票面" in st["recognized"] and "5 个金额" in st["recognized"]
    assert "候选草稿" in st["processed"] and "该报哪几笔" in st["processed"]
    assert "由你决定" in st["how"]
    assert "置信度" not in st["recognized"], "这一档不该带「置信度 0」这种噪音"


def test_file_stages_says_transfer_only_when_the_money_really_left():
    """有转账记录时**只列转账**，文案不能说"逐条列进待确认"（那会把订单/行程单也算进去）。"""
    st = _file_stages("…115033_73_19.jpg",
                      [{"ok": True, "amount_cents": 0, "text_amounts": 6, "draft_count": 1,
                        "transfer_amounts": 1, "confidence": 0}], [], 1, 10000)
    assert "不是票面" in st["recognized"] and "1 笔是转账" in st["recognized"]
    assert "只列转账" in st["processed"]
    assert "逐条列进" not in st["recognized"], "有转账时不是逐条列，别让用户以为订单金额也记了"

    # 读到了金额、但没有一笔是"客户付出去的钱" → 如实说没建草稿，别含糊成「未生成草稿」
    none = _file_stages("…115032_72_19.jpg",
                        [{"ok": True, "amount_cents": 0, "text_amounts": 2, "draft_count": 0,
                          "transfer_amounts": 0, "confidence": 0}], [], 0, 0)
    assert "客户付出去的钱" in none["processed"]


def test_order_list_is_matched_to_a_voucher_by_time_not_by_amount():
    """订单列表 ↔ 付款凭证：**以时间为主**配对（用户 2026-09-28 定的口径）。

    用户的原话：「我不知道你是不是只是从 78 元这个数字来判断，但从我的角度是从一张图的
    支付时间，和另一张图片的打印时间来判定」—— 实弹那两张就是
    付款凭证 2026-09-18 **11:56:37** 对上订单列表 **11:55:53**（相差 44 秒）。
    金额只作附注：78 这种数字很容易撞车，但时间差 44 秒几乎不可能是两件事。
    """
    voucher = {"file": "…123051.jpg", "ok": True, "doc_kind": "payment",
               "gross_cents": 7800, "occurred_at": "2026-09-18 11:56:37"}
    listed = {"file": "…123055.jpg", "ok": True, "doc_kind": "list", "orders": [
        {"at": "2026-09-18 12:14:55", "amount": 0.0, "name": "A4文档", "status": "已完成"},
        {"at": "2026-09-18 11:55:53", "amount": 78.0, "name": "A4文档", "status": "已退款"},
    ]}
    out = _match_list_orders([voucher, listed])
    assert len(out) == 1 and out[0]["file"] == "…123055.jpg"
    hit = out[0]["matched"]
    assert [m["at"] for m in hit] == ["2026-09-18 11:55:53"], "时间对得上的那条要配上"
    assert hit[0]["delta_seconds"] == 44 and hit[0]["voucher_file"] == "…123051.jpg"
    assert hit[0]["amount_differs"] is False
    # 另一条 12:14:55 离得远（19 分钟）→ 不配，但**必须列出来**，不能悄悄吞掉
    assert [m["at"] for m in out[0]["unmatched"]] == ["2026-09-18 12:14:55"]


def test_order_list_matching_degrades_safely_without_a_clock():
    """两边都没有时分秒时，**必须同一天且金额一致**才算同一笔。

    否则一张列表里十条同日订单会全都配到同一张凭证上（时间窗形同虚设）。
    """
    day_only = {"file": "v.jpg", "ok": True, "doc_kind": "payment",
                "gross_cents": 7800, "occurred_at": "2026-09-18"}
    listed = {"file": "l.jpg", "ok": True, "doc_kind": "list", "orders": [
        {"at": "2026-09-18", "amount": 78.0}, {"at": "2026-09-18", "amount": 12.5}]}
    out = _match_list_orders([day_only, listed])[0]
    assert len(out["matched"]) == 1 and out["matched"][0]["amount_cents"] == 7800
    assert len(out["unmatched"]) == 1 and out["unmatched"][0]["amount_cents"] == 1250

    # 时间差超过窗口 → 不配（金额一致也不行）
    far = dict(day_only, occurred_at="2026-09-18 14:00:00")
    listed2 = {"file": "l.jpg", "ok": True, "doc_kind": "list",
               "orders": [{"at": "2026-09-18 11:55:53", "amount": 78.0}]}
    assert _match_list_orders([far, listed2])[0]["matched"] == []

    # 时间对得上但金额不一致 → 仍算同一笔，但**标出来让人核对**（不否决）
    odd = dict(day_only, occurred_at="2026-09-18 11:56:37")
    listed3 = {"file": "l.jpg", "ok": True, "doc_kind": "list",
               "orders": [{"at": "2026-09-18 11:55:53", "amount": 72.0}]}
    m = _match_list_orders([odd, listed3])[0]["matched"][0]
    assert m["amount_differs"] is True and m["delta_seconds"] == 44


def test_prompt_reads_full_timestamps_and_order_rows():
    """引擎层要读全时间与订单行 —— 时间正是"这两张是不是同一笔"的判据。"""
    src = (Path(__file__).resolve().parents[1] / "engine" / "mcp" / "receipt_mcp.py").read_text(encoding="utf-8")
    assert '"occurred_at"' in src and '"orders"' in src
    assert "YYYY-MM-DD HH:MM:SS" in src, "要读到时分秒"
    assert "不要自己编时分秒" in src, "读不到就不许编"
    assert "一行一条，不要漏、不要自己算合计" in src, "订单列表要逐行抄，不许算合计"


def test_workbench_summary_reports_the_pairing():
    """界面上要说出「同一笔只记一次」与「哪几笔没配到」——
    不然用户看到列表页"没生成草稿"会以为系统漏了。"""
    src = (Path(__file__).resolve().parents[1] / "frontend" / "app" / "page.tsx").read_text(encoding="utf-8")
    assert "list_matches" in src
    assert "是同一笔" in src and "只按付款凭证记一次" in src
    assert "「${m.file}」（订单列表截图）里有" in src, "要点名是哪张图（用户说「有点莫名」）"
    assert "没配到付款凭证" in src, "没配到的要如实列出来"
    assert "需要的话你自己记一笔" in src, "把决定权交回用户（列表页只有总价）"


def test_file_stages_explains_unpaid_list_and_refund():
    """待支付 / 订单列表 / 全额退款 —— 三种都要说得出来，且不许含糊成「未生成草稿」。

    待支付这条 2026-09-28 按用户口径改过：**有应付金额就列一条候选**（他说
    「一个过去、一个回来的打车记录，车牌号不一样，57.7 是应该被记录的」），
    状态写进备注让人判断；只有读不到金额时才真的不记。
    """
    def st(item, draft_count=0, total=0):
        return _file_stages("x.jpg", [{"ok": True, "amount_cents": 0, "confidence": 0.5, **item}],
                            [], draft_count, total)

    paid_pending = st({"doc_kind": "unpaid", "amount_cents": 5770}, draft_count=1, total=5770)
    assert "待支付" in paid_pending["recognized"] and "去支付" in paid_pending["recognized"]
    assert "候选草稿" in paid_pending["processed"] and "是否已付" in paid_pending["processed"]
    assert "由你确认" in paid_pending["how"], "付没付由用户确认，机器不替他决定"

    no_amount = st({"doc_kind": "unpaid", "no_draft_reason": "unpaid"})
    assert "重新上传一次" in no_amount["processed"], "读不出金额要给可执行的下一步：重传这张"

    # 彻底没读出来（没金额、没文字、没订单行）→ 明说 + 请重传
    blank = st({"no_draft_reason": "unreadable"})
    assert "没读出来" in blank["recognized"]
    assert "重新上传一次" in blank["processed"]
    assert "重传一次最省事" in blank["how"]

    full = st({"no_draft_reason": "fully_refunded", "gross_cents": 7800})
    assert "全额退款" in full["recognized"] and "没有实际支出" in full["processed"]
    assert "78.00" in full["recognized"], "实付金额要摆出来"


def test_file_stages_names_which_image_the_order_list_duplicates():
    """订单列表不记账，但**必须点名和哪张图重复**（用户 2026-09-28：
    「最好能记录下是和哪个图片的信息有重复，方便我排查」）。"""
    got = {"file": "…123055.jpg", "ok": True, "amount_cents": 0, "doc_kind": "list",
           "confidence": 0.5, "no_draft_reason": "list",
           "list_match": {
               "matched": [{"at": "2026-09-18 11:55:53", "amount_cents": 7800,
                            "name": "A4文档(彩色单面)", "status": "已退款",
                            "voucher_file": "微信图片_20260924123051.jpg",
                            "delta_seconds": 44, "amount_differs": False}],
               "unmatched": [{"at": "2026-09-18 12:14:55", "amount_cents": 0,
                              "name": "A4文档(彩色单面)", "status": "已完成"}]}}
    st = _file_stages("微信图片_20260924123055.jpg", [got], [], 0, 0)
    assert "微信图片_20260924123051.jpg" in st["processed"], "点名是哪张图重复"
    assert "44 秒" in st["processed"] and "78.00" in st["processed"]
    assert "微信图片_20260924123051.jpg" in st["how"], "「要记就用那张」也要点名"
    assert "没配到付款凭证的 1 笔" in st["how"], "没配到的照样列出来"
    assert "12:14:55" in st["how"]
    assert "没有建草稿" in st["how"], "没配到的这条是 0 元 → 不该建草稿，也要说清"

    # 没配到、但有金额又不是退款 → **要列成候选**（用户口径：员工先垫付、第二天报销是正常的）
    got2 = dict(got, list_match={
        "matched": [],
        "unmatched": [{"at": "昨天 11:19", "amount_cents": 1120,
                       "name": "闪送-同城最快27分钟送达", "status": "付款成功"}],
        "drafted": [{"amount_cents": 1120, "category": "经营"}]})
    st2 = _file_stages("…123045.jpg", [got2], [], 1, 1120)
    assert "候选草稿" in st2["processed"] and "11.20" in st2["processed"]
    assert "已列进「待确认」" in st2["how"] and "第二天报销是正常的" in st2["how"]
    assert "1 笔与另一张图是同一笔" in st["recognized"]


def test_file_stages_shows_gross_refund_and_net_when_partially_refunded():
    """部分退款：金额已按净额建草稿，四段里必须把 实付 / 退款 / 净额 三个数都摆出来。"""
    st = _file_stages("x.jpg",
                      [{"ok": True, "amount_cents": 7200, "gross_cents": 7800,
                        "refund_cents": 600, "confidence": 0.9}], [], 1, 7200)
    assert "78.00" in st["recognized"] and "6.00" in st["recognized"] and "72.00" in st["recognized"]
    assert "净额" in st["processed"] and "退款" in st["how"]


def test_file_stages_never_mentions_vendor_or_cloud_tier():
    """口径护栏（2026-09-28 立）：界面文案里不得出现供应商 / 上云 / 出本机这类基础设施叙述。

    这条断言是把 David 的决定固化下来 —— 防止以后有人为了「更透明」又加回去。
    必要的告知放在「关于」页与合规文件里，不在逐文件进度里重复。
    """
    cases = [
        ("发票.jpg", [{"ok": True, "amount_cents": 96200, "cloud_uploaded": True}], ".jpg"),
        ("发票.pdf", [{"ok": True, "amount_cents": 96200, "cloud_uploaded": False}], ".pdf"),
        ("报销表.xlsx", [{"ok": True, "kind": "sheet", "row_count": 3}], ".xlsx"),
        ("账单.csv", [{"ok": True, "kind": "csv", "records": 3, "drafted": 1}], ".csv"),
    ]
    banned = ("模型服务商", "发给模型", "上云", "出本机", "不出本机", "第三方")
    for name, got, ext in cases:
        stages = _file_stages(name, got, [], 1, 1000)
        joined = " ".join(str(v) for v in stages.values())
        for word in banned:
            assert word not in joined, f"{name} 的进度文案里出现了「{word}」：{joined}"
        # 心跳里那句「现在在干什么」同样受这条护栏约束
        assert not any(w in _tick_note(name) for w in banned), _tick_note(name)


def test_file_stages_pdf_text_layer_says_local():
    """PDF 有文本层时，方式里说清「本机直读」——这是能力（更快更准），不是数据去向叙述。"""
    st = _file_stages(
        "发票.pdf",
        [{"ok": True, "amount_cents": 96200, "date": "2026-09-20", "cloud_uploaded": False}], [], 1, 96200)
    assert st["read"] == "1 个 PDF"
    assert "本机直读" in st["how"] and "待确认清单" in st["how"]


def test_file_stages_never_leaks_internal_values():
    """内部值 unknown 不能直接甩给用户：信息透明 ≠ 甩术语。"""
    csv = _file_stages(
        "账单.csv",
        [{"ok": True, "kind": "csv", "records": 2, "drafted": 0, "source": "unknown"}], [], 0, 0)
    joined = " ".join(csv.values())
    assert "unknown" not in joined and "未识别" in joined
    assert csv["processed"] == "未生成草稿"
    assert csv["recognized"] == "没有识别到可入账的收支"


def test_file_stages_fills_all_four_for_unsupported_file():
    """不支持的类型也要把四段填满，原因写在「方式」里 —— 而不是只丢一句「没有记录」。"""
    # 不支持的类型：files 为空，原因只出现在 batch errors 里
    st = _file_stages("说明.txt", [],
                      [{"file": "说明.txt", "error": "暂不支持的文件类型（unknown）"}], 0, 0)
    assert st["read"] == "—" and st["recognized"] == "—"
    assert st["processed"] == "未生成草稿"
    assert "暂不支持这种文件类型" in st["how"]
    assert "unknown" not in " ".join(st.values())          # 内部值不甩给用户
    assert "已跳过" in st["how"] and "不影响其他文件" in st["how"]


# ─── 心跳：慢文件期间不能让这条流长时间静默 ──────────────────────
# 起因（2026-09-25 真机实测）：一张报销表跑了 239 秒、期间零字节输出，
# 连接被按空闲超时掐断，用户只看到「导入失败：network error」，而后端其实还在正常跑。

def _drain(resp):
    """把流式响应的 NDJSON 逐行解出来（Starlette 会把同步生成器包成异步迭代器）。"""
    import asyncio

    async def go():
        out = []
        async for chunk in resp.body_iterator:
            text = chunk.decode() if isinstance(chunk, bytes) else chunk
            for line in text.splitlines():
                if line.strip():
                    out.append(json.loads(line))
        return out

    return asyncio.run(go())


def test_stream_pushes_heartbeat_while_a_file_is_slow(monkeypatch):
    """慢文件必须边跑边推心跳（带真实已等秒数），否则连接静默太久会被掐断。"""
    import app.main as main

    def slow_ingest(data_dir, files, project_ref=None, reconcile=True, progress=None):
        # 桩要接住 real 签名（2026-09-28 加了 reconcile / progress）。
        # 顺便模拟一次"核到第 3/10 张"，验证子进度能进心跳 ——
        # 一张 10 行的报表跑三分钟却只显示一句笼统的话，用户会以为卡死了。
        if progress:
            progress(3, 10, "核对表内嵌入图")
        time.sleep(0.08)
        return {"batches": []}

    monkeypatch.setattr(main.intake, "ingest", slow_ingest)
    monkeypatch.setattr(main, "_INTAKE_TICK_SECONDS", 0.02)
    body = main.IntakeIn(files=[main.IntakeFileIn(
        name="云南报销.xlsx", rel_path="p/云南报销.xlsx", content_b64="")])

    events = _drain(main.intake_stream(body))
    stages = [e["stage"] for e in events]

    assert stages[0] == "start" and stages[-1] == "all_done"
    ticks = [e for e in events if e["stage"] == "tick"]
    assert len(ticks) >= 2, f"心跳太少，流会长时间静默：{stages}"
    assert all(e["index"] == 1 and e["file"] == "云南报销.xlsx" for e in ticks)
    assert ticks[0]["elapsed"] >= 0 and ticks[-1]["elapsed"] >= ticks[0]["elapsed"]
    assert "已等" not in ticks[0]["note"] and "报销表" in ticks[0]["note"]
    # 子进度要如实出现（后端给了就显示，不编百分比）
    assert any("3/10" in e["note"] for e in ticks), [e["note"] for e in ticks]
    # 心跳也不许泄露内部值（与四段反馈同一口径）
    assert "unknown" not in " ".join(e["note"] for e in ticks)


def test_stream_survives_every_file_failing(monkeypatch):
    """单个文件炸掉不能把整批带崩，也不能让这条流半路断掉（要照常收到 all_done）。"""
    import app.main as main

    def boom(data_dir, files, project_ref=None, reconcile=True, progress=None):
        raise ValueError("模型服务超时")

    monkeypatch.setattr(main.intake, "ingest", boom)
    monkeypatch.setattr(main, "_INTAKE_TICK_SECONDS", 0.02)
    body = main.IntakeIn(files=[
        main.IntakeFileIn(name="a.jpg", rel_path="p/a.jpg", content_b64=""),
        main.IntakeFileIn(name="b.jpg", rel_path="p/b.jpg", content_b64=""),
    ])

    events = _drain(main.intake_stream(body))
    failed = [e for e in events if e["stage"] == "file_failed"]
    assert [e["index"] for e in failed] == [1, 2]
    assert "模型服务超时" in failed[0]["error"]
    assert events[-1]["stage"] == "all_done"          # 两个都失败，收尾照样发出来


def test_retry_files_are_listed_for_unreadable_and_failed_files():
    """读不出来的图要在**批次汇总里点名**，让用户知道重传哪几张（2026-09-28 用户建议）。"""
    from app.main import _retry_files

    files = [
        {"file": "读出来的.jpg", "ok": True, "amount_cents": 4050},
        {"file": "没读出来.jpg", "ok": True, "amount_cents": 0, "no_draft_reason": "unreadable"},
        {"file": "识别失败.jpg", "ok": False, "error": "模型服务超时"},
        {"file": "订单列表.jpg", "ok": True, "amount_cents": 0, "no_draft_reason": "list"},
        {"file": "待支付.jpg", "ok": True, "amount_cents": 5770, "doc_kind": "unpaid"},
    ]
    assert _retry_files(files) == ["没读出来.jpg", "识别失败.jpg"], (
        "要重传的是「没读出来」和「识别失败」这两类；订单列表/待支付不需要重传"
    )


def test_stream_sends_file_update_naming_the_voucher(tmp_path, monkeypatch):
    """订单列表那一行要在**收尾时补上「和哪张图重复」**，并且是原地更新（不新增行、不换位置）。

    用户 2026-09-28：「最好能记录下是和哪个图片的信息有重复，方便我排查」。
    配对要等整批读完（流式逐个文件跑），所以逐文件那行发出去之后再补发一次 `file_update`。
    """
    import app.main as main

    def fake_ingest(data_dir, files, project_ref=None, reconcile=True, progress=None):
        n = files[0]["name"]
        if n == "v.jpg":
            item = {"file": n, "ok": True, "amount_cents": 7800, "gross_cents": 7800,
                    "doc_kind": "payment", "occurred_at": "2026-09-18 11:56:37"}
        else:
            item = {"file": n, "ok": True, "amount_cents": 0, "doc_kind": "list",
                    "no_draft_reason": "list",
                    "orders": [{"at": "2026-09-18 11:55:53", "amount": 78.0, "name": "A4文档"}]}
        return {"batches": [{"project_name": "p", "draft_count": 0,
                             "identified_total_cents": 0, "errors": [], "files": [item]}]}

    monkeypatch.setattr(main.intake, "ingest", fake_ingest)
    files = [{"name": "l.jpg", "rel_path": "p/l.jpg", "content": b"x"},
             {"name": "v.jpg", "rel_path": "p/v.jpg", "content": b"x"}]
    events = [json.loads(line) for line in _intake_stream_events(files, None, False, tmp_path)]

    upd = [e for e in events if e["stage"] == "file_update"]
    assert len(upd) == 1, f"只该补发一次（订单列表那张）：{upd}"
    assert upd[0]["index"] == 1 and upd[0]["file"] == "l.jpg", "要更新的是订单列表那一行"
    assert "v.jpg" in upd[0]["stages"]["processed"], "必须点名是哪张图重复"
    assert "44 秒" in upd[0]["stages"]["processed"], "时间差也要写出来"
    assert "78.00" in upd[0]["stages"]["processed"]
    # 不许新增行：文件级事件只有「两个 file_done + 一次 file_update」
    assert [e["stage"] for e in events if e["stage"].startswith("file_")] == \
        ["file_done", "file_done", "file_update"]


def test_stream_stops_remaining_files_when_the_client_disconnects(tmp_path, monkeypatch):
    """**取消导入 = 客户端断开连接**：后面的文件一个都不许再开始（生成器是拉驱动的）。

    用户 2026-09-28 的要求：「正在导入信息要增加一个取消按钮，万一导入错了，还得等
    导入完成了才能结束」。界面上是 AbortController.abort()，服务端靠的正是这条性质 ——
    只有消费方再要下一行，循环才往前走。这条测试就是那个承诺的钉子。
    """
    import app.main as main

    calls: list[str] = []

    def fake_ingest(data_dir, files, project_ref=None, reconcile=True, progress=None):
        calls.append(files[0]["name"])
        return {"batches": [{"project_name": "p", "draft_count": 0,
                             "identified_total_cents": 0, "errors": [],
                             "files": [{"file": files[0]["name"], "ok": True,
                                        "amount_cents": 0}]}]}

    monkeypatch.setattr(main.intake, "ingest", fake_ingest)
    files = [{"name": f"{i}.jpg", "rel_path": f"x/{i}.jpg", "content": b"x"} for i in (1, 2, 3)]
    gen = _intake_stream_events(files, None, False, tmp_path)

    first = json.loads(next(gen))
    assert first["stage"] == "start" and first["total"] == 3
    assert calls == [], "还没人要下一行，就不该开始处理"

    json.loads(next(gen))                 # 这一拉才真的开始处理第 1 个文件
    assert calls == ["1.jpg"]

    gen.close()                           # ≈ 浏览器 abort：不再向它要下一行
    assert calls == ["1.jpg"], "断开之后不许再碰后面的文件（否则取消就是假的）"


def test_tick_note_says_what_is_really_happening():
    """心跳那句「在干什么」按文件类型说真话，且不甩内部值。"""
    from app.main import _tick_note

    assert "报销表" in _tick_note("云南报销.xlsx")
    assert "报销表" in _tick_note("账单.CSV")          # 大小写不敏感
    assert "PDF" in _tick_note("发票.pdf")
    assert "图片" in _tick_note("微信图片_20260924123115.JPG")
    fallback = _tick_note("说明.unknown")
    assert "unknown" not in fallback and "正在识别" in fallback


# ─── 前端调用链护栏（2026-09-28 真机 bug 后补）────────────────────

def test_workbench_intake_request_carries_current_project():
    """工作台调 /api/intake/stream 时必须带上当前选中项目。

    这条护栏为什么存在：2026-09-28 David 在真机上选中「919 昆明项目」后拖文件夹，
    系统却又按文件夹名新建了一个项目，草稿全落到了新项目上。根因**不在后端**——
    `intake.ingest` 早就实现了「显式指定 > 文件夹名 > 未归」并且有单测
    （见 test_intake.py::test_ingest_uses_explicit_project_over_folder），
    是**前端请求体里从来没传 project 这个字段**，于是那条正确路径根本没被走到。
    服务层测试全绿而用户路径是坏的 —— 所以这里对调用点本身下一道护栏。
    """
    page = (Path(__file__).resolve().parents[1] / "frontend" / "app" / "page.tsx").read_text(encoding="utf-8")
    # 用 fetch 调用点定位：字符串 "/api/intake/stream" 在文件里出现多次（文件头的注释里也有）
    idx = page.find('fetch("/api/intake/stream"')
    assert idx != -1, "找不到导入端点的调用点"
    # 取调用点后面一小段（请求体就在紧邻的几行里），确认带了 project
    window = page[idx: idx + 700]
    assert "JSON.stringify" in window, "导入请求体不是 JSON.stringify？"
    assert "project: activeProject" in window, (
        "导入请求没有带当前选中项目 —— 用户选了项目也会被文件夹名另建一个项目盖掉"
    )


def test_workbench_chat_request_carries_the_local_user_name():
    """对话请求必须带上本机登录名 —— 否则助手答不出「我是谁」。

    起因（2026-09-28 真机）：用户填了登录名，问「你知道我是谁吗」，助手答不知道；
    根因是名字只存在 localStorage，`/api/chat` 的请求体里从来没有它。
    这条钉在**调用点**上（与"导入必须带 project"同一类坑：能力实现了，但调用时没传）。
    """
    src = (Path(__file__).resolve().parents[1] / "frontend" / "app" / "page.tsx").read_text(
        encoding="utf-8")
    # 调用点用的是 j<> 包装（不是裸 fetch），按实际写法取那段
    i = src.index('"/api/chat"')
    call = src[i:i + 420]
    assert "user" in call, "对话请求体要带 user（本机登录名）"
    assert "user }" in call or "user," in call or "user:" in call, "要真的把它放进请求体"


def test_workbench_intake_request_carries_the_reconcile_switch():
    """导入请求必须能把「核对表内发票」这个开关带上。

    起因（2026-09-28 用户）：一张 10 行的报销表导入跑了 3 分钟，时间全在逐张核对嵌入图。
    表内数字本来就是权威、核对不是入账必需 —— 所以界面上给了开关，**调用点必须真的传它**，
    否则又是一个"能力实现了但用户走不到"（与 project 那次同型）。
    """
    src = (Path(__file__).resolve().parents[1] / "frontend" / "app" / "page.tsx").read_text(
        encoding="utf-8")
    i = src.index('"/api/intake/stream"')
    body = src[i:i + 700]
    assert "reconcile" in body, "导入请求体要带 reconcile"
    assert "核对表内发票" in src, "界面上要有「核对表内发票」开关"
    # ⚠️ 别用 split("reconcile") 定位 —— 这个文件里 reconcile 早就出现在批次类型里了
    # 默认 = **不核对**（用户 2026-09-28 定）：核对是整条链路最慢的一步（10 张图串行 3 分钟），
    # 而表内数字本来就是权威；想查「表和票对不对得上」的用户自己勾。
    assert "const [reconcile, setReconcile] = useState(false)" in src, (
        "开关默认应当是不核对（用户明确要的默认；核对改成按需）"
    )
    # 关掉 ≠ 核对过，界面上必须如实说「本次未核对」，不能出现「0/0 张一致」这种假汇报
    assert "reconcile_skipped" in src and "本次未核对" in src, (
        "跳过核对时要在结果里如实说明，并把它与「核对了但一张都没对」区分开"
    )
    assert "b.reconcile.checked > 0" in src, "只有真核对过（checked > 0）才显示「双源核对 X/Y」"
