# -*- coding: utf-8 -*-
"""端到端验证: 真机上 Ctrl+Alt+Esc 能否穿透低级钩子触发逃生回调。

单元测试只能证明 `PanicCombo` 的状态机对 —— 证明不了**钩子回调真的会
被调用、且真的会把组合键喂给判定器**。这里装一套真实钩子, 用 SendInput
注入 Ctrl+Alt+Esc(不带我们的 TAG, 因此会被当成"物理按键"), 看回调次数
与 panic_hits。

⚠ 会短暂按下 Ctrl+Alt(约 80ms), 注入的 Esc 在前台窗口里可能被当作
  一次 Esc 按键。运行前请确认前台不是需要 Esc 的程序。
⚠ 运行期间**不要**开别的 Com Bridge 实例抢钩子(不影响结果, 但会双重收键)。
"""
from __future__ import annotations

import sys
import time

from com_bridge.hook import LowLevelHook
from com_bridge.input import InputInjector

VK_CONTROL = 0x11
VK_MENU = 0x12
VK_ESCAPE = 0x1B

fired = []


def on_panic():
    fired.append(time.monotonic())


def main() -> int:
    h = LowLevelHook(name="PanicProbe", on_panic=on_panic)
    if not h.start():
        print("!! 钩子安装失败 err=%s" % h.last_error)
        return 1
    print("钩子已安装; 先注入一个无关键热身...")
    InputInjector.key(0xE8, 0, True)
    InputInjector.key(0xE8, 0, False)
    time.sleep(0.1)
    base_calls = h.stats["kbd_calls"]

    print("注入 Ctrl+Alt+Esc ...")
    # 注入时**不**带 TAG: 让它走"物理按键"分支(带 TAG 会被提前放行,
    # 根本到不了逃生判定)。
    InputInjector.key(VK_CONTROL, 0x1D, True)
    InputInjector.key(VK_MENU, 0x38, True)
    time.sleep(0.03)
    InputInjector.key(VK_ESCAPE, 0x01, True)
    InputInjector.key(VK_ESCAPE, 0x01, False)
    time.sleep(0.03)
    InputInjector.key(VK_MENU, 0x38, False)
    InputInjector.key(VK_CONTROL, 0x1D, False)
    time.sleep(0.4)

    d = h.diag()
    h.stop()
    print(f"kbd_calls: {base_calls} -> {d['kbd_calls']}")
    print(f"panic_hits = {d['panic_hits']}  (期望 1)")
    print(f"回调触发   = {len(fired)}  (期望 1)")
    ok = d["panic_hits"] == 1 and len(fired) == 1 and d["kbd_calls"] > base_calls
    print("=> " + ("通过" if ok else "失败"))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
