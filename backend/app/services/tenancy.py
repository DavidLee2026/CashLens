"""邀请码租户：一码一账（2026-10-19 立）

## 为什么要有这一层

上线给朋友用（对账）时，两个硬需求：
1. **一个地址**（cashlens.leerobert.site），不同人输不同的码进去；
2. **各自的账**——不能像原型那样「所有人共用一本账」。

本文件只解决这两件事，不碰业务逻辑：业务服务（ledger / pending / projects / …）
本来就接收 `data_dir` 参数，这里只负责「当前请求该用哪个目录」。

## 四条设计约束（改代码前先读）

1. **默认不启用**。只有 `CASH_MULTI_TENANT=1` 时才生效；关着的时候 `data_dir()`
   就是 `CASH_DATA_DIR` 本身，与改造前完全一致（本地 345 个用例、启动脚本、
   「服务客户用隔离数据目录」那条 SOP 都不受影响）。
2. **码只存哈希**。`tenants.json` 里存 `salt` + `code_hash`，明文只在生成那一刻
   打印一次。库存文件被看到，不等于码泄露。
3. **目录名不用码、也不用真名**。slug 是随机 8 位十六进制：既防路径穿越，
   也防「从目录列表看出客户是谁」。
4. **模型配置留在根目录**。`model_config.json` 是设备级配置（含 API key），
   不属于任何客户 —— 租户目录里只放这个客户自己的账本与草稿。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]  # backend/app/services/tenancy.py → 仓库根

COOKIE_NAME = "cl_tenant"
SESSION_DAYS = 30
ALLOW_PREFIXES = ("/api/session/",)          # 不需要 session 的端点前缀
ALLOW_EXACT = {"/api/health", "/api/session"}  # 不需要 session 的精确路径

# 邀请码字母表：去掉 0/O/1/I/L，手抄不容易错
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_SLUG_RE = re.compile(r"^[0-9a-f]{8}$")

_current_slug: ContextVar[str | None] = ContextVar("cashlens_tenant_slug", default=None)


# ─── 开关与根目录 ────────────────────────────────────────
def enabled() -> bool:
    """多租户是否启用。默认关：本地开发行为与改造前一致。"""
    return os.environ.get("CASH_MULTI_TENANT", "").strip() in ("1", "true", "yes", "on")


def root() -> Path:
    """数据根目录（租户表、密钥、模型配置、tenants/ 都在这里）。"""
    return Path(os.environ.get("CASH_DATA_DIR") or (REPO_ROOT / "data"))


def tenants_dir() -> Path:
    return root() / "tenants"


def current_slug() -> str | None:
    return _current_slug.get()


def set_current(slug: str | None) -> None:
    _current_slug.set(slug)


def reset_current() -> None:
    _current_slug.set(None)


# ─── 当前请求该用哪个目录 ────────────────────────────────
def data_dir() -> Path:
    """当前请求的数据目录：多租户命中租户目录，否则回落根目录。"""
    slug = current_slug()
    if enabled() and slug:
        d = tenants_dir() / slug
        d.mkdir(parents=True, exist_ok=True)
        return d
    d = root()
    d.mkdir(parents=True, exist_ok=True)
    return d


def ledger_path() -> Path:
    return data_dir() / "finance_events.jsonl"


def db_path() -> Path:
    return data_dir() / "proj.db"


def model_config_path() -> Path:
    """模型配置永远在根目录（设备级），不随租户走。"""
    root().mkdir(parents=True, exist_ok=True)
    return root() / "model_config.json"


# ─── 码与租户表 ─────────────────────────────────────────
def _codes_path() -> Path:
    root().mkdir(parents=True, exist_ok=True)
    return root() / "tenants.json"


def _load() -> list[dict]:
    p = _codes_path()
    if not p.exists():
        return []
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("slug")]


def _save(rows: list[dict]) -> None:
    p = _codes_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass


def _hash(code: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{code}".encode("utf-8")).hexdigest()


def normalize_code(raw: str) -> str:
    """容错：忽略大小写、空格、多余的连字符（用户手抄/微信转发容易变形）。"""
    s = re.sub(r"[^A-Za-z0-9]", "", raw or "").upper()
    return s


def _format_code() -> str:
    groups = ["".join(secrets.choice(_ALPHABET) for _ in range(4)) for _ in range(3)]
    return "CL-" + "-".join(groups)


def create_code(label: str) -> tuple[str, str]:
    """建一个租户，返回 (明文码, slug)。明文只在这里出现一次。"""
    label = (label or "").strip() or "未命名"
    rows = _load()
    for _ in range(20):
        slug = secrets.token_hex(4)          # 8 位十六进制
        if _SLUG_RE.match(slug) and not any(r["slug"] == slug for r in rows):
            break
    else:  # pragma: no cover - 概率上不会发生
        raise RuntimeError("生成 slug 失败")
    code = _format_code()
    salt = secrets.token_hex(8)
    rows.append({
        "slug": slug,
        "label": label,
        "salt": salt,
        "code_hash": _hash(normalize_code(code), salt),
        "active": True,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "last_seen": "",
    })
    _save(rows)
    (tenants_dir() / slug).mkdir(parents=True, exist_ok=True)
    return code, slug


def list_tenants() -> list[dict]:
    """对外视图：绝不含 salt 与 hash。"""
    return [{"slug": r["slug"], "label": r.get("label", ""),
             "active": bool(r.get("active")), "created_at": r.get("created_at", ""),
             "last_seen": r.get("last_seen", "")} for r in _load()]


def get_tenant(slug: str) -> dict | None:
    for r in _load():
        if r["slug"] == slug:
            return r
    return None


def verify_code(code: str) -> str | None:
    """码对不对。命中返回 slug。码长固定、哈希比较用 compare_digest。"""
    norm = normalize_code(code)
    if len(norm) < 11:          # CL + 12 位
        return None
    for r in _load():
        if not r.get("active"):
            continue
        if hmac.compare_digest(_hash(norm, r.get("salt", "")), r.get("code_hash", "")):
            return r["slug"]
    return None


def touch(slug: str) -> None:
    rows = _load()
    changed = False
    for r in rows:
        if r["slug"] == slug:
            r["last_seen"] = datetime.now().isoformat(timespec="seconds")
            changed = True
    if changed:
        _save(rows)


def revoke(slug: str) -> bool:
    rows = _load()
    hit = False
    for r in rows:
        if r["slug"] == slug:
            r["active"] = False
            hit = True
    if hit:
        _save(rows)
    return hit


def rotate(slug: str) -> str | None:
    """换码（旧码立即失效，账本不动）。"""
    rows = _load()
    code = _format_code()
    for r in rows:
        if r["slug"] == slug:
            r["salt"] = secrets.token_hex(8)
            r["code_hash"] = _hash(normalize_code(code), r["salt"])
            r["active"] = True
            _save(rows)
            return code
    return None


# ─── 会话 cookie（HMAC 签名，不引第三方库）───────────────
def _secret() -> bytes:
    p = root() / "session.key"
    root().mkdir(parents=True, exist_ok=True)
    if p.exists():
        try:
            data = p.read_bytes()
            if len(data) >= 32:
                return data
        except OSError:
            pass
    data = secrets.token_bytes(32)
    p.write_bytes(data)
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return data


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def issue_token(slug: str, days: int = SESSION_DAYS) -> str:
    """会话 token = slug.expiry.signature。

    签名里带该租户的 salt —— 所以 `rotate` 一换码，**旧会话立刻失效**（salt 变了，
    验签不过）；`revoke` 靠 active=false 拦。两者都不用去改客户端。
    """
    row = get_tenant(slug) or {}
    exp = int(time.time()) + days * 86400
    payload = f"{slug}.{exp}.{row.get('salt', '')}"
    return f"{slug}.{exp}.{_sign(payload)}"


def verify_token(token: str | None) -> str | None:
    """验签名 + 验过期 + 验租户仍在用 + 验 salt 未变。任何一步不过都当未登录。"""
    if not token:
        return None
    parts = token.rsplit(".", 2)
    if len(parts) != 3:
        return None
    slug, exp_s, sig = parts
    if not _SLUG_RE.match(slug) or not exp_s.isdigit():
        return None
    row = get_tenant(slug)
    if not row or not row.get("active"):
        return None
    if not hmac.compare_digest(_sign(f"{slug}.{exp_s}.{row.get('salt', '')}"), sig):
        return None
    if int(exp_s) <= int(time.time()):   # 到点即失效（含刚签发就过期）
        return None
    return slug


def session_cookie_secure() -> bool:
    """HTTPS 下必须 Secure；本地 http 调试可设 CASH_COOKIE_SECURE=0 关掉。"""
    return os.environ.get("CASH_COOKIE_SECURE", "1") != "0"


# ─── 登录失败限速（防穷举）──────────────────────────────
_attempts: dict[str, list[float]] = {}
_MAX_ATTEMPTS = 8
_WINDOW_SECONDS = 600.0


def too_many_attempts(who: str) -> bool:
    now = time.time()
    hits = [t for t in _attempts.get(who, []) if now - t < _WINDOW_SECONDS]
    _attempts[who] = hits
    return len(hits) >= _MAX_ATTEMPTS


def note_attempt(who: str) -> None:
    _attempts.setdefault(who, []).append(time.time())


def clear_attempts(who: str) -> None:
    _attempts.pop(who, None)


def reset_state_for_tests() -> None:
    """只在测试里用：清掉限速记忆与当前请求上下文。"""
    _attempts.clear()
    reset_current()
