"""模型配置按租户隔离（2026-10-20 立项）

起因：线上是「一码一租户」，但模型配置原先是**设备级一份**（数据根目录的 model_config.json）——
任何一个拿码的人改一次，所有人一起坏，所以只能用 `CASH_LOCK_MODEL=1` 把界面锁死，
用户看到的是「已锁定」，心里冒出的问题是「我是不是被锁死在这家模型上」。

David 拍板：**每个账号用自己的模型**（可以填自己的 API Key / 自己的端点）。
这组用例钉四件事：
① 两个租户各存一份、互不覆盖；
② 服务器自带的 key 不落进客户目录（写哨兵，读时换回真值）；
③ 锁的开关仍在，但默认不锁；
④ 单用户模式（本机安装版）行为不变 —— 还是数据根目录那一份。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services import model_config, tenancy  # noqa: E402


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CASH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CASH_LOCK_MODEL", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "server-key-123")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_MODEL", "server-model")
    for slug in ("aaaaaaaa", "bbbbbbbb"):
        (tmp_path / "tenants" / slug).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _as(slug: str):
    """模拟“当前请求属于某个租户”。"""
    return tenancy.use_tenant(slug) if hasattr(tenancy, "use_tenant") else None


def test_config_path_is_per_tenant(data_dir, monkeypatch):
    monkeypatch.setattr(model_config, "_tenant_slug", lambda: "aaaaaaaa")
    assert model_config.config_path() == data_dir / "tenants" / "aaaaaaaa" / "model_config.json"


def test_single_user_mode_keeps_root_file(data_dir, monkeypatch):
    monkeypatch.setattr(model_config, "_tenant_slug", lambda: "")
    assert model_config.config_path() == data_dir / "model_config.json"


def test_two_tenants_do_not_overwrite_each_other(data_dir, monkeypatch):
    monkeypatch.setattr(model_config, "_tenant_slug", lambda: "aaaaaaaa")
    model_config.select(tier="local", base_url="http://127.0.0.1:11434", model="qwen2.5-vl")
    monkeypatch.setattr(model_config, "_tenant_slug", lambda: "bbbbbbbb")
    model_config.select(tier="cloud")
    # 各自回到自己的配置
    monkeypatch.setattr(model_config, "_tenant_slug", lambda: "aaaaaaaa")
    assert model_config.load()["tier"] == "local"
    monkeypatch.setattr(model_config, "_tenant_slug", lambda: "bbbbbbbb")
    assert model_config.load()["tier"] == "cloud"


def test_server_key_is_not_copied_into_tenant_file(data_dir, monkeypatch):
    monkeypatch.setattr(model_config, "_tenant_slug", lambda: "aaaaaaaa")
    model_config.select(tier="cloud")          # 只点档位，没填 key
    raw = json.loads((data_dir / "tenants" / "aaaaaaaa" / "model_config.json").read_text(encoding="utf-8"))
    assert raw["cloud"]["api_key"] == model_config._ENV_KEY_SENTINEL, "服务器 key 不许抄进客户目录"
    # 读回来仍然是能用的真 key
    assert model_config.load()["cloud"]["api_key"] == "server-key-123"
    assert model_config.active()["usable"] is True


def test_user_key_is_stored_as_is(data_dir, monkeypatch):
    monkeypatch.setattr(model_config, "_tenant_slug", lambda: "bbbbbbbb")
    model_config.select(tier="cloud", api_key="user-own-key")
    raw = json.loads((data_dir / "tenants" / "bbbbbbbb" / "model_config.json").read_text(encoding="utf-8"))
    assert raw["cloud"]["api_key"] == "user-own-key"
    assert model_config.load()["cloud"]["api_key"] == "user-own-key"


def test_lock_switch_defaults_off(data_dir, monkeypatch):
    monkeypatch.delenv("CASH_LOCK_MODEL", raising=False)
    assert model_config.locked() is False
    monkeypatch.setenv("CASH_LOCK_MODEL", "1")
    assert model_config.locked() is True, "运维仍需一个临时全站只读的开关"
