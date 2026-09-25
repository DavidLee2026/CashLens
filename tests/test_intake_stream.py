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
from app.main import _file_stages, _merge_batches  # noqa: E402


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
    assert "模型服务商" in img["how"]        # 数据去向必须如实说


def test_file_stages_pdf_text_layer_says_local():
    """PDF 有文本层本机直读时，方式里要说清「没发给模型」——这是隐私口径的一部分。"""
    st = _file_stages(
        "发票.pdf",
        [{"ok": True, "amount_cents": 96200, "date": "2026-09-20", "cloud_uploaded": False}], [], 1, 96200)
    assert st["read"] == "1 个 PDF"
    assert "本机读文本层" in st["how"] and "未发给模型" in st["how"]


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

    def slow_ingest(data_dir, files, project_ref=None):
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
    # 心跳也不许泄露内部值（与四段反馈同一口径）
    assert "unknown" not in " ".join(e["note"] for e in ticks)


def test_stream_survives_every_file_failing(monkeypatch):
    """单个文件炸掉不能把整批带崩，也不能让这条流半路断掉（要照常收到 all_done）。"""
    import app.main as main

    def boom(data_dir, files, project_ref=None):
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
