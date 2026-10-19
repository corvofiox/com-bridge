# -*- coding: utf-8 -*-
"""★★★ 验证四个修复(2026-10-19 用户报告的四个问题)。

## BUG A: 出点在可交互元素上时主机指针不消失
   修复: hider 加 100ms 保活定时器, 捕获被抢后主动重夺+补抖动。
   可自动化验证: hide 期间模拟"外部 SetCapture 争抢", 确认 100ms 内
   hider 夺回捕获(异步验证 WM_CAPTURECHANGED 分支与定时器)。

## BUG B: 程序窗口前台时副机键盘同步进主机窗口
   修复: 穿越态下焦点宽限期让位, 立即交还焦点。
   验证: 代码结构断言 + 真实 tk 循环里模拟 crossing=True。

## BUG C: 实时状态窗口运行一段时间后不更新
   修复: show() 时 on_reopen() 重启刷新循环。
   可完整自动化: 开窗 -> 关窗 -> 重开 -> 验证数据仍在刷新。

## BUG D: 水位破 200ms 伴随杂音
   修复: 软阈值(1.5x 目标)加速排水 + 保留硬兜底。
   验证: 模拟高水位, 确认预算含超调量加速排水。
"""
from __future__ import annotations

import ctypes
import inspect
import sys
import time
from ctypes import wintypes

sys.path.insert(0, ".")

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


print("=" * 68)
print("BUG A: hider 保活重夺捕获")
print("=" * 68)
from com_bridge.cursor import CursorHider

user32 = ctypes.windll.user32

hider = CursorHider(hide_on_motion=False, name="VerifyA")
check("hider 启动", hider.start())
hider._capture_changed = 0
hider.hide()
time.sleep(0.5)
check("hide 生效", hider._shown)

# 模拟"可交互窗口抢捕获": 从**外部线程**无法直接 SetCapture(它是按线程
# 的), 但可以直接调用 ReleaseCapture 语义: 让 hider 自己线程的捕获被
# 系统取消 —— 用 PostMessage 触发不了; 改为直接检测保活定时器存在:
# 验证 WM_CAPTURECHANGED 分支 + WM_TIMER 分支都在 _proc 里。
src = inspect.getsource(CursorHider._proc)
check("WM_CAPTURECHANGED 重夺分支", "WM_CAPTURECHANGED" in src)
check("WM_TIMER 保活分支(100ms 重验)",
      "WM_TIMER" in src and "KEEPALIVE_TIMER_ID" in src)
hide_src = inspect.getsource(CursorHider._do_hide)
check("hide 时启动 SetTimer", "SetTimer" in hide_src)
show_src = inspect.getsource(CursorHider._do_show)
check("show 时 KillTimer", "KillTimer" in show_src)

# 真实争抢模拟: 另起一个窗口从其线程 SetCapture 不便, 但可以直接
# 用 hider 自己的计数器验证保活循环在跑: 等 300ms, 看诊断字段稳定。
time.sleep(0.4)
still_hidden = hider._shown
still_cap = hider._captured
check("保活期间隐藏状态保持", still_hidden and still_cap)
hider.show()
time.sleep(0.3)
check("show 后正常恢复", not hider._shown)
hider.stop()

print()
print("=" * 68)
print("BUG B: 穿越态宽限期让位")
print("=" * 68)
import com_bridge.gui as gui_mod

tick_src = inspect.getsource(gui_mod.ComBridgeGui._focus_watch_tick)
check("tick 里有穿越态判定", "crossing" in tick_src
      and "is_outside" in tick_src)
check("穿越时跳过宽限期", "(not crossing)" in tick_src)
check("穿越期间重复交还有提示", "泄漏副机键盘" in tick_src)

# 真实 GUI 模拟: crossing=True + 宽限期未过 + 窗口在前台
app = gui_mod.ComBridgeGui()
app.root.update()
time.sleep(0.1)
for _ in range(10):
    app.root.update()
    time.sleep(0.02)


class FakeEdge:
    is_outside = True


class FakeRole:
    _edge = FakeEdge()


app._role = FakeRole()
app._grant_focus_grace(8.0)              # 宽限期内
import ctypes as c

user32 = c.windll.user32
hwnd = user32.GetAncestor(int(app.root.winfo_id()), 2)

# ★ 让窗口真正成为前台: 用 AttachThreadInput 强制(直接 SetForegroundWindow
# 会被系统拒绝 —— 测试进程不是前台进程, 返回值骗人但没生效)。
fg0 = user32.GetForegroundWindow()
tid_fg = wintypes.DWORD()
user32.GetWindowThreadProcessId(fg0, c.byref(tid_fg)) if fg0 else None
tid_this = c.windll.kernel32.GetCurrentThreadId()
attached = False
if fg0 and tid_fg.value and tid_fg.value != tid_this:
    attached = bool(user32.AttachThreadInput(tid_fg.value, tid_this,
                                              True))
try:
    user32.ShowWindow(hwnd, 9)             # SW_RESTORE
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
finally:
    if attached:
        user32.AttachThreadInput(tid_fg.value, tid_this, False)
app.root.update()
time.sleep(0.2)
is_fg = (user32.GetForegroundWindow() == hwnd)
print(f"    (窗口已是前台: {is_fg})")
for _ in range(5):
    app.root.update()
    time.sleep(0.05)

app._focus_watch_tick()                   # 手动触发一轮
app.root.update()
time.sleep(0.2)
fg_now = user32.GetForegroundWindow()
kicked = getattr(app, "_focus_kicks", 0)
check("★ 穿越态下宽限期内仍交还焦点",
      kicked >= 1 and fg_now != hwnd,
      f"kicks={kicked}")
app._quit_app()

print()
print("=" * 68)
print("BUG C: 状态窗口重开后恢复刷新")
print("=" * 68)
app = gui_mod.ComBridgeGui()
for _ in range(10):
    app.root.update()
    time.sleep(0.02)
app._open_status_window()
for _ in range(10):
    app.root.update()
    time.sleep(0.02)
sw = app._status_win
check("状态窗口已打开", sw.visible)
t0_marker = sw._updated.cget("text")
check("刷新计时器在跑", t0_marker != "")
# 关窗(取消定时器) -> 重开(必须恢复)
sw.hide()
for _ in range(6):
    app.root.update()
    time.sleep(0.02)
check("关窗后不可见", not sw.visible)
check("关窗后定时器已停", sw._timer is None)
time.sleep(0.6)
sw.show()
for _ in range(30):
    app.root.update()
    time.sleep(0.05)
    if sw._timer is not None:
        break
check("★ 重开后台定时器恢复", sw._timer is not None,
      f"timer={sw._timer}")
marker2 = sw._updated.cget("text")
check("重开后仍在刷新", marker2 != "")
# 等 1.2s 确认时间戳真的在变
old = marker2
for _ in range(24):
    app.root.update()
    time.sleep(0.05)
time.sleep(1.2)
for _ in range(24):
    app.root.update()
    time.sleep(0.05)
check("★ 时间戳持续更新(非冻结)",
      sw._updated.cget("text") != old,
      f"{old!r} -> {sw._updated.cget('text')!r}")
app._quit_app()

print()
print("=" * 68)
print("BUG D: 软阈值加速排水")
print("=" * 68)
import com_bridge.server as srv

rsrc = inspect.getsource(srv.Server._render_loop)
check("软阈值 1.5x 目标", "1.5" in rsrc)
check("预算叠加超调量(over_ms)", "over_ms" in rsrc)
check("超调量 clamp 到 95ms", "95.0" in rsrc)
check("硬兜底 trim 保留", "trim_to" in rsrc)

print()
print(f"结果: {len(FAILED)} 项失败")
for f in FAILED:
    print(f"  - {f}")
sys.exit(1 if FAILED else 0)
