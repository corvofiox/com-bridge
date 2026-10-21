# -*- coding: utf-8 -*-
"""开机静默启动 + 副机等待主电脑 的回归测试(2026-10-21 用户实测需求)。

## 两个需求

1. **静默启动**: 自启后**不弹 GUI 主窗口**, 只留托盘并自动拉起上次的角色。
   旧实现登记的命令是裸 exe 路径, 与双击启动完全一样 -> 登录后窗口直接出现。
2. **副机等待主电脑**: 两台机器都是开机自启, 主电脑往往更慢。旧实现
   第一次 `ControlClient.connect()` 失败就 `raise RuntimeError` -> 副机
   报错退出, 用户必须手动再点一次启动。现在改为常驻重试(退避 <=5s),
   直到握手成功 / 超时 / 被取消。

★ 这些测试**不碰注册表**(用户机器上开着自启, 改它就是破坏现场),
  只测纯函数与连接等待逻辑; 注册表迁移由 `migrate_command()` 负责,
  在 GUI 启动时执行一次(见 tools/verify_startup_wait.py 的说明)。
"""
from __future__ import annotations

import inspect
import socket
import threading
import time

import pytest

from com_bridge import autostart
from com_bridge.agent import Agent, HostWaitCancelled


def _free_port() -> int:
    """取一个"当前没人监听"的端口(绑 0 拿号后立刻关)。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _agent(port: int, wait_s: float = 0.0) -> Agent:
    """最小 Agent: 不回传、不剪贴板、不静音(避免影响真机)。"""
    return Agent(server_host="127.0.0.1", control_port=port,
                 wait_for_host_s=wait_s, enable_return=False,
                 enable_clipboard=False, mute_local=False)


# --- 1. 自启命令必须带静默标志 -------------------------------------------

class TestAutostartFlag:
    def test_flag_constant(self):
        assert autostart.AUTOSTART_FLAG == "--autostart"

    def test_with_flag_appends_once(self):
        cmd = autostart._with_flag('"C:\\a b\\com-bridge-gui.exe"')
        assert cmd.endswith("--autostart")
        assert cmd.count("--autostart") == 1
        # 幂等
        assert autostart._with_flag(cmd) == cmd

    def test_with_flag_empty(self):
        assert autostart._with_flag("") == ""
        assert autostart._with_flag(None) == ""

    def test_exe_command_carries_flag(self):
        """`_exe_command()` 是唯一写进注册表的入口, 必须自带标志。

        它是 `enable()` 与 `migrate_command()` 的共同来源 —— 两处都不会
        漏掉标志。
        """
        assert autostart._exe_command().endswith(autostart.AUTOSTART_FLAG)

    def test_needs_migration(self):
        # 旧版本登记的裸命令 -> 需要迁移
        assert autostart.needs_migration('"D:\\x\\com-bridge-gui.exe"') is True
        # 已带标志 -> 不需要
        assert autostart.needs_migration(
            '"D:\\x\\com-bridge-gui.exe" --autostart') is False
        # 空值 = 没启用, 不算需要迁移
        assert autostart.needs_migration("") is False

    def test_enable_writes_migrated_command(self):
        """enable() 必须写 `_exe_command()`(带标志), 而不是当初的裸路径。"""
        src = inspect.getsource(autostart.enable)
        assert "_exe_command()" in src

    def test_migrate_does_not_touch_registry_when_disabled(self, monkeypatch):
        """未启用时 migrate_command() 必须什么都不做(返回 "off")。"""
        monkeypatch.setattr(autostart, "is_enabled", lambda: False)
        called = []
        monkeypatch.setattr(autostart.winreg, "SetValueEx",
                            lambda *a, **k: called.append(a))
        assert autostart.migrate_command() == "off"
        assert called == [], "未启用时绝不能写注册表"


# --- 2. GUI 静默启动 -----------------------------------------------------

class TestSilentStartup:
    def test_flag_parsing(self):
        from com_bridge.gui import _is_autostart_argv
        assert _is_autostart_argv(["--autostart"]) is True
        assert _is_autostart_argv(["--silent"]) is True
        assert _is_autostart_argv([]) is False
        assert _is_autostart_argv(["--hookdiag"]) is False

    def test_init_withdraws_when_hidden(self):
        """静默启动必须在 mainloop 之前 withdraw, 否则窗口会闪一下。"""
        from com_bridge.gui import ComBridgeGui
        src = inspect.getsource(ComBridgeGui.__init__)
        i_hidden = src.index("self._start_hidden")
        i_withdraw = src.index("self.root.withdraw()")
        assert i_hidden < i_withdraw

    def test_autostart_instance_does_not_wake_existing_window(self):
        """自启时若已有实例在跑, 只能静默退出 —— 不能把窗口弹到用户面前。"""
        from com_bridge import gui as gui_mod
        src = inspect.getsource(gui_mod.main)
        i_hidden = src.index("start_hidden = ")
        i_notify = src.index("notify_existing()")
        # notify_existing 必须被 "if not start_hidden" 包住
        assert i_hidden < i_notify
        seg = src[i_notify - 200:i_notify]
        assert "not start_hidden" in seg

    def test_main_migrates_registry(self):
        from com_bridge import gui as gui_mod
        src = inspect.getsource(gui_mod.main)
        assert "migrate_command()" in src


# --- 3. 副机等待主电脑 ---------------------------------------------------

class TestWaitForHost:
    def test_wait_control_connects_when_host_appears(self):
        """主机晚到 -> 等待中的 agent 必须自动完成握手(不报错)。"""
        from com_bridge.transport import ControlServer
        port = _free_port()
        a = _agent(port, wait_s=10.0)
        srv = ControlServer(port=port, name="late-host")
        srv.bind_now()

        def bring_up() -> None:
            time.sleep(0.4)
            srv.start()

        threading.Thread(target=bring_up, daemon=True).start()
        t0 = time.monotonic()
        ctrl = a._wait_control("127.0.0.1")
        elapsed = time.monotonic() - t0
        try:
            assert ctrl is not None
            assert ctrl.negotiated_minor is not None, "应完成版本协商(握手)"
            assert elapsed >= 0.3, "应当真的等待过主机, 而不是一次就成功"
            assert a.host_wait_state() is None, "握手后必须清掉等待状态"
        finally:
            ctrl.close()
            srv.stop()

    def test_wait_state_visible_while_waiting(self):
        port = _free_port()
        a = _agent(port, wait_s=0.0)      # 一直等, 由测试自己收尾
        seen = []

        def run() -> None:
            try:
                a._wait_control("127.0.0.1")
            except BaseException:  # noqa: BLE001
                pass

        t = threading.Thread(target=run, daemon=True)
        t.start()
        # ★ 本机实测: 连一个"有防火墙/SYN 检查"的关闭端口要 ~2s 才返回
        #   ECONNREFUSED(不是瞬时), 所以第一次探测计数出现得比较晚 ——
        #   这里用截止时间轮询, 不假设次数立刻出现。
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            st = a.host_wait_state()
            if st is not None:
                seen.append(st)
                if st[2] >= 1:
                    break
            time.sleep(0.05)
        a.stop()
        t.join(timeout=5)
        assert seen, "等待期间 host_wait_state() 必须非 None(GUI 靠它显示等待)"
        phase, elapsed, attempts = seen[-1]
        assert phase == "connect"
        assert attempts >= 1, "必须真的发起过探测"
        assert elapsed >= 0.0

    def test_wait_times_out_with_clear_message(self):
        port = _free_port()
        a = _agent(port, wait_s=0.5)
        t0 = time.monotonic()
        with pytest.raises(RuntimeError) as ei:
            a._wait_control("127.0.0.1")
        assert "等待主电脑超时" in str(ei.value)
        assert time.monotonic() - t0 >= 0.4
        assert a.host_wait_state() is None, "超时后必须清掉等待状态"

    def test_wait_is_cancelled_by_stop(self):
        """用户按停止 -> 立即退出等待, 且用**取消**而不是失败来收尾。"""
        port = _free_port()
        a = _agent(port, wait_s=0.0)      # 0 = 一直等
        outcome: list[BaseException] = []

        def run() -> None:
            try:
                a._wait_control("127.0.0.1")
            except BaseException as exc:  # noqa: BLE001
                outcome.append(exc)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        time.sleep(0.3)
        assert a.host_wait_state() is not None
        a.stop()                          # 置 _stop, 等 wait() 立刻返回
        t.join(timeout=3.0)
        assert not t.is_alive(), "取消后必须很快退出等待循环"
        assert outcome and isinstance(outcome[0], HostWaitCancelled)
        assert a.host_wait_state() is None

    def test_default_waits_forever(self):
        """默认 wait_for_host_s=0 => 不设上限(冷启动可能等几分钟)。"""
        a = Agent(server_host=None, enable_return=False,
                  enable_clipboard=False, mute_local=False)
        assert a.wait_for_host_s == 0.0

    def test_start_uses_waiting_helpers(self):
        """start() 不能再走"一次连接失败就抛错"的老路。"""
        src = inspect.getsource(Agent.start)
        assert "_wait_control(" in src
        assert "_wait_discover()" in src
        assert "无法连接主电脑控制通道" not in src

    def test_discover_still_available_for_cli(self):
        """CLI/工具用的单次 discover() 保留(但自启路径不走它)。"""
        assert hasattr(Agent, "discover")
        src = inspect.getsource(Agent.discover)
        assert "未发现主电脑" in src


# --- 4. GUI 与等待状态的接线 ---------------------------------------------

class TestGuiWaitingWiring:
    def test_watchdog_does_not_call_wait_timeout(self):
        """等待主电脑期间不得触发 20s 启动超时(用户报的"直接报错"根因)。"""
        from com_bridge.gui import ComBridgeGui
        src = inspect.getsource(ComBridgeGui._check_watchdog)
        i_wait = src.index("_waiting_for_host()")
        i_20 = src.index("20.0")
        assert i_wait < i_20, "等待判定必须先于超时判定"

    def test_stop_can_cancel_pending_start(self):
        from com_bridge.gui import ComBridgeGui
        src = inspect.getsource(ComBridgeGui._on_stop)
        assert "_pending_role" in src

    def test_poll_handles_cancelled(self):
        from com_bridge.gui import ComBridgeGui
        src = inspect.getsource(ComBridgeGui._poll)
        assert '"cancelled"' in src
