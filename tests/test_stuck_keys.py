# -*- coding: utf-8 -*-
"""按键卡死防护(照搬 Deskflow updateKeysCB/leave 语义)的回归测试。

UDP 会丢包: EV_KEY 的 down 送达而 up 丢失 -> 副机按键永久卡住。
Agent 现在跟踪仍按住的键, 在三个时机释放:
  1. 控制通道重连成功(_resync_loop 里调 _release_stuck_inputs)
  2. 主电脑心跳超时(StuckKeyWatchdog 线程)
  3. stop() 退出前
这些测试不依赖真实桌面(纯状态机 + 源码断言)。
"""
from __future__ import annotations

import time

import pytest

from com_bridge.agent import Agent
from com_bridge.protocol import (
    EV_KEY,
    EV_MOUSE_BUTTON,
    EV_MOUSE_MOVE_REL,
    KEY_DOWN,
    KEY_EXTENDED,
    KEY_UP,
    MB_DOWN,
    MB_UP,
)


def _key_ev(vk: int, down: bool, extended: bool = False):
    from com_bridge.protocol import InputEvent
    flags = (KEY_DOWN if down else KEY_UP) | (KEY_EXTENDED if extended else 0)
    # InputEvent 字段序: type, flags, code, a, b, ts_us (键盘: code=vk, a=扫描码)
    return InputEvent(EV_KEY, flags, vk, 0, 0, 0)


def _btn_ev(code: int, down: bool):
    from com_bridge.protocol import InputEvent
    return InputEvent(EV_MOUSE_BUTTON, MB_DOWN if down else MB_UP, code,
                      0, 0, 0)


def _fresh_agent() -> Agent:
    """不经 start() 构造最小 Agent(避免真实网络/音频)。"""
    return Agent(server_host="127.0.0.1", enable_return=False)


class TestDownTracking:
    """down/up 状态跟踪的纯逻辑。"""

    def test_key_down_tracked(self):
        a = _fresh_agent()
        a._on_input_event(_key_ev(0x41, True))
        assert (0x41, False) in a._down_keys

    def test_key_up_untracks(self):
        a = _fresh_agent()
        a._on_input_event(_key_ev(0x41, True))
        a._on_input_event(_key_ev(0x41, False))
        assert a._down_keys == {}

    def test_extended_key_is_separate_slot(self):
        a = _fresh_agent()
        a._on_input_event(_key_ev(0x25, True, extended=False))
        a._on_input_event(_key_ev(0x25, True, extended=True))
        assert len(a._down_keys) == 2
        a._on_input_event(_key_ev(0x25, False, extended=True))
        assert (0x25, False) in a._down_keys

    def test_button_down_tracked(self):
        a = _fresh_agent()
        a._on_input_event(_btn_ev(1, True))
        assert 1 in a._down_buttons

    def test_button_up_untracks(self):
        a = _fresh_agent()
        a._on_input_event(_btn_ev(1, True))
        a._on_input_event(_btn_ev(1, False))
        assert a._down_buttons == {}

    def test_mouse_move_not_tracked(self):
        a = _fresh_agent()
        from com_bridge.protocol import InputEvent
        a._on_input_event(InputEvent(EV_MOUSE_MOVE_REL, 5, 5, 0, 0, 0))
        assert a._down_keys == {} and a._down_buttons == {}


class TestReleaseStuck:
    """_release_stuck_inputs 的行为。"""

    def test_releases_all_down_keys_and_buttons(self, monkeypatch):
        a = _fresh_agent()
        released = []
        monkeypatch.setattr(
            "com_bridge.input.InputInjector.key",
            lambda vk, scan, down, extended=False: released.append(
                ("key", vk, down, extended)))
        monkeypatch.setattr(
            "com_bridge.input.InputInjector.button",
            lambda code, down: released.append(("btn", code, down)))
        a._on_input_event(_key_ev(0x41, True))
        a._on_input_event(_key_ev(0x10, True, extended=True))
        a._on_input_event(_btn_ev(1, True))
        a._on_input_event(_btn_ev(3, True))
        a._release_stuck_inputs("测试")
        assert ("key", 0x41, False, False) in released
        assert ("key", 0x10, False, True) in released
        assert ("btn", 1, False) in released
        assert ("btn", 3, False) in released
        # 释放后状态清空
        assert a._down_keys == {} and a._down_buttons == {}

    def test_noop_when_nothing_down(self, monkeypatch):
        a = _fresh_agent()
        calls = []
        monkeypatch.setattr(
            "com_bridge.input.InputInjector.key",
            lambda *args, **kw: calls.append(1))
        monkeypatch.setattr(
            "com_bridge.input.InputInjector.button",
            lambda *args, **kw: calls.append(1))
        a._release_stuck_inputs("空")
        assert calls == []

    def test_state_cleared_even_if_injection_raises(self, monkeypatch):
        """注入失败也不能留下脏状态(否则之后会反复注入 up)。"""
        a = _fresh_agent()
        def _boom(*args, **kw):
            raise OSError("网络不可用")
        monkeypatch.setattr("com_bridge.input.InputInjector.key", _boom)
        a._on_input_event(_key_ev(0x41, True))
        a._release_stuck_inputs("注入异常")
        assert a._down_keys == {}


class TestWiring:
    """三个触发时机必须真的接上(源码断言)。"""

    def test_resync_releases_on_reconnect(self):
        import inspect
        from com_bridge import agent as agent_mod
        src = inspect.getsource(agent_mod.Agent._resync_loop)
        assert "_release_stuck_inputs" in src

    def test_stop_releases_before_exit(self):
        import inspect
        from com_bridge import agent as agent_mod
        src = inspect.getsource(agent_mod.Agent.stop)
        assert "_release_stuck_inputs" in src

    def test_watchdog_thread_started(self):
        import inspect
        from com_bridge import agent as agent_mod
        src = inspect.getsource(agent_mod.Agent._start_punch_heartbeat)
        assert "_start_stuck_key_watchdog()" in src

    def test_watchdog_uses_heartbeat_timestamp(self):
        import inspect
        from com_bridge import agent as agent_mod
        src = inspect.getsource(
            agent_mod.Agent._start_stuck_key_watchdog)
        assert "last_heartbeat_mono" in src
        assert "STUCK_KEY_AFTER_S" in src

    def test_transport_records_heartbeat_time(self):
        import inspect
        from com_bridge import transport as transport_mod
        src = inspect.getsource(transport_mod.InputReceiver.run)
        assert "last_heartbeat_mono" in src

    def test_release_called_with_reason(self):
        import inspect
        from com_bridge import agent as agent_mod
        src = inspect.getsource(agent_mod.Agent._release_stuck_inputs)
        assert "reason" in src
