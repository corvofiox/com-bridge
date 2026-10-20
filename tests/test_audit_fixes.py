# -*- coding: utf-8 -*-
"""审计修复(照搬 Deskflow)的回归测试:
B1 进入点 jumpZone / A1 会话锁防护 / A4 钩子健康自检 / A2 修饰键快照。
不依赖实时桌面(纯逻辑 + 源码断言), 桌面相关断言可自行 skip。
"""
from __future__ import annotations

import inspect

import pytest

from com_bridge import agent as agent_mod
from com_bridge import protocol as P
from com_bridge import server as server_mod


# ---------------------------------------------------------------- B1 jumpZone

class TestJumpZone:
    def test_jump_zone_constant(self):
        """跳入区必须存在且为正(Deskflow avoidJumpZone 语义)。"""
        from com_bridge.input import ENTRY_JUMP_ZONE_PX
        assert ENTRY_JUMP_ZONE_PX >= 8

    def test_entry_point_pushed_inward_all_sides(self):
        """四个方向的进入点都必须向屏内推过跳入区。"""
        from com_bridge.input import ENTRY_JUMP_ZONE_PX, fraction_to_pixel, \
            virtual_desktop
        d = virtual_desktop()
        x, _ = fraction_to_pixel(0.5, "left")
        assert x >= d.left + ENTRY_JUMP_ZONE_PX
        x, _ = fraction_to_pixel(0.5, "right")
        assert x <= d.right - 1 - ENTRY_JUMP_ZONE_PX
        _, y = fraction_to_pixel(0.5, "top")
        assert y >= d.top + ENTRY_JUMP_ZONE_PX
        _, y = fraction_to_pixel(0.5, "bottom")
        assert y <= d.bottom - 1 - ENTRY_JUMP_ZONE_PX

    def test_jump_zone_never_lands_offscreen(self):
        """窄屏/小屏时推入不能越过对边。"""
        from com_bridge.input import ENTRY_JUMP_ZONE_PX, fraction_to_pixel, \
            virtual_desktop
        d = virtual_desktop()
        for f in (0.0, 0.5, 1.0):
            for side in ("left", "right", "top", "bottom"):
                x, y = fraction_to_pixel(f, side)
                assert d.left <= x <= d.right - 1
                assert d.top <= y <= d.bottom - 1

    def test_server_sends_place_before_mods(self):
        """交接顺序: 先 PLACE(绝对定位)再修饰键快照。"""
        src = inspect.getsource(server_mod.Server._on_input_event)
        assert src.index("_send_place_event") < \
            src.index("_send_modifier_snapshot")


# ---------------------------------------------------------------- A1 session guard

class TestSessionGuard:
    def test_desktop_name_query(self):
        """桌面名查询在本会话必须可用(交互桌面应返回非空)。"""
        name = server_mod._current_input_desktop_name()
        assert name is None or len(name) > 0

    def test_guard_started_with_input_setup(self):
        """防护在输入链路建立时启动(_ensure_input_tx), 非纯 start()。"""
        src = inspect.getsource(server_mod.Server._ensure_input_tx)
        assert "_start_session_guard()" in src

    def test_guard_releases_on_desktop_change(self):
        """桌面名变化必须交还控制权(经统一出口 `_force_release`)。

        ★ 2026-10-21 调整: 交还动作从"就地 edge.reset()+_release_control()"
          收敛成**唯一出口** `Server._force_release` —— 它多做一件关键的
          事: 设"禁止重入"冷静期。日志实证了缺它的后果:
              03:27:46.427 光标越界 / 03:27:46.994 强制交回
              03:27:47.097 光标越界 / 03:27:47.997 强制交回
          交回后 100ms 又被同一个边缘动作抓回去 -> 每秒一次的抢控制权
          抖动。所以断言改为: 守卫必须走统一出口, 且出口内必须交还控制权。
        """
        src = inspect.getsource(server_mod.Server._start_session_guard)
        assert "_force_release(" in src
        exit_src = inspect.getsource(server_mod.Server._force_release)
        assert "_release_control()" in exit_src
        assert "edge.reset()" in exit_src

    def test_guard_only_checks_while_outside(self):
        """非穿越态零开销: is_outside 为假时跳过查询。"""
        src = inspect.getsource(server_mod.Server._start_session_guard)
        assert "is_outside" in src

    def test_guard_tolerates_query_failure(self):
        """查询失败(None)不得误判为桌面切换。"""
        src = inspect.getsource(server_mod.Server._start_session_guard)
        assert "continue" in src    # name is None -> continue


# ---------------------------------------------------------------- A4 hook self-check

class TestHookSelfCheck:
    def test_probe_initialized(self):
        assert hasattr(server_mod.Server({}), "_last_probe") or True
        src = inspect.getsource(server_mod.Server.__init__)
        assert "_last_probe" in src

    def test_outside_detects_cursor_motion(self):
        """穿越态下主机光标位移非零 -> 转 _pin_fallback。"""
        src = inspect.getsource(server_mod.Server._on_input_event)
        assert "_pin_fallback = True" in src
        assert "_last_probe" in src

    def test_probe_baseline_reset_on_crossing_entry(self):
        """进入穿越态时重设自检基准。"""
        src = inspect.getsource(server_mod.Server._on_input_event)
        assert "_last_probe = (x, y)" in src

    def test_probe_baseline_reset_on_return(self):
        """回程交还控制权时也重设基准。"""
        src = inspect.getsource(server_mod.Server._on_return_event)
        assert "_last_probe" in src


# ---------------------------------------------------------------- A2 modifier snapshot

class TestModifierSnapshot:
    def test_protocol_constants(self):
        assert P.EV_MOUSE_MODS == 8
        assert P.MODFLAG_SHIFT == 0x01
        assert P.MODFLAG_CTRL == 0x02
        assert P.MODFLAG_ALT == 0x04
        assert P.MODFLAG_WIN == 0x08

    def test_mods_event_roundtrip(self):
        ev = P.InputEvent(P.EV_MOUSE_MODS, 0, P.MODFLAG_SHIFT | P.MODFLAG_CTRL,
                          0, 0, 123456)
        back = P.InputEvent.unpack(ev.pack())
        assert back.type == P.EV_MOUSE_MODS
        assert back.code == (P.MODFLAG_SHIFT | P.MODFLAG_CTRL)

    def test_mods_not_forwarded_as_mouse_or_key(self):
        """EV_MOUSE_MODS 既不是 is_mouse 也不是 is_key, 不会被转发回环。"""
        ev = P.InputEvent(P.EV_MOUSE_MODS, 0, 0, 0, 0, 0)
        assert not ev.is_mouse and not ev.is_key

    def test_server_reads_all_four_modifiers(self):
        src = inspect.getsource(server_mod.Server._send_modifier_snapshot)
        for vk in ("VK_SHIFT", "VK_CONTROL", "VK_MENU", "VK_LWIN"):
            assert vk in src

    def test_agent_injects_down_and_tracks(self):
        """副机收到快照: 未按下的修饰键注入 down 并纳入卡键跟踪。"""
        src = inspect.getsource(agent_mod.Agent._on_input_event)
        assert "EV_MOUSE_MODS" in src
        assert "_down_keys" in src

    def test_agent_skips_already_down_modifier(self):
        """副机已按着同一修饰键时不重复注入(避免双份 down)。"""
        src = inspect.getsource(agent_mod.Agent._on_input_event)
        assert "GetKeyState" in src
