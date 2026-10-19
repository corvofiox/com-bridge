# -*- coding: utf-8 -*-
"""★★★ 回归: hider 绝不能抢前台 —— 抢前台会杀死键盘钩子。

## 实测归因(tools/exp_hide_step_attribution.py, 逐步施加 hide 的每一步)

    基线                   kbd_calls+4  ✅
    + 全屏窗显示           kbd_calls+4  ✅
    + SetActiveWindow      kbd_calls+4  ✅
    + SetForegroundWindow  kbd_calls+0  ❌ **钩子当场死亡**
    + SetCapture           kbd_calls+4  ✅
    + ShowCursor(FALSE)    kbd_calls+4  ✅

## 症状因果链(用户报的 BUG1 最终根因)

  鼠标进副机 -> _acquire_control -> set_relay(True) + hider.hide()
  -> hide 里 SetForegroundWindow(全屏 hider) 成为前台
  -> Windows 对前台窗口所属进程改变输入路由, 键盘不再经过本进程 LL 钩子
  -> kbd_calls 恒 0, 键照常流入系统(主机焦点应用/输入法收到)
  -> 用户看到"在副机打字, 主机也同步输入"
  -> 且重装无效(钩子没坏, 是路由变了)

Deskflow 的 primary 分支只用 EnableWindow + SetActiveWindow,
secondary 用 SetCapture, 两边都不抢前台 —— 这正是我们该对齐的形状。
"""
import ast
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import com_bridge.cursor as cursor_mod  # noqa: E402

SRC = inspect.getsource(cursor_mod)


def _strip_comments(src: str) -> str:
    """去掉注释行与文档字符串, 只留可执行代码(避免注释里提到 API 就误判)。"""
    return "\n".join(
        line for line in src.splitlines()
        if not line.strip().startswith("#"))


def _code_only(src: str) -> str:
    """用 AST 提取真正的调用, 排除注释和 docstring。

    ⚠ inspect.getsource(方法) 返回带缩进的源码, 必须先 textwrap.dedent。
    """
    import textwrap
    tree = ast.parse(textwrap.dedent(src))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.append(node.attr)
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute):
                names.append(fn.attr)
            elif isinstance(fn, ast.Name):
                names.append(fn.id)
    return "\n".join(names)


def test_force_foreground_is_gone():
    """_force_foreground 方法必须被删除(它是抢前台的唯一入口)。"""
    assert not hasattr(cursor_mod.CursorHider, "_force_foreground"), \
        "抢前台的方法必须删除 —— 它会让键盘钩子死亡(实测)"


def test_do_hide_never_sets_foreground():
    """_do_hide 的可执行代码里绝不能出现 SetForegroundWindow。"""
    src = inspect.getsource(cursor_mod.CursorHider._do_hide)
    calls = _code_only(src)
    assert "SetForegroundWindow" not in calls, \
        "★ hide() 里抢前台会杀死键盘钩子(实测 kbd_calls 4->0)"
    assert "AttachThreadInput" not in calls, \
        "AttachThreadInput 是抢前台的辅助手段, 同样不该在 hide() 里"
    # 但必须仍然激活(SetActiveWindow 是安全的, 实测钩子存活)
    assert "SetActiveWindow" in calls, \
        "必须保留 SetActiveWindow(它不杀钩子, 且让光标隐藏生效)"


def test_do_hide_still_captures_and_hides_cursor():
    """修复不能丢掉隐藏光标的两个关键动作。"""
    src = inspect.getsource(cursor_mod.CursorHider._do_hide)
    calls = _code_only(src)
    assert "SetCapture" in calls, "SetCapture 必须保留(接管鼠标输入)"
    assert "_set_cursor_visibility" in calls, "ShowCursor 计数器必须保留"
    assert "move_relative" in calls, "抖动注入必须保留(提交计数器)"


def test_do_show_does_not_steal_foreground_back():
    """show() 不该再把前台抢回去(我们从没抢过, 抢回去反而打断用户)。"""
    src = inspect.getsource(cursor_mod.CursorHider._do_show)
    calls = _code_only(src)
    assert "SetForegroundWindow" not in calls, \
        "show() 里抢前台会中断用户正在打字的窗口"
