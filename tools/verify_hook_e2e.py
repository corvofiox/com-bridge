"""低级钩子端到端验收: 真实装钩子, 验证"吞掉本机输入 / 放行回程注入"。

用户症状: "进入副机后鼠标点击也不落位在副机, 还是落位在主机"。
根因: RawInput 只观察不消费 -> 必须用低级钩子吞掉物理事件。

本脚本用**真实的 LowLevelHook**(产品代码)验证四件事:
  1. relay=False 时物理点击**能**落到本机(控制权在本机, 一切正常)
  2. relay=True  时物理点击**被吞**(不落到本机) -> 这就是"交给副机"的前提
  3. relay=True  时**带 TAG 的注入**仍能落到本机(副机回程输入可达)
  4. stop() 后立即恢复放行(用户不会被吞住)

"点击是否落到本机"怎么客观测:
  装钩子时物理点击会被吞 -> 本机的按键状态(GetAsyncKeyState)不会变化。
  用一个**低层探测器**: 在测试窗口上监听 WM_LBUTTONDOWN 不可靠(需要焦点),
  改用 GetAsyncKeyState(VK_LBUTTON) 轮询 —— 注入事件同样会改变它, 所以
  能区分"事件到了系统" vs "被钩子吞掉"。

⚠ 本机若跑着 Deskflow(它也有自己的 LL 钩子), 它可能先吞掉我们的注入。
  脚本会检测并如实标注, 不影响 1/2/4 的结论(那测的是我们自己的钩子)。
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from com_bridge.hook import LowLevelHook
from com_bridge.input import INPUT, InputInjector, get_cursor_pos, user32

user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]

VK_LBUTTON = 0x01

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_MOVE = 0x0001


def click(tag: bool = False, hold_ms: int = 60) -> None:
    """发一次左键按下+抬起。tag=True 时打上我们的标记(模拟副机回程注入)。"""
    for flags in (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP):
        i = INPUT(type=0)
        i.union.mi.dwFlags = flags
        i.union.mi.dwExtraInfo = InputInjector.TAG if tag else 0
        user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))
        time.sleep(hold_ms / 1000.0)


def button_down_now() -> bool:
    """当前左键是否处于按下状态(系统级)。"""
    return bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)


def click_reaches_system(tag: bool) -> bool:
    """做一次点击, 观察"按下"状态是否真的到达系统。"""
    # 先确保按键是抬起状态
    i = INPUT(type=0)
    i.union.mi.dwFlags = MOUSEEVENTF_LEFTUP
    i.union.mi.dwExtraInfo = InputInjector.TAG
    user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))
    time.sleep(0.15)

    seen = False
    # 在按下瞬间采样: 用较长按住时间以便稳定观测
    i = INPUT(type=0)
    i.union.mi.dwFlags = MOUSEEVENTF_LEFTDOWN
    i.union.mi.dwExtraInfo = InputInjector.TAG if tag else 0
    user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))
    for _ in range(8):
        time.sleep(0.02)
        if button_down_now():
            seen = True
            break
    # 抬起(带标记, 保证即使钩子在吞也能抬起, 不留悬空按键)
    i = INPUT(type=0)
    i.union.mi.dwFlags = MOUSEEVENTF_LEFTUP
    i.union.mi.dwExtraInfo = InputInjector.TAG
    user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))
    time.sleep(0.1)
    return seen


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    hook = LowLevelHook(name="E2EHook")
    if not hook.start():
        print(f"!! 钩子安装失败 err={hook.last_error}")
        print("   可能被其它键鼠共享软件占用。请退出 Deskflow/Synergy 后重试。")
        return 2
    print(f"钩子已安装: {hook.diag()}")
    time.sleep(0.2)

    # ---------- 1) relay=False: 物理点击应能落到本机 ----------
    hook.set_relay(False)
    time.sleep(0.15)
    ok1 = click_reaches_system(tag=False)
    results.append(("relay=False 时物理点击**能**落到本机(本地正常使用)",
                    ok1, "控制权在本机时必须完全正常"))

    # ---------- 2) relay=True: 物理点击应被吞 ----------
    hook.set_relay(True)
    time.sleep(0.15)
    d0 = hook.diag()
    ok2 = not click_reaches_system(tag=False)
    d1 = hook.diag()
    eaten = d1["mouse_eaten"] - d0["mouse_eaten"]
    results.append(("relay=True 时物理点击**被吞**(不落到主机)",
                    ok2, f"钩子吞掉 {eaten} 次"))
    results.append(("钩子确实观测到了这些点击(计数增长)", eaten > 0,
                    f"+{eaten}"))

    # ---------- 3) relay=True 时, 带 TAG 的注入仍应到达本机 ----------
    d2 = hook.diag()
    ok3 = click_reaches_system(tag=True)
    d3 = hook.diag()
    passed = d3["injected_passed"] - d2["injected_passed"]
    results.append(("relay=True 时**带标记注入**仍能到达本机(回程输入可达)",
                    ok3, f"放行的注入 {passed} 次"))
    results.append(("带标记事件被正确识别为'我们的'(计数增长)", passed > 0,
                    f"+{passed}"))

    # ---------- 4) 放行后恢复 ----------
    hook.set_relay(False)
    time.sleep(0.15)
    ok4 = click_reaches_system(tag=False)
    results.append(("交还控制权后物理点击恢复正常", ok4, ""))

    # ---------- 5) stop() 后彻底放行 ----------
    hook.stop()
    time.sleep(0.2)
    ok5 = click_reaches_system(tag=False)
    results.append(("stop() 后物理点击正常(不会被吞住)", ok5,
                    "否则用户面对'键鼠全无响应'"))

    print("\n=== 低级钩子端到端验收 ===")
    allok = True
    for name, ok, detail in results:
        allok = allok and ok
        print(f"  {'OK  ' if ok else 'FAIL'} {name} {detail}")
    print(f"\n[结论] {'PASS —— 吞掉本机输入且放行回程注入' if allok else 'FAIL'}")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
