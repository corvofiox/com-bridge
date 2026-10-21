# -*- coding: utf-8 -*-
"""活体验证 A: 开机自启的**静默启动** —— 窗口真的不出现, 托盘真的在。

为什么必须活体验证: `--autostart` 的效果是"窗口不映射", 这只能真的建一次
窗口才看得出来。单测只能断言源码里有 `withdraw()`。

做法: 直接构造 `ComBridgeGui(start_hidden=True)`(不走 main(), 因此不会
撞上正在运行的实例的单实例互斥体), 检查:
  * `root.state()` == "withdrawn"
  * `winfo_ismapped()` == 0 (窗口没有被映射到屏幕)
  * 托盘对象存在且 `icon_ok`
随后销毁, 不写 config、不改注册表。

⚠ 期间会短暂出现第二个托盘图标(本进程自己), 结束即消失。
⚠ 已把 `_restore_and_maybe_autostart` 打成 no-op: 否则 300ms 后它会
  按"上次是运行中"自动启动角色, 与正在运行的正式实例抢端口。
"""
from __future__ import annotations

import sys
import time


def main() -> int:
    from com_bridge import gui as gui_mod

    # 1. 纯函数: 命令行 -> 是否静默
    cases = [
        (["--autostart"], True),
        (["--silent"], True),
        ([], False),
        (["--hookdiag"], False),
    ]
    ok = True
    for argv, want in cases:
        got = gui_mod._is_autostart_argv(argv)
        flag = "ok" if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"[flag] {argv} -> {got} (期望 {want}) {flag}")

    # 2. 真建一次窗口(静默模式)
    gui_mod.ComBridgeGui._restore_and_maybe_autostart = lambda self: None
    app = gui_mod.ComBridgeGui(start_hidden=True)
    try:
        for _ in range(5):
            app.root.update()
            time.sleep(0.03)
        state = app.root.state()
        mapped = app.root.winfo_ismapped()
        tray = getattr(app, "_tray", None)
        icon_ok = bool(getattr(tray, "icon_ok", False)) if tray else False
        print(f"[window] state={state!r} ismapped={mapped} (期望 withdrawn / 0)")
        print(f"[tray]   存在={tray is not None} icon_ok={icon_ok} "
              f"(期望 True / True)")
        ok = ok and state == "withdrawn" and int(mapped) == 0 and icon_ok

        # 3. 再从托盘"显示窗口"应能立刻调出来(证明静默启动不是"打不开")
        app._show_from_tray()
        for _ in range(3):
            app.root.update()
            time.sleep(0.03)
        shown = app.root.state()
        print(f"[window] 托盘显示后 state={shown!r} (期望 normal)")
        ok = ok and shown == "normal"
    finally:
        tray = getattr(app, "_tray", None)
        if tray is not None:
            try:
                tray.stop()
            except Exception:  # noqa: BLE001
                pass
        try:
            app.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    print("=> " + ("通过" if ok else "失败"))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
