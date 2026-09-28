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
from app.main import _file_stages, _merge_batches, _tick_note  # noqa: E402


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


def test_file_stages_explains_text_amount_candidates():
    """聊天截图那一档的四段文案：说清"不是票面、读到了几个金额、记哪几笔由你决定"。"""
    st = _file_stages("微信图片_1.jpg",
                      [{"ok": True, "amount_cents": 0, "text_amounts": 5, "draft_count": 5,
                        "confidence": 0}], [], 5, 145439)
    assert "不是票面" in st["recognized"] and "5 个金额" in st["recognized"]
    assert "候选草稿" in st["processed"] and "只挑该报的" in st["processed"]
    assert "由你决定" in st["how"]
    assert "置信度" not in st["recognized"], "这一档不该带「置信度 0」这种噪音"


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
