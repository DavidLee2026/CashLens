"""项目归属从文字里确定性提取（2026-10-19 立）

起因：David 第二次说「收到尾款 5200，这笔记到秋季市集」，草稿却落到「未归项目」——
因为项目归属**完全靠模型抽**，这一次它没抽出来。与 9/13 工时那次同一个病：
**结构化字段不能让模型猜**。

口径（与 `_text_channel` 同源）：能否识别由服务端的规则决定，模型只做兜底。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.main import _project_from_text  # noqa: E402

KNOWN = ["秋季市集", "示例项目B"]


def test_hint_phrase_after_amount_is_picked_up():
    assert _project_from_text("收到尾款 5200，这笔记到秋季市集", KNOWN) == "秋季市集"
    assert _project_from_text("打车报销 86，记到秋季市集", KNOWN) == "秋季市集"
    assert _project_from_text("这笔记入「示例项目B」", KNOWN) == "示例项目B"


def test_existing_project_name_mentioned_anywhere():
    """没有「记到」句式时，文字里出现已知项目名也算。"""
    assert _project_from_text("秋季市集收到尾款 5200", KNOWN) == "秋季市集"


def test_no_project_mentioned_stays_empty():
    """没说项目就是未归项目 —— 不许瞎猜（这是 9/26 定死的那条）。"""
    assert _project_from_text("收到尾款 5200", KNOWN) is None
    assert _project_from_text("", KNOWN) is None


def test_longer_candidate_snaps_back_to_known_name():
    """「秋季市集项目」要落回「秋季市集」——`projects.resolve` 只做精确匹配，
    多一个字就会新建出一个重复项目。"""
    assert _project_from_text("收到尾款 5200，这笔记到秋季市集项目", KNOWN) == "秋季市集"


def test_unknown_name_is_returned_as_is():
    """表里没有的名字照样带出去，由 resolve_incoming 按这个名字新建（别丢掉用户的归类意图）。"""
    assert _project_from_text("收到尾款 5200，这笔记到春日市集", KNOWN) == "春日市集"


def test_chat_paths_use_the_deterministic_extractor():
    """护栏：两条记账路径都必须先问文字，不许再让「当前选中」或模型直接定项目。"""
    src = (Path(__file__).resolve().parents[1] / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    assert src.count("_project_from_text(text, _known_project_names())") == 2
