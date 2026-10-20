"""「每行一个条目 + 行尾金额」的清单结构（2026-10-20 立）

## 起因（真机，两次发同一份清单）

客户把 16 行成本清单发进来（`3个n6三脚架  100` … `灯光1200w 四套  1000`，末尾三行没标价的
配置项，最后一句「帮我算个总价」）：

- **第一次**：模型只挑了其中一行（「10块v口电池」→ ¥10.00），其余 12 行全丢；
- **第二次**：他补一句「**每一行空格之后的数字是需要合计的费用 请帮我合计**」，模型才拆对 12 笔。

也就是说：这个结构原来**靠用户解释才成立**。报价单 / 成本清单里「名称 + 行尾数字」是最常见的
写法，没有理由让人先教系统一遍。所以它变成后端的确定性解析，并且**排在模型前面**。

这里锁四件事：
1. 逐行一笔、行尾数字是金额、合计由代码加（用户那句话不再是前提）；
2. 行尾不是数字的行不当金额（`音频线4根` / `10块v口电池` 这类配置项）；
3. 算式行（`打车186.79+闪送110=296.79`）不归清单 —— 那是 v3.8 的算式逻辑，别抢；
4. 文字路径与图片路径**同一套解析**（图片识别出 text_lines 后也走它）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services import expense_list, pending  # noqa: E402

# 真机那份清单（原样，含三行没标价的配置项和最后那句要求）
_LIST = """3个n6三脚架  100
大监1  80
Fx3+16-35 一套配背夹  250
一套调音台配4手持话筒 500
1台电视机和移动支架配1个电小二 150
4个摄像  4000
2个摄像助理  1000
1个场务  400
一个音控师带笔记本 1000
音频助理一位  500
灯光助理2位  1000
灯光1200w 四套  1000
准备6根40米的sdi信号线8根短的信号线和10块v口电池
音频线4根
hdmi线根据电视来配
帮我算个总价"""


# ─── 解析层（确定性，不依赖模型）────────────────────────

def test_every_line_is_one_item_and_the_total_is_computed_by_code():
    out = expense_list.parse(_LIST)
    assert out["is_list"] is True
    assert len(out["items"]) == 12
    assert out["total_cents"] == 998000           # ¥9,980.00 —— 真机确认框里那个数
    assert [i["amount_cents"] for i in out["items"]][:3] == [10000, 8000, 25000]
    assert out["items"][0]["label"] == "3个n6三脚架"
    assert out["items"][-1]["label"] == "灯光1200w 四套"


def test_lines_without_a_price_are_not_guessed():
    """`音频线4根` / `10块v口电池` 这类配置项没有价格：不猜、不编，单独说出来。"""
    out = expense_list.parse(_LIST)
    joined = " ".join(out["skipped"])
    assert "音频线4根" in joined
    assert "sdi信号线" in joined
    assert all("v口电池" not in i["label"] for i in out["items"])
    # 要求句不算"没标金额的条目"
    assert all("帮我算个总价" not in s for s in out["skipped"])


def test_items_are_classified_by_label_and_by_list_context():
    out = expense_list.parse(_LIST)
    assert {i["category"] for i in out["items"]} == {"经营"}
    # 词表认得的那几条标 label，剩下的标 context（回复里会明说"整体按项目成本归类"）
    assert out["context_filled"] >= 3
    assert any(i["category_from"] == "label" for i in out["items"])


def test_formula_lines_are_not_a_list():
    """「打车186.79+闪送110=296.79」是**算式**，归 v3.8 的算式逻辑；
    当成清单会把整行读成一笔 296.79、明细全丢（这条是回归实测抓到的）。"""
    out = expense_list.parse("""打车186.79+闪送110=296.79
午饭晚饭水一共549.6
296.79+549.6=846.39""")
    assert out["is_list"] is False


def test_one_or_two_amounts_are_not_a_list():
    """一句话里顺带一个数、或两句记账，不许被清单解析抢走。"""
    assert expense_list.parse("打车 28")["is_list"] is False
    assert expense_list.parse("打车 28\n午饭 45")["is_list"] is False
    assert expense_list.parse("阿明今天 8 小时，时薪 50")["is_list"] is False


def test_total_line_is_not_an_item():
    """清单里自己写的「合计 9980」是合计行，收进去就是重复记账。"""
    out = expense_list.parse("设备 100\n人工 200\n场地 300\n合计 600")
    assert out["is_list"] is True
    assert [i["amount_cents"] for i in out["items"]] == [10000, 20000, 30000]
    assert out["total_cents"] == 60000


def test_dates_and_times_are_not_amounts():
    out = expense_list.parse("2026-10-20\n12:30\n打车 28\n午饭 45\n咖啡 30")
    assert [i["amount_cents"] for i in out["items"]] == [2800, 4500, 3000]


def test_income_hints_flip_the_direction():
    """清单也可能是"客户回款清单"：只在条目名里出现收入词时才改方向，不猜。"""
    out = expense_list.parse("A客户 收到 3000\nB客户 收到 5000\nC客户 收到 2000")
    assert [i["direction"] for i in out["items"]] == ["income", "income", "income"]
    assert out["income_cents"] == 1000000 and out["expense_cents"] == 0


# ─── 端点级：文字清单（不配模型也要能拆对）────────────────

def _client(tmp_path, monkeypatch, configured=False):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("CASH_DATA_DIR", str(tmp_path))
    from app import main
    monkeypatch.setattr(main.llm_skill, "is_configured", lambda: configured)
    return TestClient(main.app), main


def test_chat_understands_the_list_without_the_user_explaining_it(tmp_path, monkeypatch):
    """**核心回归**：只发清单 + 一句「帮我算个总价」，就要出 12 笔 ¥9,980.00。

    不配模型 —— 这条逻辑不依赖它（原来靠模型开窍，才需要用户补那句说明）。
    """
    client, _ = _client(tmp_path, monkeypatch, configured=False)
    body = client.post("/api/chat", json={"text": _LIST}).json()

    assert len(body["pending"]) == 12
    assert sum(d["amount_cents"] for d in body["pending"]) == 998000
    assert "¥9,980.00" in body["text"]
    assert "8980" not in body["text"]
    assert "音频线4根" in body["text"], "没标金额的行要点名说出来，不能悄悄丢掉"
    assert body["list"]["total_cents"] == 998000
    # 只写草稿，不入账
    assert len(pending.list_all(tmp_path)) == 12
    assert not (tmp_path / "finance_events.jsonl").exists()


def test_chat_list_works_even_when_a_model_is_configured(tmp_path, monkeypatch):
    """配了模型也走确定性清单解析 —— 金额与合计不交给模型（顺序：确定性 > 模型）。"""
    client, main = _client(tmp_path, monkeypatch, configured=True)
    called = {"n": 0}

    def boom(*a, **kw):
        called["n"] += 1
        raise AssertionError("清单不该再交给模型解析")

    monkeypatch.setattr(main.llm_skill, "parse_accounting", boom)
    body = client.post("/api/chat", json={"text": _LIST}).json()
    assert called["n"] == 0
    assert len(body["pending"]) == 12


def test_two_line_records_still_go_through_the_normal_path(tmp_path, monkeypatch):
    """回归：两行普通记账不该被清单逻辑抢走（清单门槛是 3 行起）。"""
    client, _ = _client(tmp_path, monkeypatch, configured=False)
    body = client.post("/api/chat", json={"text": "打车 28"}).json()
    assert body["pending"] and body["pending"][0]["amount_cents"] == 2800
    assert "list" not in body


# ─── 图片路径：同一套解析 ────────────────────────────

def test_screenshot_of_a_list_is_parsed_as_a_list(tmp_path, monkeypatch, monkeypatch_recognize):
    """截图：识别出的 text_lines 直接进同一套清单解析，产出干净的「清单行」草稿。

    真机上模型的 doc_kind 不稳定（同一张图一次 chat、一次 list），所以这条逻辑
    **不看 doc_kind**，只看行结构。
    """
    from app.services import intake

    monkeypatch_recognize({
        "doc_kind": "chat", "amount": 0, "text_lines": _LIST.splitlines(),
        "orders": [], "items": [], "note": "不是票据，是聊天/文字截图",
    })
    intake.ingest(tmp_path, [{"name": "清单.png", "rel_path": "清单.png", "content": b"x"}])
    drafts = pending.list_all(tmp_path)
    assert len(drafts) == 12
    assert sum(d["amount_cents"] for d in drafts) == 998000
    assert all(d["note"].startswith("清单行：") for d in drafts)
    assert all(d["category"] == "经营" for d in drafts)
    assert not any("订单列表" in d["note"] for d in drafts), "别把成本清单说成订单列表"


@pytest.fixture
def monkeypatch_recognize(monkeypatch):
    """把引擎层识别换成固定结果（不打网络、不依赖真图）。"""
    from app.services import intake

    def _install(payload):
        monkeypatch.setattr(intake, "recognize_file", lambda path: dict(payload))
    return _install


def test_image_stage_copy_says_it_is_a_list():
    """导入那四段文案：不许把清单说成票面，也不许含糊成「未生成草稿」。"""
    from app import main
    item = {"file": "清单.png", "ok": True, "doc_kind": "expense_list",
            "amount_cents": 0, "list_lines": 12, "skipped_lines": 3,
            "draft_count": 12}
    st = main._file_stages("清单.png", [item], [], 12, 998000)
    assert "费用清单" in st["recognized"] and "12" in st["recognized"]
    assert "9980.00" in st["processed"]
    assert "3 行没标金额" in st["how"]
