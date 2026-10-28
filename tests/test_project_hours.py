"""项目工时端点 + 「工时 → 历史时薪 → 接单决策」整条链路（2026-10-29 · A 档）。

## 为什么要端到端测

2026-10-29 查出两个断点：
1. `hours` 只是 `POST /api/events` 的一个可选字段，`record` 动作白名单里没有它，
   界面上也没有入口 —— 真人从手机走一遍，「带工时的收入」永远是 0 条，
   决策卡永远显示「你的历史时薪：算不出」。
2. 就算有工时，「预估 vs 实际」也不配对（decision 是无状态的）。

这一版先补第 1 条：工时挂到**项目**上（A 档口径），时薪 = 项目收入 ÷ 项目工时。
这里从 HTTP 层验一遍，证明三个部件接上了：端点 → 映射表 → 决策引擎。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


def _isolate(tmp_path, monkeypatch):
    """把数据目录指到临时目录。只改 `CASH_DATA_DIR`（2026-10-19 之后路径按请求解析）。"""
    from app import main
    monkeypatch.setenv("CASH_DATA_DIR", str(tmp_path))
    return main


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = _isolate(tmp_path, monkeypatch)
    return TestClient(main.app)


def test_hours_endpoint_saves_and_never_touches_ledger(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    pid = c.post("/api/projects", json={"name": "详情页 80 张"}).json()["project"]["id"]

    res = c.post(f"/api/projects/{pid}/hours", json={"hours": 80})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True and body["hours"] == 80.0

    detail = c.get(f"/api/projects/{pid}/detail").json()
    assert detail["project"]["hours"] == 80.0
    assert not (tmp_path / "finance_events.jsonl").exists(), "记工时不许写账本"


def test_hours_endpoint_rejects_negative_and_unknown(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    pid = c.post("/api/projects", json={"name": "活"}).json()["project"]["id"]
    assert c.post(f"/api/projects/{pid}/hours", json={"hours": -3}).status_code == 422
    assert c.post("/api/projects/project_zzzz/hours", json={"hours": 3}).status_code == 404


def test_decision_uses_project_hours_end_to_end(tmp_path, monkeypatch):
    """整个链条：建项目 → 记两笔收入 → 填工时 → 问这单接不接。

    项目 A：收 ¥600 / 16 小时 = ¥37.50/时
    项目 B：收 ¥800 / 56 小时 = ¥14.29/时
    历史时薪 = 两者中位数 = ¥25.89/时
    新单 ¥300 / 10 小时 = ¥30/时 ≥ ¥25.89 → 至少不是「不接」
    """
    c = _client(tmp_path, monkeypatch)
    pa = c.post("/api/projects", json={"name": "A"}).json()["project"]["id"]
    pb = c.post("/api/projects", json={"name": "B"}).json()["project"]["id"]

    for amount, project in ((60000, pa), (80000, pb)):
        r = c.post("/api/events", json={"event_type": "income", "amount_cents": amount, "evidence_kind": "flow",
                                        "channel": "wechat", "category": "接单",
                                        "project": project, "confirmed": True})
        assert r.status_code == 200, r.text

    assert c.post(f"/api/projects/{pa}/hours", json={"hours": 16}).status_code == 200
    assert c.post(f"/api/projects/{pb}/hours", json={"hours": 56}).status_code == 200

    out = c.post("/api/decision", json={"amount_cents": 30000, "estimated_hours": 10}).json()
    assert out["hourly"]["historical_cents"] == 2589, "¥37.50 与 ¥14.29 的中位数 ≈ ¥25.89/时"
    assert "填了工时的项目" in out["hourly"]["basis"], "理由要说明基准来自项目工时"
    assert out["decision"] != "不接"


def test_without_hours_decision_still_says_cannot_compute(tmp_path, monkeypatch):
    """没填工时 → 如实说算不出，不给假下限。这条是红线，不能被新口径冲掉。"""
    c = _client(tmp_path, monkeypatch)
    pid = c.post("/api/projects", json={"name": "没填工时的活"}).json()["project"]["id"]
    c.post("/api/events", json={"event_type": "income", "amount_cents": 60000, "evidence_kind": "flow",
                                "channel": "wechat", "category": "接单",
                                "project": pid, "confirmed": True})
    out = c.post("/api/decision", json={"amount_cents": 30000, "estimated_hours": 10}).json()
    assert out["hourly"]["historical_cents"] is None
    assert "算不出" in out["hourly"]["basis"]
    assert out["quote"]["floor_cents"] is None
