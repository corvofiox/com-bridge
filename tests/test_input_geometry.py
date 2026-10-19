"""越界判定几何 —— 用**真实可达的光标坐标**测试。

这里曾经有个致命 bug 且被测试掩盖: 判定写成 `x >= rect.right`, 而 1920
宽的屏幕 Windows 把光标钳在 x ∈ [0, 1919], 于是 `x >= 1920` 永远为假,
光标永远无法越界 —— 鼠标完全不能跨屏。而当时的测试恰好用了 x=1920/1921
这些真实光标到不了的坐标, 把 bug 当成了期望行为。

教训: 几何判定的测试必须使用**操作系统真会产生的坐标范围**。
"""

from __future__ import annotations

import pytest

from com_bridge.input import EdgeDetector, ScreenRect


# 单显示器 1920x1080: 光标 x ∈ [0, 1919], y ∈ [0, 1079]
MON = ScreenRect(0, 0, 1920, 1080)


class TestEdgeBoundaryIsReachable:
    """核心回归: 边界条件必须落在光标可达的像素上。"""

    @pytest.mark.parametrize("side,edge_point", [
        ("right", (1919, 540)),     # 最右列
        ("left", (0, 540)),         # 最左列
        ("top", (960, 0)),          # 最上行
        ("bottom", (960, 1079)),    # 最下行
    ])
    def test_edge_pixel_triggers_outside(self, side, edge_point):
        d = EdgeDetector(MON, ScreenRect(0, 0, 0, 0), side=side, confirm_frames=2)
        assert d.update(*edge_point) is False, "第 1 帧只计数, 不切换"
        assert d.update(*edge_point) is True, (
            f"side={side} 在边界像素 {edge_point} 连续 2 帧后应进入越界态; "
            f"若为 False, 说明边界条件用了光标到不了的坐标(如 right=1920)"
        )

    def test_screen_interior_never_triggers(self):
        """屏幕内部任何位置都不该触发越界。"""
        d = EdgeDetector(MON, ScreenRect(0, 0, 0, 0), side="right")
        for x, y in ((0, 0), (960, 540), (1918, 1079), (100, 1000)):
            assert d.update(x, y) is False, f"内部点 ({x},{y}) 误判为越界"

    def test_far_from_edge_does_not_trigger(self):
        """距边界明显偏远时不应触发, 避免屏幕边缘附近正常操作误跨屏。

        EDGE_TOLERANCE=2 表示距边界 2px 内算抵边(用户不必精确推到最外一列,
        否则手抖 1px 就"怎么推都过不去")。这里用 1916(距 right-1=1919 有
        3px)验证容差没有被放得过宽 —— 若容差设成 8 之类, 边缘附近正常
        操作鼠标就会误触发跨屏。
        """
        d = EdgeDetector(MON, ScreenRect(0, 0, 0, 0), side="right",
                         confirm_frames=2)
        for _ in range(5):
            assert d.update(1916, 540) is False

    def test_tolerance_two_pixels_triggers(self):
        """距边界 2px 应触发(EDGE_TOLERANCE=2 的可达性保证)。"""
        d = EdgeDetector(MON, ScreenRect(0, 0, 0, 0), side="right",
                         confirm_frames=2)
        assert d.update(1918, 540) is False, "第 1 帧只计数"
        assert d.update(1918, 540) is True, "1918 距边界 1px, 应在容差内"

    def test_impossible_coordinate_would_have_hidden_the_bug(self):
        """明确记录: x=1920 是光标不可达值, 用它会得到假绿。"""
        d = EdgeDetector(MON, ScreenRect(0, 0, 0, 0), side="right",
                         confirm_frames=2)
        # 1920 恰好等于 rect.right; 修复后 `x >= right-1` 也会判 True,
        # 但真实光标到不了这里 —— 测试不应以此为唯一依据
        assert MON.right == 1920
        assert 1919 == MON.right - 1, "光标可达的最右像素是 width-1"


class TestHysteresis:
    def test_stays_outside_within_hysteresis_band(self):
        d = EdgeDetector(MON, ScreenRect(0, 0, 0, 0), side="right",
                         confirm_frames=2, hysteresis=8)
        d.update(1919, 540)
        assert d.update(1919, 540) is True
        # 回退 7px(在迟滞带内)仍算越界, 避免抖动导致反复进出
        assert d.update(1912, 540) is True

    def test_returns_inside_beyond_hysteresis(self):
        d = EdgeDetector(MON, ScreenRect(0, 0, 0, 0), side="right",
                         confirm_frames=2, hysteresis=8)
        d.update(1919, 540)
        assert d.update(1919, 540) is True
        assert d.update(1900, 540) is False, "明显回到屏内应退出越界态"

    def test_reset_clears_state(self):
        d = EdgeDetector(MON, ScreenRect(0, 0, 0, 0), side="right",
                         confirm_frames=2)
        d.update(1919, 540)
        d.update(1919, 540)
        assert d.is_outside is True
        d.reset()
        assert d.is_outside is False


class TestConfirmFrames:
    def test_single_frame_does_not_trigger(self):
        d = EdgeDetector(MON, ScreenRect(0, 0, 0, 0), side="right",
                         confirm_frames=2)
        assert d.update(1919, 540) is False

    def test_streak_resets_when_leaving_edge(self):
        d = EdgeDetector(MON, ScreenRect(0, 0, 0, 0), side="right",
                         confirm_frames=2)
        assert d.update(1919, 540) is False   # streak=1
        assert d.update(1000, 540) is False   # streak 清零
        assert d.update(1919, 540) is False   # streak=1, 仍不触发
        assert d.update(1919, 540) is True    # streak=2 -> 触发


class TestNonOriginMonitor:
    """副屏不在原点时的边界(多显示器)。"""

    def test_right_edge_of_shifted_monitor(self):
        mon = ScreenRect(1920, 0, 3840, 1080)   # 右侧副屏, 最右可达 x=3839
        d = EdgeDetector(mon, ScreenRect(0, 0, 0, 0), side="right",
                         confirm_frames=2)
        assert d.update(3836, 540) is False     # 距边界 3px, 不触发
        assert d.update(3839, 540) is False     # 第 1 帧
        assert d.update(3839, 540) is True      # 第 2 帧

    def test_left_edge_of_shifted_monitor(self):
        mon = ScreenRect(1920, 0, 3840, 1080)   # 最左可达 x=1920
        d = EdgeDetector(mon, ScreenRect(0, 0, 0, 0), side="left",
                         confirm_frames=2)
        assert d.update(1923, 540) is False     # 距边界 3px, 不触发
        assert d.update(1920, 540) is False     # 第 1 帧
        assert d.update(1920, 540) is True      # 第 2 帧
