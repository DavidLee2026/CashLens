"""待确认草稿（识别→确认→入账）单测 —— 用临时目录，不碰真实数据。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services import pending  # noqa: E402
from app.services.categories import UNCONFIRMED_CATEGORY  # noqa: E402


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


def test_accept_never_writes_the_unconfirmed_placeholder_as_a_category(tmp_path):
    """账本里永远不出现「待确认」这个"分类"。

    起因（2026-09-28 真机）：用户把 17 笔全确认入账后，「最近事件」第一条显示
    「¥78.00 待确认 · 票据」。查证是草稿的占位符分类被原样写进了账本 —— 而
    `categories.py` 自己写着「判不出就进『待确认』，它是要人来确认的池子，
    **不是一个类别**」「不当类别统计」。占位符一旦落库，就会进支出构成统计，
    而且账本是追加式不可变的，错了只能再追加作废事件去补。

    收口方式：占位符与空值都落**空串**（下游 `projects.summary()` 会把空串
    如实归到「未分类」）。同时允许用户在确认那一刻补分类（category_override）。
    """
    captured = {}

    def append_fn(direction, amount_cents, category, channel, note, counterparty, project):
        captured.update(category=category)
        return {"event_id": "x"}, True

    # ① 占位符 → 落「其他」（流水线本来就用的"认不出"桶），不落「待确认」
    d = pending.create(tmp_path, direction="expense", amount_cents=7800,
                       category="待确认", channel="receipt", note="商户与分类需人工确认")
    pending.accept(tmp_path, d["id"], append_fn)
    assert captured["category"] == "其他", \
        f"占位符不得写进账本，实际写了 {captured['category']!r}"
    assert captured["category"] != UNCONFIRMED_CATEGORY

    # ①b 收入侧的占位符 → 「其他收入」（收入没有「其他」这个分类）
    d = pending.create(tmp_path, direction="income", amount_cents=29900,
                       category="待确认", channel="alipay", note="说不清的一笔进账")
    pending.accept(tmp_path, d["id"], append_fn)
    assert captured["category"] == "其他收入"

    # ② 用户在确认那一刻补了分类 → 按补的写
    d = pending.create(tmp_path, direction="expense", amount_cents=96200,
                       category="待确认", channel="receipt", note="数电票")
    pending.accept(tmp_path, d["id"], append_fn, category_override="经营")
    assert captured["category"] == "经营"

    # ③ 本来就有真分类、且没覆盖 → 原样保留（别把正常路径改坏）
    d = pending.create(tmp_path, direction="expense", amount_cents=4050,
                       category="购物", channel="receipt", note="便利店")
    pending.accept(tmp_path, d["id"], append_fn)
    assert captured["category"] == "购物"


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
      ② 确认条里必须先摆出笔数与金额，再给确认按钮；
      ③ 批量走的是同一个 accept 端点（不新增接口，端点数不变）；
      ④ 中途失败要如实报告已入账几笔，不许假装全成功。
    另外锁一件**反直觉**的事：确认条里**不要**再夹带说教。
    用户 2026-09-28 明确要求：「既然用户选择了全部确认，二次确认就可以了，
    不要再提示什么（发票号存疑 / 分类待定）」—— 在二次确认里罗列哪几笔存疑、
    再给「只确认一部分」的岔路，反而让人怀疑自己是不是点错了。
    「需核对」徽章只留在**每一行**上（那才是用户逐笔判断的地方）。
    """
    src = _page_tsx()

    assert "全部确认" in src, "左栏待确认面板应有批量入口"
    assert 'setBatchAsk("accept")' in src, "批量入口只能打开确认条，不能直接入账"
    # 确认条里要摆数字再让用户点
    assert "batchExpense" in src and "batchIncome" in src, "确认条要先摆出支出/收入合计"
    assert "确认入账" in src, "确认条里要有明确的确认按钮"
    # 二次确认不要夹带说教、也不要给岔路。
    # ⚠️ 断言必须**只看确认条那段 JSX**：整份源码里还有注释提到这两个词
    # （needsReview 的文档注释），按全文断言会误伤。
    bar = src[src.index('{batchAsk === "accept" && ('):src.index('{batchAsk === "decline" && (')]
    assert "只确认无提示的" not in bar, "批量确认条不应再给「只确认一部分」的岔路"
    assert "发票号存疑" not in bar and "分类待定" not in bar, \
        "批量确认条不应在二次确认里罗列存疑原因（用户 2026-09-28 明确要求）"
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


def test_panel_offers_a_category_picker_for_unconfirmed_rows_and_never_labels_it_as_a_category():
    """「待确认」的行：给分类下拉，且不把占位符当分类显示。

    这补的是上面那条后端护栏的界面一侧：后端保证「账本里不出现待确认」，
    但用户如果没机会补分类，那两笔就只能永远挂在「未分类」上。
    所以界面上要给出补的入口，并且**只在真正需要补的那几行**上出现
    （不选也不会写占位符，后端会落空串 → 详情里如实显示「未分类」）。
    """
    src = _page_tsx()

    assert "isUnconfirmed" in src, "需要一个判断占位符分类的函数"
    assert 'draftCategory' in src, "需要记录用户在确认那一刻选的分类"
    assert "选择分类…" in src, "「待确认」的行要有分类下拉"
    assert "cats.expense.filter((x) => x !== cats.unconfirmed)" in src, \
        "分类下拉必须排除占位符本身 —— 否则等于让用户把「待确认」当分类选"
    # 确认时要把分类一起提交（单笔与批量两条路）
    assert "acceptIt ? { project: proj, category: cat }" in src, "单笔确认要带上分类"
    assert "category: draftCategory[p.id] ?? \"\"" in src, "批量确认要带上每行选的分类"
    # 空分类显示成「未分类」，不拿「其他」冒充
    assert 'ev.category || "未分类"' in src, "空分类应显示「未分类」"


# ─── 去重挡下时不能静默丢数据（2026-09-28 真机）────────────────────
# 现场：用户口述记了 4 笔 ¥28，点「全部确认」，界面报「已确认入账 4 笔（支出 ¥112.00）」，
# 但账本只有 1 笔 —— 另外 3 笔被 strict_dedupe 挡下（4 个草稿的
# project/channel/金额/日期/分类/商户 完全一样，去重键相同），而 accept 不管成没成
# 都把草稿删了。结果：草稿没了、账没记上、界面还说成功。

def _temp_data(tmp_path, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "LEDGER_PATH", tmp_path / "finance_events.jsonl")
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "proj.db")
    return tmp_path


def test_same_manual_entry_confirmed_twice_records_twice(tmp_path, monkeypatch):
    """口述的草稿：他说两遍就是两笔，不该被去重悄悄吃掉。

    去重是为了防「同一份票据被导入两次」；口述是用户逐笔确认的动作，
    没有"重复导入"这回事。所以严格去重只对**文件导入**的草稿生效。
    """
    from fastapi.testclient import TestClient

    from app import main

    _temp_data(tmp_path, monkeypatch)
    client = TestClient(main.app)

    for _ in range(2):
        d = pending.create(tmp_path, direction="expense", amount_cents=2800,
                           category="交通", channel="manual", note="打车")
        assert client.post(f"/api/pending/{d['id']}/accept", json={}).json()["appended"] is True

    events = [e for e in main.EventLedger(main.LEDGER_PATH).load() if e["type"] == "expense"]
    assert len(events) == 2, f"口述两笔应当记两笔，实际 {len(events)} 笔"
    assert sum(e["amount_cents"] for e in events) == 5600
    assert pending.list_all(tmp_path) == [], "两笔都写成功了，草稿应当都已弹掉"


def test_duplicate_file_draft_is_skipped_but_the_draft_is_kept(tmp_path, monkeypatch):
    """文件导入的草稿：去重照旧生效，但**必须留下草稿**，并且如实返回 appended=False。

    以前这里会连草稿一起删掉 —— 用户既没记上账、也找不回那条草稿，是静默丢数据。
    现在草稿继续留在「待确认」里，前端会明说"与已有记录完全相同、没有重复入账"。
    """
    from fastapi.testclient import TestClient

    from app import main

    _temp_data(tmp_path, monkeypatch)
    client = TestClient(main.app)

    def one(source: str):
        d = pending.create(tmp_path, direction="expense", amount_cents=4050,
                           category="购物", channel="receipt", note="便利店")
        pending.update(tmp_path, d["id"], source=source)
        return client.post(f"/api/pending/{d['id']}/accept", json={}).json()

    assert one("识别")["appended"] is True
    second = one("识别")
    assert second["appended"] is False, "同一份票据导入两次，第二笔应当被去重挡下"
    assert len(pending.list_all(tmp_path)) == 1, "被挡下的那笔草稿必须留着，不能静默删掉"

    events = [e for e in main.EventLedger(main.LEDGER_PATH).load() if e["type"] == "expense"]
    assert len(events) == 1, "去重生效：账本里只应有一笔"


def test_confirm_reports_truthfully_when_a_draft_is_deduped():
    """界面上报「已确认入账」之前，必须看过后端返回的 `appended`。

    2026-09-28 真机：一次批量报了「已确认入账 4 笔（支出 ¥112.00）」，账本只有 1 笔。
    以前单笔与批量都只数"请求成功了几次"，不看有没有真写进账本 —— 这是**假汇报**。
    """
    src = _page_tsx()
    assert "res.appended === false" in src, "单笔确认要按 appended 分支"
    assert "r.appended === false" in src, "批量确认要按 appended 计数"
    assert "skipped" in src, "批量要把被去重挡下的笔数单独报出来"
    assert "没有重复入账" in src, "要说清为什么没入账，不能只说成功"
