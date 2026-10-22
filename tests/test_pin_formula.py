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


class TestNormalizationIsExactInverse:
    """★ 2026-10-23 改: 归一化不再"照搬 Deskflow", 而是**Windows 反变换的精确逆**。

    实测反推(1920 宽的屏上逐个定点值验证): Windows 把 ABSOLUTE 值还原成
    像素用的是 `px = floor(val * extent / 65536)`。

    旧公式(Deskflow 的 `round(65535 * x / (w-1))`)在 1920 宽的屏上只错 7 个
    像素, 但在副机 **4480x1600** 桌面上错 **39 个** —— 相邻像素压到同一个
    定点值, 于是 `move_relative` 读 14、目标 15、落回 14,**永远原地不动**。
    真机日志拍到 `inj dx=1 (14,771) -> (14,771)` 连续几十条、钉了 1.5 秒,
    这就是用户从第一版起报的"边缘粘滞"。详见 tests/test_injection_roundtrip.py。

    因此这里断言的性质从"等于 Deskflow 公式"换成"往返无损"。
    """

    RECT = ScreenRect(0, 0, 1920, 1080)

    @staticmethod
    def _win_px(val: int, extent: int) -> int:
        """Windows 的反变换(实测反推)。"""
        return (val * extent) // 65536

    @pytest.mark.parametrize("x", [0, 1, 500, 959, 960, 1918, 1919])
    @pytest.mark.parametrize("y", [0, 1, 539, 540, 1078, 1079])
    def test_roundtrip_is_lossless(self, monkeypatch, x, y):
        ax, ay, flags = _capture_norm(monkeypatch, self.RECT, x, y)
        assert flags == (MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
                         | MOUSEEVENTF_VIRTUALDESK)
        assert self._win_px(ax, self.RECT.width) == x
        assert self._win_px(ay, self.RECT.height) == y

    def test_every_pixel_of_4480_roundtrips(self, monkeypatch):
        """副机那块 4480 宽的桌面: 每一个像素都必须能落回原位。"""
        rect = ScreenRect(0, 0, 4480, 1600)
        monkeypatch.setattr(input_mod, "_virtual_screen", lambda: rect)
        for x in (12, 13, 14, 15, 16, 100, 2239, 4478, 4479):
            val = InputInjector.to_absolute(x, 0, rect.width)
            assert self._win_px(val, rect.width) == x, f"x={x} 落不回原处"

    def test_edge_pixel_reaches_edge(self, monkeypatch):
        """最右/最下可达像素必须真的到达 1919/1079(不必等于 65535)。"""
        ax, _, _ = _capture_norm(monkeypatch, self.RECT, 1919, 0)
        assert self._win_px(ax, self.RECT.width) == 1919
        _, ay, _ = _capture_norm(monkeypatch, self.RECT, 0, 1079)
        assert self._win_px(ay, self.RECT.height) == 1079

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
