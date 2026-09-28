"""工作台界面护栏：两块各自滚、左灰右白、去掉卡片框、一次导入只占一个框。

起因（David 2026-09-28 真机，分两轮）：
① 「页面里需要滚动的地方太多了…**去掉框**，和 deepseek harness 一样」
   「对话框里导入文件的信息，就在一个框里进行…不要我说一句话又出来一个导入框」
② 我第一轮理解成「整页只留一个滚动条」→ 被当场否掉：
   「**左右板块没有区分**」「应该是**左边管左边，右边管右边**，怎么我上下滚动的时候，
   左边和右边顶部的文字在滚动？」并让我「看下 deepseek harness 是怎么做的」。
   正确模型 = **工作台不整页滚动**（`.main` 占满视口 − 顶栏），左栏自己滚、右栏自己滚；
   左栏是**浅灰的面**、右边是**纯白**，输入区是右栏底部一个**圆角框**（照 DSH）。

⚠️ 这些是体验决策，而且已经被改回去过一次，所以逐条钉住。
"""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PAGE = _ROOT / "frontend" / "app" / "page.tsx"
_CSS = _ROOT / "frontend" / "app" / "globals.css"


def _page_src() -> str:
    return _PAGE.read_text(encoding="utf-8")


def _css() -> str:
    return _CSS.read_text(encoding="utf-8")


def _rules(selector: str) -> str:
    """同名选择器的**所有**规则拼起来（同一选择器会出现在基础规则与窄屏覆盖里）。"""
    css = _css()
    out, i = [], 0
    while True:
        i = css.find(selector, i)
        if i == -1:
            return "\n".join(out)
        out.append(css[i: css.index("}", i) + 1])
        i += 1


def _rule(selector: str) -> str:
    """只出现一次的选择器，取那一条。"""
    got = _rules(selector)
    assert got, f"CSS 里找不到 {selector}"
    return got


def _block_after(start: str) -> str:
    """按花括号配对取出一段块（@media / :root 用）。"""
    css = _css()
    i = css.index(start)
    i = css.index("{", i)
    depth = 0
    for j in range(i, len(css)):
        if css[j] == "{":
            depth += 1
        elif css[j] == "}":
            depth -= 1
            if depth == 0:
                return css[i: j + 1]
    raise AssertionError(f"{start} 的花括号没配对")


def _blocks_after(start: str) -> list[str]:
    """把某个 @media 的**所有**同名块按花括号配对完整取出（同宽度可能写了两段）。"""
    css = _css()
    out, i = [], 0
    while True:
        i = css.find(start, i)
        if i == -1:
            return out
        brace = css.index("{", i)
        depth = 0
        for j in range(brace, len(css)):
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
                if depth == 0:
                    out.append(css[i: j + 1])
                    i = j
                    break
        i += 1


def _tight(text: str) -> str:
    """去掉所有空白，便于在 CSS 里做"含某条声明"的断言。"""
    return "".join(text.split())


# ------------------------------------------------------- 两块各自滚（用户原话：左边管左边）


def test_page_itself_does_not_scroll():
    """工作台占满视口、**整页不滚** —— 否则「顶部的文字会跟着滚」（用户原话）。"""
    main = _tight(_rules(".main{"))
    assert "height:100vh" in main, "`.main` 直接占满视口（顶栏已取消，品牌进了左栏），不能靠内容撑高"
    assert "display:flex" in main, "`.main` 是纵向 flex：grid 吃掉剩余高度、脚注占一行"
    assert "max-width" not in main, (
        "⚠️ 不能再来 max-width + margin auto：那会让左栏离窗口左边缘留出空白，"
        "用户要的是「账目信息移到最左边」（deepseek harness 的侧栏也是贴着窗口左边缘的）"
    )
    grid = _tight(_rules(".grid{"))
    assert "flex:1" in grid and "min-height:0" in grid, (
        "`.grid` 要 flex:1 + min-height:0，否则子项不肯收缩、整页又会被撑出滚动条"
    )


def test_brand_sits_at_the_top_of_the_left_panel():
    """品牌「CashLens 本地工作台」在**左栏顶部**（原来在通栏顶栏里）。

    结构：.side-head（品牌 + 登录，固定）+ .side-body（四段账目信息，自己滚）。
    """
    src = _page_src()
    assert 'className="side-head"' in src, "左栏顶部要有 .side-head"
    assert 'className="topbar"' not in src, "通栏顶栏已取消（品牌搬进左栏）"
    head = _tight(_rules(".side-head{"))
    assert "flex:00auto" in head, "品牌行固定高、不跟着滚（flex:0 0 auto）"
    assert "border-bottom:1pxsolidvar(--border)" in head, "品牌行下面一条发丝线，和下面的账目信息分开"
    # 品牌必须在左栏里，而不是在对话栏里
    assert src.index('className="side-head"') > src.index('<aside className="side"'), (
        "品牌行要放在 .side 里面"
    )
    assert "侧栏顶部" in src or "左栏顶部" in src, "留一句注释说明品牌为什么在左栏"


def test_left_and_right_scroll_independently():
    """左栏自己滚、右栏自己滚（左右各一条，互不影响）。

    左栏的滚动区是 `.side-body`（品牌与登录固定在上面，不跟着滚），不是 `.side` 本身。
    """
    assert "overflow-y:auto" in _tight(_rules(".side-body{")), "左栏中间那段要自己滚（左栏管左栏）"
    assert "overflow-y:auto" in _tight(_rules(".log{")), "对话区要自己滚（右栏管右栏）"
    # ⚠️ 别再给 .log 加 contain:size —— 那是"两栏等高 + 整页滚"时代的零件，
    # 用了会把内容高度吃掉（"整页滚"那一版已被用户否掉）。
    assert "contain:size" not in _tight(_rules(".log{"))


def test_composer_is_a_rounded_box_pinned_to_the_column_foot():
    """输入区 = 右栏底部一个**圆角框**（照 deepseek harness），不是通栏横线、也不 sticky。"""
    composer = _tight(_rule(".composer{"))
    assert "border:1pxsolidvar(--border)" in composer, "输入框要有边框（DSH 就是这样一个框）"
    assert "border-radius:14px" in composer, "圆角是它像「输入框」而不是「分栏线」的关键"
    assert "position:sticky" not in composer, (
        "整页已经不滚了，sticky 是上一版的补丁；留着会让输入框在栏内滑动"
    )


def test_narrow_screen_falls_back_to_single_column_page_scroll():
    """窄屏塞不下两栏，退回「单列 + 整页滚」（手机上双栏各自滚更难用）。

    ⚠️ 覆盖必须写在基础规则之后（媒体查询不增加优先级，本项目踩过静默失效）。
    """
    narrow = _tight("".join(_blocks_after("@media (max-width:980px)")))
    assert "height:auto" in narrow, "窄屏要把 .main 的固定高度放开"
    assert ".side-body{overflow:visible}" in narrow, "窄屏左栏不要自己的滚动条"
    assert "border-right:none" in narrow, "窄屏没有左右两栏，分隔线也要去掉"
    assert ".brandbar{display:inline-flex" in narrow and ".side-head.brand{display:none}" in narrow, (
        "窄屏左栏被排到对话下面：品牌要挪到对话区顶部那行 .brandbar，别在页面中间又出现一次"
    )


def test_narrow_overrides_come_after_the_rules_they_override():
    """窄屏覆盖必须写在基础规则**之后**。

    这是本项目唯一重复踩过两次的坑（2026-09-25 布局覆盖静默失效、2026-09-28 把品牌搬进
    左栏时又踩了一次：移动端品牌行整行不显示）。媒体查询**不增加优先级**，同优先级下后写的
    获胜 —— 所以"覆盖规则在文件里存在"不等于"它生效了"。这条按**顺序**断言。
    """
    css = _css()
    pairs = [
        (".brandbar{display:none", ".brandbar{display:inline-flex"),
        (".side-body{flex:1;min-height:0;overflow-y:auto", ".side-body{overflow:visible"),
        (".side{display:flex;flex-direction:column;gap:0;padding:0;min-height:0;",
         ".side{min-height:0;background:transparent"),
        (".main{height:100vh", ".main{height:auto"),
    ]
    for base, override in pairs:
        assert base in css, f"基础规则不见了：{base}"
        assert override in css, f"窄屏覆盖不见了：{override}"
        assert css.index(base) < css.index(override), (
            f"「{override}」写在了「{base}」前面 —— 媒体查询不加优先级，这条覆盖会静默失效"
        )


def test_right_column_header_has_dashed_rule_and_top_right_login():
    """用户 2026-09-28：「右边顶部有分栏虚线，右上角是客户需要登录输入用户名」。

    登录入口因此**不在左栏**了 —— 左栏左上角只留 logo（「左上角显示 logo」）。
    """
    head = _tight(_rules(".convo-head{"))
    assert "border-bottom:1pxdashedvar(--border)" in head, "右栏顶部那条分隔要**虚线**（用户原话）"
    assert "min-height:52px" in head, "与左栏 .side-head 同高，两条头线才能在一条水平线上"
    src = _page_src()
    i = src.index('className="convo-head"')
    convo_head = src[i: src.index('className="log"', i)]
    assert "登录" in convo_head and "nameText" in convo_head, "登录入口要在右栏顶部（右上角）"
    side_head = src[src.index('className="side-head"'): src.index('className="side-body"')]
    assert "nameText" not in side_head and "loginOpen" not in side_head, (
        "登录已挪到右栏右上角，左栏左上角只留 logo"
    )


# ------------------------------------------------------- 左右有区分（左灰右白）


def test_left_panel_is_a_distinct_surface():
    """左边浅灰的面 + 一条分隔，右边纯白 —— 修「左右板块没有区分」。"""
    assert "--bg:var(--white)" in _tight(_block_after(":root")), "页面 / 对话区底色应当是纯白"
    side = _tight(_rules(".side{"))
    assert "background:var(--gray-50)" in side, "左栏是浅灰的那一块（与右边纯白形成区分）"
    assert "border-right:1pxsolidvar(--border)" in side, "两块之间要有分隔"


def test_blocks_contrast_with_the_panel_they_sit_on():
    """左栏里的「块」必须与所在的面**反过来**（面浅灰 → 块白）。

    这条为什么单独立一条：今天已经来回翻过两次 —— 取消卡片时把格子改白（面变成了灰页面）、
    左栏改白时又得改回灰、最后照 DSH 把左栏改浅灰又得改白。
    改 `.side` 底色的人必须同时看这两处，否则格子会隐形。
    """
    assert "background:var(--card)" in _tight(_rule(".kpi .cell{")), (
        "左栏是浅灰面，KPI 小格要用白面才看得出来"
    )
    assert "background:var(--card)" in _tight(_rule(".pd-confirm{")), "批量确认条同理，要用白面"


def test_panels_are_not_cards():
    """两栏都不是带边框 / 阴影的卡片（"面"≠"框"：通高、无圆角、无四周描边）。"""
    assert ".card{" not in _css(), "`.card` 那套边框 + 阴影已按用户要求去掉，别再长回来"
    assert 'className="card ' not in _page_src(), "两栏不该再挂 card 类"


def test_recent_events_has_no_inner_scrollbar():
    """「最近事件」不许变成左栏里的**第三个**滚动区（用户第一次投诉的点）。"""
    rule = _rule(".ev-list{")
    assert "overflow" not in rule, "最近事件不该再有局部滚动条"
    assert "max-height" not in rule, "窗口高度那套（calc(5 * 45.5px)）随内部滚动一起去掉"
    src = _page_src()
    assert "展开其余" in src, "去掉滚动之后必须给「展开其余 N 笔」的出口"
    assert "showAllEvents ? events.length : EV_WINDOW" in src, "默认只露 EV_WINDOW 条，展开才全量"


def test_footer_line_belongs_to_the_right_column_and_left_panel_runs_to_the_bottom():
    """用户 2026-09-28：① 底部那行口径说明要在**右侧区域居中**；② 左边要通到底部一整块。

    这其实是同一个结构问题：那行说明原先挂在 `.main` 上（两栏之外），于是它跨**整窗**居中，
    还在左栏底下留出一条空带、左栏看起来没到底。搬进 `.convo` 之后两件事一起解决。
    """
    src = _page_src()
    i_foot = src.index('className="footnote"')
    assert i_foot < src.index('<aside className="side"'), (
        "口径说明必须在右栏（.convo）里 —— DOM 顺序是对话栏在前，所以它的位置要早于 .side"
    )
    assert i_foot > src.index('className="composer"'), "它在输入区下面"
    main = _tight(_rules(".main{"))
    assert "height:100vh" in main, "grid 要吃掉整屏高度，左栏才能一通到底"
    assert "flex-direction:column" not in main, "`.main` 现在只装 .grid 一个子项"
    assert "text-align:center" in _tight(_rules(".footnote{")), "口径说明在右栏内居中"


def test_import_progress_updates_by_identity_not_by_position():
    """导入进度必须**按 importId 原地更新**，不能靠「是不是最后一条消息」。

    起因（2026-09-28 用户）：「导入信息干扰了我的对话…我输入信息，导入信息，就刷新一次，
    重复出现」。根因：导入还在跑的时候他插了一句话（或助手回了一句），导入框就不再是最后一条，
    而旧写法只在「最后一条是导入框」时才原地更新 —— 于是每刷一次进度就**新建一个导入框**。
    反向验证：`.logs/verify-import-inplace.mjs` 把这条逻辑改回旧写法，框数立刻从 1 变 2。
    """
    src = _page_src()
    assert "importId" in src, "导入消息要带稳定身份"
    assert "m.findIndex((x) => x.importId === importId)" in src, "按 id 找到自己那条、原地替换"
    assert "last && last.isImport" not in src, "不许再用「最后一条是不是导入框」来判断该不该原地更新"


def test_no_draft_reason_is_shown_on_the_same_line():
    """「未生成草稿」那一行必须带上**原因**，不能只说"没成"。

    起因（2026-09-28 真机）：用户导入三张微信聊天截图（群里的报账对话），
    界面每张只写「未生成草稿」，而真正的原因（"没识别到金额"）藏在折叠的四段明细里 ——
    看起来就像系统坏了。**只报"没成"不报"为什么"，等于没报。**
    """
    src = _page_src()
    assert 'st.processed === "未生成草稿"' in src, "没生成草稿时要单独拼一句带原因的 brief"
    assert "${st.processed} · ${st.recognized}" in src, "原因取「识别」栏那句（如「没识别到金额」）"


# ------------------------------------------------------- 一次导入只占一个框


def test_import_progress_keeps_one_bordered_box():
    """一次导入只占**一个框**；助手回复不再包框。"""
    assert "border:1px solid var(--border)" in _rule(".b.import{"), "导入框要有边界（唯一保留的框）"
    assert "border" not in _rule(".b.ai{"), "助手消息不该再包框"


def test_import_is_a_single_message_with_progress_and_summary():
    """导入不许再拆成「用户气泡 → 进度气泡 → 结果气泡」三段。"""
    src = _page_src()
    assert "（导入 ${items.length} 个文件）" not in src, "那个用户气泡已按用户要求去掉"
    assert "isImport: true" in src, "导入消息要带 isImport 标记（标题 + 文件 + 汇总同框渲染）"
    assert "importTitle" in src, "导入框要有标题行（导入 N 个文件）"
    assert 'paint(summary || "没有可导入的内容。", false)' in src, (
        "最终汇总必须原地落在同一个导入框里，不能再 push 一条新消息"
    )


def test_import_file_rows_are_one_line_until_expanded():
    """逐文件一行；只有正在跑的（或手动点开的）才展开四段明细。"""
    src = _page_src()
    assert "pb-brief" in src, "完成的文件要显示一行结果摘要（而不是四段明细全铺开）"
    assert 'const open = !b.done || openFile === b.file;' in src, (
        "四段明细只在「正在跑」或「手动点开」时展开 —— 十几个文件各四段能刷满整屏"
    )
