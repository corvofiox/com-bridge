# -*- coding: utf-8 -*-
"""★★★ 回归: GUI 窗口不得持有前台焦点(否则键盘钩子失效)。

## 根因(tools/exp_window_vs_hook_precise.py + 用户实测)

    无窗口                -> kbd_calls+8  ✅
    窗口隐藏              -> kbd_calls+8  ✅
    窗口显示(无焦点)      -> kbd_calls+8  ✅
    窗口获得焦点          -> kbd_calls+0  ❌ 回调永久失效
    焦点转移给别的窗口    -> kbd_calls+6  ✅ 立刻恢复
    有焦点时重装钩子      -> kbd_calls+0  ❌ 重装无效(所以自愈3次全失败)

用户实测确认(2026-10-19 13:4x):
    "主机程序在前台, 副机打字会发送到主机的程序窗口。
     主机程序在后台, 副机打字没有发送到主机。"

机制: 本进程窗口是**前台窗口**时, Windows 不把键盘事件交给本进程的
低级键盘钩子。后果 = "在副机打字, 主机也同步输入"。
Deskflow 不受影响, 因为它没有窗口, 是后台服务。

## 修复三层
1. WS_EX_NOACTIVATE 窗口样式(辅助)
2. 启动时把前台交给 shell
3. 每 700ms 监控, 自己是前台就立刻交出去(主力)
详见 tests/test_focus_watch_regression.py。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import com_bridge.gui as gui_mod  # noqa: E402


def _strip_comments(src: str) -> str:
    return "\n".join(
        line for line in src.splitlines()
        if not line.strip().startswith("#"))


def test_release_focus_exists():
    """必须有"放弃焦点"的方法。"""
    assert hasattr(gui_mod.ComBridgeGui, "_release_focus_for_hook")


def test_release_focus_sets_foreground_away():
    """放弃焦点必须把前台交给别的窗口(SetForegroundWindow 到 shell)。"""
    src = inspect.getsource(gui_mod.ComBridgeGui._release_focus_for_hook)
    assert "SetForegroundWindow" in src, \
        "必须把前台交给别的窗口, 否则钩子持续失效"
    assert "GetShellWindow" in src, "优先交给 shell(桌面/任务栏)"


def test_release_focus_called_on_server_start():
    """★ 主电脑(server)启动成功后必须调用放弃焦点。

    只在 server 角色调用: agent 在副机, 不需要吞键, 也不该干扰用户。
    """
    src = _strip_comments(inspect.getsource(gui_mod.ComBridgeGui._poll))
    assert "_release_focus_for_hook" in src, \
        "server 启动后必须放弃焦点, 否则键盘钩子失效"
    idx = src.index("_release_focus_for_hook")
    context = src[max(0, idx - 400):idx]
    assert "server" in context, "只应对 server 角色放弃焦点"
