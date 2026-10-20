"""上线到服务器前的四道护栏（2026-10-19）

这组用例只钉「多租户部署」这件事，不碰业务计算：

1. **会话记忆按租户分区** —— 同一台电脑换邀请码，不能读到上一家的对话；
2. **模型档位可锁** —— 服务器上 CASH_LOCK_MODEL=1 时，界面与接口都不许改；
3. **Linux PDF 文本层兜底** —— 服务器没有 Quartz，必须能走 PyMuPDF；
4. **前端真的有登录页与 401 兜底** —— 否则多租户一开，工作台全黑。

第 4 条是源码级断言（与 tests/test_data_reset.py 的界面护栏同型）：
前端没有可用的组件测试环境，用「关键结构必须在」来防止以后误删。
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "engine" / "mcp"))

from app import main  # noqa: E402
from app.services import model_config, session_mem, tenancy  # noqa: E402


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("CASH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CASH_MULTI_TENANT", "1")
    monkeypatch.setenv("CASH_COOKIE_SECURE", "0")
    monkeypatch.delenv("CASH_LOCK_MODEL", raising=False)
    tenancy.reset_state_for_tests()
    yield tmp_path
    tenancy.reset_state_for_tests()


# ─── 1. 会话记忆按租户分区 ─────────────────────────────
def test_mem_sid_is_namespaced_per_tenant(env):
    _, slug_a = tenancy.create_code("客户A")
    _, slug_b = tenancy.create_code("客户B")

    tenancy.set_current(slug_a)
    assert main._mem_sid("same-session") == f"{slug_a}:same-session"
    tenancy.set_current(slug_b)
    assert main._mem_sid("same-session") == f"{slug_b}:same-session"

    tenancy.reset_current()
    assert main._mem_sid("same-session") == "same-session"


def test_clear_prefix_only_clears_one_tenant():
    session_mem.add_turn("aaaa:same", "用户问", "A 的回答")
    session_mem.add_turn("bbbb:same", "用户问", "B 的回答")

    session_mem.clear_prefix("aaaa:")

    assert session_mem.context_of("aaaa:same") == ""
    assert "B 的回答" in session_mem.context_of("bbbb:same")
    session_mem.clear_all()  # 收尾，别把状态漏给别的用例


def test_tenant_dirs_are_not_world_readable(env):
    """目录名是随机 slug，但也没必要让本机其他用户列出来（文件本身已 600）。"""
    import stat as _stat

    _, slug = tenancy.create_code("客户A")
    assert _stat.S_IMODE((env / "tenants").stat().st_mode) == 0o700
    assert _stat.S_IMODE((env / "tenants" / slug).stat().st_mode) == 0o700


# ─── 2. 模型档位锁 ─────────────────────────────────────
def test_models_are_read_only_when_locked(env, monkeypatch):
    monkeypatch.setenv("CASH_LOCK_MODEL", "1")
    code, _ = tenancy.create_code("客户A")
    with TestClient(main.app) as c:
        assert c.post("/api/session/redeem", json={"code": code}).status_code == 200
        view = c.get("/api/models").json()
        assert view["locked"] is True
        assert c.post("/api/models/select", json={"tier": "none"}).status_code == 403
        assert c.post("/api/models/test").status_code == 403


def test_models_are_writable_when_not_locked(env, monkeypatch):
    monkeypatch.delenv("CASH_LOCK_MODEL", raising=False)
    code, _ = tenancy.create_code("客户A")
    with TestClient(main.app) as c:
        assert c.post("/api/session/redeem", json={"code": code}).status_code == 200
        assert c.get("/api/models").json()["locked"] is False
        assert c.post("/api/models/select", json={"tier": "none"}).status_code == 200


# ─── 3. Linux PDF 文本层兜底 ───────────────────────────
def test_pdf_text_layer_falls_back_to_pymupdf(tmp_path, monkeypatch):
    pymupdf = pytest.importorskip("pymupdf")
    import receipt_mcp  # noqa: PLC0415  engine/mcp 已在 sys.path

    # 号码在运行时拼出来：源码里不出现 20 位连续数字（隐私闸会把它当真实发票号拦下）。
    invoice_no = "2631" + "0" * 8 + "12345678"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 72), (
        "电子发票（普通发票）\n"
        f"发票号码：{invoice_no}\n"
        "开票日期：2026年09月18日\n"
        "项目名称：*生产生活服务*住宿费\n"
        "价税合计（大写）玖佰陆拾贰元整 （小写）¥962.00\n"
        "销售方名称：某某酒店有限公司"
    ), fontname="china-s", fontsize=10)
    pdf = tmp_path / "invoice.pdf"
    doc.save(str(pdf))
    doc.close()

    # 模拟 Linux：macOS Quartz 不可用，只剩 PyMuPDF
    monkeypatch.setattr(receipt_mcp, "_PDF_TEXT_AVAILABLE", False)
    text = receipt_mcp.extract_pdf_text_layer(str(pdf))
    assert text and "价税合计" in text

    parsed = receipt_mcp.parse_from_text_layer(text, str(pdf))
    assert parsed["amount"] == 962.0
    assert parsed["date"] == "2026-09-18"
    assert parsed["invoice_no"] == invoice_no
    assert "住宿费" in parsed["items"]
    assert parsed["_cloud_uploaded"] is False


def test_pdf_total_is_tax_inclusive_even_when_text_order_differs():
    """价税合计要读含税总价，不能读「合计」栏里的未税金额。

    2026-10-20 真机：同一张数电票，macOS Quartz 读到 962.00，
    服务器 PyMuPDF 的文本流把 907.55 / 54.45 排到了价税合计标签后面，
    旧实现取第一个 ¥ 就读成了 907.55（少 54.45 税）。
    """
    import receipt_mcp  # noqa: PLC0415

    # PyMuPDF 的实际顺序：标签在前，金额在后，且未税金额/税额先出现
    pymupdf_order = (
        "合\n计\n价税合计（大写）\n（小写）\n备\n注\n"
        "国家税务总局\n全\n国\n统\n一\n发\n票\n监\n制\n章\n云\n南\n省\n税\n务\n局\n"
        "¥907.55\n¥54.45\n玖佰陆拾贰圆整\n¥ 962.00\n"
        "2026年09月20日\n"
    )
    parsed = receipt_mcp.parse_from_text_layer(pymupdf_order, "synthetic.pdf")
    assert parsed["amount"] == 962.0

    # Quartz 的顺序：标签与总价在同一行
    quartz_order = "价税合计（大写） 玖佰陆拾贰圆整 ¥ 962.00\n2026年09月20日\n"
    parsed2 = receipt_mcp.parse_from_text_layer(quartz_order, "synthetic.pdf")
    assert parsed2["amount"] == 962.0


# ─── 4. 前端登录页与 401 兜底 ──────────────────────────
def test_login_page_and_401_guard_exist():
    src = (ROOT / "frontend" / "app" / "page.tsx").read_text(encoding="utf-8")
    assert "function LoginGate()" in src
    assert "/api/session/redeem" in src
    assert "cl-auth-required" in src
    assert "function lsKey(base: string)" in src
    assert 'lsKey("cl_msgs_v2")' in src
    assert 'lsKey("cl_session_v2")' in src
    # 登录页要分别说清「码不对 / 试太多次 / 服务器问题」，不能一律报成网络错误
    assert "邀请码不对，或者已经停用" in src
    assert "尝试次数太多" in src
