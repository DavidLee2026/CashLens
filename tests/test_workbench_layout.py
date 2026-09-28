"""工作台界面护栏：一个滚动条、不带框、一次导入只占一个框。

起因（David 2026-09-28 真机）：「页面里需要滚动的地方太多了，总的页面滚动、对话框滚动、
左边下面的最近信息滚动，太多了，**去掉框**，和 deepseek harness 一样」；
「对话框里导入文件的信息，就在一个框里进行，不要我说一句话，然后又出来了一个导入框，
然后产品又说一句话，又一个导入框，太多信息了，看得头晕。」

这些是**体验决策**，不是随手能改的实现细节 —— 本项目已有先例：两栏等高那套规则被
"顺手"改回 height、左栏又长出滚动条、`.ev-list` 的内部滚动被当成 bug 删掉。
所以对源码下一道护栏，改动必须连测试一起改，逼人停下来想一遍。
"""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PAGE = _ROOT / "frontend" / "app" / "page.tsx"
_CSS = _ROOT / "frontend" / "app" / "globals.css"


def _page_src() -> str:
    return _PAGE.read_text(encoding="utf-8")


def _css_rule(selector: str) -> str:
    """取出一条 CSS 规则（本项目 CSS 没有嵌套，取到第一个 } 即可）。"""
    css = _CSS.read_text(encoding="utf-8")
    i = css.index(selector)
    return css[i: css.index("}", i) + 1]


# ---------------------------------------------------------------- 一个滚动条


def test_conversation_does_not_scroll_on_its_own():
    """对话区不许自己滚 —— 整页只留一个滚动条。

    旧的「两栏等高 + 对话内部滚」解法是 `.log{flex:1;height:0;contain:size;overflow-y:auto}`，
    用户已经明确不要（对话框一条、页面一条、最近事件一条，三条滚动条看着头晕）。
    现在靠 `.log{flex:1 1 auto}` 跟着内容长高 + `.composer{position:sticky}` 钉住输入区，
    长对话时输入框仍然够得着，不需要第二个滚动条。
    """
    rule = _css_rule(".log{")
    assert "overflow-y:auto" not in rule.replace(" ", ""), (
        "对话区又开始自己滚了 —— 用户要的是「整页只有一个滚动条」"
    )
    assert "contain:size" not in rule, (
        "contain:size 是「对话内部滚」那套解法的零件，去掉内部滚动后它会把内容高度吃掉"
    )
    assert "flex:1 1 auto" in rule, "对话区应当跟着内容长高（flex:1 1 auto），把余高留给输入区"


def test_composer_sticks_to_the_viewport_bottom():
    """输入区整组 sticky 钉在视口底部：整页只有一个滚动条时，长对话不能把输入框推出屏幕。"""
    rule = _css_rule(".composer{")
    assert "position:sticky" in rule.replace(" ", ""), "输入区不 sticky 就会被长对话推出屏幕"
    assert "bottom:0" in rule.replace(" ", ""), "sticky 要贴在底边（bottom:0）"


def test_recent_events_has_no_inner_scrollbar():
    """「最近事件」不许再有局部滚动窗口 —— 改成默认露 5 条 + 展开其余。"""
    rule = _css_rule(".ev-list{")
    assert "overflow" not in rule, "最近事件不该再有第二个滚动条（用户明确说太多了）"
    assert "max-height" not in rule, "窗口高度那套（calc(5 * 45.5px)）随内部滚动一起去掉"

    src = _page_src()
    assert "展开其余" in src, "去掉滚动之后必须给「展开其余 N 笔」的出口"
    assert "showAllEvents ? events.length : EV_WINDOW" in src, (
        "默认只露 EV_WINDOW 条，展开才全量 —— 否则去掉滚动会让左栏无限长"
    )


# ---------------------------------------------------------------- 去掉框


def test_panels_are_not_cards():
    """左右两栏不再是带边框/阴影的卡片（与 deepseek harness 一样是平面布局）。"""
    css = _CSS.read_text(encoding="utf-8")
    assert ".card{" not in css, "`.card` 那套边框 + 阴影已按用户要求去掉，别再长回来"
    src = _page_src()
    assert 'className="card ' not in src, "两栏不该再挂 card 类（框已经去掉）"


def test_import_progress_keeps_one_bordered_box():
    """一次导入只占**一个框**：整次导入是"一件事"，需要一眼看出边界；其余消息不加框。"""
    rule = _css_rule(".b.import{")
    assert "border:1px solid var(--border)" in rule, "导入框要有边界（它是唯一保留的框）"
    ai = _css_rule(".b.ai{")
    assert "border" not in ai, "助手消息不该再包框"


# ---------------------------------------------------------------- 一次导入一个框


def test_import_is_a_single_message_with_progress_and_summary():
    """导入不许再拆成「用户气泡 → 进度气泡 → 结果气泡」三段。"""
    src = _page_src()
    assert "（导入 ${items.length} 个文件）" not in src, (
        "「（导入 N 个文件）」那个用户气泡已按用户要求去掉 —— 一次导入只留一个框"
    )
    assert "isImport: true" in src, "导入消息要带 isImport 标记（标题 + 文件 + 汇总同框渲染）"
    assert "importTitle" in src, "导入框要有标题行（导入 N 个文件）"
    # 进度与最终汇总都走同一个 paint：收尾那次只是把 progress 关掉
    assert 'paint(summary || "没有可导入的内容。", false)' in src, (
        "最终汇总必须原地落在同一个导入框里，不能再 push 一条新消息"
    )


def test_import_file_rows_are_one_line_until_expanded():
    """逐文件一行；只有正在跑的那个（或手动点开的）才展开四段明细。"""
    src = _page_src()
    assert "pb-brief" in src, "完成的文件要显示一行结果摘要（而不是四段明细全铺开）"
    assert 'const open = !b.done || openFile === b.file;' in src, (
        "四段明细只在「正在跑」或「手动点开」时展开 —— 十几个文件各四段能刷满整屏"
    )
