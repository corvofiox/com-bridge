# -*- coding: utf-8 -*-
"""真实 Server 环境下二分: 哪个后台子系统让键盘钩子在穿越期间失效。

## 已确立的事实
  * 干净环境下 hider.hide() 六种模式 6s 内 probe 恒 True   (不作案)
  * relay=True 本身            probe 恒 True               (不作案)
  * 真实 Server + _acquire_control -> probe **立刻 False** (复现)
  * 复现时 kbd_calls 零推进, pump_age 正常(<5s), 退回后立刻恢复

## 本脚本逐个屏蔽候选子系统, 只在**真实 Server** 里跑
  RAWIN   : 不启动 RawInputListener
  AUDIO   : 不启动音频(receiver/renderer)
  CLIP    : 不启动剪贴板(构造时 enable_clipboard=False)
  HIDER   : _acquire_control 不调 hider.hide()
  NONE    : 全开(对照组, 应复现)

用法: python tools/exp_bisect_server_subsystems.py RAWIN
"""
from __future__ import annotations

import ctypes
import sys
import time

sys.path.insert(0, ".")
user32 = ctypes.windll.user32

WHICH = (sys.argv[1] if len(sys.argv) > 1 else "NONE").upper()


def title(h) -> str:
    if not h:
        return "(none)"
    n = user32.GetWindowTextLengthW(h)
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(h, buf, n + 2)
    return buf.value or f"<{h}>"


def main() -> int:
    from com_bridge.server import Server

    kwargs = dict(name="repro", port=24899, side="right",
                  capture_local=False, accept_synthetic_input=True)
    if WHICH == "CLIP":
        kwargs["enable_clipboard"] = False

    srv = Server(**kwargs)

    if WHICH == "RAWIN":
        # 让 _start_input 里的 listener.start()/wait_ready() 变成空操作
        import com_bridge.input as cbin
        class _NoRaw:
            def __init__(self, *a, **k):
                pass
            def start(self):
                return True
            def wait_ready(self):
                return True
            def stop(self):
                pass
        cbin.RawInputListener = _NoRaw       # type: ignore
        import com_bridge.server as cbs
        cbs.RawInputListener = _NoRaw        # type: ignore

    if WHICH == "AUDIO":
        import com_bridge.server as cbs
        cbs.Server._start_audio = lambda self: None   # type: ignore

    srv.start()
    time.sleep(2.0)

    hook = srv._hook
    if hook is None:
        print(f"{WHICH}: no hook (installed=False)")
        srv.stop()
        return 1

    shell = user32.GetShellWindow()
    if shell:
        user32.SetForegroundWindow(shell)
    time.sleep(0.3)

    base = hook.probe_keyboard_hook()
    if base is not True:
        print(f"{WHICH}: baseline={base} -> environment not usable")
        srv.stop()
        return 2

    if WHICH == "HIDER":
        saved = srv._hider
        srv._hider = None
        try:
            srv._acquire_control()
        finally:
            srv._hider = saved
    else:
        srv._acquire_control()

    time.sleep(0.5)
    seq = []
    calls0 = hook.diag()["kbd_calls"]
    for _ in range(6):
        p = hook.probe_keyboard_hook()
        seq.append("T" if p is True else ("F" if p is False else "?"))
        if p is False:
            break
        time.sleep(0.4)
    d = hook.diag()
    calls1 = d["kbd_calls"]

    srv._release_control()
    time.sleep(0.4)
    after = hook.probe_keyboard_hook()
    srv.stop()
    if shell:
        user32.SetForegroundWindow(shell)

    s = "".join(seq)
    print(f"{WHICH}: seq={s} kbd_calls={calls0}->{calls1} "
          f"after={after} => {'REPRO' if 'F' in s else 'NO-REPRO'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
