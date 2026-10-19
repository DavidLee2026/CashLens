"""模型档位配置（三档隐私与成本光谱）单测

所有用例都把 .env 与档位配置文件隔离到临时目录，
既不碰真实密钥，也不会因为读了真实 .env 而意外发起网络调用。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.services import model_config  # noqa: E402


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """隔离真实 .env 与真实配置：全部落到临时目录。"""
    monkeypatch.setattr(model_config, "ENV_PATH", tmp_path / "nonexistent.env")
    monkeypatch.setattr(model_config, "config_path", lambda: tmp_path / "model_config.json")
    for var in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    # 2026-10-19：public_view 会问一次本机 Ollama 装了哪些模型 —— 单测一律默认「探不到」，
    # 保证这套用例在任何机器上都不发网络请求；需要结论的用例自己 stub。
    monkeypatch.setattr(model_config, "_ollama_tags", lambda *a, **k: None)
    return model_config


# ─── 默认档位 ───────────────────────────────────────────
def test_default_tier_is_cloud(isolated):
    act = isolated.active()
    assert act["tier"] == "cloud"
    assert act["model"]  # 有默认模型名
    assert act["vision"] is True


def test_cloud_without_key_is_not_usable(isolated):
    act = isolated.active()
    assert act["usable"] is False


def test_cloud_with_env_key_becomes_usable(isolated, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-test-not-real")
    act = isolated.active()
    assert act["usable"] is True


# ─── 切换档位 ───────────────────────────────────────────
def test_select_local_preset(isolated):
    view = isolated.select("local", preset_id="ollama-qwen-vl")
    act = isolated.active()
    assert act["tier"] == "local"
    assert act["base_url"] == "http://localhost:11434/v1"
    assert act["model"] == "qwen2.5-vl:7b"
    # 本地端点不需要密钥
    assert act["usable"] is True
    assert view["tier"] == "local"


def test_select_none_tier(isolated):
    isolated.select("none")
    act = isolated.active()
    assert act["tier"] == "none"
    assert act["usable"] is True
    assert act["vision"] is False
    assert act["base_url"] == ""


def test_local_switch_without_preset_autofills(isolated):
    """界面上只点一下「本地模型」时，不能给出一个空配置的档位。"""
    isolated.select("local")
    act = isolated.active()
    assert act["usable"] is True
    assert act["base_url"]
    assert act["model"]


def test_autofill_does_not_overwrite_configured_local(isolated):
    """已经配好自定义端点时，再次切档不得被自动预设覆盖。"""
    isolated.select("local", preset_id="custom",
                    base_url="http://127.0.0.1:1234/v1", model="my-local-vl")
    isolated.select("local")
    act = isolated.active()
    assert act["base_url"] == "http://127.0.0.1:1234/v1"
    assert act["model"] == "my-local-vl"


def test_select_custom_endpoint(isolated):
    isolated.select("local", preset_id="custom",
                    base_url="http://127.0.0.1:1234/v1", model="my-local-vl",
                    declared_vision=True)
    act = isolated.active()
    assert act["base_url"] == "http://127.0.0.1:1234/v1"
    assert act["model"] == "my-local-vl"
    assert act["usable"] is True


def test_custom_without_endpoint_not_usable(isolated):
    isolated.select("local", preset_id="custom")
    assert isolated.active()["usable"] is False


def test_select_rejects_unknown_tier(isolated):
    with pytest.raises(ValueError):
        isolated.select("gpu-cluster")


def test_select_rejects_unknown_preset(isolated):
    with pytest.raises(ValueError):
        isolated.select("local", preset_id="not-a-real-preset")


def test_declared_vision_false_propagates(isolated):
    """自定义端点无法自动探测视觉能力，用户声明为否时必须如实传递。"""
    isolated.select("local", preset_id="custom", base_url="http://localhost:9/v1",
                    model="x", declared_vision=False)
    assert isolated.active()["vision"] is False


# ─── 持久化 ─────────────────────────────────────────────
def test_persistence_roundtrip(isolated):
    isolated.select("local", preset_id="ollama-minicpm-v")
    fresh = isolated.load()
    assert fresh["tier"] == "local"
    assert fresh["local"]["model"] == "minicpm-v:8b"
    assert fresh["updated_at"]


def test_config_file_is_owner_only(isolated):
    """配置文件里可能含密钥，权限必须收紧到 600。"""
    isolated.select("cloud", preset_id="doubao-lite", api_key="sk-test-not-real")
    mode = isolated.config_path().stat().st_mode & 0o777
    assert mode == 0o600


def test_broken_config_falls_back_to_defaults(isolated):
    isolated.config_path().write_text("{ 这不是合法 JSON", encoding="utf-8")
    act = isolated.active()
    assert act["tier"] == "cloud"  # 坏配置不应让整体崩掉


# ─── 密钥不外泄 ─────────────────────────────────────────
def test_public_view_never_leaks_api_key(isolated, monkeypatch):
    secret = "sk-super-secret-value"
    monkeypatch.setenv("LLM_API_KEY", secret)
    dumped = json.dumps(isolated.public_view(), ensure_ascii=False)
    assert secret not in dumped
    assert '"api_key"' not in dumped  # 不存在名为 api_key 的字段，只有布尔标记
    assert isolated.public_view()["api_key_configured"] is True


def test_public_view_has_three_tiers(isolated):
    view = isolated.public_view()
    assert [t["id"] for t in view["tiers"]] == ["cloud", "local", "none"]
    by = {t["id"]: t for t in view["tiers"]}
    assert by["cloud"]["data_leaves_device"] is True
    assert by["local"]["data_leaves_device"] is False
    assert by["none"]["data_leaves_device"] is False
    assert by["cloud"]["active"] is True


def test_public_view_marks_active_tier_after_switch(isolated):
    isolated.select("local", preset_id="ollama-qwen-vl")
    by = {t["id"]: t for t in isolated.public_view()["tiers"]}
    assert by["local"]["active"] is True
    assert by["cloud"]["active"] is False


# ─── 能力降级必须如实提示 ───────────────────────────────
def test_local_tier_warns_about_accuracy_drop(isolated):
    isolated.select("local", preset_id="ollama-qwen-vl")
    warnings = isolated.public_view()["warnings"]
    assert any("准确率" in w for w in warnings)


def test_local_without_vision_warns_recognition_unavailable(isolated):
    isolated.select("local", preset_id="custom", base_url="http://localhost:9/v1",
                    model="text-only", declared_vision=False)
    warnings = isolated.public_view()["warnings"]
    assert any("图像输入" in w for w in warnings)


def test_none_tier_warns_photo_unavailable(isolated):
    isolated.select("none")
    warnings = isolated.public_view()["warnings"]
    assert any("拍照" in w for w in warnings)


def test_cloud_tier_has_no_warning_when_usable(isolated, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-test-not-real")
    assert isolated.public_view()["warnings"] == []


def test_privacy_note_states_ledger_is_local(isolated):
    """无论哪一档，都要说清账本与预测不经模型。"""
    note = isolated.public_view()["privacy_note"]
    assert "本机" in note


# ─── 自检端点的 none 档短路 ─────────────────────────────
def test_probe_short_circuits_on_none_tier(isolated):
    isolated.select("none")
    out = isolated.probe()
    assert out["ok"] is True
    assert "无需连通" in out["detail"]


# ─── 本机是否真的装了这些模型（2026-10-19 立）────────────────
# 起因：界面把四个本地模型并列摆着，看着都能用；而真机上往往只 pull 了其中一个，
# 选没装的那个必然识别失败 —— 界面承诺大于实际能力。
# 规矩：探到才下结论；探不到一律 None（不许把选项标成不可用）。
def _by_id(view: dict) -> dict:
    return {p["id"]: p for p in view["local_presets"]}


def test_local_presets_mark_installed_from_ollama_tags(isolated, monkeypatch):
    monkeypatch.setattr(model_config, "_ollama_tags",
                        lambda *a, **k: {"qwen3-vl:30b-a3b", "llava:13b"})
    by = _by_id(isolated.public_view())
    assert by["ollama-qwen3-vl"]["installed"] is True
    assert by["ollama-llava"]["installed"] is True
    assert by["ollama-qwen-vl"]["installed"] is False
    assert by["ollama-minicpm-v"]["installed"] is False
    # 「自定义端点」是别人的服务，探不到就不猜
    assert by["custom"]["installed"] is None


def test_local_presets_unknown_when_ollama_unreachable(isolated, monkeypatch):
    monkeypatch.setattr(model_config, "_ollama_tags", lambda *a, **k: None)
    view = isolated.public_view()
    assert all(p["installed"] is None for p in view["local_presets"])
    assert view["tier"] == "cloud"                  # 探不到不许影响档位判断
    assert [t["id"] for t in view["tiers"]] == ["cloud", "local", "none"]


def test_public_view_does_not_mutate_local_presets(isolated, monkeypatch):
    monkeypatch.setattr(model_config, "_ollama_tags", lambda *a, **k: {"qwen3-vl:30b-a3b"})
    for _ in range(3):
        isolated.public_view()
    assert all("installed" not in p for p in model_config.LOCAL_PRESETS)


def test_ollama_tags_parses_payload(monkeypatch):
    class _Resp:
        def read(self):
            return json.dumps({"models": [{"name": "qwen3-vl:30b-a3b"},
                                          {"name": "llava:13b"},
                                          {"size": 1}]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(model_config.urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert model_config._ollama_tags(refresh=True) == {"qwen3-vl:30b-a3b", "llava:13b"}


def test_ollama_tags_is_none_on_connection_error(monkeypatch):
    def boom(*a, **k):
        raise model_config.urllib.error.URLError("connection refused")

    monkeypatch.setattr(model_config.urllib.request, "urlopen", boom)
    assert model_config._ollama_tags(refresh=True) is None


def test_ollama_tags_is_none_on_garbage(monkeypatch):
    class _Resp:
        def read(self):
            return b"<html>not json</html>"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(model_config.urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert model_config._ollama_tags(refresh=True) is None


def test_model_picker_marks_missing_local_models():
    """界面护栏：没装的那档必须挂出「本机未安装」，不能和能用的长得一样。"""
    root = Path(__file__).resolve().parents[1]
    page = (root / "frontend" / "app" / "page.tsx").read_text(encoding="utf-8")
    assert 'p.installed === false' in page
    assert "本机未安装" in page
    assert "ollama pull" in page        # 悬停给出补救命令
    css = (root / "frontend" / "app" / "globals.css").read_text(encoding="utf-8")
    assert ".model-subitem.off" in css
