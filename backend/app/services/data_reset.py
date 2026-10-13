"""本机演示数据「重置」——**存档再开新账**，不是删文件

## 为什么不能直接删（2026-09-29）

产品红线是「账本追加式、不可变、可追溯、误删可救」。把 `finance_events.jsonl`
截断就是破线动作。所以「重置」在这里的定义是：

1. 把当前整套**产品状态文件**搬进带时间戳的备份目录 `data/_reset_backup_<ts>/`；
2. 再起一套空账（账本为空、草稿为空、项目为空、投影重建）。

于是界面上能如实说「已备份到 data/_reset_backup_…」，需要时整套拷回即可恢复。

## 保留项：model_config.json

模型档位 / 端点 / 密钥属于**设备配置**，不是演示数据。清掉它，录视频时还得重新
配一遍模型 —— 那不是"重置账户"该干的事，所以**刻意不搬**。

## 演示样本

`seed_demo=True` 时把 `samples/synthetic_small_business/data/` 的合成样本装进来，
让每次重录的开局完全一致（否则第二次录的账目跟第一次不一样，解说词对不上）。
样本的 proj.db 不直接拷 —— 从账本重建，避免样本投影与当前 MODEL_VERSION 不一致。
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from . import pending as pending_svc
from . import projects as projects_svc
from . import timesheet_records
from .state_engine import EventLedger, Projection

# 重置时要搬走的**产品状态**（相对 data 目录）。
# ⚠️ 刻意不含 model_config.json，理由见模块头。
_STATE_FILES = (
    "finance_events.jsonl",
    "pending.json",
    "projects.json",
    "proj.db",
    "timesheet.jsonl",
    "_recog_cache.json",
)
_STATE_DIRS = ("_intake",)

# 合成演示样本（可复现的演示/回归数据，脚本 scripts/make_synthetic_business_ledger.py）
_SAMPLE_DIR = Path(__file__).resolve().parents[3] / "samples" / "synthetic_small_business" / "data"

# 模型档位配置：重置**不动**它（对外如实说明）
_KEPT_FILE = "model_config.json"


def _ledger_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "finance_events.jsonl"


def _db_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "proj.db"


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def snapshot(data_dir: str | Path) -> dict:
    """当前数据规模（重置前后各取一次，用来把数字如实摆给用户看）。"""
    data_dir = Path(data_dir)
    return {
        "events": len(EventLedger(_ledger_path(data_dir)).load()),
        "projects": len(projects_svc.visible_rows(data_dir)),
        "pending": len(pending_svc.list_all(data_dir)),
        "timesheet": len(timesheet_records.list_all(data_dir)),
    }


def reset(data_dir: str | Path, *, seed_demo: bool = False,
          sample_dir: str | Path | None = None) -> dict:
    """把本机演示数据重置为空（可选载入合成演示样本）。

    返回里带 `before` / `after` 两套计数与 `backup_dir` —— 界面据此如实回报，
    而不是只说一句"已重置"（说不清代价与去向的破坏性动作，用户没法放心）。
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    before = snapshot(data_dir)

    # 先搬走（**有东西才建备份目录**：本来就是空的时候不制造空备份目录）
    sources = [data_dir / n for n in _STATE_FILES if (data_dir / n).exists()]
    sources += [data_dir / d for d in _STATE_DIRS if (data_dir / d).is_dir()]
    backup_dir: Path | None = None
    moved: list[str] = []
    if sources:
        # 备份目录要唯一：同一秒内连着重置两次时，第二次不能覆盖/混进第一次的备份
        # （实测：`_stamp()` 只到秒，连续两次会落到同一个目录里）。
        stamp = _stamp()
        backup_dir = data_dir / f"_reset_backup_{stamp}"
        n = 2
        while backup_dir.exists():
            backup_dir = data_dir / f"_reset_backup_{stamp}-{n}"
            n += 1
        backup_dir.mkdir(parents=True, exist_ok=True)
        for src in sources:
            shutil.move(str(src), str(backup_dir / src.name))
            moved.append(src.name + ("/" if src.is_dir() else ""))

    # 起一套空账：投影从空账本重建（不是直接删 proj.db 就完事 —— 那样下次还要重建）
    proj = Projection(_db_path(data_dir))
    proj.rebuild([])
    proj.close()

    if seed_demo:
        after = _seed_demo(data_dir, Path(sample_dir) if sample_dir else _SAMPLE_DIR)
    else:
        after = {"events": 0, "projects": 0, "pending": 0, "timesheet": 0}

    kept = sorted(n for n in (_KEPT_FILE,) if (data_dir / n).exists())
    return {
        "ok": True,
        "action": "reset_seeded" if seed_demo else "reset",
        "backup_dir": _display(data_dir, backup_dir) if backup_dir else "",
        "moved": moved,
        "kept": kept,
        "before": before,
        "after": after,
    }


def _seed_demo(data_dir: Path, sample: Path) -> dict:
    """载入合成演示样本（账本 + 项目映射），投影从账本重建。"""
    if not (sample / "finance_events.jsonl").exists():
        raise FileNotFoundError(f"演示样本不存在：{sample}")
    shutil.copyfile(sample / "finance_events.jsonl", data_dir / "finance_events.jsonl")
    if (sample / "projects.json").exists():
        shutil.copyfile(sample / "projects.json", data_dir / "projects.json")
    proj = Projection(_db_path(data_dir))
    proj.rebuild(EventLedger(_ledger_path(data_dir)).load())
    proj.close()
    return snapshot(data_dir)


def _display(data_dir: Path, target: Path) -> str:
    """备份目录的展示路径：能相对仓库根就相对（`data/_reset_backup_…`），否则给绝对路径。

    测试会用临时数据目录（CASH_DATA_DIR），那种情况下 `relative_to` 会失败 ——
    不能为了好看的路径把重置本身弄崩。
    """
    try:
        return str(target.relative_to(data_dir.parent))
    except ValueError:
        return str(target)
