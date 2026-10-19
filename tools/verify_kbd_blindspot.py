# -*- coding: utf-8 -*-
"""活体验证: "键盘钩子死亡但鼠标钩子活着"这一盲区, 现有检测是否抓得住,
交还时的复核(_verify_kbd_on_release)是否修好它。

## 场景(用户实测: "钩子吞输入 鼠标有数字 键盘0 回调0")

键盘钩子失效的两条路:
  a) 被系统静默摘除 —— 句柄不置空, 鼠标钩子通常幸存;
  b) 被别的程序先吞掉 —— 键到不了我们(实测场景, 用户装着 Deskflow)。
两条路的公共点: **鼠标一切正常**, 于是"光标位移自检"不报警;
而 (b) 里键也不产生 RawInput, 泄漏检测同样失明。

## 本工具用 (b) 复现

装一个"抢键钩子"(relay=True 吞掉所有键盘)排在前面, 我们的钩子就
再也收不到键盘回调(kbd_calls 停增), 但鼠标不受影响。
然后依次验证:
  1. 老的两道检测: 鼠标自检(看光标位移) -> 不报警(盲区实锤);
  2. 新的交还复核: _verify_kbd_on_release 的探针 -> 必须报警并重装;
  3. 重装后我们的钩子排到链首, 键盘回调恢复。

★ 注入一律走 inject_util(强制复用生产 INPUT 结构并断言尺寸)。
★ 运行期间会短暂吞掉本机键盘, 请勿打字。
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

sys.path.insert(0, ".")

from com_bridge.hook import LowLevelHook
from com_bridge.server import Server
from inject_util import PROBE_VK, burst, mouse_move, require_injection

u = ctypes.windll.user32

# --- 乱杀钩子: 原生回调, 吞掉**一切**键盘事件(不看 TAG, 模拟真实第三方) ---
# ★ 不能用生产 LowLevelHook 当 grabber: 它的回调放行"带我们自己 TAG 的键",
#   而 probe 探针恰好带 TAG —— 实测这会让 probe 误报 True(exp_injected_key_chain.py
#   已证明)。真实第三方(Deskflow 等)不看 TAG, 连探针一起吞。
KBDLL = 13
_killer_ref = None       # ★ 必须常驻, 否则 GC 后野指针崩溃
_killer_handle = None

HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                             wintypes.WPARAM, wintypes.LPARAM)
u.SetWindowsHookExW.restype = ctypes.c_void_p
u.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p,
                                wintypes.DWORD]
u.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]


@HOOKPROC
def _kill_all_kbd(code, wparam, lparam):
    if code >= 0:
        return 1        # 吞掉一切键盘事件
    return u.CallNextHookEx(None, code, wparam, lparam)


def install_killer() -> bool:
    global _killer_ref, _killer_handle
    msg = wintypes.MSG()
    u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)   # 线程要有消息队列
    _killer_ref = _kill_all_kbd
    _killer_handle = u.SetWindowsHookExW(KBDLL, _kill_all_kbd, None, 0)
    return bool(_killer_handle)


def remove_killer() -> None:
    global _killer_handle
    if _killer_handle:
        u.UnhookWindowsHookEx(_killer_handle)
        _killer_handle = None
    time.sleep(0.3)


FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def hook_probe(fake) -> str:
    """复刻 Server._verify_kbd_on_release 的核心逻辑(不起完整 server)。"""
    if not fake["installed"]:
        return "skip"
    probe = fake["probe"]()      # callable -> True/False/None
    if probe is False:
        fake["reinstall"]()
        return "reinstalled"
    return "noop"


print("== 1. 装我们的钩子(生产同款), 进入穿越态 ==")
ours = LowLevelHook(name="Ours", on_key_event=lambda *a: None)
if not ours.start():
    print("安装失败"); sys.exit(2)
ours.set_relay(True)
time.sleep(0.4)

print("== 2. 装'乱杀钩子'模拟真实第三方在链前吞掉一切键盘(复现盲区) ==")
# ★ 必须用原生"吞一切"钩子, 不能用生产 LowLevelHook 当 grabber:
#   生产回调会放行带我们 TAG 的键, 而 probe 探针恰好带 TAG,
#   会让 probe 误报 True(exp_injected_key_chain.py 已实测证明)。
if not install_killer():
    print("乱杀钩子安装失败"); sys.exit(2)
time.sleep(0.5)

b = ours.diag()
sent = burst(3, PROBE_VK)
require_injection(sent, 6)
a = ours.diag()
d_kbd = a["kbd_calls"] - b["kbd_calls"]
mb = mouse_move(5, 3)
time.sleep(0.2)
a2 = ours.diag()
d_mouse = a2["mouse_eaten"] - b["mouse_eaten"]
print(f"  被抢占后: 我们的 kbd_calls+{d_kbd} (0=键盘到不了我们), "
      f"mouse_eaten+{d_mouse}")
check("复现盲区: 键盘回调为 0(键被别的程序先收走)", d_kbd == 0,
      f"kbd_calls+{d_kbd}")
check("同时鼠标一切正常(这正是盲区所在)", d_mouse > 0,
      f"mouse_eaten+{d_mouse}")

print()
print("== 3. 老检测失明验证: 鼠标健康自检的判据是光标位移 ==")
# 鼠标钩子活着 -> 物理移动被吞 -> 光标不动 -> 位移判据恒 False = 不报警
from com_bridge.input import get_cursor_pos
x0, y0 = get_cursor_pos()
mouse_move(30, 20)
time.sleep(0.15)
x1, y1 = get_cursor_pos()
moved = abs(x1 - x0) > Server.HOOK_PROBE_TOLERANCE_PX or \
    abs(y1 - y0) > Server.HOOK_PROBE_TOLERANCE_PX
check("鼠标自检不报警(它只看得见'钩子死了导致的光标位移')",
      not moved, f"光标 {x0},{y0} -> {x1},{y1}")

print()
print("== 4. 新的交还复核: 探针必须发现键盘死了并重装 ==")
state = {
    "installed": ours.installed,
    "probe": ours.probe_keyboard_hook,
    "reinstall": ours.request_reinstall,
}
r = hook_probe(state)
print(f"  hook_probe(交还复核核心) -> {r}")
check("交还复核发现键盘失效并重装", r == "reinstalled", f"-> {r}")

print()
print("== 5. 重装后键盘回调恢复 ==")
time.sleep(0.5)     # 等钩子线程处理 WM_APP_REINSTALL
b3 = ours.diag()
sent3 = burst(3, PROBE_VK)
require_injection(sent3, 6)
a3 = ours.diag()
d3 = a3["kbd_calls"] - b3["kbd_calls"]
print(f"  重装后: 我们的 kbd_calls+{d3}")
check("重装后键盘回调恢复(下一轮穿越可正常吞键)", d3 > 0, f"+{d3}")

ours.set_relay(False)
remove_killer()
ours.stop()

print()
print(f"结果: {len(FAILED)} 项失败")
if FAILED:
    print("  盲区未被修复, 需要继续:")
    for f in FAILED:
        print(f"    - {f}")
    sys.exit(1)
print("全部通过 —— '键被别的程序先收走'的盲区由交还复核兜住了")
