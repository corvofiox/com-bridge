# -*- coding: utf-8 -*-
"""决定性实验: 罪魁是 RawInput(RIDEV_INPUTSINK)吗?

## 已确认
  钩子 + RawInput + hider抢前台 = 键盘彻底收不到 (物理键实测 +0, RawInput +36)
  钩子 + hider抢前台 (无 RawInput) = probe 恒 True  <- 但那是**注入**探针, 有假阴性嫌疑

## Deskflow 的对照(源码 tools/deskflow_full)
  * 它也抢前台(MSWindowsDesks.cpp:620-622 SetForegroundWindow(m_window))
  * 它的钩子装法与我们**完全相同**:
        SetWindowsHookEx(WH_KEYBOARD_LL, &keyboardLLHook, nullptr, 0)
  * 它**全程不用 RawInput**: 全项目仅 MSWindowsKeyState.cpp 一处提到,
    且不是 RIDEV_INPUTSINK 注册

=> 所以差异极可能就是 RawInput。本实验用**物理按键**证实/否证。

## 提示渠道(上次的教训)
上次脚本只把提示打印到终端, 用户完全看不到, 只能盲按。
这次用 **tkinter 弹窗**把提示摆在屏幕上, 确保用户看得见。

用法: python tools/exp_rawinput_is_culprit.py            (无 RawInput)
      python tools/exp_rawinput_is_culprit.py --rawinput (有 RawInput)
"""
from __future__ import annotations

import ctypes
import sys
import time

sys.path.insert(0, ".")
user32 = ctypes.windll.user32

WITH_RAWIN = "--rawinput" in sys.argv


def main() -> int:
    import tkinter as tk
    from com_bridge.hook import LowLevelHook
    from com_bridge.cursor import CursorHider

    hook = LowLevelHook()
    assert hook.start()
    time.sleep(0.3)

    li = None
    if WITH_RAWIN:
        from com_bridge.input import RawInputListener
        li = RawInputListener(on_event=lambda ev: None, accept_synthetic=True)
        li.start()
        assert li.wait_ready()
        time.sleep(0.3)

    hider = CursorHider(hide_on_motion=False, name="CulpritHider")
    hider.start()
    time.sleep(0.4)

    shell = user32.GetShellWindow()
    if shell:
        user32.SetForegroundWindow(shell)
    time.sleep(0.3)

    # ---- 可见提示窗口 ----
    root = tk.Tk()
    root.title("Com Bridge 诊断 — 请按键")
    root.geometry("560x240+120+120")
    root.attributes("-topmost", True)

    title_lbl = tk.Label(root, text="", font=("Microsoft YaHei UI", 15, "bold"))
    title_lbl.pack(pady=(16, 6))
    body = tk.Label(root, text="", font=("Microsoft YaHei UI", 11),
                    justify="left", wraplength=520)
    body.pack(pady=4)
    count = tk.Label(root, text="", font=("Consolas", 12), fg="#0a7")
    count.pack(pady=6)
    btn = tk.Button(root, text="开始", font=("Microsoft YaHei UI", 11),
                    width=14, height=1)
    btn.pack(pady=6)

    state = {"phase": 0, "c0": 0, "done": False, "rAw": 0}

    def refresh() -> None:
        d = hook.diag()
        state["rAw"] = d["kbd_calls"] - state["c0"]
        count.config(text=f"钩子已收到按键: {state['rAw']} 次")

    def tick() -> None:
        if state["done"]:
            return
        refresh()
        root.after(120, tick)

    def run_phase(idx: int) -> None:
        state["phase"] = idx
        if idx == 1:
            title_lbl.config(text="第 1 步 / 共 2 步：正常状态")
            body.config(text="请现在按键盘（任意键，按 5~10 下）。\n"
                             "此时未进入跨屏，光标正常。\n"
                             "★ 按够了再点下面的按钮。")
            state["c0"] = hook.diag()["kbd_calls"]
            btn.config(text="按好了，下一步", command=lambda: start_hide())
        elif idx == 2:
            title_lbl.config(text="第 2 步 / 共 2 步：跨屏隐藏光标状态")
            body.config(text="现在屏幕会短暂变空（光标已隐藏），这是正常的。\n"
                             "请再按 5~10 下键盘。\n"
                             "★ 按够了再点下面的按钮。")
            state["c0"] = hook.diag()["kbd_calls"]
            btn.config(text="按好了，结束", command=lambda: finish())
        refresh()

    def start_hide() -> None:
        r1 = state["rAw"]
        state["r1"] = r1
        if r1 == 0:
            # ★ 第 1 步一次都没收到 -> 用户还没按, 不能让流程继续
            body.config(text="⚠ 还没有收到任何按键。\n\n"
                             "请先按键盘 5~10 下，看到上方数字变大后再点按钮。\n"
                             "（数字不涨说明这个进程收不到键盘，本身也是信息）")
            btn.config(text="再试一次", command=lambda: run_phase(1))
            return
        hider.hide()
        root.lift()
        root.attributes("-topmost", True)
        root.after(400, lambda: run_phase(2))

    def finish() -> None:
        r2 = state["rAw"]
        state["r2"] = r2
        state["done"] = True
        hider.show()
        title_lbl.config(text="测试结束")
        body.config(text=f"正常状态收到: {state.get('r1', 0)} 次\n"
                         f"隐藏光标状态收到: {r2} 次\n\n"
                         f"{'✅ 键盘在两种状态都正常' if r2 > 0 else '❌ 隐藏光标后键盘收不到'}")
        btn.config(text="关闭", command=lambda: root.destroy())
        root.after(200, lambda: report(state.get("r1", 0), r2))

    def report(r1: int, r2: int) -> None:
        print()
        print(f"WITH_RAWIN={WITH_RAWIN}")
        print(f"  正常状态钩子收到: {r1}")
        print(f"  隐藏光标后钩子收到: {r2}")
        if r1 == 0:
            print("  !! 正常状态就是 0, 本实验无效")
        elif r2 > 0:
            print("  => 隐藏光标后物理键仍到达钩子")
        else:
            print("  => 隐藏光标后物理键到不了钩子")

    run_phase(1)
    root.after(120, tick)
    root.protocol("WM_DELETE_WINDOW", lambda: (hider.show(), root.destroy()))
    root.mainloop()

    hider.stop()
    if li is not None:
        li.stop()
    hook.stop()
    if shell:
        user32.SetForegroundWindow(shell)
    return 0


if __name__ == "__main__":
    sys.exit(main())
