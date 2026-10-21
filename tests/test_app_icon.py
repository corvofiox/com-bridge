# -*- coding: utf-8 -*-
"""应用图标(exe / 窗口 / 任务栏)回归测试。

## 背景
用户要求"弄 .exe 图标和程序图标"。托盘图标与窗口图标必须**同一母题**,
否则任务栏里会出现两个不相关的图案。

★ 2026-10-21 造型二次定稿: 「H」(两根竖柱 + 一条横杠)取代了原来的
"三条横线"。用户明确给了图并说"这个程序图标非常好"。几何统一定义在
`com_bridge/brand.py`, 托盘/窗口/exe/主界面品牌标记共用。

## 这些测试锁什么(全部可量化)
1. 内嵌模块可解码, 且是多尺寸
2. **形状就是 H**: 柱身行 2 段(两柱)、横杠行把两柱连通
3. 关键几何都是**整数像素**(半像素会被降采样摊薄)
4. 对比度达 WCAG 图形门槛(3:1)、底板几乎铺满
5. .ico 真的含多个尺寸(PyInstaller / Windows 需要)
6. GUI 真的调用了 appicon.apply
"""
from __future__ import annotations

import base64
import io
import os
import struct

import pytest
from PIL import Image

from com_bridge import appicon, brand
from tools.icons.gen_app_icon import BOT, FG, render


def _decode(name: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(getattr(appicon, name))))


def _row_segments(img: Image.Image, y: int, thr: int = 200) -> list[int]:
    """某一行上前景像素的分段宽度。

    ★ 这是 "H" 的正确判据: 一行上有几段前景, 直接对应图形结构
      (柱身行 2 段 = 两柱; 横杠行连成 1 段 = 中间连通)。
    """
    px = img.load()
    out, cur = [], 0
    for x in range(img.width):
        r, g, b, a = px[x, y]
        if a >= 128 and r > thr and g > thr and b > thr:
            cur += 1
        elif cur:
            out.append(cur)
            cur = 0
    if cur:
        out.append(cur)
    return out


# --- 1. 内嵌模块 --------------------------------------------------------

class TestEmbeddedModule:
    def test_decodes(self):
        for name, size in (("PNG_256", 256), ("PNG_48", 48), ("PNG_16", 16)):
            im = _decode(name)
            assert im.size == (size, size), f"{name} 尺寸不对"
            assert im.mode == "RGBA"

    def test_has_multiple_sizes(self):
        sizes = {_decode(n).width for n in ("PNG_256", "PNG_48", "PNG_16")}
        assert len(sizes) == 3

    def test_apply_signature(self):
        import inspect
        src = inspect.getsource(appicon.apply)
        assert "iconphoto" in src
        # ★ 必须保留引用, 否则被 GC 回收后图标变回默认羽毛
        assert "_app_icon_photos" in src


# --- 2. 形状是 H --------------------------------------------------------

class TestShapeIsH:
    """★ 核心回归: 图形必须是 H, 不是"三条横线"也不是"两块方块"。

    判据(就是 H 的定义):
        * 柱身行(竖直 1/3 处) -> **2 段**, 且两段等宽
        * 横杠行              -> 不能断成 2 段(横杠必须连通两柱)
    """

    @pytest.mark.parametrize("size", [16, 20, 24, 32, 40, 48, 64, 128, 256])
    def test_two_columns(self, size):
        g = brand.layout_for(size)
        body_y = g["top"] + g["bar_h"] // 3
        segs = _row_segments(render(size), body_y)
        assert len(segs) == 2, f"{size}px 柱身行应有 2 段(两柱), 实际 {segs}"
        assert segs[0] == segs[1], f"{size}px 两柱不等宽: {segs}"

    @pytest.mark.parametrize("size", [16, 24, 32, 48, 64, 256])
    def test_crossbar_connects(self, size):
        g = brand.layout_for(size)
        arm_y = g["arm_y"] + g["arm_h"] // 2
        segs = _row_segments(render(size), arm_y)
        assert segs, f"{size}px 横杠行没有前景"
        # 横杠行不能是两段(那说明横杠短到只碰一边, 读成 "H H")
        assert len(segs) != 2, f"{size}px 横杠断成两段: {segs}"
        gap = g["right_x"] - (g["left_x"] + g["bar_w"])
        assert sum(segs) >= gap + 2, (
            f"{size}px 横杠没连通两柱(跨度 {sum(segs)}, 柱间 {gap}): {segs}")

    def test_no_solid_row(self):
        """任何一行都不能是整条实心(那说明已经糊成方块)。"""
        for size in (16, 32, 64):
            img = render(size)
            for y in range(size):
                segs = _row_segments(img, y)
                if segs:
                    assert max(segs) < size, f"{size}px y={y} 出现整行实心"
                    assert len(segs) <= 3, f"{size}px y={y} 出现 {len(segs)} 段"

    def test_brand_module_is_single_source(self):
        """托盘与窗口图标必须**同源**(都走 brand)。

        ★ 实测教训: 早先两处各写一套常量, 结果"同一个图标, 托盘那版
          和窗口那版差一个像素", 右柱位置对不上。
        """
        import inspect

        from com_bridge import tray
        assert "brand.tray_rects" in inspect.getsource(tray._make_channel_icon)
        assert "layout_for" in inspect.getsource(brand.tray_rects)


# --- 3. 几何是整数像素 --------------------------------------------------

class TestIntegerGeometry:
    def test_layout_values_are_int(self):
        for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
            g = brand.layout_for(size)
            for key in ("pad", "bar_w", "bar_h", "top", "arm_h",
                        "arm_x0", "arm_x1", "arm_y", "left_x", "right_x"):
                assert isinstance(g[key], int), \
                    f"{size}px 的 {key}={g[key]} 不是整数(半像素会被摊薄)"

    def test_columns_inside_canvas(self):
        for size in (16, 20, 24, 32, 64, 256):
            g = brand.layout_for(size)
            assert g["left_x"] >= 0
            assert g["right_x"] + g["bar_w"] <= size, "右柱越界"
            assert g["top"] >= 0
            assert g["top"] + g["bar_h"] <= size, "柱高越界"

    def test_crossbar_symmetric(self):
        """横杠相对两柱必须**左右对称**(含端点会差 1px, 允许)。"""
        for size in (16, 24, 32, 48, 128):
            g = brand.layout_for(size)
            left_gap = g["arm_x0"] - (g["left_x"] + g["bar_w"])
            right_gap = g["right_x"] - g["arm_x1"] - 1
            assert abs(left_gap - right_gap) <= 1, (
                f"{size}px 横杠不对称: 左隙 {left_gap} / 右隙 {right_gap}")

    def test_columns_centered_vertically(self):
        for size in (16, 24, 32, 48, 64, 128, 256):
            g = brand.layout_for(size)
            above = g["top"]
            below = size - (g["top"] + g["bar_h"])
            assert abs(above - below) <= 1, \
                f"{size}px 竖柱没有竖直居中(上 {above} / 下 {below})"

    def test_bar_radius_zero_at_small_sizes(self):
        """小尺寸不能用圆头: 3px 宽的柱子加 1.5px 圆角会被吃掉一半。"""
        for size in (16, 20, 24):
            assert brand.bar_radius(size) == 0
        assert brand.bar_radius(64) > 0


# --- 4. 视觉质量 --------------------------------------------------------

class TestVisualQuality:
    def test_white_on_blue_contrast(self):
        def lum(c):
            def ch(v):
                v = v / 255.0
                return v / 12.92 if v <= 0.04045 else \
                    ((v + 0.055) / 1.055) ** 2.4
            return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])
        la, lb = lum(FG), lum(BOT)
        ct = (max(la, lb) + 0.05) / (min(la, lb) + 0.05)
        assert ct >= 3.0, f"对比度只有 {ct:.2f}"

    def test_body_opaque(self):
        for size in (16, 32, 64):
            im = render(size)
            a = im.getchannel("A").tobytes()
            solid = sum(1 for v in a if v >= 128) / (size * size)
            assert solid >= 0.85, f"{size}px 不透明区只有 {solid*100:.0f}%"

    def test_foreground_ratio_sane(self):
        """H 占底板的比例要在合理区间: 太细糊、太粗成块。"""
        for size in (16, 24, 32, 48, 64, 256):
            im = render(size)
            px = im.load()
            solid = fg = 0
            for y in range(size):
                for x in range(size):
                    r, g, b, a = px[x, y]
                    if a < 128:
                        continue
                    solid += 1
                    if r > 200 and g > 200 and b > 200:
                        fg += 1
            ratio = fg / max(1, solid)
            assert 0.05 <= ratio <= 0.40, \
                f"{size}px 前景占比 {ratio*100:.1f}% 超出合理区间"


# --- 5. 缩放鲁棒性 ------------------------------------------------------

class TestScalingRobustness:
    """Tk 的 iconphoto 传多个尺寸时实际只用一个, Windows 会缩放。

    所以必须保证"32px 缩小到 16/20/24 后仍是 H", 不能糊成两块。
    """

    def test_32px_downscale_keeps_two_columns(self):
        src = render(32)
        bad = []
        for target in (16, 20, 24):
            small = src.resize((target, target), Image.LANCZOS)
            g = brand.layout_for(target)
            body_y = min(target - 1, max(0, g["top"] + g["bar_h"] // 3))
            segs = _row_segments(small, body_y)
            if len(segs) != 2:
                bad.append((target, segs))
        assert not bad, f"32px 缩放后不再是两柱: {bad}"

    def test_native_sizes_all_two_columns(self):
        bad = []
        for size in (16, 20, 24, 32, 48, 64):
            g = brand.layout_for(size)
            segs = _row_segments(render(size), g["top"] + g["bar_h"] // 3)
            if len(segs) != 2:
                bad.append((size, segs))
        assert not bad, f"这些尺寸不是两柱: {bad}"


# --- 6. .ico ------------------------------------------------------------

class TestIcoFile:
    def _ico(self):
        p = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "com-bridge.ico")
        if not os.path.exists(p):
            pytest.skip("com-bridge.ico 尚未生成")
        return p

    def test_exists(self):
        assert os.path.getsize(self._ico()) > 5000, "ico 太小, 可能只有单尺寸"

    def test_contains_multiple_sizes(self):
        with open(self._ico(), "rb") as f:
            data = f.read()
        reserved, itype, count = struct.unpack("<HHH", data[:6])
        assert reserved == 0 and itype == 1, "不是合法 ICO"
        assert count >= 5, f"只含 {count} 个尺寸, 建议 >=5(16..256)"
        sizes = [data[6 + i * 16] or 256 for i in range(count)]
        assert 16 in sizes, f"缺 16px(任务栏小图标): {sizes}"
        assert 256 in sizes, f"缺 256px(大图标视图): {sizes}"

    def test_ico_opens(self):
        with Image.open(self._ico()) as im:
            assert im.size[0] >= 16


# --- 7. GUI 接线 --------------------------------------------------------

class TestGuiWiring:
    def test_gui_applies_icon(self):
        import inspect
        from com_bridge.gui import ComBridgeGui
        src = inspect.getsource(ComBridgeGui.__init__)
        assert "appicon" in src, "GUI 必须应用应用图标"
        assert "appicon.apply" in src

    def test_header_has_no_duplicate_brand(self):
        """★ 2026-10-21 用户澄清: "我让你移除的是标题栏和日志栏那一行中的
        程序品牌图标和程序名称, 标题栏已经有一个了, 日志栏那一行的图标
        和程序名纯多余"。

        Windows 标题栏本身已经显示图标 + "Com Bridge", 窗口内容区再画
        一份就是重复。所以 `_build` 里**不得**再放 brand_mark / 名称标签。
        """
        import inspect

        from com_bridge.gui import ComBridgeGui
        src = inspect.getsource(ComBridgeGui._build)
        assert "brand_mark" not in src, "顶栏不应再重复品牌图标"
        # 注意: root.title("Com Bridge") 是**标题栏**必需的文字, 不能误判。
        # 这里只查"用 tk.Label 再画一遍程序名"那种重复。
        assert 'text="Com Bridge"' not in src, "顶栏不应再用 Label 重复程序名"
        # 三个入口按钮必须仍在
        for label in ('"日志"', '"状态"'):
            assert label in src, f"顶栏应保留 {label}"

# --- 8. 图标统一与入口调整(2026-10-21 用户要求) ------------------------

class TestIconUnification:
    """用户要求: 用「H」替换标题图标 / exe 图标 / 托盘图标。

    同时"只保留标题栏图标和 Com Bridge, 日志栏移除" —— 但日志功能不能
    因此消失, 所以移到了托盘右键菜单。
    """

    def test_tray_uses_h_not_bars(self):
        """托盘必须用 H 的两柱一杠, 不能再是三条横线。"""
        import inspect

        from com_bridge import tray
        src = inspect.getsource(tray._make_channel_icon)
        assert "tray_rects" in src, "托盘必须走 brand.tray_rects"
        assert "BAR_YS" not in src, "不应再有旧的三条横线几何"

    def test_no_legacy_bar_constants(self):
        from com_bridge import tray
        assert not hasattr(tray, "BAR_YS"), "旧的 BAR_YS 应已删除"
        assert not hasattr(tray, "BAR_X"), "旧的 BAR_X 应已删除"

    def test_header_has_log_button(self):
        """★ 2026-10-21 二次反馈: "还有一个严重问题日志入口没了"。

        上一版按"日志栏移除"的字面意思删掉了顶栏入口, 用户找不到日志。
        现在顶栏**必须**有日志按钮(托盘菜单那条也保留, 两条路都通)。
        """
        import inspect

        from com_bridge.gui import ComBridgeGui
        src = inspect.getsource(ComBridgeGui._build)
        assert '"日志"' in src, "顶栏必须有日志入口"
        assert "_open_log_window" in src
        assert "_open_status_window" in src, "状态入口也要在"

    def test_log_still_reachable_from_tray(self):
        """移除按钮后日志必须仍有入口(托盘菜单), 否则是功能回退。"""
        import inspect

        from com_bridge import tray
        assert hasattr(tray, "CMD_LOG"), "托盘需要日志命令 ID"
        assert "查看日志" in inspect.getsource(tray.TrayIcon._show_menu)
        from com_bridge.gui import ComBridgeGui
        assert "on_log=" in inspect.getsource(ComBridgeGui._init_tray)

    def test_header_mark_uses_brand_geometry(self):
        """主界面品牌标记必须调 brand.bars_for(与图标同源)。"""
        import inspect

        from com_bridge import ui_render
        assert "bars_for" in inspect.getsource(ui_render.brand_icon)
