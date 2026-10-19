# -*- coding: utf-8 -*-
"""决定性实验(第 2 版): 低级键盘钩子吞掉按键后, RawInput 还能收到吗?

第 1 版(tools/diag_hook_kbd_rawinput.py 初版)的教训: 脚本自带了一个独立
WM_INPUT 读取器, 它在生产 Listener **之后**注册了键盘 usage —— 同进程内
对同一 usUsage 的后一次 RegisterRawInputDevices 会覆盖前一次
(last-wins), 导致 Listener 收不到任何键盘 WM_INPUT, 阶段 0 数据被污染。

本版只保留生产 RawInputListener(与生产完全同构), 用四个阶段做干净对照:
  阶段 0  不装钩子, 注入无标记 'A'(down+up)      -> Listener 应收到
  阶段 1  装钩子+吞无标记, 注入无标记 'B'         -> 收不到 => 根因确认
  阶段 2  同一钩子但切换为"全放行"模式, 注入 'A'  -> 收得到 => 抑制来自
          "吞"本身, 而非钩子的存在
  阶段 3  恢复吞模式, 注入带 TAG 的键(生产注入路径) -> 放行且 Listener
          按 TAG 过滤(synthetic +2) => 注入事件照常走 RawInput

物理键盘无法无人值守测试, 全程用注入键: SendInput 事件与物理事件走同一
条输入管道, LL 钩子一视同仁, 结论可外推。

用法: python tools/diag_hook_kbd_rawinput.py
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import (
    POINTER as P,
    byref,
    c_int,
    c_ssize_t,
    c_void_p,
    wintypes,
)

sys.path.insert(0, ".")

from com_bridge.input import (
    INPUT,
    KEYEVENTF_KEYUP,
    INPUT_KEYBOARD,
    InputInjector,
    RawInputListener,
    kernel32,
    user32,
)

VK_A = 0x41
VK_B = 0x42

WH_KEYBOARD_LL = 13
WM_QUIT = 0x0012
PM_REMOVE = 0x0001

TAG = InputInjector.TAG


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", c_void_p),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(c_ssize_t, c_int, wintypes.WPARAM,
                              wintypes.LPARAM)

user32.SetWindowsHookExW.restype = c_void_p
user32.SetWindowsHookExW.argtypes = [c_int, HOOKPROC, c_void_p,
                                     wintypes.DWORD]
user32.UnhookWindowsHookEx.argtypes = [c_void_p]
user32.CallNextHookEx.restype = c_ssize_t
user32.CallNextHookEx.argtypes = [c_void_p, c_int, wintypes.WPARAM,
                                  wintypes.LPARAM]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

state = {"mode": "eat"}   # "eat" = 吞无标记; "pass" = 全放行
eaten = {"n": 0}
passed = {"n": 0}
hook_ref = None           # 常驻引用防 GC
hook_ready = threading.Event()
hook_stop = threading.Event()


def hook_proc(code, wparam, lparam):
    try:
        if code < 0:
            return user32.CallNextHookEx(None, code, wparam, lparam)
        info = ctypes.cast(lparam, P(KBDLLHOOKSTRUCT)).contents
        if info.dwExtraInfo == TAG or state["mode"] == "pass":
            passed["n"] += 1
            return user32.CallNextHookEx(None, code, wparam, lparam)
        eaten["n"] += 1
        return 1  # 吞
    except Exception:
        return user32.CallNextHookEx(None, code, wparam, lparam)


def hook_thread():
    global hook_ref
    hook_ref = HOOKPROC(hook_proc)
    h = user32.SetWindowsHookExW(WH_KEYBOARD_LL, hook_ref, None, 0)
    if not h:
        print("[hook] SetWindowsHookExW 失败: %d" % kernel32.GetLastError())
        return
    hook_ready.set()
    msg = wintypes.MSG()
    while not hook_stop.is_set():
        r = user32.PeekMessageW(byref(msg), None, 0, 0, PM_REMOVE)
        if r:
            if msg.message == WM_QUIT:
                break
            user32.TranslateMessage(byref(msg))
            user32.DispatchMessageW(byref(msg))
        else:
            time.sleep(0.002)
    user32.UnhookWindowsHookEx(h)


def send_key(vk, down, tag):
    i = INPUT(type=INPUT_KEYBOARD)
    i.union.ki.wVk = vk
    i.union.ki.dwFlags = 0 if down else KEYEVENTF_KEYUP
    if tag:
        i.union.ki.dwExtraInfo = TAG
    if user32.SendInput(1, byref(i), ctypes.sizeof(INPUT)) != 1:
        print("  [!] SendInput 失败")


def send_pair(vk, tag):
    send_key(vk, True, tag)
    time.sleep(0.05)
    send_key(vk, False, tag)


def wait_quiet(t=0.25):
    time.sleep(t)


def snap(lst):
    d = lst.diag()
    return (d["total"], d["key"], d["synthetic"])


def phase(name, note, lst, run):
    time.sleep(0.3)
    b0 = snap(lst)
    run()
    time.sleep(0.4)
    b1 = snap(lst)
    dt, dk, ds = b1[0] - b0[0], b1[1] - b0[1], b1[2] - b0[2]
    print("\n%s" % name)
    print("  %s" % note)
    print("  Listener: total +%d  key +%d  synthetic +%d" % (dt, dk, ds))
    return dt, dk, ds


def main():
    print("=" * 72)
    print("决定性实验 v2: LL 键盘钩子吞掉按键后, RawInput 是否仍收到?")
    print("=" * 72)

    lst = RawInputListener(on_event=lambda ev: None)
    lst.start()
    if not lst.wait_ready(2.0):
        print("Listener 未就绪, 终止")
        return 1
    print("[listener] 就绪 diag=%s" % lst.diag())

    # ---- 阶段 0: 无钩子 + 无标记注入 'A' ----------------------------------
    p0 = phase("阶段0 无钩子 + 无标记注入 'A'(down/up)",
               "预期: 注入本身产生 RawInput, Listener 收到", lst,
               lambda: send_pair(VK_A, tag=False))
    stage0_ok = p0[0] >= 2 and p0[1] >= 2

    # ---- 阶段 1: 装钩子(吞) + 无标记注入 'B' ------------------------------
    threading.Thread(target=hook_thread, daemon=True).start()
    hook_ready.wait(2.0)
    time.sleep(0.2)
    e0 = eaten["n"]
    p1 = phase("阶段1 钩子已装(吞无标记) + 无标记注入 'B'(down/up)",
               "关键对照: 被吞的事件是否还出现在 RawInput?", lst,
               lambda: send_pair(VK_B, tag=False))
    print("  钩子: eaten=+%d" % (eaten["n"] - e0))
    stage1_got = p1[0] >= 2

    # ---- 阶段 2: 同一钩子切全放行 + 无标记注入 'A' ------------------------
    state["mode"] = "pass"
    time.sleep(0.2)
    p2 = phase("阶段2 同一钩子但全放行(pass 模式) + 无标记注入 'A'",
               "分离变量: 若收得到, 抑制来自'吞'而非钩子存在", lst,
               lambda: send_pair(VK_A, tag=False))
    stage2_got = p2[0] >= 2
    state["mode"] = "eat"
    time.sleep(0.2)

    # ---- 阶段 3: 吞模式 + 带 TAG 注入 -------------------------------------
    p3 = phase("阶段3 吞模式 + 带 TAG 注入 'A'(生产注入路径)",
               "预期: 放行且 Listener 按 TAG 丢弃(synthetic +2)", lst,
               lambda: send_pair(VK_A, tag=True))
    stage3_ok = p3[2] >= 2 and p3[1] == 0

    hook_stop.set()
    time.sleep(0.3)

    print("\n" + "=" * 72)
    print("结论")
    print("=" * 72)
    print("阶段0 无钩子时注入产生 RawInput:        %s"
          % ("是" if stage0_ok else "否(!环境异常, 结论不可信)"))
    print("阶段1 被钩子吞掉的键出现在 RawInput:    %s"
          % ("是" if stage1_got else "否 —— 被吞即消失"))
    print("阶段2 钩子放行时事件出现在 RawInput:    %s"
          % ("是" if stage2_got else "否(异常)"))
    print("阶段3 带 TAG 注入被 Listener 正确过滤:  %s"
          % ("是" if stage3_ok else "否"))
    if stage0_ok and not stage1_got and stage2_got:
        print("\n=> ROOT CAUSE 确认: WH_KEYBOARD_LL 吞掉(return 1)的键盘事件")
        print("   **不会**产生 RawInput —— 生产'钩子只吞+RawInput 唯一来源'")
        print("   架构对键盘不成立, 键盘事件必须改由钩子回调自产(照搬 Deskflow)")
    elif stage0_ok and stage1_got:
        print("\n=> 被吞键盘事件仍在 RawInput —— 键盘失效另有原因(过滤链/链路)")
    else:
        print("\n=> 数据不自洽, 需人工复查")
    return 0


if __name__ == "__main__":
    sys.exit(main())
