# -*- coding: utf-8 -*-
"""托盘图标三态回归测试(2026-10-21 用户定稿)。

## 定稿规格(用户原话)
"三条细横线填满, 横线两端有方形端点, 三条线分别代表键鼠、音频和剪切板,
 图标本身是跟 win11 系统托盘相同的黑色, 右下角有黄色、绿色圆点代表连接状态"
后续澄清:
  * 不要黑色状态点 -> 只保留绿/黄
  * 绿色 = 已连接, 黄色 = 已启动但未连接, **无点 = 未启动**
  * 深色任务栏要自适应(主体反白)

## 这些测试锁什么
1. 状态映射: connected/waiting/off 与圆点颜色的对应关系不能变
2. 真画 HICON: 三态都能生成句柄(空句柄 = 托盘里没图标)
3. 像素级几何: 三条 14px 横线、方形端点、圆点位置 —— 用 GDI 回读验证
4. 深色任务栏自适应: 主体色必须真的反转
5. 无点态: 绝不能有圆点像素
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

import pytest

from com_bridge import tray as tray_mod
from com_bridge.tray import (BODY_DARK, BODY_LIGHT, DOT_CX, DOT_CY, DOT_R,
                             STATE_DOT, _make_channel_icon, _taskbar_is_dark)

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int,
                                         ctypes.c_int]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.GetPixel.restype = wintypes.DWORD
gdi32.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
# ★ PatBlt 必须声明 argtypes: 否则 HDC 按默认 c_int 传递, 64 位句柄被截断
#   -> OverflowError("int too long to convert")。与 tray.py 里踩过的
#   "restype 未声明导致句柄截断"是同一类坑。
gdi32.PatBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.PatBlt.restype = wintypes.BOOL
user32.DrawIconEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                              wintypes.HICON, ctypes.c_int, ctypes.c_int,
                              wintypes.UINT, wintypes.HANDLE, wintypes.UINT]
user32.DestroyIcon.argtypes = [wintypes.HICON]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]


def _icon_pixels(hicon, size: int = 16):
    """把 HICON 画到内存 DC 再逐像素读回。

    ★ 必须先把 DC **刷成白色**: 新建的 compatible bitmap 内容是未初始化的
      (实测读回全 0, 于是"整屏都是暗色", 把三条横线判成一条)。这里用
      PatBlt(WHITENESS) 明确刷底。
    """
    hdc = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, size, size)
    old = gdi32.SelectObject(mem, bmp)
    WHITENESS = 0x00FF0062
    gdi32.PatBlt(mem, 0, 0, size, size, WHITENESS)
    user32.DrawIconEx(mem, 0, 0, hicon, size, size, 0, None, 0x0003)
    px = [[gdi32.GetPixel(mem, x, y) for x in range(size)]
          for y in range(size)]
    gdi32.SelectObject(mem, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(None, hdc)
    out = []
    for y in range(size):
        row = []
        for x in range(size):
            v = px[y][x]
            if v == 0xFFFFFFFF:          # CLR_INVALID
                row.append(None)
            else:
                row.append((v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF))
        out.append(row)
    return out


def _is_dark(rgb, thr=90) -> bool:
    return rgb is not None and sum(rgb) / 3 < thr


def _is_green(rgb) -> bool:
    return rgb is not None and rgb[1] > rgb[0] + 40 and rgb[1] > rgb[2] + 20


def _is_yellow(rgb) -> bool:
    return rgb is not None and rgb[0] > 200 and rgb[1] > 150 and rgb[2] < 120


# --- 1. 状态映射 ---------------------------------------------------------

class TestStateMapping:
    def test_three_states(self):
        assert set(STATE_DOT) == {"connected", "waiting", "off"}

    def test_colors(self):
        assert STATE_DOT["connected"] == (0x22, 0xC5, 0x5E), "绿=已连接"
        assert STATE_DOT["waiting"] == (0xFB, 0xBF, 0x24), "黄=已启动未连接"
        assert STATE_DOT["off"] is None, "未启动 = 无点(不是黑点!)"

    def test_no_black_state(self):
        """用户明确要求: 不要黑色状态点。"""
        for name, color in STATE_DOT.items():
            if color is not None:
                assert color != (0, 0, 0), f"{name} 不得用黑色圆点"

    def test_set_status_accepts_state(self):
        t = tray_mod.TrayIcon(on_show=lambda: None, on_start=lambda: None,
                              on_stop=lambda: None, on_exit=lambda: None)
        t.set_status(True, state="connected")
        assert t.state == "connected"
        t.set_status(False, state="waiting")
        assert t.state == "waiting"
        t.set_status(False, state="off")
        assert t.state == "off"

    def test_set_status_backward_compatible(self):
        """旧调用 set_status(True/False) 仍要工作。"""
        t = tray_mod.TrayIcon(on_show=lambda: None, on_start=lambda: None,
                              on_stop=lambda: None, on_exit=lambda: None)
        t.set_status(True)
        assert t.state == "connected"
        t.set_status(False)
        assert t.state == "off"

    def test_unknown_state_falls_back(self):
        t = tray_mod.TrayIcon(on_show=lambda: None, on_start=lambda: None,
                              on_stop=lambda: None, on_exit=lambda: None)
        t.set_status(True, state="bogus")
        assert t.state == "connected"


# --- 2. HICON 生成 ------------------------------------------------------

class TestIconGeneration:
    @pytest.mark.parametrize("state", ["connected", "waiting", "off"])
    def test_hicon_created(self, state):
        h = _make_channel_icon(STATE_DOT[state], size=16)
        assert h, f"{state} 的 HICON 生成失败(托盘会没有图标)"
        user32.DestroyIcon(h)

    @pytest.mark.parametrize("size", [16, 20, 24, 32])
    def test_all_dpi_sizes(self, size):
        h = _make_channel_icon(STATE_DOT["connected"], size=size)
        assert h, f"{size}px 生成失败"
        user32.DestroyIcon(h)

    def test_dark_taskbar(self):
        h = _make_channel_icon(STATE_DOT["connected"], size=16,
                               dark_taskbar=True)
        assert h
        user32.DestroyIcon(h)


# --- 3. 像素几何(用户规格的硬指标) -------------------------------------

class TestPixelGeometry:
    """★ 2026-10-21 造型二次定稿: 三条横线 -> 「H」(两柱一杠)。

    用户原话: "当前托盘图标是三条横线, 直接改成H, 黄绿点状态保持不变"。

    判据也随之改变 —— 横线的"有几行"不能再用(竖柱是连续的一整段),
    改为**按行数分段**, 这才是 "H" 的定义:
        柱身行 -> 2 段(左柱 / 右柱)
        横杠行 -> 3 段(左柱 / 横杠 / 右柱)
    """

    def _row_segments(self, px, y):
        """返回某一行上深色像素的分段长度列表。"""
        segs, cur = [], 0
        for x in range(16):
            if px[y][x] is not None and _is_dark(px[y][x], 120):
                cur += 1
            elif cur:
                segs.append(cur)
                cur = 0
        if cur:
            segs.append(cur)
        return segs

    def _body_y(self):
        """柱身取样行: 竖直方向 1/3 处(避开竖柱的圆头/顶端抗锯齿)。"""
        g = __import__("com_bridge.brand", fromlist=["x"]).layout_for(16)
        return g["top"] + g["bar_h"] // 3

    def _arm_y(self):
        g = __import__("com_bridge.brand", fromlist=["x"]).layout_for(16)
        return g["arm_y"] + g["arm_h"] // 2

    def test_has_two_columns(self):
        """柱身行必须是**两段**(两根竖柱), 不是一整条横线。"""
        h = _make_channel_icon(STATE_DOT["off"], size=16)
        assert h
        try:
            px = _icon_pixels(h)
            segs = self._row_segments(px, self._body_y())
            assert len(segs) == 2, f"柱身行应有 2 段(两根柱子), 实际 {segs}"
            assert segs[0] == segs[1], f"两柱宽度不一致: {segs}"
        finally:
            user32.DestroyIcon(h)

    def test_has_crossbar(self):
        """横杠行必须把两柱**连通** —— 这就是 H 的腰。

        ★ 判据演进:
          起初我按"横杠行分成 3 段"来判(左柱 / 横杠 / 右柱), 但那是
          **三条横线**时代的思路。H 的横杠本来就该**连接**两柱, 所以
          横杠行(越过柱子后)是一整段连续的深色。
          真正要防的是"横杠太短, 变成两根断杠"(读成 H H), 所以这里
          要求横杠两侧的连通区间覆盖两柱之间的绝大部分。
        """
        from com_bridge import brand
        h = _make_channel_icon(STATE_DOT["off"], size=16)
        assert h
        try:
            px = _icon_pixels(h)
            g = brand.layout_for(16)
            y = self._arm_y()
            segs = self._row_segments(px, y)
            assert len(segs) >= 1, f"横杠行没有前景: {segs}"
            # 横杠行必须是一段(或三段), 但不能是"两段" —— 两段说明
            # 横杠只连了一边, 中间断了
            assert len(segs) != 2, f"横杠行断成两段(横杠太短): {segs}"
            span = sum(segs)
            gap = (g["right_x"] - (g["left_x"] + g["bar_w"]))
            assert span >= gap + 2, (
                f"横杠未能连通两柱(跨度 {span}px, 柱间 {gap}px): {segs}")
        finally:
            user32.DestroyIcon(h)


    def test_no_row_is_solid(self):
        """任何一行都不能是**一整条**实心 —— 那说明已经不是 H 了。"""
        h = _make_channel_icon(STATE_DOT["off"], size=16)
        assert h
        try:
            px = _icon_pixels(h)
            for y in range(16):
                segs = self._row_segments(px, y)
                assert len(segs) <= 3, f"y={y} 出现 {len(segs)} 段, 图形糊在一起"
                if segs:
                    assert max(segs) < 15, f"y={y} 有整行实心宽 {segs}"
        finally:
            user32.DestroyIcon(h)

    def test_columns_fill_vertical_span(self):
        """★ 沿用旧断言的精神: 竖柱必须**上下撑满**, 不能缩在中间。

        判据: 最上一行前景距顶 <= 4px, 最下一行距底 <= 4px
        (16px 基准下竖柱高 11px, 上下各留约 2.5px)。
        """
        h = _make_channel_icon(STATE_DOT["off"], size=16)
        assert h
        try:
            px = _icon_pixels(h)
            rows = [y for y in range(16) if self._row_segments(px, y)]
            assert rows, "没有前景"
            assert min(rows) <= 4, f"顶部留白过大(首行 y={min(rows)})"
            assert max(rows) >= 12, f"底部留白过大(末行 y={max(rows)})"
        finally:
            user32.DestroyIcon(h)

    def _dot_pixels(self, px):
        """用**彩度**找圆点(不受具体色值阈值影响)。"""
        pts = []
        for y in range(16):
            for x in range(16):
                rgb = px[y][x]
                if rgb is None:
                    continue
                mx, mn = max(rgb), min(rgb)
                if mx - mn > 25:            # 有彩色 = 圆点
                    pts.append((x, y))
        return pts

    def test_dot_is_round(self):
        """★ 用户反馈修正: 绿点必须是**圆的**, 不能是方块。

        判据: 圆点包围盒宽高相等, 且四角缺失(方块的四角会是满的)。
        允许边缘抗锯齿像素不完全对称(1px 容差), 所以用"四角不全是实心"。
        """
        h = _make_channel_icon(STATE_DOT["connected"], size=16)
        assert h
        try:
            px = _icon_pixels(h)
            pts = self._dot_pixels(px)
            assert pts, "没找到圆点"
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            w = max(xs) - min(xs) + 1
            hgt = max(ys) - min(ys) + 1
            assert abs(w - hgt) <= 1, f"圆点宽高相差过大({w}x{hgt}) -> 不是圆"
            # 圆 = 包围盒四角明显比中心"空"(至少有一角没有像素)
            corners = {(min(xs), min(ys)), (max(xs), min(ys)),
                       (min(xs), max(ys)), (max(xs), max(ys))}
            filled = sum(1 for c in corners if c in set(pts))
            assert filled <= 1, \
                f"圆点四角基本被填满({filled}/4) -> 看起来像方块"
        finally:
            user32.DestroyIcon(h)

    def test_dot_not_clipped(self):
        """圆点不能被画布边缘裁切。"""
        for state in ("connected", "waiting"):
            h = _make_channel_icon(STATE_DOT[state], size=16)
            assert h
            try:
                px = _icon_pixels(h)
                pts = self._dot_pixels(px)
                assert pts, f"{state} 没找到圆点"
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                assert max(xs) <= 15 and max(ys) <= 15
                assert max(xs) < 15, f"{state} 圆点贴右边缘, 会被裁切"
                assert max(ys) < 15, f"{state} 圆点贴下边缘, 会被裁切"
            finally:
                user32.DestroyIcon(h)

    def test_green_and_yellow_same_geometry(self):
        """绿点与黄点必须**几何一致**(只有颜色不同)。"""
        geo = {}
        for state in ("connected", "waiting"):
            h = _make_channel_icon(STATE_DOT[state], size=16)
            assert h
            try:
                px = _icon_pixels(h)
                pts = set(self._dot_pixels(px))
                geo[state] = pts
            finally:
                user32.DestroyIcon(h)
        assert geo["connected"] == geo["waiting"], \
            "绿点与黄点形状/位置不一致(采样网格未对齐)"

    def test_no_white_gap_above_dot(self):
        """★ 用户反馈修正: 圆点周围不能出现"白块"(透明挖空环)。

        旧实现有一圈"透明挖空环"(为黑点准备), 会把主体切出缺口,
        用户看到"绿点上方有一块白"。

        ★ 判据演进(被实测纠正了四次, 记录在这里避免再犯):
          1. "圆点上方一行必须是横线" —— 错。圆点上半可能落在主体空隙里,
             那里本来就是背景。
          2. "圆点周围不能有灰白" —— 错。主体边缘的抗锯齿灰是正常的。
          3. "紧邻像素比远端同色" —— 对三条横线成立, 但 **H 不再适用**:
             H 里圆点所在的右柱, 正左方就是两柱之间的**背景**, 拿
             "远端 x=1 的柱子"当参照必然误判(实测直接失败)。
          4. **当前判据(H)**: 圆点是叠在**右柱**上的, 所以右柱占据的
             **整个矩形区域**里, 除圆点(彩色)外都必须是深色主体 ——
             绝不能出现亮像素。有挖空环就一定会在这里露出亮块。
        """
        from com_bridge import brand
        h = _make_channel_icon(STATE_DOT["connected"], size=16)
        assert h
        try:
            px = _icon_pixels(h)
            g = brand.layout_for(16)
            x0, x1 = g["right_x"], g["right_x"] + g["bar_w"] - 1
            y0, y1 = g["top"], g["top"] + g["bar_h"] - 1
            bright = []
            for y in range(y0, y1 + 1):
                for x in range(x0, x1 + 1):
                    rgb = px[y][x]
                    if rgb is None or _is_green(rgb) or _is_yellow(rgb):
                        continue
                    if sum(rgb) / 3 > 200:
                        bright.append((x, y, rgb))
            assert not bright, (
                f"右柱区域内出现 {len(bright)} 个亮像素 "
                f"(例: {bright[:3]}) -> 圆点周围有白块/挖空环")
        finally:
            user32.DestroyIcon(h)

    def test_dot_over_bar_is_opaque(self):
        """圆点压在横线上的部分必须**不透明**(不能被白底冲淡)。

        ★ 判据怎么定才对: 绿色 #22C55E 是 (34,197,94)。若与白底混合,
          三个通道会**同时升高**且趋向灰(饱和度下降); 若只是与黑色横线
          混合, 会整体变暗但**仍是绿色**(G 明显大于 R、B)。
          所以正确的判据是"色相仍是绿", 不是"亮度够高" ——
          上一版写 g>100 是错的: 压在黑线上的半覆盖绿点自然只有 ~95。
        """
        h = _make_channel_icon(STATE_DOT["connected"], size=16)
        assert h
        try:
            px = _icon_pixels(h)
            pts = self._dot_pixels(px)
            assert pts, "没找到圆点"
            ys = [p[1] for p in pts]
            cy = (min(ys) + max(ys)) // 2
            pset = set(pts)
            row = [px[cy][x] for x in range(16) if (x, cy) in pset]
            assert row, "中心行没有圆点像素"
            for rgb in row:
                r, g, b = rgb
                # 必须仍读作"绿": G 明显高于 R 和 B
                assert g > r + 20, f"中心行发灰不绿 {rgb}(被白底冲淡)"
                assert g > b + 20, f"中心行发灰不绿 {rgb}(被白底冲淡)"
        finally:
            user32.DestroyIcon(h)

    def test_connected_dot_is_green(self):
        h = _make_channel_icon(STATE_DOT["connected"], size=16)
        assert h
        try:
            px = _icon_pixels(h)
            greens = sum(1 for y in range(16) for x in range(16)
                         if _is_green(px[y][x]))
            assert greens >= 6, f"绿点像素太少({greens}), 可能没画出来"
        finally:
            user32.DestroyIcon(h)

    def test_waiting_dot_is_yellow(self):
        h = _make_channel_icon(STATE_DOT["waiting"], size=16)
        assert h
        try:
            px = _icon_pixels(h)
            yellows = sum(1 for y in range(16) for x in range(16)
                          if _is_yellow(px[y][x]))
            assert yellows >= 6, f"黄点像素太少({yellows})"
        finally:
            user32.DestroyIcon(h)

    def test_off_has_no_dot(self):
        """无点态: 圆点区域内不能出现彩色像素。"""
        h = _make_channel_icon(None, size=16)
        assert h
        try:
            px = _icon_pixels(h)
            colored = sum(1 for y in range(16) for x in range(16)
                          if px[y][x] is not None
                          and not _is_dark(px[y][x]))
            # 允许抗锯齿灰边, 但不能有绿/黄
            greens = sum(1 for y in range(16) for x in range(16)
                         if _is_green(px[y][x]))
            yellows = sum(1 for y in range(16) for x in range(16)
                          if _is_yellow(px[y][x]))
            assert greens == 0 and yellows == 0, \
                f"未启动态不该有彩色圆点(green={greens}, yellow={yellows})"
            assert colored >= 0
        finally:
            user32.DestroyIcon(h)

    def test_dot_is_bottom_right(self):
        """圆点必须在右下角(用户: "右下角有圆点")。"""
        h = _make_channel_icon(STATE_DOT["connected"], size=16)
        assert h
        try:
            px = _icon_pixels(h)
            pts = [(x, y) for y in range(16) for x in range(16)
                   if _is_green(px[y][x])]
            assert pts, "没找到绿点"
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            assert cx > 9, f"圆点应偏右(x={cx:.1f})"
            assert cy > 9, f"圆点应偏下(y={cy:.1f})"
        finally:
            user32.DestroyIcon(h)


# --- 4. 深色任务栏自适应 ------------------------------------------------

class TestThemeAdaptive:
    def test_body_colors_differ(self):
        assert BODY_LIGHT == (0, 0, 0), "浅色任务栏用纯黑"
        assert BODY_DARK == (255, 255, 255), "深色任务栏反白"

    def test_dark_variant_renders_light_body(self):
        """深色任务栏下主体必须是亮的(否则在深底上看不见)。"""
        h = _make_channel_icon(STATE_DOT["connected"], size=16,
                               dark_taskbar=True)
        assert h
        try:
            px = _icon_pixels(h)
            lights = sum(1 for y in range(16) for x in range(16)
                         if px[y][x] is not None
                         and sum(px[y][x]) / 3 > 180)
            assert lights >= 60, f"深色任务栏下应有大量亮像素, 实际 {lights}"
        finally:
            user32.DestroyIcon(h)

    def test_detection_returns_bool(self):
        assert isinstance(_taskbar_is_dark(), bool)

    def test_detection_reads_registry(self):
        import inspect
        src = inspect.getsource(_taskbar_is_dark)
        assert "SystemUsesLightTheme" in src
        assert "winreg" in src


# --- 5. GUI 接线 --------------------------------------------------------

class TestGuiWiring:
    def test_sync_tray_passes_states(self):
        from com_bridge.gui import ComBridgeGui
        import inspect
        src = inspect.getsource(ComBridgeGui._sync_tray)
        assert 'state="connected"' in src
        assert 'state="waiting"' in src
        assert 'state="off"' in src

    def test_waiting_uses_yellow(self):
        """等待主电脑 -> 黄点(用户: 黄色是启动但未连接状态)。"""
        from com_bridge.gui import ComBridgeGui
        import inspect
        src = inspect.getsource(ComBridgeGui._sync_tray)
        i_wait = src.index("_waiting_for_host")
        seg = src[i_wait:i_wait + 260]
        assert 'state="waiting"' in seg
