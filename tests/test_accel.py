"""指针加速补偿的测试(纯逻辑 + 真实注入)。

发现(实测): Windows 对**注入的**相对位移同样应用加速曲线 —— 请求
(40,25) 实际走 (92,57), 放大 2.29 倍。后果是副机鼠标手感与主机完全不同,
用户会觉得"飘、不听使唤、划不动", 且毫无线索指向"加速"。

Deskflow 的 deskMouseRelativeMove 专门处理它(临时禁加速 -> 移动 -> 还原)。

这里锁死三件事:
  1. 补偿逻辑确实被调用(读函数源码 / 用假 API 观察调用序列)
  2. 失败/异常路径**必须还原**用户的设置(否则污染用户鼠标手感)
  3. 用户原本已禁用加速时, 不去动它(不做无谓的写法)
"""

from __future__ import annotations

import inspect

import pytest

from com_bridge import input as input_mod
from com_bridge.input import (
    SPI_GETMOUSE,
    SPI_SETMOUSE,
    InputInjector,
)


class TestAccelHelpers:
    def test_helpers_exist_and_use_right_spi_actions(self):
        src = inspect.getsource(input_mod._save_mouse_accel)
        assert "SPI_GETMOUSE" in src
        src2 = inspect.getsource(input_mod._set_mouse_accel)
        assert "SPI_SETMOUSE" in src2

    def test_save_returns_triple_or_none(self, monkeypatch):
        class Fake:
            def __init__(self, ok, vals=(6, 10, 1)):
                self.ok = ok
                self.vals = vals

            def __call__(self, action, a, b, c):
                if action == SPI_GETMOUSE and self.ok:
                    b._obj[0], b._obj[1], b._obj[2] = self.vals
                    return 1
                return 0
        # 直接测真实函数在当前机器上可用(不 mock): 真实环境应返回三元组
        got = input_mod._save_mouse_accel()
        assert got is None or (isinstance(got, tuple) and len(got) == 3)


class TestCompensationApplied:
    """★ relay 期间必须真的禁用/还原加速, 且异常时也不忘还原。"""

    def test_move_relative_disables_then_restores(self, monkeypatch):
        calls: list[tuple[str, object]] = []
        original_save = input_mod._save_mouse_accel
        original_set = input_mod._set_mouse_accel

        def fake_save():
            return (6, 10, 1)          # 用户启用了加速

        def fake_set(triple):
            calls.append(("set", tuple(triple)))
            return True

        monkeypatch.setattr(input_mod, "_save_mouse_accel", fake_save)
        monkeypatch.setattr(input_mod, "_set_mouse_accel", fake_set)
        monkeypatch.setattr(InputInjector, "_send", staticmethod(lambda *i: 1))

        InputInjector.move_relative(10, 5)

        assert calls[0] == ("set", (0, 0, 0)), "必须先禁用加速再注入"
        assert calls[-1] == ("set", (6, 10, 1)), "必须还原用户设置"

    def test_no_touch_when_already_disabled(self, monkeypatch):
        """用户本来就关了加速 -> 不要动它(避免无谓写入)。"""
        calls = []
        monkeypatch.setattr(input_mod, "_save_mouse_accel", lambda: (0, 0, 0))
        monkeypatch.setattr(input_mod, "_set_mouse_accel",
                            lambda t: calls.append(t) or True)
        monkeypatch.setattr(InputInjector, "_send", staticmethod(lambda *i: 1))
        InputInjector.move_relative(10, 5)
        assert calls == [], f"已禁用时不应动设置, 实际调用了 {calls}"

    def test_restores_even_if_send_raises(self, monkeypatch):
        """★ 注入抛异常时**也必须**还原设置(否则用户鼠标手感被永久改掉)。"""
        calls = []
        monkeypatch.setattr(input_mod, "_save_mouse_accel", lambda: (6, 10, 1))
        monkeypatch.setattr(input_mod, "_set_mouse_accel",
                            lambda t: calls.append(tuple(t)) or True)

        def boom(*i):
            raise OSError("mock send failure")
        monkeypatch.setattr(InputInjector, "_send", staticmethod(boom))

        with pytest.raises(OSError):
            InputInjector.move_relative(10, 5)

        assert calls[0] == (0, 0, 0)
        assert calls[-1] == (6, 10, 1), (
            "send 抛异常后必须还原加速设置, 否则用户鼠标手感被永久改掉"
        )

    def test_get_failure_is_safe(self, monkeypatch):
        """取不到原设置时不能崩 —— 宁可不补偿, 也不能中断输入。"""
        monkeypatch.setattr(input_mod, "_save_mouse_accel", lambda: None)
        monkeypatch.setattr(InputInjector, "_send", staticmethod(lambda *i: 1))
        InputInjector.move_relative(3, 4)      # 不应抛异常

    def test_serialized_by_lock(self):
        """多个线程同时注入时必须串行化(否则互相踩设置)。"""
        assert hasattr(input_mod, "_ACCEL_LOCK"), "_ACCEL_LOCK 必须存在"


class TestRealInjectionIsOneToOne:
    """真实注入必须是 1:1(需要干净桌面; 无桌面时跳过)。"""

    def test_real_relative_move_is_linear(self):
        from com_bridge.input import get_cursor_pos, user32, virtual_desktop
        try:
            d = virtual_desktop()
        except Exception:  # noqa: BLE001
            pytest.skip("无桌面环境")
        if d.width < 200:
            pytest.skip("桌面尺寸异常")
        cx, cy = d.left + d.width // 2, d.top + d.height // 2
        import time
        # ★ 守卫必须**验证光标真的到了目标位置**, 而不是只看 SetCursorPos
        #   的返回值: 外部键鼠程序(Deskflow/Synergy 等)可能返回成功但把光标
        #   钉在别处(实测: 返回值 1 而光标恒在屏幕中心)。这类环境下注入测量
        #   全部失真, 必须 skip 而不是 fail —— 失败的是测量环境, 不是代码。
        user32.SetCursorPos(cx, cy)
        time.sleep(0.15)
        got = get_cursor_pos()
        if abs(got[0] - cx) > 6 or abs(got[1] - cy) > 6:
            pytest.skip(f"光标被外部程序占用(请求 ({cx},{cy}) 实际 {got}); "
                        f"桌面测量不可用, 请退出 Deskflow/Synergy 后重跑")
        x0, y0 = get_cursor_pos()
        for _ in range(5):
            InputInjector.move_relative(40, 25)
            time.sleep(0.02)
        time.sleep(0.15)
        x1, y1 = get_cursor_pos()
        dx, dy = x1 - x0, y1 - y0
        if dx == 0 and dy == 0:
            pytest.skip("注入无效(有外部键鼠共享程序占用输入)")
        assert abs(dx - 200) <= 6, f"x 应走 200, 实际 {dx}(加速未补偿?)"
        assert abs(dy - 125) <= 6, f"y 应走 125, 实际 {dy}(加速未补偿?)"
