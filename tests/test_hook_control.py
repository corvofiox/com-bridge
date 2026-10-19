"""低级钩子"吞/放行"判定的单元测试(纯逻辑, 不需要真的装钩子)。

这些断言锁死的是**控制权转移**这件事的正确性 —— 用户实测症状:
"进入副机后鼠标点击也不落位在副机, 还是落位在主机"。

根因: RawInput(RIDEV_INPUTSINK)只观察不消费, 本机点击照样落到本机焦点窗口。
必须用低级钩子(WH_MOUSE_LL/WH_KEYBOARD_LL)把物理事件**吞掉**。

最容易出错、后果也最严重的两条(所以用测试锁死):
  1. **我们自己的注入必须放行**。若把带 TAG 的注入也吞掉, 副机转发来的
     位移/点击会被主机钩子吃掉 -> 回程输入永远送不到本机 -> 用户被困副机。
  2. **退出时 relay 必须关**。若忘了关(或关得晚), 用户面对一台
     "键鼠全无响应"的电脑 —— 最坏的用户体验, 且极难自查。
"""

from __future__ import annotations

import pytest

from com_bridge.hook import (
    PASSTHROUGH_VKS,
    VK_CAPITAL,
    VK_NUMLOCK,
    VK_SCROLL,
    decide_key_eat,
    decide_mouse_eat,
)
from com_bridge.input import InputInjector

TAG = InputInjector.TAG


class TestInjectedAlwaysPasses:
    """★ 我们自己的注入**任何模式下都必须放行**。"""

    @pytest.mark.parametrize("relay", [True, False])
    def test_mouse_injected_passes(self, relay):
        assert decide_mouse_eat(relay, TAG) is False

    @pytest.mark.parametrize("relay", [True, False])
    def test_key_injected_passes(self, relay):
        assert decide_key_eat(relay, 0x41, TAG) is False

    def test_mouse_tag_alias_not_confused(self):
        """标记必须是精确匹配: 相邻/相似值不能被误认为我们的。"""
        assert decide_mouse_eat(True, TAG + 1) is True
        assert decide_mouse_eat(True, 0) is True


class TestControlOwnership:
    """控制权在本机(relay=False) -> 一切放行; 在副机(relay=True) -> 吞掉。"""

    def test_relay_off_passes_everything(self):
        for extra in (0, TAG, 0xDEADBEEF):
            assert decide_mouse_eat(False, extra) is False
        for vk in (0x41, VK_CAPITAL, VK_NUMLOCK, VK_SCROLL):
            assert decide_key_eat(False, vk, 0) is False

    def test_relay_on_eats_physical(self):
        assert decide_mouse_eat(True, 0) is True
        assert decide_key_eat(True, 0x41, 0) is True

    @pytest.mark.parametrize("vk", sorted(PASSTHROUGH_VKS))
    def test_relay_on_passes_indicator_keys(self, vk):
        """★ Caps/Num/Scroll 在 relay 模式下也必须放行。

        否则本机指示灯与实际状态不符, 用户会以为键盘坏了。
        Deskflow 同款("we want to let these through ... otherwise the
        keyboard lights may not stay synchronized")。
        """
        assert decide_key_eat(True, vk, 0) is False, (
            f"vk=0x{vk:02x} 在 relay 模式下必须放行以同步指示灯"
        )

    def test_indicator_set_is_exactly_three(self):
        """防止后续误加别的键(比如把普通键加进来会漏吞物理键盘)。"""
        assert PASSTHROUGH_VKS == {VK_CAPITAL, VK_NUMLOCK, VK_SCROLL}


class TestControlReleaseSafety:
    """退出/交还路径的安全性(用真实 LowLevelHook 对象, 但不装钩子)。"""

    def test_set_relay_is_idempotent(self):
        from com_bridge.hook import LowLevelHook
        h = LowLevelHook(name="NoInstallTest")
        h.set_relay(True)
        assert h.relay is True
        h.set_relay(True)
        assert h.relay is True
        h.set_relay(False)
        assert h.relay is False

    def test_stop_forces_relay_off_before_unhook(self):
        """★ stop() 的第一件事必须是关 relay —— 即使卸载失败, 也不能继续吞。"""
        from com_bridge.hook import LowLevelHook
        h = LowLevelHook(name="StopSafetyTest")
        h.set_relay(True)
        h.stop()                      # 未 start() 过也应安全
        assert h.relay is False, (
            "stop() 后 relay 必须为 False, 否则用户键鼠会被永久吞掉"
        )

    def test_diag_reports_state(self):
        from com_bridge.hook import LowLevelHook
        h = LowLevelHook(name="DiagTest")
        d = h.diag()
        assert set(("relay", "installed", "mouse_eaten", "mouse_passed",
                    "kbd_eaten", "kbd_passed", "injected_passed")) <= set(d)


class TestServerOwnershipWiring:
    """Server 侧的夺取/交还必须成对且顺序正确(静态检查 + 语义检查)。"""

    def test_acquire_opens_relay_before_hiding_cursor(self):
        import inspect
        from com_bridge.server import Server
        src = inspect.getsource(Server._acquire_control)
        i_relay = src.find("set_relay(True)")
        i_hide = src.find("hide()")
        assert i_relay != -1 and i_hide != -1
        assert i_relay < i_hide, (
            "_acquire_control 必须先开 relay 再隐藏光标 —— 否则中间那段"
            "物理点击仍会落到本机"
        )

    def test_release_closes_relay_first(self):
        import inspect
        from com_bridge.server import Server
        src = inspect.getsource(Server._release_control)
        i_relay = src.find("set_relay(False)")
        i_show = src.find("_show_cursor()")
        assert i_relay != -1 and i_show != -1
        assert i_relay < i_show, (
            "_release_control 必须先关 relay 再恢复光标 —— relay 是"
            "'用户能不能用自己的键鼠'的开关"
        )

    def test_all_exit_paths_release_control(self):
        """★ 所有退出穿越态的路径都必须交还控制权, 否则用户会被吞住。

        检查三处: 回程信号、心跳丢失看门狗、stop()。
        (输入链路里**没有**时间兜底 —— 那曾是回归: 用户在副机上停止活动
        几秒就被拽回主机。防被困由看门狗负责, 触发条件是"对端失联"。)
        """
        import inspect
        from com_bridge.server import Server

        # 回程信号
        src = inspect.getsource(Server._on_return_event)
        assert "_release_control()" in src, "回程信号必须交还控制权"

        # 心跳丢失看门狗(取代旧的时间兜底)
        src2 = inspect.getsource(Server._start_peer_watchdog)
        assert "_release_control()" in src2, \
            "看门狗判定副电脑失联时必须交还控制权"

        # stop()
        src3 = inspect.getsource(Server.stop)
        assert "set_relay(False)" in src3, "stop() 必须先关 relay"

    def test_acquire_control_called_on_entry(self):
        import inspect
        from com_bridge.server import Server
        src = inspect.getsource(Server._on_input_event)
        assert "_acquire_control()" in src, (
            "越界那一刻必须夺取控制权(开 relay), 否则点击落到主机"
        )


class TestKeyboardForwarded:
    """★ 键盘必须被转发 —— 旧代码 `if ev.is_mouse:` 把它整个丢掉了。"""

    def test_input_event_is_key_property(self):
        from com_bridge.protocol import EV_KEY, InputEvent
        ev = InputEvent(EV_KEY, 0x01, 0x41, 30, 0, 0)
        assert ev.is_key and not ev.is_mouse

    def test_server_forwards_keys(self):
        import inspect
        from com_bridge.server import Server
        src = inspect.getsource(Server._on_input_event)
        assert "ev.is_key" in src, (
            "穿越期间键盘也必须转发(否则副机打不了字)"
        )
        assert "ev.is_mouse or ev.is_key" in src, (
            "转发条件必须同时覆盖鼠标与键盘"
        )

    def test_server_does_not_locally_inject_keys(self):
        """主电脑**不**在本机注入键盘: 控制权在副机时键盘归副机。

        若本机也注入, 会出现"两边同时收到按键"(比如副机文档里多打几个字)。
        """
        import inspect
        from com_bridge.server import Server
        src = inspect.getsource(Server._on_input_event)
        assert "inj.key(" not in src, (
            "_on_input_event 不应在本机注入键盘事件"
        )
