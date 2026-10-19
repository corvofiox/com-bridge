# -*- coding: utf-8 -*-
"""★★★ 决定性实验: hider 窗口"抢前台"是否会杀死键盘钩子?

## 为什么必须做这个实验
MSDN 对 SetCapture 的明确限制:
  "This function only captures the mouse when the mouse button is down
   or the window is in the foreground."
  "Only the foreground window can capture the mouse."

我们的 hider 刻意**不抢前台**(因为过去测出"窗口前台 -> kbd_calls 0"),
于是 SetCapture 在"鼠标按钮没按下 + 非前台"时**不生效** ——
本线程收不到鼠标消息 -> ShowCursor 计数器不提交 -> 光标可见。
这正是用户反复报告的"光标不消失", 而且它在**任何**光标位置都可能发生
(不限于可交互元素)。

## 但 Deskflow 的做法是抢前台
MSWindowsDesks.cpp deskLeave 的 primary 分支(lowLevel 模式):
    EnableWindow(m_window, TRUE);
    SetActiveWindow(m_window);
    AttachThreadInput(thatThread, thisThread, TRUE);
    SetForegroundWindow(m_window);
    AttachThreadInput(..., FALSE);
它抢前台, 而它的键盘钩子依然工作。

## 所以要重新验证: **hider 这种窗口**抢前台时, 钩子是否真的死?
过去测的是 **tkinter 主窗口**("kbd_calls 8->0"), 那是个普通 GUI 窗口。
hider 是 WS_EX_TRANSPARENT + WS_EX_TOOLWINDOW + 全屏 + 类光标全透明,
行为可能完全不同。

## 实验设计
A. 基线: 无前台窗口, 注入 TAG 键, 看 kbd_calls
B. hider 窗口抢前台(AttachThreadInput 强制), 再注入, 看 kbd_calls
C. 抢完前台后检查光标可见性(GetCursorInfo)
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

sys.path.insert(0, ".")

user32 = ctypes.windll.user32

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def cursor_showing() -> bool:
    class CI(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("hCursor", wintypes.HANDLE), ("pt", wintypes.POINT)]
    ci = CI()
    ci.cbSize = ctypes.sizeof(CI)
    user32.GetCursorInfo.argtypes = [ctypes.c_void_p]
    user32.GetCursorInfo(ctypes.byref(ci))
    return bool(ci.flags & 1)


def hwnd_title(h) -> str:
    if not h:
        return "(无)"
    n = user32.GetWindowTextLengthW(h)
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(h, buf, n + 2)
    return buf.value or f"<{h}>"


print("== 启动钩子(生产配置) ==")
from com_bridge.hook import LowLevelHook
from com_bridge.input import InputInjector

hook = LowLevelHook()
ok = hook.start()
print(f"  钩子启动: {ok}")
time.sleep(0.3)


def count_kbd(duration=0.6) -> int:
    """注入一个带 TAG 的键, 返回 kbd_calls 增量。"""
    before = hook.diag()["kbd_calls"]
    # VK_F24 是最安全的测试键(不触发任何功能)
    InputInjector.key(0x87, 0, down=True)
    time.sleep(0.05)
    InputInjector.key(0x87, 0, down=False)
    t0 = time.time()
    while time.time() - t0 < duration:
        time.sleep(0.03)
    return hook.diag()["kbd_calls"] - before


print()
print("== A. 基线(无前台窗口, 焦点在资源管理器) ==")
shell = user32.GetShellWindow()
if shell:
    user32.SetForegroundWindow(shell)
time.sleep(0.4)
a = count_kbd()
print(f"  kbd_calls 增量 = {a}")
check("基线钩子可达", a > 0, f"+{a}")

print()
print("== B. hider 窗口抢前台(复刻 Deskflow primary lowLevel 分支) ==")
# 启动一个 hider 并让它成为前台
from com_bridge.cursor import CursorHider

hider = CursorHider(hide_on_motion=False, name="ForegroundTest")
hider.start()
time.sleep(0.3)
hwnd = hider._hwnd
print(f"  hider hwnd = {hwnd}")

# Deskflow 的完整序列
user32.EnableWindow(hwnd, 1)
user32.SetActiveWindow(hwnd)
that_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(),
                                              None)
this_thread = ctypes.windll.kernel32.GetCurrentThreadId()
if that_thread and that_thread != this_thread:
    user32.AttachThreadInput(that_thread, this_thread, True)
try:
    user32.SetForegroundWindow(hwnd)
finally:
    if that_thread and that_thread != this_thread:
        user32.AttachThreadInput(that_thread, this_thread, False)
time.sleep(0.3)
fg = user32.GetForegroundWindow()
print(f"  抢前台后 fg = {hwnd_title(fg)} (hider={hwnd}) "
      f"成功={fg == hwnd}")

b = count_kbd()
print(f"  kbd_calls 增量 = {b}")
check("★ hider 抢前台后钩子**仍然可达**", b > 0,
      f"+{b} (若为 0 说明抢前台会杀死钩子, 只能另寻他法)")

print()
print("== C. 抢前台 + SetCapture + ShowCursor 后光标是否隐藏 ==")
user32.SetCapture(hwnd)
print(f"  GetCapture = {user32.GetCapture()} "
      f"(== hwnd? {user32.GetCapture() == hwnd})")
for _ in range(10):
    n = user32.ShowCursor(0)
    if n < 0:
        break
print(f"  ShowCursor 计数 = {n}")
time.sleep(0.3)
vis = cursor_showing()
print(f"  光标可见 = {vis}")
check("★ 前景+捕获下光标隐藏", not vis, f"vis={vis}")

# 再检查一次钩子(在隐藏状态下)
c2 = count_kbd()
print(f"  隐藏状态下 kbd_calls 增量 = {c2}")

print()
print("== D. 收回: 恢复前台与光标 ==")
user32.ReleaseCapture()
for _ in range(10):
    m = user32.ShowCursor(1)
    if m >= 0:
        break
if shell:
    user32.SetForegroundWindow(shell)
time.sleep(0.3)
print(f"  光标可见 = {cursor_showing()}")
hider.show()
hider.stop()
hook.stop()

print()
print(f"结果: {len(FAILED)} 项失败")
for f in FAILED:
    print(f"  - {f}")
print()
print("判读:")
print("  若 B 通过(b>0) -> 可以放心抢前台, 光标问题可彻底解决")
print("  若 B 失败(b==0) -> 必须找不依赖前台的隐藏方案")
sys.exit(1 if FAILED else 0)
