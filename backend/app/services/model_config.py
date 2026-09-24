"""模型档位配置（运行时切换 · 三档隐私与成本光谱）

档位：
  cloud  云端 LLM API（默认）：识别准确率最高，对话文本与票据图像会出本机
  local  本地模型（Ollama / LM Studio / vLLM 等 OpenAI 兼容端点）：不出本机，零 API 成本
  none   完全不用模型：不出本机；数电票 PDF 走本机文本层，拍照与截图识别不可用

设计约束：
1. api_key 只入不出。对外视图一律只回传「是否已配置」，绝不回传原值。
2. 配置落盘 data/model_config.json（data/ 已在 .gitignore 内），文件权限收紧到 600；
   未显式配置时回退环境变量，兼容既有 .env 部署方式。
3. 换模型不改架构：两处用模型的地方（对话语义理解、票据识别）都从这里取配置。
   票据识别跑在独立 MCP 进程里，不 import 本模块，直接读同一个 JSON；
   格式同源，见 engine/mcp/receipt_mcp.py 的 load_runtime_config。
4. 能力边界如实暴露：本地模型不保证支持图像输入，切换时必须把降级后果讲清楚。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]  # backend/app/services/model_config.py → 仓库根
ENV_PATH = REPO_ROOT / ".env"

_DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
_DEFAULT_MODEL = "doubao-seed-2-0-lite-260428"

TIERS = ("cloud", "local", "none")

TIER_META: dict[str, dict] = {
    "cloud": {
        "label": "云端 LLM API",
        "data_leaves_device": True,
        "cost": "按量计费",
        "desc": "识别准确率最高。对话文本与票据图像会上传到第三方大模型服务。",
    },
    "local": {
        "label": "本地模型",
        "data_leaves_device": False,
        "cost": "零 API 成本（耗本机算力）",
        "desc": "数据不出本机。需本机已运行 OpenAI 兼容推理服务，且模型须支持图像输入才能识别票据。",
    },
    "none": {
        "label": "不用模型",
        "data_leaves_device": False,
        "cost": "零",
        "desc": "完全不调用任何模型。数电票 PDF 走本机文本层解析；拍照与截图识别不可用，记账改为手动输入。",
    },
}

CLOUD_PRESETS: list[dict] = [
    {
        "id": "doubao-lite",
        "label": "豆包 Seed 2.0 Lite",
        "vendor": "豆包",
        "base_url": _DEFAULT_BASE_URL,
        "model": _DEFAULT_MODEL,
        "vision": True,
        "note": "默认档位。长数字串已实测会漏位，系统用格式校验与本机文本层交叉核对兜底。",
    },
]

LOCAL_PRESETS: list[dict] = [
    {
        "id": "custom",
        "label": "自定义 OpenAI 兼容端点",
        "vendor": "自定义",
        "base_url": "",
        "model": "",
        "vision": None,  # 无法自动探测，须用户声明
        "note": "适用于 Ollama / LM Studio / vLLM / Xinference 等。须自行确认模型是否支持图像输入。",
    },
    {
        "id": "ollama-qwen3-vl",
        "label": "Qwen3-VL 30B（MoE）",
        "vendor": "Ollama",
        "base_url": "http://localhost:11434/v1",
        "model": "qwen3-vl:30b-a3b",
        "vision": True,
        "note": "激活参数 3B 的 MoE，图像理解能力强，内存约需 20GB。",
    },
    {
        "id": "ollama-qwen-vl",
        "label": "Qwen2.5-VL 7B",
        "vendor": "Ollama",
        "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5-vl:7b",
        "vision": True,
        "note": "体积较小，显存约需 8GB；中文票据版式表现尚可。",
    },
    {
        "id": "ollama-minicpm-v",
        "label": "MiniCPM-V 8B",
        "vendor": "Ollama",
        "base_url": "http://localhost:11434/v1",
        "model": "minicpm-v:8b",
        "vision": True,
        "note": "偏 OCR 取向的小模型，票据文字识别可用。",
    },
    {
        "id": "ollama-llava",
        "label": "LLaVA 13B",
        "vendor": "Ollama",
        "base_url": "http://localhost:11434/v1",
        "model": "llava:13b",
        "vision": True,
        "note": "通用视觉模型，中文票据识别弱于前两者。",
    },
]


# ─── 读写 ───────────────────────────────────────────────
def config_path() -> Path:
    data_dir = Path(os.environ.get("CASH_DATA_DIR") or (REPO_ROOT / "data"))
    return data_dir / "model_config.json"


def _load_env_file() -> None:
    """把仓库根 .env 读进环境（已存在的值不覆盖；只读，不输出任何值）。"""
    if not ENV_PATH.exists():
        return
    try:
        text = ENV_PATH.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k and not os.environ.get(k):
            os.environ[k] = v.strip().strip('"')


def _defaults() -> dict:
    return {
        "tier": "cloud",
        "cloud": {
            "preset_id": "doubao-lite",
            "base_url": os.environ.get("LLM_BASE_URL") or _DEFAULT_BASE_URL,
            "model": os.environ.get("LLM_MODEL") or _DEFAULT_MODEL,
            "api_key": os.environ.get("LLM_API_KEY") or "",
            "declared_vision": True,
        },
        "local": {
            "preset_id": "custom",
            "base_url": "",
            "model": "",
            "api_key": "",
            "declared_vision": True,
        },
        "updated_at": "",
    }


def load() -> dict:
    """读配置（不落盘）。缺失字段补默认值，坏文件按默认处理，不抛异常。"""
    _load_env_file()
    cfg = _defaults()
    p = config_path()
    if not p.exists():
        return cfg
    try:
        saved = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return cfg
    if not isinstance(saved, dict):
        return cfg
    if saved.get("tier") in TIERS:
        cfg["tier"] = saved["tier"]
    for tier in ("cloud", "local"):
        slot = saved.get(tier)
        if isinstance(slot, dict):
            for k, v in slot.items():
                if v is not None:
                    cfg[tier][k] = v
    cfg["updated_at"] = str(saved.get("updated_at") or "")
    return cfg


def save(cfg: dict) -> dict:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        p.chmod(0o600)  # 内含 api_key，权限与环境文件一致
    except OSError:
        pass
    return cfg


def find_preset(tier: str, preset_id: str) -> dict | None:
    src = CLOUD_PRESETS if tier == "cloud" else LOCAL_PRESETS
    for preset in src:
        if preset["id"] == preset_id:
            return preset
    return None


# ─── 生效配置 ───────────────────────────────────────────
def active() -> dict:
    """当前生效的模型配置，供调用方直接使用（含密钥，仅限内部）。"""
    cfg = load()
    tier = cfg["tier"]
    if tier == "cloud":
        slot = cfg["cloud"]
        return {
            "tier": "cloud",
            "base_url": str(slot.get("base_url") or _DEFAULT_BASE_URL).rstrip("/"),
            "model": slot.get("model") or _DEFAULT_MODEL,
            "api_key": slot.get("api_key") or "",
            "vision": True,
            "usable": bool(slot.get("api_key")),
        }
    if tier == "local":
        slot = cfg["local"]
        server = str(slot.get("base_url") or "").rstrip("/")
        model = str(slot.get("model") or "")
        return {
            "tier": "local",
            "base_url": server,
            "model": model,
            "api_key": slot.get("api_key") or "",
            "vision": bool(slot.get("declared_vision", True)),
            "usable": bool(server and model),
        }
    return {"tier": "none", "base_url": "", "model": "", "api_key": "",
            "vision": False, "usable": True}


def warnings_for(act: dict) -> list[str]:
    """把当前档位的能力降级如实说出来，不藏着。"""
    out: list[str] = []
    if act["tier"] == "local":
        if not act["vision"]:
            out.append("当前本地模型未声明支持图像输入：票据识别（拍照/截图）不可用，"
                       "只有数电票 PDF 的本机文本层解析可用。")
        out.append("本地模型的识别准确率通常低于云端，20 位发票号码这类长数字串漏读风险更高；"
                   "系统仍会做格式硬校验与人工确认，但请以原件为准。")
    if act["tier"] == "none":
        out.append("当前不调用任何模型：对话记账需手动输入，拍照与截图的票据识别不可用，"
                   "数电票 PDF 仍可本机解析。")
    if act["tier"] == "cloud" and not act["usable"]:
        out.append("云端档位尚未配置密钥，将回退到规则解析，识别能力下降。")
    return out


def public_view() -> dict:
    """对外视图：绝不含任何密钥，只回传是否已配置。"""
    cfg = load()
    act = active()
    tiers = []
    for tier in TIERS:
        meta = TIER_META[tier]
        entry: dict = {
            "id": tier,
            "label": meta["label"],
            "desc": meta["desc"],
            "data_leaves_device": meta["data_leaves_device"],
            "cost": meta["cost"],
            "active": cfg["tier"] == tier,
        }
        if tier == "cloud":
            pre = find_preset("cloud", cfg["cloud"].get("preset_id") or "doubao-lite")
            entry.update({
                "vendor": (pre or {}).get("vendor") or "云端服务",
                "preset_id": cfg["cloud"].get("preset_id") or "doubao-lite",
                "preset_label": (pre or {}).get("label") or "",
                "model": cfg["cloud"].get("model") or _DEFAULT_MODEL,
                "base_url": cfg["cloud"].get("base_url") or _DEFAULT_BASE_URL,
                "vision": True,
                "configured": bool(cfg["cloud"].get("api_key")),
                "usable": bool(cfg["cloud"].get("api_key")),
            })
        elif tier == "local":
            pre = find_preset("local", cfg["local"].get("preset_id") or "custom")
            server = cfg["local"].get("base_url") or ""
            model = cfg["local"].get("model") or ""
            entry.update({
                "vendor": (pre or {}).get("vendor") or "本地",
                "preset_id": cfg["local"].get("preset_id") or "custom",
                "preset_label": (pre or {}).get("label") or "",
                "model": model,
                "base_url": server,
                "vision": bool(cfg["local"].get("declared_vision", True)),
                "configured": bool(server and model),
                "usable": bool(server and model),
            })
        else:
            entry.update({"vendor": None, "model": None, "base_url": "",
                          "vision": False, "configured": True, "usable": True})
        tiers.append(entry)

    return {
        "tier": cfg["tier"],
        "active": {"tier": act["tier"], "model": act["model"],
                   "vision": act["vision"], "usable": act["usable"]},
        "tiers": tiers,
        "cloud_presets": CLOUD_PRESETS,
        "local_presets": LOCAL_PRESETS,
        "api_key_configured": bool(cfg["cloud"].get("api_key")),
        "updated_at": cfg["updated_at"],
        "warnings": warnings_for(act),
        "privacy_note": "无论选哪一档，账本、状态计算与现金流预测都在本机完成，不经任何模型。",
    }


def select(tier: str, base_url: str | None = None, model: str | None = None,
           api_key: str | None = None, preset_id: str | None = None,
           declared_vision: bool | None = None) -> dict:
    """切换档位或修改端点配置。未传入的字段保持原值。"""
    if tier not in TIERS:
        raise ValueError(f"未知档位：{tier}")
    cfg = load()
    cfg["tier"] = tier
    if tier in ("cloud", "local"):
        slot = cfg[tier]
        if preset_id:
            pre = find_preset(tier, preset_id)
            if pre is None:
                raise ValueError(f"未知预设：{preset_id}")
            slot["preset_id"] = preset_id
            if pre.get("base_url"):
                slot["base_url"] = pre["base_url"]
            if pre.get("model"):
                slot["model"] = pre["model"]
            if pre.get("vision") is not None:
                slot["declared_vision"] = bool(pre["vision"])
        if base_url is not None:
            slot["base_url"] = base_url.strip()
        if model is not None:
            slot["model"] = model.strip()
        if api_key:
            slot["api_key"] = api_key.strip()
        if declared_vision is not None:
            slot["declared_vision"] = bool(declared_vision)
        # 只点档位、没选具体预设时，自动套用第一个具体模型预设，避免出现「切过去了但不可用」的空档位；
        # 但用户显式选了「自定义端点」预设时必须留空，等他填地址与模型名。
        if (tier == "local" and not preset_id
                and not (slot.get("base_url") and slot.get("model"))):
            for pre in LOCAL_PRESETS:
                if pre["id"] != "custom" and pre.get("base_url") and pre.get("model"):
                    slot["preset_id"] = pre["id"]
                    slot["base_url"] = pre["base_url"]
                    slot["model"] = pre["model"]
                    slot["declared_vision"] = bool(pre.get("vision", True))
                    break
    cfg["updated_at"] = datetime.now().isoformat(timespec="seconds")
    save(cfg)
    return public_view()


def probe() -> dict:
    """按当前档位做一次连通性自检。只发一句话，不发送任何用户数据。"""
    act = active()
    if act["tier"] == "none":
        return {"ok": True, "tier": "none", "detail": "当前档位不调用模型，无需连通。"}
    if not act["usable"]:
        return {"ok": False, "tier": act["tier"], "model": act["model"],
                "detail": "端点地址、模型名或密钥不完整。"}
    payload = {"model": act["model"],
               "messages": [{"role": "user", "content": "ping"}],
               "max_tokens": 1}
    req = urllib.request.Request(
        f"{act['base_url']}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {act['api_key'] or 'local'}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            resp.read(64)
        return {"ok": True, "tier": act["tier"], "model": act["model"],
                "detail": "端点可用。"}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "ignore")[:160]
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "tier": act["tier"], "model": act["model"],
                "detail": f"HTTP {e.code}", "body": body}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "tier": act["tier"], "model": act["model"],
                "detail": f"{type(e).__name__}: {str(e)[:120]}"}
