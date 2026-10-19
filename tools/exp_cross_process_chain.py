# -*- coding: utf-8 -*-
"""★ 跨进程钩子链测试: 另一个进程吞掉所有键盘时, 我们还能收到吗?

## 为什么必须测跨进程
之前所有钩子链实验都是**同进程**内两个 LowLevelHook, 结论是
"同进程内所有钩子都会被调用, 吞键不阻断后者"。但真实场景里
干扰者(Deskflow core / 输入法 / 安全软件)是**另一个进程**。

## 用户现场
* 键盘鼠标只接在主机
* 进入副机后打字 -> 主机焦点窗口也收到(钩子没吞)
* 自检失败: kbd_calls=0(回调从未被调用)
* Deskflow 服务已 Stopped/Disabled, 问题依旧

## 本工具
1. 用 subprocess 起一个**独立进程**装"吞一切"键盘钩子(模拟干扰者)
2. 本进程装我们的钩子
3. 测 probe + kbd_calls
4. 再让干扰进程退出, 重测

判读:
  * 干扰进程在时我们收不到, 退出后能收到
    -> 跨进程链首吞键确实会阻断我们 -> 必须找出用户机器上那个进程
  * 干扰进程在时我们也能收到
    -> 跨进程不阻断 -> 用户的问题在别处(线程/时序)
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, ".")

HERE = os.path.dirname(os.path.abspath(__file__))
GRABBER = os.path.join(HERE, "_grabber_proc.py")

# --- 生成干扰进程脚本 ---
GRABBER_SRC = '''# -*- coding: utf-8 -*-
"""独立进程: 装一个吞掉所有键盘事件的低级钩子, 然后保持运行。"""
import ctypes, sys, time
from ctypes import wintypes
user32 = ctypes.windll.user32
KBDLL = 13
HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                             wintypes.WPARAM, wintypes.LPARAM)
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                     ctypes.c_void_p, wintypes.DWORD]
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]

@HOOKPROC
def grab(code, wparam, lparam):
    if code >= 0:
        return 1          # 吞掉一切
    return user32.CallNextHookEx(None, code, wparam, lparam)

msg = wintypes.MSG()
user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
h = user32.SetWindowsHookExW(KBDLL, grab, None, 0)
if not h:
    print("GRABBER_FAIL", flush=True)
    sys.exit(1)
print("GRABBER_READY", flush=True)
# 泵消息(必须, 否则系统不会调用我们的钩子)
end = time.time() + 60
while time.time() < end:
    r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0, )
    if r <= 0:
        break
    user32.TranslateMessage(ctypes.byref(msg))
    user32.DispatchMessageW(ctypes.byref(msg))
user32.UnhookWindowsHookEx(h)
print("GRABBER_EXIT", flush=True)
'''


def measure(tag: str):
    from com_bridge.hook import LowLevelHook
    from inject_util import PROBE_VK, burst, require_injection
    h = LowLevelHook(name=tag, on_key_event=lambda *a: None)
    if not h.start():
        h.stop()
        print(f"  [{tag}] 钩子装不上")
        return None, -1
    p = h.probe_keyboard_hook()
    b = h.diag()
    sent = burst(2, PROBE_VK)
    try:
        require_injection(sent, 4)
    except Exception:  # noqa: BLE001
        h.stop()
        return p, -1
    time.sleep(0.35)
    a = h.diag()
    d = a["kbd_calls"] - b["kbd_calls"]
    print(f"  [{tag}] probe={p} kbd_calls+{d}")
    h.stop()
    time.sleep(0.2)
    return p, d


# 写干扰脚本
with open(GRABBER, "w", encoding="utf-8") as f:
    f.write(GRABBER_SRC)

print("== 1. 基线(无干扰进程) ==")
p0, c0 = measure("baseline")

print()
print("== 2. 起独立进程装'吞一切'钩子 ==")
proc = subprocess.Popen([sys.executable, GRABBER],
                        stdout=subprocess.PIPE, text=True)
ready = False
t0 = time.time()
while time.time() - t0 < 10:
    line = proc.stdout.readline()
    if not line:
        break
    if "GRABBER_READY" in line:
        ready = True
        break
    if "GRABBER_FAIL" in line:
        break
print(f"  干扰进程就绪: {ready} (pid={proc.pid})")
time.sleep(0.5)

print()
print("== 3. 干扰进程在时, 装我们的钩子 ==")
p1, c1 = measure("with-grabber")

print()
print("== 4. 让干扰进程退出, 重测 ==")
proc.terminate()
try:
    proc.wait(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill()
time.sleep(0.8)
p2, c2 = measure("after-grabber-exit")

try:
    os.unlink(GRABBER)
except OSError:
    pass

print()
print("== 结果 ==")
print(f"  1 基线(无干扰)      : probe={p0} calls+{c0}")
print(f"  2 干扰进程在(吞一切): probe={p1} calls+{c1}")
print(f"  3 干扰退出后        : probe={p2} calls+{c2}")
print()
if c0 > 0 and c1 == 0 and c2 > 0:
    print("  ★★★ 结论: **跨进程'吞一切'钩子会完全阻断我们**")
    print("     用户机器上必然存在这样一个进程(Deskflow 已排除, 需另找)")
elif c1 > 0:
    print("  ★ 结论: 跨进程不阻断(我们仍被调用)")
    print("     说明用户的问题不是'被别的进程抢钩子', 而是本进程内部状态")
else:
    print("  数据异常, 需要复查")
