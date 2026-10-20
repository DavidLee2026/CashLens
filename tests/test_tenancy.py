"""邀请码租户：一码一账（2026-10-19 立）

起因：要把 CashLens 放到 `cashlens.leerobert.site` 给朋友对账用，
一个地址、不同人输不同的码、各看各的账。

这组用例钉住四件事：
1. **没有会话进不去**（除了健康检查与会话端点）；
2. **坏码 / 停用码 / 篡改过的 cookie 都进不去**；
3. **两个租户互相看不见**（这是最重要的一条：漏改一处就是串账）；
4. **本地单用户模式不受影响**（`CASH_MULTI_TENANT` 不设 = 与改造前一致）。
"""
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.main import app  # noqa: E402
from app.services import tenancy  # noqa: E402


@pytest.fixture
def env(monkeypatch, tmp_path):
    """多租户模式 + 临时数据根目录 + http（cookie 不要求 Secure）。"""
    monkeypatch.setenv("CASH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CASH_MULTI_TENANT", "1")
    monkeypatch.setenv("CASH_COOKIE_SECURE", "0")
    tenancy.reset_state_for_tests()
    yield tmp_path
    tenancy.reset_state_for_tests()


@pytest.fixture
def client(env):
    with TestClient(app) as c:
        yield c


def _login(c: TestClient, code: str):
    return c.post("/api/session/redeem", json={"code": code})


def _one_expense(c: TestClient, cents: int = 1234):
    return c.post("/api/events", json={
        "event_type": "expense", "amount_cents": cents,
        "category": "办公", "counterparty": "打印店",
    })


# ─── 门：没有会话就进不去 ───────────────────────────────
def test_without_session_api_is_closed(client):
    assert client.get("/api/health").status_code == 200        # 探活不用码
    r = client.get("/api/events")
    assert r.status_code == 401
    assert r.json()["code"] == "tenant_required"                # 前端据此弹邀请码页


def test_redeem_with_good_code_opens_the_door(client):
    code, _ = tenancy.create_code("客户A")
    r = _login(client, code)
    assert r.status_code == 200 and r.json()["label"] == "客户A"
    assert client.get("/api/session/me").json()["label"] == "客户A"
    assert client.get("/api/events").status_code == 200


def test_code_tolerates_spaces_case_and_dashes(client):
    """微信转发/手抄容易变形：大小写、空格、连字符都该能认。"""
    code, _ = tenancy.create_code("客户A")
    messy = " " + code.lower().replace("-", " ") + " "
    assert _login(client, messy).status_code == 200


def test_wrong_code_is_rejected_then_throttled(client):
    bad = {"code": "CL-AAAA-BBBB-CCCC"}
    for _ in range(8):
        assert client.post("/api/session/redeem", json=bad).status_code == 401
    assert client.post("/api/session/redeem", json=bad).status_code == 429


def test_revoked_code_kills_both_code_and_session(client):
    code, slug = tenancy.create_code("临时")
    assert _login(client, code).status_code == 200
    assert client.get("/api/events").status_code == 200
    assert tenancy.revoke(slug) is True
    assert client.get("/api/events").status_code == 401          # 已登录的也被踢出去
    assert _login(client, code).status_code == 401


def test_tampered_cookie_is_rejected(client):
    """签名被改一个字符就不认（用一个干净的 client 手发 cookie，避免 jar 里新旧两条并存）。"""
    code, _ = tenancy.create_code("X")
    assert _login(client, code).status_code == 200
    token = client.cookies.get(tenancy.COOKIE_NAME)
    assert token
    tail = "00" if not token.endswith("00") else "11"
    bad = token[:-2] + tail
    with TestClient(app) as fresh:
        r = fresh.get("/api/events", headers={"cookie": f"{tenancy.COOKIE_NAME}={bad}"})
    assert r.status_code == 401


def test_rotated_code_invalidates_old_code_and_session(env):
    code, slug = tenancy.create_code("X")
    with TestClient(app) as c:
        assert _login(c, code).status_code == 200
        assert _one_expense(c).status_code == 200
        new_code = tenancy.rotate(slug)
        assert new_code and new_code != code
        assert tenancy.verify_code(code) is None
        assert tenancy.verify_code(new_code) == slug
        assert c.get("/api/events").status_code == 401           # salt 变了 → 旧会话失效
    assert (env / "tenants" / slug / "finance_events.jsonl").exists()   # 账本一个字没动


# ─── 隔离：两个码 = 两本账（最重要的一条）───────────────
def test_each_code_has_its_own_ledger(env):
    mine, slug_mine = tenancy.create_code("我的测试")
    his, slug_his = tenancy.create_code("客户A")
    with TestClient(app) as a, TestClient(app) as b:
        assert _login(a, mine).status_code == 200
        assert _login(b, his).status_code == 200

        assert _one_expense(a, 1234).status_code == 200
        assert _one_expense(b, 9900).status_code == 200

        mine_events = a.get("/api/events").json()["events"]
        his_events = b.get("/api/events").json()["events"]
        assert len(mine_events) == 1 and mine_events[0]["amount_cents"] == 1234
        assert len(his_events) == 1 and his_events[0]["amount_cents"] == 9900

        # 项目也是各算各的（不同目录下的 projects.json）
        assert a.get("/api/projects").status_code == 200
        assert b.get("/api/projects").status_code == 200

    # 落盘位置不同，且都在自己的 slug 目录下
    for slug in (slug_mine, slug_his):
        assert (env / "tenants" / slug / "finance_events.jsonl").exists()
    assert (env / "tenants" / slug_mine / "finance_events.jsonl").read_text() != \
           (env / "tenants" / slug_his / "finance_events.jsonl").read_text()


def test_tenants_do_not_see_each_others_state(env):
    """状态/汇总也走同一个 data_dir() —— 这里用 totals 抄底再确认一次。"""
    a_code, _ = tenancy.create_code("A")
    b_code, _ = tenancy.create_code("B")
    with TestClient(app) as a, TestClient(app) as b:
        _login(a, a_code)
        _login(b, b_code)
        _one_expense(a, 5000)
        sa = a.get("/api/state").json()
        sb = b.get("/api/state").json()
        assert json.dumps(sa) != json.dumps(sb)


# ─── 安全细节 ───────────────────────────────────────────
def test_code_is_never_stored_in_plaintext(env):
    code, _ = tenancy.create_code("客户A")
    raw = (env / "tenants.json").read_text(encoding="utf-8")
    assert code not in raw
    assert tenancy.normalize_code(code) not in raw
    assert "code_hash" in raw and "salt" in raw


def test_model_config_stays_at_root_not_per_tenant(env):
    """API key 是设备配置，不该跟着客户目录走。"""
    tenancy.create_code("X")
    assert tenancy.model_config_path() == env / "model_config.json"


def test_expired_token_is_rejected(env, monkeypatch):
    code, slug = tenancy.create_code("X")
    token = tenancy.issue_token(slug, days=0)      # 立刻过期
    assert tenancy.verify_token(token) is None


# ─── 本地单用户模式不受影响 ─────────────────────────────
def test_local_mode_needs_no_code(monkeypatch, tmp_path):
    monkeypatch.delenv("CASH_MULTI_TENANT", raising=False)
    monkeypatch.setenv("CASH_DATA_DIR", str(tmp_path))
    got = {"ok": True}
    with TestClient(app) as c:
        assert c.get("/api/events").status_code == 200
        me = c.get("/api/session/me").json()
        assert me["multi_tenant"] is False
    assert got["ok"]


def test_local_mode_data_stays_in_root(monkeypatch, tmp_path):
    monkeypatch.delenv("CASH_MULTI_TENANT", raising=False)
    monkeypatch.setenv("CASH_DATA_DIR", str(tmp_path))
    with TestClient(app) as c:
        assert _one_expense(c).status_code == 200
    assert (tmp_path / "finance_events.jsonl").exists()      # 还是老位置
    assert not (tmp_path / "tenants").exists()


def test_rename_updates_label_without_touching_ledger(client):
    """登录后改名：只改称呼，账本/目录/slug/会话都不动。"""
    code, slug = tenancy.create_code("旧称呼")
    assert _login(client, code).status_code == 200
    assert _one_expense(client).status_code == 200
    before = (client.get("/api/events").json().get("events") or [])

    r = client.post("/api/session/rename", json={"label": "新称呼"})
    assert r.status_code == 200 and r.json()["label"] == "新称呼"
    assert client.get("/api/session/me").json()["label"] == "新称呼"
    assert tenancy.get_tenant(slug)["label"] == "新称呼"
    # 账本还在、slug 没变、会话仍然有效
    assert client.get("/api/events").status_code == 200
    assert len(client.get("/api/events").json().get("events") or []) == len(before)
    assert tenancy.get_tenant(slug)["slug"] == slug

    assert client.post("/api/session/rename", json={"label": "   "}).status_code == 422
    assert client.post("/api/session/rename", json={"label": "x" * 21}).status_code == 422
