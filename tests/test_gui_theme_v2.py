# -*- coding: utf-8 -*-
"""UI 设计系统 v6 的回归测试(双主题 + 位图渲染质量)。

## 为什么这批断言是这样写的

界面已经被推翻过三次(v4 卡片 → v5 极简 → v6 双主题), 每次返工都源于
"争论审美"而非"验证事实"。所以这里只断言**可量化的事实**:

  * 两套配色必须真的不同, 且各自明暗正确;
  * 文字必须在**最终分辨率**上绘制 —— 这是实际踩到的坑:
    先在 4x 画布写字再 LANCZOS 缩到 1x, 细笔画会被平均掉,
    白字在蓝底上没有任何一个像素超过 245(实测), 界面整体发虚;
  * 圆角/胶囊边缘必须有半透明过渡像素(否则就是"毛刺");
  * 状态三态的色彩必须互不相同, 否则用户分不清在不在跑。
"""
from __future__ import annotations

import inspect

import pytest

from com_bridge import ui_theme as T

try:
    import tkinter as tk
    from PIL import ImageTk
    _HAS_TK = True
except Exception:  # pragma: no cover
    _HAS_TK = False


def _lum(c: str) -> float:
    c = c.lstrip("#")
    r, g, b = (int(c[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _photo_px(photo):
    """PhotoImage -> 可读像素的 PIL 图。"""
    return ImageTk.getimage(photo).convert("RGBA")


# --- 1. 双主题 ----------------------------------------------------------

class TestDualTheme:
    def test_both_palettes_exist(self):
        assert set(T._PALETTES) == {"dark", "light"}

    def test_palette_keys_identical(self):
        """两套配色的键必须完全一致, 否则切换主题会 KeyError。"""
        d, l = T._PALETTES["dark"], T._PALETTES["light"]
        assert set(d) == set(l)

    def test_light_is_light_and_dark_is_dark(self):
        T.set_mode("light")
        assert _lum(T.BG) > 235, "浅色背景应接近白"
        assert _lum(T.CARD) > 245
        light_text = _lum(T.TEXT)
        T.set_mode("dark")
        assert _lum(T.BG) < 40, "深色背景应接近黑"
        assert _lum(T.CARD) < 50
        assert _lum(T.TEXT) > 180
        assert abs(_lum(T.TEXT) - light_text) > 150, "两套主题文字明暗必须相反"

    def test_set_mode_is_idempotent_and_reversible(self):
        T.set_mode("dark")
        first = (T.BG, T.TEXT)
        T.set_mode("dark")
        assert (T.BG, T.TEXT) == first
        T.set_mode("light")
        assert T.BG != first[0]
        T.set_mode("light")          # 复原, 避免影响其它测试

    def test_unknown_mode_falls_back_to_light(self):
        assert T.set_mode("purple") == "light"
        assert T.current_mode() == "light"


# --- 2. 语义色 ----------------------------------------------------------

class TestSemanticColors:
    def test_three_states_are_distinct(self):
        for mode in ("light", "dark"):
            T.set_mode(mode)
            bgs = {T.RUN_BG, T.WAIT_BG, T.IDLE_BG}
            assert len(bgs) == 3, f"{mode} 三态底色必须不同"
            dots = {T.RUN_DOT, T.WAIT_DOT, T.IDLE_DOT}
            assert len(dots) == 3, f"{mode} 三态圆点必须不同"
        T.set_mode("light")

    def test_run_is_greenish_and_wait_is_amberish(self):
        T.set_mode("light")

        def rgb(c):
            c = c.lstrip("#")
            return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))

        r, g, b = rgb(T.RUN_DOT)
        assert g > r and g > b, "运行态应为绿色"
        r, g, b = rgb(T.WAIT_DOT)
        assert r > b and g > b, "等待态应为琥珀色"


# --- 3. 布局与文案约束 --------------------------------------------------

class TestLayoutContract:
    def test_window_geometry(self):
        assert T.WINDOW_W == 620
        assert T.WINDOW_H == 660

    def test_no_cancel_button(self):
        """用户明确要求: 不要"取消等待"按钮。"""
        from com_bridge.gui import ComBridgeGui
        src = inspect.getsource(ComBridgeGui._build)
        assert "取消等待" not in src

    def test_primary_button_says_start(self):
        from com_bridge.gui import ComBridgeGui
        src = inspect.getsource(ComBridgeGui._build)
        assert "启动服务" in src

    def test_primary_is_media_button(self):
        """★ 2026-10-21 用户要求: 启停改成"播放电影那种按钮"。

        原话: "把启动和停止改为播放电影的那种按钮, 侧三角和正方形。
               就放在水位 ms 下方"。
        所以不再是底部整行的 BigButton, 而是状态卡右侧的圆形
        播放/停止按钮, 且只能是自绘(不用系统 ttk 按钮)。
        """
        from com_bridge.gui import ComBridgeGui
        src = inspect.getsource(ComBridgeGui._build)
        assert "MediaButton" in src, "启停应为播放器样式按钮"
        assert '"play"' in src and '"stop"' in src
        assert "ttk.Button" not in src

    def test_main_window_has_no_diagnostics(self):
        from com_bridge.gui import ComBridgeGui
        src = inspect.getsource(ComBridgeGui._build)
        for banned in ("丢包", "断口", "重采样", "键鼠吞", "ratio"):
            assert banned not in src, f"主界面不应显示「{banned}」"

    def test_status_window_still_exists(self):
        from com_bridge.gui import ComBridgeGui
        assert hasattr(ComBridgeGui, "_open_status_window")
        assert hasattr(ComBridgeGui, "_open_log_window")


# --- 4. 位图渲染质量(核心回归) ----------------------------------------

@pytest.fixture(scope="module")
def root():
    if not _HAS_TK:
        pytest.skip("tkinter 不可用")
    r = tk.Tk()
    r.withdraw()
    yield r
    try:
        r.destroy()
    except Exception:  # noqa: BLE001
        pass


class TestBitmapQuality:
    def test_button_text_is_drawn_at_final_resolution(self, root):
        """★ 回归: 白字必须真的白。

        曾经在 4x 画布写字再降采样, 结果白字在蓝底上**没有任何像素**
        超过 245 —— 整个界面看起来"发虚", 用户直接说"像初学者做的"。
        """
        T.set_mode("light")
        from com_bridge import ui_render as R
        ph = R.button(200, 50, 14, "#4F46E5", "#4338CA", root,
                      glow="#4338CA", text="启动服务", fg="#FFFFFF",
                      text_px=15)
        px = _photo_px(ph).convert("RGB").load()
        w, h = ph.width(), ph.height()
        white = sum(1 for y in range(h) for x in range(w)
                    if sum(px[x, y]) / 3 > 245)
        assert white > 100, f"按钮白字像素太少({white}), 文字可能没画在 1x 上"

    def test_status_pill_has_text_and_dot(self, root):
        from com_bridge import ui_render as R
        ph, w = R.status_pill("运行中", root, bg="#E9FBF1", fg="#0B7A4B",
                              dot="#12B76A")
        px = _photo_px(ph).convert("RGB").load()
        dark = sum(1 for y in range(ph.height()) for x in range(w)
                   if sum(px[x, y]) / 3 < 120)
        assert dark > 80, f"状态胶囊文字过淡({dark})"

    def test_segmented_renders_text(self, root):
        from com_bridge import ui_render as R
        T.set_mode("light")
        ph, widths = R.segmented([("主电脑", True), ("副电脑", False)], root,
                                 track=T.SEG_TRACK, pill=T.SEG_PILL,
                                 fg=T.SEG_FG, fg_on=T.SEG_FG_ON)
        px = _photo_px(ph).convert("RGB").load()
        dark = sum(1 for y in range(ph.height()) for x in range(ph.width())
                   if sum(px[x, y]) / 3 < 140)
        assert dark > 120, f"分段控件文字过淡({dark})"
        assert len(widths) == 2 and all(w > 30 for w in widths)

    def test_rounded_corner_is_antialiased(self):
        """圆角外侧必须有半透明过渡像素(否则就是锯齿)。"""
        from com_bridge import ui_render as R
        ss = R.SS
        from PIL import Image, ImageDraw
        src = Image.new("RGBA", (40 * ss, 40 * ss), (0, 0, 0, 0))
        ImageDraw.Draw(src).rounded_rectangle(
            [0, 0, 40 * ss - 1, 40 * ss - 1], radius=10 * ss,
            fill=(0x4F, 0x46, 0xE5, 255))
        small = src.resize((40, 40), Image.LANCZOS)
        px = small.load()
        partial = sum(1 for y in range(40) for x in range(40)
                      if 8 < px[x, y][3] < 248)
        assert partial > 12, f"抗锯齿过渡像素太少({partial})"

    def test_card_has_soft_shadow(self):
        """浅色卡片的阴影必须是"柔和的半透明", 不能是一圈实心灰边。"""
        from com_bridge import ui_render as R
        T.set_mode("light")
        from PIL import Image, ImageTk  # noqa: F401
        ph = R.card(200, 100, 16, None, fill=T.CARD, border=T.CARD_BORDER,
                    border_alpha=T.CARD_BORDER_ALPHA,
                    shadow_alpha=T.SHADOW_ALPHA, blur=7, dy=2)
        im = ImageTk.getimage(ph).convert("RGBA")
        px = im.load()
        # 卡片下方阴影区域的 alpha 应在"几乎透明"和"不透明"之间
        ys = range(im.height - 6, im.height)
        alphas = [px[x, y][3] for y in ys for x in range(40, 160)]
        assert max(alphas) > 0, "阴影完全没画出来"
        assert max(alphas) < 250, "阴影太重(实心灰边)"

    def test_badge_draws_icon(self, root):
        from com_bridge import ui_render as R
        ph = R.badge(30, "#EEF2FF", "#4F46E5", "screen", root)
        px = _photo_px(ph).convert("RGB").load()
        ink = sum(1 for y in range(30) for x in range(30)
                  if sum(px[x, y]) / 3 < 200)
        assert ink > 20, f"徽章图标没画出来({ink})"


# --- 5. 数字显示 --------------------------------------------------------

class TestNumberFormat:
    def test_integral_float_has_no_decimal_point(self):
        from com_bridge.gui import _fmt_num
        assert _fmt_num(60.0) == "60"
        assert _fmt_num(60) == "60"
        assert _fmt_num("60.0") == "60"

    def test_real_fraction_is_preserved(self):
        from com_bridge.gui import _fmt_num
        assert _fmt_num(12.5) == "12.5"

    def test_autostart_path_uses_formatter(self):
        from com_bridge.gui import ComBridgeGui
        src = inspect.getsource(ComBridgeGui._autostart_now)
        assert "_fmt_num(" in src
        assert 'str(cfg["jitter_ms"])' not in src


# --- 6. 主题持久化 ------------------------------------------------------

class TestThemePersistence:
    def test_config_knows_ui_theme(self):
        from com_bridge import config as C
        assert "ui_theme" in C._DEFAULTS
        assert C._DEFAULTS["ui_theme"] in ("auto", "light", "dark")

    def test_resolve_mode_honours_explicit_preference(self, monkeypatch):
        from com_bridge.gui import ComBridgeGui
        fake = ComBridgeGui.__new__(ComBridgeGui)      # 不建窗口
        monkeypatch.setattr(fake, "_load_cfg", lambda: {"ui_theme": "dark"})
        assert fake._resolve_mode() == "dark"
        monkeypatch.setattr(fake, "_load_cfg", lambda: {"ui_theme": "light"})
        assert fake._resolve_mode() == "light"


# --- 7. 次级窗口(状态 / 日志) ------------------------------------------

class _StubApp:
    """次级窗口只依赖 app 的两个成员: collect_status() 与 _log_q。"""

    def __init__(self, data=None):
        import queue
        self._data = data or {}
        self._log_q = queue.Queue()

    def collect_status(self):
        return dict(self._data)


class TestSecondaryWindows:
    """★ 回归: 状态窗口"一行数据都读不出来"。

    根因是 `status_dot().set_color()` 的参数名从 glow 改成了 glow_on,
    而 gui_windows 仍按 glow= 调用 -> TypeError -> 被 `_tick` 的
    `except Exception: pass` 吞掉 -> 所有行永远停在占位符。
    这种"异常被吞掉"的 bug 只能靠行为测试兜住。
    """

    def test_status_dot_set_color_accepts_glow_kwarg(self, root):
        d = T.status_dot(root, color="#98A2B3", size=7, glow=False)
        d.set_color("#12B76A", glow=True)      # 不得抛 TypeError
        d.set_color("#E5484D", glow=False)

    def test_refresh_actually_fills_rows(self, root):
        from com_bridge import gui_windows as W
        app = _StubApp({"connection": ("已连接: 是", 1),
                        "buffer": ("水位 60ms", 2)})
        win = W.StatusWindow(root, app)
        try:
            win.show()
            root.update()
            win.refresh()
            assert "已连接" in win._rows["connection"].cget("text")
            assert "水位" in win._rows["buffer"].cget("text")
            assert win._rows["hook"].cget("text") == "—"   # 未提供 -> 占位符
        finally:
            win.destroy()

    def test_refresh_covers_every_field(self, root):
        from com_bridge import gui_windows as W
        app = _StubApp({k: (f"{k} 正常", 1)
                        for k, _g, _l in W.StatusWindow.FIELDS})
        win = W.StatusWindow(root, app)
        try:
            win.show()
            root.update()
            win.refresh()
            for key, _g, _l in W.StatusWindow.FIELDS:
                assert win._rows[key].cget("text") != "—"
        finally:
            win.destroy()

    def test_log_window_accepts_lines(self, root):
        from com_bridge import gui_windows as W
        win = W.LogWindow(root, _StubApp())
        try:
            win.show()
            root.update()
            win.append("[test] 就绪")
            win.append("!! 失败")
            root.update()
            body = win.text_widget.get("1.0", "end")
            assert "就绪" in body and "失败" in body
        finally:
            win.destroy()


# --- 8. 播放 / 停止按钮(2026-10-21 用户定稿) ---------------------------

class TestMediaButtons:
    """用户定稿原话:

        "按钮不能有底色, 而是纯粹的黑色播放三角和黑色正方形停止按键。
         当程序停止时, 停止按钮由黑变红, 当程序运行时分两个状态,
         等待连接时三角变黄, 连接成功时三角变绿, 播放在左停止在右。"

    颜色是这组按钮**唯一**的状态载体(没有圆底、没有文字), 所以必须
    逐状态精确断言, 不能只测"有颜色"。
    """

    def _btn(self, root, kind):
        from com_bridge import ui_theme as T
        T.set_mode("light")
        return T.MediaButton(root, kind, size=30, bg=T.CARD)

    def test_no_background_plate(self):
        """图形必须是**裸的**: 四角像素完全透明, 没有圆底/底板。"""
        from com_bridge import ui_theme as T
        from PIL import ImageTk
        for kind in ("play", "stop"):
            ph = T.R.media_button(30, kind, None, fg="#0D1117")
            im = ImageTk.getimage(ph).convert("RGBA")
            px = im.load()
            corners = [px[0, 0], px[29, 0], px[0, 29], px[29, 29]]
            assert all(c[3] == 0 for c in corners), \
                f"{kind} 四角有底色(alpha={[c[3] for c in corners]})"

    def test_play_is_triangle_stop_is_square(self):
        """形状: 播放=三角(左窄右宽收成一点), 停止=方块(四边等宽)。"""
        from com_bridge import ui_theme as T
        from PIL import ImageTk
        play = ImageTk.getimage(
            T.R.media_button(30, "play", None, fg="#000000")).convert("RGBA")
        stop = ImageTk.getimage(
            T.R.media_button(30, "stop", None, fg="#000000")).convert("RGBA")

        def width_at(im, y):
            px = im.load()
            xs = [x for x in range(im.width) if px[x, y][3] > 128]
            return (max(xs) - min(xs) + 1) if xs else 0

        # 三角: 中段最宽, 上下两端明显更窄
        mid = width_at(play, 15)
        top = width_at(play, 4)
        assert mid > top * 2, f"播放不是三角(中段{mid} vs 上端{top})"
        # 方块: 上下左右四边等宽
        widths = [width_at(stop, y) for y in (10, 15, 20)]
        assert len(set(widths)) == 1, f"停止不是方块(各行宽 {widths})"

    def test_play_is_left_of_stop(self, root):
        """用户指定顺序: 播放在左, 停止在右。"""
        play = self._btn(root, "play")
        stop = self._btn(root, "stop")
        holder = play.master
        play.pack(side="left")
        stop.pack(side="left", padx=(10, 0))
        root.update_idletasks()
        assert play.winfo_x() < stop.winfo_x(), "播放应在停止左侧"
        play.destroy()
        stop.destroy()

    def test_colors_follow_state(self, root):
        """三态颜色矩阵(用户逐条指定)。"""
        from PIL import ImageTk

        def dominant(btn):
            im = ImageTk.getimage(btn._photo).convert("RGBA")
            px = im.load()
            best = (0, (0, 0, 0), 0)
            counts = {}
            for y in range(im.height):
                for x in range(im.width):
                    r, g, b, a = px[x, y]
                    if a > 200:
                        counts[(r, g, b)] = counts.get((r, g, b), 0) + 1
            top = max(counts.items(), key=lambda kv: kv[1])[0]
            r, g, b = top
            kind = "red" if r > 180 and g < 110 else \
                "green" if g > 150 and r < 120 else \
                "yellow" if r > 200 and 120 < g < 200 and b < 90 else \
                "black" if max(r, g, b) < 70 else "other"
            return kind

        play = self._btn(root, "play")
        stop = self._btn(root, "stop")
        try:
            play.set_state("idle")
            stop.set_state("idle")
            assert dominant(play) == "black", "未启动时播放应为黑色"
            assert dominant(stop) == "red", "未启动时停止应为红色"

            play.set_state("wait")
            stop.set_state("wait")
            assert dominant(play) == "yellow", "等待连接时三角应为黄色"
            assert dominant(stop) == "black", "等待时停止应保持黑色"

            play.set_state("run")
            stop.set_state("run")
            assert dominant(play) == "green", "连接成功时三角应为绿色"
            assert dominant(stop) == "black", "运行中停止应保持黑色"
        finally:
            play.destroy()
            stop.destroy()

    def test_hero_state_updates_button_colors(self):
        """_set_hero_state 必须把状态传给按钮(否则颜色永远不变)。"""
        import inspect
        from com_bridge.gui import ComBridgeGui
        src = inspect.getsource(ComBridgeGui._set_hero_state)
        assert "set_state" in src
        assert "_start_btn" in src and "_stop_btn" in src

    def test_wait_and_run_are_distinguished(self):
        """★ 运行中必须区分"等待连接"(黄) 与"已连接"(绿)。

        早先 _sync_tray 一律 _set_hero_state("run"), 于是"已启动但还没
        连上"也显示成绿色。现在按是否真连上对端分流。
        """
        import inspect
        from com_bridge.gui import ComBridgeGui
        src = inspect.getsource(ComBridgeGui._sync_tray)
        assert '"run" if connected else "wait"' in src, \
            "应按已连接与否在 run / wait 之间切换"


# --- 9. 状态卡紧凑布局(2026-10-21 用户反馈) ----------------------------

class TestHeroCompactLayout:
    """用户反馈原话:

        "左上角, 右下角全是大片空白, 运行中的标识完全悬空,
         不能更紧凑一点么"
        "保留左边的缓冲, 把缓冲的文本改为音频缓冲避免歧义,
         右侧的大缓冲延迟移除, 它使得音频缓冲看起来像连接整体延迟"
    """

    def test_three_row_grid(self):
        """左右必须**按行对齐**(三行网格), 不能一边一块导致大片空白。

        早先左边一整块、右边竖着摞三段, 右侧总高远大于左侧 ->
        左上与右下都空。
        """
        import inspect
        from com_bridge.gui import ComBridgeGui
        src = inspect.getsource(ComBridgeGui._build)
        assert ".grid(row=0" in src and ".grid(row=1" in src \
            and ".grid(row=2" in src, "状态卡应为三行网格"
        # 三行的 set() 注释锚点(防止以后被改回 pack 三段)
        assert "row0" in src and "row1" in src and "row2" in src

    def test_no_big_latency_number(self):
        """右侧那个大数字必须移除(会被误读成整条链路的连接延迟)。"""
        import inspect
        from com_bridge.gui import ComBridgeGui
        src = inspect.getsource(ComBridgeGui._build)
        assert "_lat_var" not in src, "大延迟数字应已移除"

    def test_summary_says_audio_buffer(self):
        """摘要里必须写"音频缓冲"(而不是含糊的"缓冲")。"""
        import inspect
        from com_bridge.gui import ComBridgeGui
        src = inspect.getsource(ComBridgeGui._sync_tray)
        assert "音频缓冲" in src, "摘要应明确标注音频缓冲"
        assert 'f"缓冲 ' not in src, "不应再用含糊的『缓冲』"

    def test_audio_buffer_is_attribute_not_peer(self):
        """★ 音频缓冲要像**连接的属性**(括号挂在已连接之后),
        不能与『已连接』用 · 并列 —— 并列会读成两个独立指标。"""
        import inspect
        from com_bridge.gui import ComBridgeGui
        src = inspect.getsource(ComBridgeGui._sync_tray)
        assert "（音频缓冲" in src, "应用中文括号挂在连接状态后"

    def test_window_height_follows_content(self):
        """★ 用户要求"底边的大空白去掉": 高度必须按内容需求, 不写死。"""
        import inspect
        from com_bridge.gui import ComBridgeGui
        src = inspect.getsource(ComBridgeGui._build)
        assert "winfo_reqheight" in src, "应按内容需求高度设窗口"
        assert "WINDOW_H)" not in src or "geometry" not in src
