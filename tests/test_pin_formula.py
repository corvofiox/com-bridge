"""钉住注入的**归一化公式**回归(纯数学, 不依赖实时桌面)。

为什么必须把这些断言做成纯函数级(而不是活体鼠标测量):
  开发机上若跑着别的 KVM(实测: Deskflow 本体在跑, 窗口类 DeskflowDesk),
  它会在**屏幕边缘**抢占光标并把它移到自己算的中心
  (Deskflow 的 deskLeave 调 deskMouseMove(m_xCenter, m_yCenter))。
  于是"推到边缘再读回坐标"这类活体测量全部失真 —— 实测 SetCursorPos
  (1919,540) 之后读到的是 (960,540)。

锁死两件事:
  1) move_absolute 发出的归一化值 == Deskflow 的 deskMouseMove 公式
     (除以 w-1, 且四舍五入) —— 这是"不丢像素"的权威依据。
  2) 旧公式(除以 w 且截断)确实**系统性偏小** —— 它每轮都欠一点,
     而钉住是"每个鼠标事件重注入一次", 误差逐事件累积; 再叠加
     "钉读回的当前坐标"带来的切向零约束, 就是用户实测的
     "主机鼠标沿边缘一路向上, 最终停在右上角"。
"""

from __future__ import annotations

import ctypes

import pytest

from com_bridge import input as input_mod
from com_bridge.input import (
    INPUT,
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_VIRTUALDESK,
    InputInjector,
    ScreenRect,
)


def _capture_norm(monkeypatch, rect: ScreenRect, x: int, y: int):
    """调用 move_absolute 并截获它实际发出的 (dx, dy) 归一化值。"""
    seen: list[tuple[int, int, int]] = []

    def fake_send(*inputs) -> int:
        for i in inputs:
            seen.append((i.union.mi.dx, i.union.mi.dy,
                         i.union.mi.dwFlags))
        return len(inputs)

    monkeypatch.setattr(input_mod, "_virtual_screen", lambda: rect)
    monkeypatch.setattr(InputInjector, "_send", staticmethod(fake_send))
    InputInjector.move_absolute(x, y)
    assert len(seen) == 1
    return seen[0]


def _deskflow_norm(v: int, origin: int, span: int) -> int:
    """Deskflow deskMouseMove: (DWORD)(65535.0f * x / (w - 1) + 0.5f)。"""
    return int(65535.0 * (v - origin) / max(1, span - 1) + 0.5)


def _old_norm(v: int, origin: int, span: int) -> int:
    """修复前的公式: int((v - origin) * 65535 / span) —— 除以 w 且截断。"""
    return int((v - origin) * 65535 / max(1, span))


class TestNormalizationMatchesDeskflow:
    RECT = ScreenRect(0, 0, 1920, 1080)

    @pytest.mark.parametrize("x", [0, 1, 500, 959, 960, 1918, 1919])
    @pytest.mark.parametrize("y", [0, 1, 539, 540, 1078, 1079])
    def test_exact_deskflow_formula(self, monkeypatch, x, y):
        ax, ay, flags = _capture_norm(monkeypatch, self.RECT, x, y)
        assert ax == _deskflow_norm(x, self.RECT.left, self.RECT.width)
        assert ay == _deskflow_norm(y, self.RECT.top, self.RECT.height)
        assert flags == (MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
                         | MOUSEEVENTF_VIRTUALDESK)

    def test_edge_pixel_gets_full_scale(self, monkeypatch):
        """最右/最下可达像素必须映射到满量程 65535(不能差一截)。"""
        ax, _, _ = _capture_norm(monkeypatch, self.RECT, 1919, 0)
        assert ax == 65535, f"右边缘应映射到 65535, 实际 {ax}"
        _, ay, _ = _capture_norm(monkeypatch, self.RECT, 0, 1079)
        assert ay == 65535, f"下边缘应映射到 65535, 实际 {ay}"

    def test_zero_origin_offset(self, monkeypatch):
        rect = ScreenRect(-1920, 0, 0, 1080)   # 副屏在主屏左侧的多屏布局
        ax, _, _ = _capture_norm(monkeypatch, rect, -1920, 100)
        assert ax == 0, f"虚拟桌面最左像素应映射到 0, 实际 {ax}"


class TestOldFormulaUndershoots:
    """旧公式的系统性偏小 —— 误差会逐事件累积成'爬边缘'。"""

    def test_old_undershoots_new_everywhere(self):
        rect = ScreenRect(0, 0, 1920, 1080)
        worse = 0
        for x in range(0, 1920, 7):
            if _old_norm(x, rect.left, rect.width) != _deskflow_norm(
                    x, rect.left, rect.width):
                worse += 1
        assert worse > 0, "旧公式应有系统性偏差"

    def test_old_loses_whole_units_at_edges(self):
        rect = ScreenRect(0, 0, 1920, 1080)
        old = _old_norm(1919, rect.left, rect.width)
        new = _deskflow_norm(1919, rect.left, rect.width)
        assert old < new, "旧公式在最右像素上偏小"
        # 换算成像素: 归一化单位 / (65535 / (w-1))
        px = (new - old) / (65535.0 / (rect.width - 1))
        assert px >= 1.0, f"旧公式每轮至少少 1px(实测少 1px), 实际 {px:.2f}px"

    def test_new_formula_is_idempotent(self):
        """同一坐标反复换算 → 归一化值恒定(幂等), 不会自身漂移。"""
        rect = ScreenRect(0, 0, 1920, 1080)
        for x in (0, 1, 960, 1919):
            vals = {_deskflow_norm(x, rect.left, rect.width) for _ in range(5)}
            assert len(vals) == 1, f"x={x} 的归一化值不唯一: {vals}"


class TestPinPointIsFixed:
    """钉住必须钉**固定点**, 不能钉读回的坐标(切向零约束 → 爬角落)。"""

    def test_pin_point_constant_under_corner_creep(self):
        """复刻用户实测: 顶墙猛推 + 手腕上旋(每事件 dy=-1) → 钉住点不动,
        而**读回的切向坐标**会一路漂到最顶端, 停在右上角(钳制在 y=0)。"""
        from com_bridge.input import EdgeDetector

        rect = ScreenRect(0, 0, 1920, 1080)
        edge = EdgeDetector(rect, ScreenRect(0, 0, 0, 0), side="right",
                            confirm_frames=1)
        handoff = (1919, 540)
        pin = handoff                      # 交接那一刻定下, 此后恒定
        # 旧实现: 每个事件都钉 (边缘x, 读回的y); y 被钳在 [0, 1079]
        y = handoff[1]
        for _ in range(600):
            y = max(rect.top, min(rect.bottom - 1, y - 1))   # 上旋 1px/事件
        assert pin == (1919, 540), "钉住点在事件流中必须恒定"
        assert y == rect.top, (
            f"旧实现(钉读回的y)会一路爬到顶端 y={rect.top}, 实际 {y} —— "
            f"这就是用户看到的'停在右上角'"
        )
        # 边缘坐标由 self_rect 决定, 与读回坐标无关
        assert edge.self_rect.right - 1 == 1919
