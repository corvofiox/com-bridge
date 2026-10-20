# -*- coding: utf-8 -*-
"""逃生通道 + 抢控制权抖动的回归测试(2026-10-21 事故复盘)。

## 事故背景(为什么这些测试必须存在)

穿越期间本机几乎没有可用的输入通道:
  * 键盘: 低级钩子吞掉除 Caps/Num/Scroll 外的所有键(含 Win/Alt+Tab)
  * 鼠标: hider 窗口 SetCapture 捕获, 点击不落到任何应用(含托盘图标)
  * 光标: 隐藏
"退出穿越"只剩**副机回传的回程信号**。若触发条件出错而副机又活着
(心跳正常 -> 失联看门狗不触发), 用户没有任何自救手段。

本文件锁住四条修复:
  1. `hook.PanicCombo` —— Ctrl+Alt+Esc 逃生键的判定(含左右修饰键)
  2. `Server._force_release` —— 强制交回后必须设冷静期(否则 1s 一次的
     抢控制权抖动, 日志实证见 Server.ENTRY_LOCKOUT_S 的注释)
  3. `Server._entry_blocked` —— 冷静期内禁止进入穿越态
  4. `cursor.rect_covers` / `CursorHider._is_foreign_fullscreen` ——
     不从前台的全屏窗口抢焦点
"""
from __future__ import annotations

import time

import pytest

from com_bridge import hook as hook_mod
from com_bridge.cursor import rect_covers
from com_bridge.hook import PanicCombo


# --- 1. 逃生键判定 -------------------------------------------------------

class TestPanicCombo:
    def test_ctrl_alt_esc_fires(self):
        c = PanicCombo()
        assert c.feed(hook_mod.VK_LCONTROL, True) is False
        assert c.feed(hook_mod.VK_LMENU, True) is False
        assert c.feed(hook_mod.VK_ESCAPE, True) is True

    def test_right_hand_modifiers_also_fire(self):
        """左 Ctrl + 右 Alt 是常见按法, 必须认。"""
        c = PanicCombo()
        c.feed(hook_mod.VK_LCONTROL, True)
        c.feed(hook_mod.VK_RMENU, True)
        assert c.feed(hook_mod.VK_ESCAPE, True) is True

    def test_generic_modifier_vks(self):
        """某些设备/钩子给通用 VK(0x11/0x12)而不是左右变体。"""
        c = PanicCombo()
        c.feed(hook_mod.VK_CONTROL, True)
        c.feed(hook_mod.VK_MENU, True)
        assert c.feed(hook_mod.VK_ESCAPE, True) is True

    def test_ctrl_alone_does_not_fire(self):
        c = PanicCombo()
        c.feed(hook_mod.VK_LCONTROL, True)
        assert c.feed(hook_mod.VK_ESCAPE, True) is False

    def test_alt_alone_does_not_fire(self):
        c = PanicCombo()
        c.feed(hook_mod.VK_LMENU, True)
        assert c.feed(hook_mod.VK_ESCAPE, True) is False

    def test_esc_alone_does_not_fire(self):
        c = PanicCombo()
        assert c.feed(hook_mod.VK_ESCAPE, True) is False

    def test_held_combo_latches_once(self):
        """按住不放不能每秒刷几十次强制收回。"""
        c = PanicCombo()
        c.feed(hook_mod.VK_LCONTROL, True)
        c.feed(hook_mod.VK_LMENU, True)
        assert c.feed(hook_mod.VK_ESCAPE, True) is True
        # 键盘自动重复
        for _ in range(5):
            assert c.feed(hook_mod.VK_ESCAPE, True) is False

    def test_release_then_press_fires_again(self):
        c = PanicCombo()
        c.feed(hook_mod.VK_LCONTROL, True)
        c.feed(hook_mod.VK_LMENU, True)
        assert c.feed(hook_mod.VK_ESCAPE, True) is True
        c.feed(hook_mod.VK_ESCAPE, False)
        assert c.feed(hook_mod.VK_ESCAPE, True) is True

    def test_modifier_release_disarms(self):
        c = PanicCombo()
        c.feed(hook_mod.VK_LCONTROL, True)
        c.feed(hook_mod.VK_LMENU, True)
        c.feed(hook_mod.VK_LMENU, False)
        assert c.feed(hook_mod.VK_ESCAPE, True) is False

    def test_offset_vk_with_high_bits_is_masked(self):
        """KBDLLHOOKSTRUCT.vkCode 只有低 8 位有效(调用方会 &0xFF)。"""
        c = PanicCombo()
        c.feed(0xFF00 | hook_mod.VK_LCONTROL, True)   # 非标准高字节
        assert c.ctrl_down is False


# --- 2/3. 强制交回 + 冷静期 ---------------------------------------------

def _bare_server():
    """不经 __init__ 造一个最小 Server(避免真实网络/音频/钩子)。

    `_force_release` 只用到 `_edge` / `_pin_point` / `_release_control` /
    `_entry_blocked_until`, 这些在这里全部手动准备。
    """
    from com_bridge.server import Server
    s = Server.__new__(Server)
    s._edge = _FakeEdge()
    s._pin_point = (10, 10)
    s._entry_blocked_until = 0.0
    s.released = []
    s._release_control = lambda: s.released.append(time.monotonic())
    return s


class _FakeEdge:
    def __init__(self, outside: bool = True):
        self.is_outside = outside
        self.resets = 0

    def reset(self) -> None:
        self.is_outside = False
        self.resets += 1


class TestForceRelease:
    def test_force_release_exits_and_locks_out(self):
        s = _bare_server()
        assert s._entry_blocked() is False
        assert s._force_release("测试") is True
        assert s._edge.is_outside is False
        assert s._edge.resets == 1
        assert s._pin_point is None
        assert len(s.released) == 1
        assert s._entry_blocked() is True        # 冷静期内
        assert s._entry_blocked(time.monotonic()
                               + s.ENTRY_LOCKOUT_S + 0.01) is False

    def test_force_release_outside_crossing_does_not_lock(self):
        """常态下调用不得产生副作用(否则误按逃生键会卡住下次穿越)。"""
        s = _bare_server()
        s._edge.is_outside = False
        assert s._force_release("测试") is False
        assert s._entry_blocked() is False

    def test_lockout_blocks_reentry_atoms(self):
        """冷静期必须挡住"贴着边继续往外推"造成的立刻重入。"""
        from com_bridge.protocol import EV_MOUSE_MOVE_REL, InputEvent
        s = _bare_server()
        s._edge = _FakeEdge(outside=True)      # 模拟"正穿越时被看门狗拽回"
        s._input_tx = object()                 # 只要非 None
        s.stats = {"mouse_forwarded": 0, "events_forwarded": 0}
        assert s._force_release("测试") is True
        before = s._edge.resets
        # side=right, 往外推 dx>0 —— 冷静期内必须被挡在门外
        s._on_input_event(InputEvent(EV_MOUSE_MOVE_REL, 0, 0, 5, 0, 0))
        assert s._edge.is_outside is False
        assert s._edge.resets > before          # 冷静期内被 reset 掉

    def test_panic_only_acts_while_crossing(self):
        s = _bare_server()
        s._edge.is_outside = False
        s._on_user_abort()
        assert s.released == []
        assert s._entry_blocked() is False


# --- 4. 全屏介入判定 -----------------------------------------------------

class TestFullscreenGuard:
    def test_covering_whole_desktop_is_fullscreen(self):
        desk = (0, 0, 1920, 1080)
        assert rect_covers((0, 0, 1920, 1080), desk) is True
        assert rect_covers((-1, -1, 1921, 1081), desk) is True

    def test_maximized_window_on_one_monitor_is_not(self):
        """最大化只盖一个显示器 -> 不算全屏(双屏拼桌面时必须区分)。"""
        desk = (0, 0, 3840, 1080)
        assert rect_covers((0, 0, 1920, 1080), desk) is False

    def test_smaller_window_is_not(self):
        assert rect_covers((100, 100, 500, 400), (0, 0, 1920, 1080)) is False

    def test_bad_input_is_false_not_raise(self):
        assert rect_covers(None, (0, 0, 1, 1)) is False
        assert rect_covers((0, 0), (0, 0, 1, 1)) is False

    def test_shell_classes_are_excluded(self):
        """空桌面的 Progman/WorkerW 同样覆盖全屏 —— 不能被当成全屏程序。

        漏了这条会退化成本项目早先反复踩的"光标在空桌面上隐藏不掉"。
        """
        from com_bridge.cursor import _SHELL_CLASSES
        assert "Progman" in _SHELL_CLASSES
        assert "WorkerW" in _SHELL_CLASSES
        import inspect
        from com_bridge.cursor import CursorHider
        src = inspect.getsource(CursorHider._is_foreign_fullscreen)
        assert "_SHELL_CLASSES" in src


# --- 源码级断言(防止有人把机制删掉/接错线) ------------------------------

class TestWiring:
    def _src(self, mod) -> str:
        import inspect
        return inspect.getsource(mod)

    def test_hook_feeds_panic_before_eat_decision(self):
        src = self._src(hook_mod)
        i_feed = src.index("self._panic_combo.feed(")
        i_tage = src.index("if extra == InputInjector.TAG:",
                           src.index("def _kbd_proc"))
        assert i_feed < i_tage, "逃生键判定必须在 TAG/relay 判定之前"

    def test_server_passes_on_panic(self):
        from com_bridge import server as server_mod
        src = self._src(server_mod)
        assert "on_panic=self._on_user_abort" in src
        # 看门狗与会话守卫都必须走统一的强制交回出口(带冷静期)
        assert src.count("self._force_release(") >= 2

    def test_events_injected_initialised(self):
        """KeyError: 'events_injected' 的回归(日志实证 03:16:23/31/34)。"""
        from com_bridge.server import Server
        import inspect
        src = inspect.getsource(Server.__init__)
        assert '"events_injected": 0' in src
