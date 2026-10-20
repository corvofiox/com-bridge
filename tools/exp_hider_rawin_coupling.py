# -*- coding: utf-8 -*-
"""定位 HIDER x RAWIN 的耦合: 到底是"hider 隐藏"还是"RawInput 在跑"导致失效?

## 二分结果(真实 Server, 干净环境)
  NONE  (全开)                -> REPRO   (probe 立刻 False, kbd_calls 不推进)
  AUDIO (关音频)              -> REPRO
  HIDER (不调 hider.hide())   -> NO-REPRO (6s 恒 True)
  RAWIN (不启动 RawInput)     -> NO-REPRO

两个互相独立的开关都能消除故障 => 不是单点原因, 而是**两者同时存在**才触发。

## 本脚本验证的假设
H0: 只需要 hider.hide()(不需要 relay/穿越)        -> 早先已否定(hide 单独 6s 恒 True)
H1: 需要 hider.hide() **且** RawInput 在跑
H2: 需要 RawInput 在跑(hider 无关)                -> 已被 HIDER 组否定

直接测 H1: 启动 Server(含 RawInput), 手动只调 hider.hide()(不进 _acquire_control,
relay 保持 False), 看 probe。

用法: python tools/exp_hider_rawin_coupling.py
"""
from __future__ import annotations

import ctypes
import sys
import time

sys.path.insert(0, ".")
user32 = ctypes.windll.user32


def title(h) -> str:
    if not h:
        return "(none)"
    n = user32.GetWindowTextLengthW(h)
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(h, buf, n + 2)
    return buf.value or f"<{h}>"


def probe_seq(hook, n=8, gap=0.4):
    seq = []
    calls0 = hook.diag()["kbd_calls"]
    for _ in range(n):
        p = hook.probe_keyboard_hook()
        seq.append("T" if p is True else ("F" if p is False else "?"))
        if p is False:
            break
        time.sleep(gap)
    return "".join(seq), calls0, hook.diag()["kbd_calls"]


def main() -> int:
    from com_bridge.server import Server

    srv = Server(name="repro", port=24899, side="right",
                 capture_local=False, accept_synthetic_input=True)
    srv.start()
    time.sleep(2.0)
    hook = srv._hook
    if hook is None:
        print("no hook")
        srv.stop()
        return 1

    shell = user32.GetShellWindow()
    if shell:
        user32.SetForegroundWindow(shell)
    time.sleep(0.3)

    base = hook.probe_keyboard_hook()
    if base is not True:
        print(f"baseline={base} -> unusable")
        srv.stop()
        return 2

    print(f"rawin_running={srv._listener is not None} base_probe={base}")

    # ---- 情形 1: 只调 hider.hide(), relay 保持 False ----
    print()
    print("== case 1: hider.hide() only, relay stays False ==")
    srv._hider.hide()
    time.sleep(0.5)
    s, c0, c1 = probe_seq(hook)
    print(f"  seq={s} kbd_calls={c0}->{c1} relay={hook.relay} "
          f"fg={title(user32.GetForegroundWindow())}")

    # ---- 情形 2: 在情形1基础上再打开 relay(但不走 _acquire_control) ----
    print()
    print("== case 2: hider hidden + relay=True (still not _acquire_control) ==")
    hook.set_relay(True)
    time.sleep(0.3)
    s2, c2, c3 = probe_seq(hook)
    print(f"  seq={s2} kbd_calls={c2}->{c3} relay={hook.relay} "
          f"fg={title(user32.GetForegroundWindow())}")

    # 收尾
    hook.set_relay(False)
    srv._hider.show()
    time.sleep(0.4)
    print()
    print(f"after cleanup probe={hook.probe_keyboard_hook()}")
    srv.stop()
    if shell:
        user32.SetForegroundWindow(shell)

    print()
    print("READ:")
    print("  case1 REPRO -> hider.hide() 与 RawInput 同时存在即失效")
    print("  case2 REPRO -> 再加 relay 就失效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
