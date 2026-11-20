"""引导页、?guide=init 测试开关与「清除全部数据」的行为测试。

引导页平时看不到（没配账号 **且** 一个订阅都没有时才出现），所以除了后端接口，
这里还用「前端守卫测试」把几条关键约定钉在源码上——它们一旦被改坏，
界面会静默地坏掉（未声明变量在运行时才炸，删掉的菜单项会留下悬空引用）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nga_reminder.api import CID_KEY, UID_KEY, WEB_DIR, create_app
from nga_reminder.config import Settings
from nga_reminder.db import (
    POLL_INTERVAL_KEY,
    VIEW_SINCE_KEY,
    VIEW_SINCE_PINNED_KEY,
    Store,
)
from nga_reminder.models import Floor


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NGA_DB_PATH", str(tmp_path / "guide.db"))
    monkeypatch.setenv("NGA_OPEN_BROWSER", "0")
    settings = Settings()
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "guide-store.db")
    yield s
    s.close()


def _floor(pid: int) -> Floor:
    return Floor(
        pid=pid,
        lou=pid,
        author_uid=42,
        author_name="目标用户",
        timestamp=1_700_000_000,
        content="内容",
    )


def _seed(store: Store) -> int:
    """把一个「用了一阵子」的库填满：订阅、已读楼层、通知、设置、账号。"""
    sub = store.upsert_subscription(47288722, 150058, alias="狼大", color="#7B5EA7")
    store.record_post(sub.id, _floor(1001), notified=True)
    store.collect_posts(sub.id, [1001])
    store.log_notification(sub.id, 1001, "标题", "摘要", delivered=True)
    store.set_value(VIEW_SINCE_KEY, "2026-11-17T01:00:00+00:00")
    store.set_value(VIEW_SINCE_PINNED_KEY, "1")
    store.set_value(POLL_INTERVAL_KEY, "120")
    store.set_value(UID_KEY, "1208980")
    store.set_value(CID_KEY, "0" * 40)
    return sub.id


# ------------------------------------------------------------ 清除全部数据


def test_wipe_all_erases_everything_and_goes_back_to_a_fresh_install(store: Store):
    _seed(store)

    since = store.wipe_all()

    assert store.list_subscriptions() == []
    assert store.get_value(UID_KEY) is None
    assert store.get_value(CID_KEY) is None
    assert store.get_value(POLL_INTERVAL_KEY) is None
    assert store.list_notifications() == []
    assert store.get_view_since() == since
    # 「不追溯」的语义跟第一次启动一样：没有 pin，也没有钉住的时间。
    assert store.get_value(VIEW_SINCE_PINNED_KEY) is None


def test_wipe_all_also_forgets_the_seen_ledger(store: Store):
    """去重台账也得清——留着它等于「记得自己曾经通知过」。"""
    sub = store.upsert_subscription(1, 2)
    store.record_post(sub.id, _floor(2001), notified=True)

    store.wipe_all()

    # 台账空了：同一个 pid 再来一次，会被当成新楼层重新通知。
    assert store.record_post(sub.id, _floor(2001), notified=True) is True


def test_delete_setup_all_endpoint_clears_the_install(client: TestClient, tmp_path):
    """账号弹窗里的「清除全部数据」→ 二级确认 → 后端清空。"""
    store = Store(tmp_path / "guide.db")
    try:
        _seed(store)
    finally:
        store.close()

    res = client.delete("/api/setup/all")
    assert res.status_code == 200
    body = res.json()
    assert body["wiped"] is True
    assert body["view_since"]

    status = client.get("/api/status").json()
    assert status["credentials_configured"] is False
    assert client.get("/api/subscriptions").json() == []
    assert client.get("/api/notifications").json() == []


def test_delete_setup_all_leaves_the_app_usable(client: TestClient, tmp_path):
    """清空之后应用还能正常响应——不是把服务搞崩了。"""
    store = Store(tmp_path / "guide.db")
    try:
        _seed(store)
    finally:
        store.close()

    client.delete("/api/setup/all")

    assert client.get("/api/status").status_code == 200
    assert client.get("/api/settings").status_code == 200
    assert client.get("/").status_code == 200


# ------------------------------------------------------------ 前端守卫


def _app_js() -> str:
    return (WEB_DIR / "app.js").read_text(encoding="utf-8")


def _index_html() -> str:
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


def _style_css() -> str:
    return (WEB_DIR / "style.css").read_text(encoding="utf-8")


def test_frontend_declares_the_forced_guide_flag():
    """``guideForced`` 必须先声明再用。

    Esc 那个分支要读它；严格模式下读一个没声明的变量会抛 ReferenceError，
    语法检查（node --check）却发现不了——只能靠这条守卫钉死。
    """
    app_js = _app_js()
    assert "let guideForced" in app_js
    assert 'get("guide") === "init"' in app_js


def test_frontend_has_no_dangling_menu_reference():
    """「全部通知」那行去掉菜单后，不能留下悬空的引用。"""
    app_js = _app_js()
    assert "allMore" not in app_js
    # 「清空通知列表」这个菜单项整个移除。
    assert "清空通知列表" not in app_js


def test_account_dialog_is_slimmed_down():
    """账号弹窗只留输入框，两段说明删掉。"""
    html = _index_html()
    assert "本工具需要读取你的" not in html
    assert "两个是" not in html
    assert "独立的" not in html


def test_guide_how_to_is_expanded_and_matches_the_account_dialog():
    """引导页的取值三步：直接展开，样式跟账号弹窗同一套（``.howto``）。

    原来是「怎么拿？」+ 一个可折叠的 ``<details>``——用户要的是一眼看得到，
    而且要和账号设置弹窗长得一样。
    """
    html = _index_html()
    assert "展开看三步" not in html
    assert "<details>" not in html
    # 两处都用 .howto：账号弹窗一处、引导页一处。
    assert html.count('class="howto"') == 2
    assert html.count('class="step-n"') == 6  # 两处 × 三步


def test_theme_button_shows_only_one_icon_in_both_places():
    """顶栏和引导页的主题按钮，**各自只显示一个图标**。

    这条规则以前只写了 ``#btn-theme``，于是引导页那个按钮日月同框。
    """
    css = _style_css()
    assert "#g-theme .icon-sun { display: none; }" in css
    assert '[data-theme="light"] #g-theme .icon-moon { display: none; }' in css
    assert '[data-theme="light"] #g-theme .icon-sun { display: block; }' in css


def test_subscription_counts_line_up_in_one_column():
    """每行的数量徽标要落在同一条竖线上。

    「全部通知」那行没有「⋯」按钮，所以它的徽标会被顶到更右边——以前一行贴着行尾、
    一行被「⋯」挤到左边，歪着。现在徽标固定宽度，「全部通知」那行补一个同宽空位。
    """
    html = _index_html()
    assert 'class="sub-more-slot"' in html
    css = _style_css()
    assert ".sub-more-slot {" in css
    assert "width: 30px;" in css  # .sub-badge 的固定宽度


def test_guide_step_two_also_takes_name_and_colour():
    """引导页加订阅也要能填显示名称、挑标识颜色。"""
    html = _index_html()
    assert 'id="g-name"' in html
    assert 'id="g-swatches"' in html
    assert 'id="g-parse"' in html
    app_js = _app_js()
    assert "guidePickedColor" in app_js
    assert "buildGuideSwatches" in app_js
    assert "updateGuideParsePreview" in app_js
    # 提交时要把名称与颜色一起带上。
    assert "color: guidePickedColor," in app_js
    assert "alias," in app_js


def test_account_dialog_offers_the_wipe_button_with_a_second_confirmation():
    """「清除全部数据」+ 二级确认弹窗都在。"""
    html = _index_html()
    assert 'id="btn-wipe-all"' in html
    assert 'id="modal-wipe"' in html
    assert 'id="btn-confirm-wipe"' in html
    # 旧的「清空通知列表」确认弹窗整个删掉。
    assert 'id="modal-clear"' not in html


def test_guide_copy_matches_the_final_wording():
    """引导页与顶栏的文案是定稿，不再改。"""
    html = _index_html()
    for text in (
        "一键订阅 · 更新通知",
        "订阅主题用户，回帖第一时间通知",
        "本地使用",
        "配置 NGA 账号",
        "获取访问凭证",
        "ngaPassportUid",
        "ngaPassportCid",
    ):
        assert text in html, text
    # 旧文案不许残留。
    for text in ("只看楼主 · 更新速递", "Cookie 只在本机", "连上账号", "两个值：用户 ID 和密钥。"):
        assert text not in html, text


def test_guide_markup_has_both_steps():
    html = _index_html()
    for marker in (
        'id="guide"',
        'id="gstep-1"',
        'id="gstep-2"',
        'id="gpanel-1"',
        'id="gpanel-2"',
        'id="g-uid"',
        'id="g-cid"',
        'id="g-url"',
        'id="g-save"',
        'id="g-add"',
    ):
        assert marker in html, marker


def test_dark_surfaces_never_use_a_hardcoded_dark_ink():
    """深色底上的字不许写死深色。

    渐变按钮和步骤序号走 ``--on-grad``，品牌图标走 ``--brand-shield`` /
    ``--brand-bang``——主题切换时这两组令牌会跟着翻，文字始终压在底色之上。
    """
    css = _style_css()
    assert "--on-grad" in css
    assert "--brand-shield" in css
    assert "--brand-bang" in css
    # 日历选中日不能被 ``.-current-.-focus-`` 那条规则盖成深色字。
    assert ".air-datepicker-cell.-current-.-focus- { color: var(--text); }" not in css
    assert "color: var(--on-accent)" in css


def test_calendar_selected_day_rule_outranks_the_vendor_one():
    """日历选中日的颜色规则必须盖得住 vendor 自带的那两条。

    vendor 里有 ``.-selected- { color:#fff }`` 和 ``.-selected-.-current- { color:#fff }``，
    后者特异性 (0,3,0)。我们只写 ``.-selected-`` 的话，那条会赢——深色主题的亮紫底
    上就只剩 2.7:1 的白字，读不清。所以两条都要逐条对上地写一遍。
    """
    css = _style_css()
    for selector in (
        ".air-datepicker-cell.-selected-,",
        ".air-datepicker-cell.-selected-.-current-,",
    ):
        assert selector in css, selector


# ============================================ 第十四轮：界面细修（7 条）


def test_all_notifications_row_has_no_count_badge():
    """「全部通知」不再显示数量——只有每个订阅自己带徽标。"""
    html = _index_html()
    assert 'id="all-badge"' not in html
    app_js = _app_js()
    assert "all-badge" not in app_js
    # 「⋯」按钮的同宽空位要留着，各行的「⋯」才对得齐。
    assert 'class="sub-more-slot"' in html


def test_subscription_menu_hover_is_visible_for_every_item():
    """菜单项悬停要看得见——以前只有「删除订阅」有反馈。

    玻璃底上的 ``--glass-3`` 几乎看不见，所以普通项改成染强调色 + 内描边。
    """
    css = _style_css()
    assert ".sub-menu button:hover {" in css
    assert "color-mix(in srgb, var(--accent) 22%, transparent)" in css
    assert ".sub-menu button.danger:hover {" in css


def test_account_dialog_shows_the_full_uid_and_the_new_wording():
    """账号弹窗：完整 uid、去掉「来源」、新说明、新按钮名。"""
    html = _index_html()
    assert "更新账号</button>" in html
    assert "保存并开始" not in html
    assert "本机数据库" not in html
    assert "直接覆盖下面的值即可更换账号" not in html

    app_js = _app_js()
    assert "更新账号不会删除订阅数据" in app_js
    # 完整 uid 由接口给出，不再只露末两位。
    assert "s.uid || s.uid_hint" in app_js


def test_topbar_uses_update_interval_and_has_no_title_hints():
    """「抓取间隔」→「更新间隔」，「不抓取」→「关闭」，两个控件都不带 title。"""
    html = _index_html()
    assert "<span class=\"select-label\">更新间隔</span>" in html
    assert "抓取间隔" not in html
    # 顶栏那两个全局开关不再有 title 提示。
    assert 'id="time-field" title=' not in html
    assert 'class="select-field" title=' not in html

    app_js = _app_js()
    assert '0: "关闭"' in app_js
    assert '"不抓取"' not in app_js


def test_guide_has_a_third_step_for_the_reminder_settings():
    """引导页第三步：提醒设置——订阅时间与更新间隔都要能设。"""
    html = _index_html()
    for marker in (
        'id="gstep-3"',
        'id="gpanel-3"',
        'id="g-time"',
        'id="g-interval"',
        'id="g-finish"',
    ):
        assert marker in html, marker
    assert "提醒设置" in html
    # 这两个设置是干什么的，要说清楚。
    assert "订阅时间" in html
    assert "更新间隔" in html

    app_js = _app_js()
    assert "guideFinishReminders" in app_js
    assert "GUIDE_INTERVAL_DEFAULT" in app_js
    assert "guideDefaultTime" in app_js


def test_guide_step_three_uses_the_new_wording():
    """提醒设置这一步的文案按用户要求改过了。"""
    html = _index_html()
    assert "设置订阅开始时间和间隔" in html
    assert "订阅的开始时间，只会显示之后的回复" in html
    assert "更新抓取的时间间隔，请设置合理数值" in html
    # 旧文案不许留着。
    assert "两件事决定你什么时候会收到通知" not in html
    assert "从这一刻起" not in html
    assert "多久去 NGA 看一次" not in html


def test_guide_step_three_stacks_one_setting_per_row():
    """两个设置改成每行一个，不再并排。"""
    html = _index_html()
    # 第 3 步用的是竖排容器，不是并排的双列。
    panel = html.split('id="gpanel-3"', 1)[1]
    assert 'class="guide-stack"' in panel
    assert 'class="guide-two"' not in panel

    css = _style_css()
    assert ".guide-stack {" in css


def test_guide_update_interval_uses_a_tabs_component():
    """引导页的「更新间隔」用分段 tab，**不复用顶栏那个下拉框**。

    下拉框自带一层弹出菜单，搬进表单区会跟旁边的输入框对不齐：触发器缩在
    壳的左上角，菜单却按整块壳右对齐（实测截图里就是这个错位）。
    现在换成一排平铺的 tab，点一下就选中，没有弹层，自然不会错位。
    """
    html = _index_html()
    panel = html.split('id="gpanel-3"', 1)[1]
    assert 'class="tabs" id="g-interval"' in panel
    assert 'role="radiogroup"' in panel
    # 顶栏下拉框的三个零件不许再出现在引导页里。
    for marker in ('id="g-interval-trigger"', 'id="g-interval-menu"', 'id="g-interval-value"'):
        assert marker not in html, marker

    app_js = _app_js()
    assert "function createTabs(" in app_js
    assert "guideIntervalTabs" in app_js
    assert 'root: $("g-interval")' in app_js
    # 引导页不再挂顶栏那套下拉框。
    assert "guideIntervalSelect" not in app_js

    css = _style_css()
    assert ".tabs {" in css
    assert '.tab[aria-checked="true"] {' in css
    # 旧的「给引导页的下拉框补壳」规则已经没用了，要删干净。
    assert ".guide-stack .select" not in css


def test_tabs_component_follows_the_radio_group_keyboard_rules():
    """分段 tab 的键盘约定要按 ARIA 的 radio 组来。

    容器是 radiogroup、每个选项是 radio + aria-checked；方向键换选项、
    Home/End 跳头尾；Tab 键只进出整组一次（组内用漫游 tabindex）。
    """
    app_js = _app_js()
    assert 'btn.setAttribute("role", "radio")' in app_js
    assert 'btn.setAttribute("aria-checked"' in app_js
    assert "ArrowRight" in app_js
    assert "ArrowLeft" in app_js
    assert 'e.key === "Home"' in app_js
    assert 'e.key === "End"' in app_js
    # 漫游 tabindex：整组只有一个 tab 能被 Tab 键选中。
    assert "btn.tabIndex = on ? 0 : -1;" in app_js


def test_guide_calendar_is_above_the_guide_overlay():
    """日历面板必须浮在引导页这层覆盖层**上面**。

    面板挂在 body 的全局容器里，层级由 ``--adp-z-index`` 决定；引导页覆盖层是
    ``z-index: 80``。面板层级低于它时，日历会被整层盖住——组件状态是「已打开」，
    页面上却什么都看不见（实测踩过）。
    """
    css = _style_css()
    z_index = 0
    for line in css.splitlines():
        if "--adp-z-index" in line:
            z_index = int(line.split(":")[1].split(";")[0].strip())
            break
    assert z_index > 80, f"日历 z-index={z_index}，会被引导页(z-index:80)盖住"
    # 也要高过弹窗与抽屉（100），否则在弹窗里用日期组件同样看不见。
    assert z_index > 100
    # 但要低于 Toast（200），免得提示条被日历压住。
    assert z_index < 200


def test_guide_date_field_has_an_explicit_picker_button():
    """日期旁边要有个「选择」按钮，能显式地唤起日历面板。"""
    html = _index_html()
    assert 'id="g-time-pick"' in html
    assert "选择" in html

    app_js = _app_js()
    assert '$("g-time-pick").onclick' in app_js
    assert "guideDatePicker.show()" in app_js
    assert "guideDatePicker.hide()" in app_js
    # 面板挂在 body 上，点外面也得能收掉。
    assert 't.closest("#g-time-pick")' in app_js


def test_guide_step_three_uses_the_vendored_date_component():
    """日期组件必须是现成的那个——不是自己画的日历，也不是原生控件。"""
    app_js = _app_js()
    assert "guideDatePicker = new AirDatepicker" in app_js
    assert 'id="g-time" type="text"' in _index_html()


def test_guide_calendar_is_closed_with_the_guide():
    """日历面板挂在 body 上——引导页收起时必须自己把它收掉。

    不收的话它会留在主页面中间不走（实测截图里看到过）。
    """
    app_js = _app_js()
    assert "closeGuideDatePicker" in app_js
    assert "guideDatePicker.visible" in app_js
    assert "closeGuideDatePicker();" in app_js


def test_feed_scrolls_and_loads_more():
    """通知流要能滚动翻页：滚到接近底部自动接上更早的一段。"""
    app_js = _app_js()
    assert "loadFeedMore" in app_js
    assert "loadFeedHead" in app_js
    assert "before: state.feedTailCursor" in app_js
    assert 'params.set("before"' in app_js
    assert 'mainBody.addEventListener(' in app_js
    assert 'id="feed-more"' in _index_html()


def test_feed_shows_the_floor_number():
    """卡片上要显示回复所在的楼层（#1234）。"""
    app_js = _app_js()
    assert '"msg-floor"' in app_js
    assert "`#${n.floor_no}`" in app_js
    css = _style_css()
    assert ".msg-floor {" in css
