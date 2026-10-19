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


def test_do_hide_must_set_foreground():
    """★★★ _do_hide **必须**抢前台 —— 这是光标能隐藏的必要条件。

    ★ 2026-10-19 结论反转(第二版):
      旧测试断言"抢前台会杀死键盘钩子", 依据是 kbd_calls 4->0。
      但那次归因用的是 **tkinter 主窗口**, 而 hider 是完全不同的窗口类型
      (WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | 全屏 | 类光标全透明)。
      重新实测(tools/exp_hider_foreground_hook.py):
          A. 基线                kbd_calls +2
          B. **hider 抢前台后**   kbd_calls +2  <- 钩子照样可达!
          C. 抢前台 + SetCapture  光标 vis=False ✅
      旧结论被错误地从"tkinter 主窗口"外推到了"所有窗口"。

    ★ 为什么必须抢前台: MSDN 对 SetCapture 的明确限制 ——
      "only captures the mouse when the mouse button is down or the
       window is in the foreground"。
      旧代码只 SetActiveWindow 不抢前台 -> 非前台时 SetCapture 无效
      -> 本线程收不到鼠标消息 -> ShowCursor 计数不提交 -> **光标可见**。
      这是用户反复报的"光标不消失"的真正根因(任何位置都可能发生)。
    """
    src = inspect.getsource(cursor_mod.CursorHider._do_hide)
    calls = _code_only(src)
    assert "SetForegroundWindow" in calls, \
        "★ 必须抢前台, 否则 SetCapture 不生效 -> 光标不消失"
    assert "AttachThreadInput" in calls, \
        "抢前台必须用 AttachThreadInput 强制(裸调会被系统拒绝)"
    assert "SetCapture" in calls, "必须捕获鼠标"
    assert "SetActiveWindow" in calls, "保留激活(抢前台的补充)"


def test_do_hide_records_foreground_success():
    """★ 必须记录"前台是否真抢到了", 供现场诊断。"""
    src = inspect.getsource(cursor_mod.CursorHider._do_hide)
    assert "_is_foreground" in src, \
        "必须记录前台是否抢到(否则无法诊断'光标没消失')"
    init_src = inspect.getsource(cursor_mod.CursorHider.__init__)
    assert "_is_foreground" in init_src


def test_do_hide_still_captures_and_hides_cursor():
    """修复不能丢掉隐藏光标的两个关键动作。"""
    src = inspect.getsource(cursor_mod.CursorHider._do_hide)
    calls = _code_only(src)
    assert "SetCapture" in calls, "SetCapture 必须保留(接管鼠标输入)"
    assert "_set_cursor_visibility" in calls, "ShowCursor 计数器必须保留"
    assert "move_relative" in calls, "抖动注入必须保留(提交计数器)"


def test_do_show_restores_previous_foreground():
    """★ show() **必须**把前台还给用户原来的窗口。

    ★ 2026-10-19 结论反转: 因为 _do_hide 现在会抢前台(SetCapture 的
      前提), 所以退出穿越时必须恢复, 否则键盘焦点会停在那个全屏透明
      窗口上 —— 表现为"回到主机后打字没反应, 要点一下窗口才行"。
    """
    src = inspect.getsource(cursor_mod.CursorHider._do_show)
    calls = _code_only(src)
    assert "SetForegroundWindow" in calls, \
        "★ 必须还原前台(否则用户键盘焦点停在全屏透明窗口上)"
    assert "_stolen_foreground" in calls, "应还原到被抢走的那个窗口"
