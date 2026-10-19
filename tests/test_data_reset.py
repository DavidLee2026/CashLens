"""本机演示数据「重置」（存档再开新账）——服务层 + 端点 + 界面护栏。

起因（David 2026-09-29）：要录产品使用视频，需要把账户重置、反复重录；
提出「总账上加重置按钮，并 2 次确认，或者放用户那里？」。分析后定：
入口放**右上角设置**（账户级动作，不放进选择列表、录视频时不出镜），
重置**不是删文件**而是先把当前数据整套备份到 `data/_reset_backup_<ts>/`，
并配对「重置并载入演示样本」让每次重录开局一致。

这套护栏钉住的是**破坏性动作的诚实性**：
  · 真的备份了吗（而且能拷回来）
  · 模型配置有没有被误清（它是设备配置，不是演示数据）
  · 两级确认有没有被压成一级
  · 界面有没有如实说清"清掉什么、备份在哪"
"""

import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import app.main as main  # noqa: E402
from app.services import data_reset, pending, projects, session_mem  # noqa: E402
from app.services.state_engine import EventLedger, Projection, make_event  # noqa: E402

_PAGE = Path(__file__).resolve().parents[1] / "frontend" / "app" / "page.tsx"
_SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "synthetic_small_business" / "data"
_LAUNCHER = Path(__file__).resolve().parents[1] / "启动-CashLens.command"


def _launcher_src() -> str:
    return _LAUNCHER.read_text(encoding="utf-8")


def _isolate_paths(tmp_path, monkeypatch):
    """把 main 的三条路径都指到临时目录（只改 DATA_DIR 不够，见 test_pending 的教训）。"""
    monkeypatch.setenv("CASH_DATA_DIR", str(tmp_path))   # 2026-10-19：路径改成按请求解析，改为设环境变量
    return main


def _seed_state(d: Path) -> None:
    """造一套"像真用过"的状态：1 笔账 + 1 个项目 + 1 条草稿 + 模型配置。"""
    led = EventLedger(d / "finance_events.jsonl")
    led.append(make_event(ts=datetime(2026, 9, 28, 10, 0), event_type="expense",
                          amount_cents=2800, evidence_kind="voice", category="交通",
                          dedupe_unique=True), strict_dedupe=False)
    projects.create(d, "示例项目 A")
    pending.create(d, direction="expense", amount_cents=100, category="餐饮",
                   channel="manual", note="待确认的一笔")
    (d / "model_config.json").write_text('{"tier": "cloud"}', encoding="utf-8")


# ─── ① 重置＝存档再开新账（不是删文件） ───────────────────────────────


def test_reset_archives_current_state_instead_of_deleting(tmp_path):
    """当前整套状态搬进备份目录，data 目录回到空账 —— 这是「误删可救」的前提。"""
    d = tmp_path
    _seed_state(d)
    before = data_reset.snapshot(d)
    assert before == {"events": 1, "projects": 1, "pending": 1, "timesheet": 0}

    out = data_reset.reset(d)

    assert out["ok"] and out["action"] == "reset"
    assert out["backup_dir"], "有数据时必须给出备份路径（否则用户没法放心）"
    backup = Path(out["backup_dir"])
    if not backup.is_absolute():
        backup = d.parent / backup
    assert backup.is_dir(), f"备份目录不存在：{backup}"
    assert (backup / "finance_events.jsonl").exists(), "账本应当搬进备份，而不是被删掉"
    assert (backup / "projects.json").exists()
    assert (backup / "pending.json").exists()
    assert out["before"] == before and out["after"] == {
        "events": 0, "projects": 0, "pending": 0, "timesheet": 0}
    # 空账：账本文件已不在 data 里，读出来是 0 笔
    assert EventLedger(d / "finance_events.jsonl").load() == []
    # 投影重建过（空表），不是留着一个指向旧事件的 proj.db
    proj = Projection(d / "proj.db")
    assert proj.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    proj.close()


def test_archived_backup_can_be_copied_back(tmp_path):
    """把备份整份拷回 → 数字与重置前一致。这就是界面上说的"需要时整份拷回即可恢复"。"""
    import shutil

    d = tmp_path
    _seed_state(d)
    before = data_reset.snapshot(d)
    out = data_reset.reset(d)
    backup = Path(out["backup_dir"])
    if not backup.is_absolute():
        backup = d.parent / backup

    for f in backup.iterdir():
        if f.is_file():
            shutil.copyfile(f, d / f.name)
    assert data_reset.snapshot(d)["events"] == before["events"], "拷回后账本笔数要回来"
    assert data_reset.snapshot(d)["projects"] == before["projects"]


def test_reset_keeps_model_config(tmp_path):
    """模型档位配置**不动**：它是设备配置，不是演示数据（清了录视频还得重配一遍模型）。"""
    d = tmp_path
    _seed_state(d)
    out = data_reset.reset(d)
    assert (d / "model_config.json").exists(), "model_config.json 不能被重置带走"
    assert "model_config.json" in out["kept"], "返回里要如实说保留了它"


def test_reset_without_any_data_creates_no_backup_dir(tmp_path):
    """本来就是空的时候不制造空备份目录，也如实说"未生成备份"。"""
    out = data_reset.reset(tmp_path)
    assert out["backup_dir"] == ""
    assert not list(tmp_path.glob("_reset_backup_*")), "空目录不该留下空备份"


def test_backup_dir_is_unique_within_the_same_second(tmp_path):
    """同一秒内连着重置两次，第二次不能覆盖/混进第一次的备份（实测踩到过）。"""
    d = tmp_path
    _seed_state(d)
    first = data_reset.reset(d)
    _seed_state(d)  # 再攒一点数据，确保第二次真的有东西可搬
    second = data_reset.reset(d)
    assert first["backup_dir"] and second["backup_dir"]
    assert first["backup_dir"] != second["backup_dir"], "两次备份必须落在不同目录"


# ─── ② 演示样本（录视频开局一致） ───────────────────────────────────


def test_reset_with_seed_loads_the_synthetic_sample(tmp_path):
    """「重置并载入演示样本」：账本/项目来自合成样本，且投影从账本重建。"""
    d = tmp_path
    _seed_state(d)
    out = data_reset.reset(d, seed_demo=True)
    assert out["action"] == "reset_seeded"

    expected = len(EventLedger(_SAMPLE / "finance_events.jsonl").load())
    assert expected > 0, "样本本身不该是空的"
    assert out["after"]["events"] == expected
    assert out["after"]["projects"] == len(projects.visible_rows(_SAMPLE))
    # 投影跟着重建，不是留着重置前的空表
    proj = Projection(d / "proj.db")
    assert proj.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == expected
    proj.close()
    # 样本是"多项目经营账"：入口文案这么写，就必须真的多于一个项目
    assert out["after"]["projects"] >= 2


def test_seed_fails_loudly_when_sample_missing(tmp_path):
    """样本缺失要**报错**，不能假装载入成功（否则演示时才发现是空账）。"""
    import pytest

    with pytest.raises(FileNotFoundError):
        data_reset.reset(tmp_path, seed_demo=True, sample_dir=tmp_path / "不存在")


# ─── ③ 端点：清零 + 如实回报 + 清会话记忆 ───────────────────────────


def test_reset_endpoint_clears_ledger_and_reports(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    m = _isolate_paths(tmp_path, monkeypatch)
    _seed_state(tmp_path)
    client = TestClient(m.app)

    r = client.post("/api/reset", json={"seed_demo": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["action"] == "reset"
    assert body["before"]["events"] == 1 and body["after"]["events"] == 0
    assert body["backup_dir"]
    assert m.EventLedger(m.ledger_path()).load() == [], "端点必须真的把账本清空"
    assert (tmp_path / "model_config.json").exists(), "模型配置不能被端点误清"
    # 报告要能被前端直接拼成一句人话
    assert body["kept"] == ["model_config.json"]


def test_reset_endpoint_defaults_to_plain_reset(tmp_path, monkeypatch):
    """不带请求体 = 纯重置（不载入样本）：默认值不能悄悄变成"载入样本"。"""
    from fastapi.testclient import TestClient

    m = _isolate_paths(tmp_path, monkeypatch)
    _seed_state(tmp_path)
    client = TestClient(m.app)
    r = client.post("/api/reset")
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "reset"
    assert m.EventLedger(m.ledger_path()).load() == []


def test_reset_endpoint_can_seed_demo(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    m = _isolate_paths(tmp_path, monkeypatch)
    _seed_state(tmp_path)
    client = TestClient(m.app)
    r = client.post("/api/reset", json={"seed_demo": True})
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "reset_seeded"
    assert len(m.EventLedger(m.ledger_path()).load()) > 0


def test_reset_endpoint_says_sample_missing_instead_of_a_flat_500(tmp_path, monkeypatch):
    """样本不在本机时要说清**怎么办**（409 + 生成命令），不能报成 500 让前端猜。

    演示样本被 `.gitignore` 的 `data/` 规则挡住、不入版本库 —— 干净克隆里就是没有，
    所以这条路径一定会被走到，错误文案必须指向生成脚本。
    """
    from fastapi.testclient import TestClient

    m = _isolate_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(data_reset, "_SAMPLE_DIR", tmp_path / "不存在")
    client = TestClient(m.app)
    r = client.post("/api/reset", json={"seed_demo": True})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "make_synthetic_business_ledger.py" in detail, "要给出生成样本的具体命令"
    assert "重置本机数据" in detail, "要给出可行的替代路径"
    # 纯重置不受影响：样本缺了不该连"清空"都做不了
    assert client.post("/api/reset", json={"seed_demo": False}).status_code == 200


def test_reset_clears_session_memory():
    """重置要**同时清掉会话记忆**：否则界面看着空了，助手却还记得那些已经不存在的账目。"""
    session_mem.add_turn("s1", "打车 28", "记好了")
    session_mem.add_turn("s2", "收 3000", "记好了")
    session_mem.clear_all()
    assert session_mem.context_of("s1") == ""
    assert session_mem.context_of("s2") == ""


# ─── ④ 界面护栏：入口位置 + 两级确认 + 诚实文案 ──────────────────────


def _page_src() -> str:
    return _PAGE.read_text(encoding="utf-8")


def test_settings_entry_lives_in_the_account_level_corner():
    """入口在**右上角**（账户级位置），不在项目选择列表里 —— 后者是选择器，塞破坏性动作易误点。"""
    src = _page_src()
    head = src[src.index('className="convo-head"'):
               src.index('className="log"', src.index('className="convo-head"'))]
    assert "openSettings" in head and "设置" in head, "设置入口要在右栏顶部（右上角）"
    # 项目行（总账户那个选择按钮）里不许出现重置
    proj_sel = src[src.index('className={`proj-sel'):
                   src.index("点一个项目", src.index('className={`proj-sel'))]
    assert "重置" not in proj_sel, "「总账户」那一行是选择器，不要在里面放重置"


def test_reset_requires_two_explicit_steps():
    """两级确认不许被压成一级：`doReset` 只能从第 2 步那个按钮调到。"""
    src = _page_src()
    assert src.count("onClick={doReset}") == 1, "真正执行重置的按钮只应有一处"
    step2 = src.index("resetStep === 2")
    assert src.index("onClick={doReset}") > step2, "执行按钮必须在第 2 步的块里"
    # 第 1 步只能"继续"，不能直接执行
    step1 = src.index("resetStep === 1")
    assert step1 < step2
    assert "onClick={doReset}" not in src[step1:step2], "第 1 步不许出现执行按钮"
    assert "setResetStep(2)" in src[step1:step2], "第 1 步的出路是进入第 2 步"


def test_reset_ui_states_scope_and_backup_honestly():
    """界面必须说清"清掉什么 / 备份在哪 / 保留什么"，并真的清前端对话历史。"""
    src = _page_src()
    for word in ("会清空", "会保留", "备份到", "对话历史也会清空"):
        assert word in src, f"重置弹层里缺少如实说明：{word}"
    # 只清后端账本是不够的：对话历史与 session id 在 localStorage 里，也要清。
    # 2026-10-19 多租户：key 走 lsKey() 按租户分区（换邀请码不能读到上一家的对话），
    # 所以断言的是 lsKey(...) 而不是裸字符串。
    assert 'removeItem(lsKey("cl_msgs_v2"))' in src
    assert 'removeItem(lsKey("cl_session_v2"))' in src
    assert 'function lsKey(base: string)' in src
    # 清完把对话恢复正常开场白（抽成常量，避免各处各写一份）
    assert "setMsgs([WELCOME_MSG])" in src
    assert "const WELCOME_MSG: Msg" in src


def test_reset_request_sends_the_seed_flag():
    """前端把「要不要载入演示样本」如实传给后端（默认纯重置）。"""
    src = _page_src()
    assert 'JSON.stringify({ seed_demo: resetSeed })' in src


def test_reset_ui_surfaces_the_backend_reason_not_a_generic_error():
    """后端写清的具体原因要显示出来 —— 共用 `j()` 只抛 `HTTP 409`，会把原因丢掉。"""
    src = _page_src()
    assert 'fetch("/api/reset"' in src, "重置要走原始 fetch，才能读到响应体里的 detail"
    assert "errBody?.detail" in src, "后端给的原因要取出来显示，不能丢成 HTTP 409"
    assert "连不上本地后端" in src, "真正的网络失败仍要有单独一句（与 409 区分开）"


# ─── ⑤ 后端旧进程：接口 404 时必须说清"要重启后端" ──────────────────


def test_reset_ui_names_the_stale_backend_case():
    """`/api/reset` 404/405 = 后端进程还是旧版本。

    2026-09-29 真机：改了代码但 8001 上还是旧进程，界面上只显示「重置失败：Not Found」——
    用户会去查网络，而真正要做的是重启后端。这与"连不上后端"必须分开说。
    """
    src = _page_src()
    assert "resp.status === 404" in src and "405" in src, "要单独接住 404/405（接口不存在）"
    assert "后端还是旧版本" in src, "要点名是后端旧了，不是网络问题"
    assert "Control + C" in src and "启动-CashLens.command" in src, "要给出可照做的重启步骤"


def test_health_exposes_source_mtime_for_freshness_checks(tmp_path, monkeypatch):
    """`/api/health` 要自报 `source_mtime`：启动脚本靠它与磁盘比对，判断进程是否在跑旧代码。"""
    from fastapi.testclient import TestClient

    m = _isolate_paths(tmp_path, monkeypatch)
    body = TestClient(m.app).get("/api/health").json()
    assert isinstance(body.get("source_mtime"), int) and body["source_mtime"] > 0
    assert body["ok"] is True and body["api"] == "cashlens"


def test_launcher_does_not_silently_reuse_a_stale_backend():
    """启动脚本不许只看 `/api/health` + `/api/categories` 就复用旧进程。

    这是 2026-09-29 那次「重置失败」的真正根因：旧进程两条校验都通过，
    脚本于是打印「直接复用（不再启动新的后端）」，新加的 /api/reset 一直 404，
    **怎么重启都是旧的**。所以复用必须再经过一道"代码新鲜度"判断。
    """
    src = _launcher_src()
    assert "source_mtime" in src, "要读后端自报的 source_mtime"
    assert "backend_current" in src, "要有独立的「跑的是不是当前代码」判断"
    # 复用分支必须被 backend_current 守着
    reuse = src.index('BACK_REUSE="yes"')
    guard = src.rindex("if backend_current", 0, reuse)
    assert guard != -1 and guard < reuse, "BACK_REUSE 必须在 backend_current 通过之后才设"
    assert 'if backend_reusable "$BACK_PORT"; then\n    if backend_current' in src, (
        "两层判断要嵌套：先确认是本仓库，再确认是当前代码"
    )
    # 旧代码要**停掉重起**，不是"让路换端口"（换端口会让前端继续连旧的）
    assert 'kill "$OLD_PID"' in src, "本仓库的旧后端要停掉，用最新代码占回同一端口"
    assert "本仓库的旧版本后端" in src, "要如实告诉用户发生了什么"
    # 让路（换端口）只留给"别的副本"这一种情况
    assert src.index("为它让路，本仓库后端改用下面这个端口") > src.index("backend_reusable \"$BACK_PORT\""), (
        "「让路换端口」只应留给别的副本；本仓库旧代码要停掉重起"
    )
    assert '[ "$local_m" -le "$reported" ]' in src, "新鲜度比较要按 mtime 数值比"
    assert "旧版本后端没有这个字段" in src or "直接判旧" in src, (
        "旧后端没有 source_mtime 字段 → 必须判为旧（这正是那次的情形）"
    )
