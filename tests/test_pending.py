"""待确认草稿（识别→确认→入账）单测 —— 用临时目录，不碰真实数据。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services import pending  # noqa: E402


def test_create_list_decline(tmp_path):
    d = pending.create(tmp_path, direction="expense", amount_cents=2800, category="交通",
                       channel="manual", note="打车报销垫付")
    assert d["id"]
    assert len(pending.list_all(tmp_path)) == 1
    assert pending.get(tmp_path, d["id"])["amount_cents"] == 2800
    assert pending.decline(tmp_path, d["id"]) is True
    assert pending.list_all(tmp_path) == []
    assert pending.decline(tmp_path, "不存在") is False


def test_accept_invokes_append(tmp_path):
    d = pending.create(tmp_path, direction="income", amount_cents=29900, category="接单",
                       channel="alipay", note="二九九套餐", project="project_a")
    captured = {}

    def append_fn(direction, amount_cents, category, channel, note, counterparty, project):
        captured.update(direction=direction, amount=amount_cents, category=category,
                        channel=channel, project=project)
        return {"event_id": "x"}, True

    out = pending.accept(tmp_path, d["id"], append_fn)
    assert out["appended"] is True
    assert captured["amount"] == 29900 and captured["direction"] == "income"
    assert captured["project"] == "project_a"  # 项目维度透传到写账本回调
    assert pending.list_all(tmp_path) == []  # 弹出后草稿消失


def test_accept_tolerates_legacy_draft_without_project(tmp_path):
    """9/24 之前生成的旧草稿没有 project 字段，accept 不能因此报错。"""
    import json
    fp = tmp_path / "pending.json"
    fp.write_text(json.dumps([{
        "id": "legacy1", "direction": "expense", "amount_cents": 4713, "category": "交通",
        "channel": "manual", "note": "旧草稿", "counterparty": "",
        "created": "2026-09-01T10:00:00",
    }], ensure_ascii=False), encoding="utf-8")

    captured = {}

    def append_fn(direction, amount_cents, category, channel, note, counterparty, project):
        captured.update(project=project)
        return {"event_id": "y"}, True

    out = pending.accept(tmp_path, "legacy1", append_fn)
    assert out["appended"] is True
    assert captured["project"] == ""


# ─── 端点层：草稿列表的项目名在读取时解析（2026-09-28 补）────────────

def test_pending_endpoint_resolves_project_name(tmp_path, monkeypatch):
    """/api/pending 必须返回**当前**项目名，而不是草稿生成时的快照、也不是 None。

    起因：真机上 17 条草稿在界面上看不到归哪个项目 —— 草稿记录里只存 `project` id，
    连 `project_name` 字段都没有。正确的做法是读取时从映射表解析（与账本同一条纪律：
    只存稳定 id，显示名走 projects.json），这样改名立刻生效、删项目如实回落。
    """
    from fastapi.testclient import TestClient

    from app import main
    from app.services import projects

    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    pid = projects.create(tmp_path, "919 昆明项目")["id"]
    pending.create(tmp_path, direction="expense", amount_cents=4050, category="购物",
                   channel="receipt", note="便利店", project=pid)
    client = TestClient(main.app)

    row = client.get("/api/pending").json()["pending"][0]
    assert row["project"] == pid, "草稿里存的应该是稳定 id"
    assert row["project_name"] == "919 昆明项目"

    # 改名之后草稿显示的名字要跟着变（不能在草稿里存名字快照）
    projects.rename(tmp_path, pid, "919 昆明差旅")
    assert client.get("/api/pending").json()["pending"][0]["project_name"] == "919 昆明差旅"

    # 项目被删掉 → 如实回落「未归项目」，与账本事件的回落口径保持一致
    projects.mark_deleted(tmp_path, pid)
    assert client.get("/api/pending").json()["pending"][0]["project_name"] == projects.UNASSIGNED_LABEL


def test_pending_endpoint_keeps_panel_fields(tmp_path, monkeypatch):
    """左栏「待确认」面板要显示金额/分类/日期/商户，端点必须原样带出来。

    `date` 由识别通道写入草稿（intake 在 create 之后补 `date` / `source` / `image`），
    所以这里直接落一份带 date 的草稿文件，验证端点原样透传、不做裁剪。
    """
    import json

    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    (tmp_path / "pending.json").write_text(json.dumps([{
        "id": "abc123", "direction": "expense", "amount_cents": 4050, "category": "购物",
        "channel": "receipt", "note": "", "counterparty": "云南强林乐家连锁便利店有限公司",
        "project": "", "created": "2026-09-28T11:19:14", "date": "2026-09-18",
        "source": "识别", "image": "微信图片.jpg",
    }], ensure_ascii=False), encoding="utf-8")

    row = TestClient(main.app).get("/api/pending").json()["pending"][0]
    assert row["amount_cents"] == 4050 and row["category"] == "购物"
    assert row["date"] == "2026-09-18"
    assert row["counterparty"] == "云南强林乐家连锁便利店有限公司"
    assert row["project_name"] == "未归项目", "未归项目的草稿也要有可显示的名字"


# ---------------------------------------------------------------- 界面护栏
# 读 page.tsx 源码下断言（同 test_intake_stream.py 的调用点护栏做法）：
# 这两条锁的是"别人以后容易改回去"的行为，不是实现细节。

def _page_tsx() -> str:
    return (Path(__file__).resolve().parents[1] / "frontend" / "app" / "page.tsx").read_text(
        encoding="utf-8")


def test_batch_confirm_stays_behind_an_explicit_second_click():
    """批量确认必须是「用户点了才出现、还要再点一次」——不得做成一键直入账。

    合规红线是「用户确认环节不得为体验而取消」。批量确认只是把 17 次点击并成 2 次，
    仍然是用户显式发起的；这条测试锁住：
      ① 存在批量入口「全部确认」，且它只是把确认条打开（setBatchAsk("accept")）；
      ② 确认条里必须先摆出笔数与金额，再给「确认全部 N 笔」；
      ③ 批量走的是同一个 accept 端点（不新增接口，端点数不变）；
      ④ 中途失败要如实报告已入账几笔，不许假装全成功。
    """
    src = _page_tsx()

    assert "全部确认" in src, "左栏待确认面板应有批量入口"
    assert 'setBatchAsk("accept")' in src, "批量入口只能打开确认条，不能直接入账"
    # 确认条里要摆数字再让用户点
    assert "batchExpense" in src and "batchIncome" in src, "确认条要先摆出支出/收入合计"
    assert "确认全部 {pendingList.length} 笔" in src, "确认条里要有明确的「确认全部 N 笔」按钮"
    assert "只确认无提示的" in src, "带「需核对」提示的笔应可单独排除"
    # 走同一个端点
    assert "`/api/pending/${p.id}/accept`" in src, "批量确认应复用单笔 accept 端点"
    # 失败要如实
    assert "批量确认中断：已入账" in src, "中途失败必须报告已入账笔数"
    # 绝不能出现「进页面就自动入账」
    assert "useEffect(() => {\n    actBatch" not in src and "actBatch(pendingList.map" not in src.split("useEffect")[0], \
        "不得在加载/副作用里自动批量入账"


def test_pending_panel_labels_fourth_field_and_missing_date():
    """第四段与缺失日期必须如实标注，不能让人猜。

    起因（2026-09-28 真机）：界面显示「−¥17.88 餐饮 · 2026-09-28 · 后端」，
    用户当场问「这个后端是什么意思」。查证：那是**报销表里一个归属列（部门/模块）
    被写进了 counterparty**，界面上既不标注含义、又把导入日当成交易日显示，
    于是看起来像"商户叫后端"。本条锁住两件事：
      ① 第四段按来源标注：报销表 → 「表内归属」，其余 → 「商户」；
      ② 认不出日期时如实显示「日期待补」，不拿 created（导入日）冒充交易日
         （created 只放进 title 里说明"导入于"）。
    """
    src = _page_tsx()

    assert "表内归属" in src and "商户" in src, "第四段要按来源标注，不能光甩一个值"
    assert 'p.source === "报销表"' in src, "来源为报销表时不得标成「商户」"
    assert "日期待补" in src, "认不出交易日要如实说「日期待补」"
    assert "p.date || (p.created" not in src, "不得再拿 created（导入日）当交易日显示"
    assert "导入于" in src, "created 应只作为 title 里的说明出现"
    # 需核对提示要看得见（批量确认时用户据此决定是否排除）
    assert "需核对" in src and "needsReview" in src


def test_batch_decline_asks_first_and_never_touches_the_ledger():
    """「全部取消」同样要先问一句，并且绝不能碰账本。

    场景（用户提的）：拖错了文件夹，17 笔全进来了，一笔一笔点「不要」太费劲。
    但「丢弃」对用户是不可逆的（草稿删掉就得重新导入），所以同样必须二次点击；
    确认条里还要如实说明「账本不受影响」—— 这些草稿本来就没入账，
    所以「全部取消」不产生任何账本记录，也不存在"能不能撤回"的问题。
    """
    src = _page_tsx()

    assert "全部取消" in src, "待确认面板应有批量丢弃入口"
    assert 'setBatchAsk("decline")' in src, "批量丢弃入口只能打开确认条，不能直接丢"
    assert "确认全部不要" in src, "确认条里要有明确的「确认全部不要」按钮"
    assert "账本不受影响" in src, "要如实说明丢弃草稿不影响账本"
    assert "`/api/pending/${p.id}/decline`" in src, "批量丢弃应复用单笔 decline 端点"
    assert "批量取消失败：已丢弃" in src, "中途失败必须报告已丢弃笔数"
    # 两个入口互斥：开了确认条就不再露出入口，避免"点了一下不知道会发生什么"
    assert 'batchAsk === "accept"' in src and 'batchAsk === "decline"' in src
