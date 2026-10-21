# -*- coding: utf-8 -*-
"""把**当前** GUI 截成图, 作为改版前的对照基线。

不经过 main()(因此不撞单实例互斥体), 也不自动启动角色
(`_restore_and_maybe_autostart` 被打成 no-op), 所以对正在运行的实例
没有任何影响 —— 只是会在屏幕上闪一个窗口(约 1 秒)。
"""
from __future__ import annotations

import os
import sys
import time

from PIL import ImageGrab


def main() -> int:
    from com_bridge import gui as gui_mod

    gui_mod.ComBridgeGui._restore_and_maybe_autostart = lambda self: None
    app = gui_mod.ComBridgeGui()
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "_ui_shots")
    os.makedirs(out_dir, exist_ok=True)
    try:
        r = app.root
        r.geometry("+120+90")
        for _ in range(10):
            r.update()
            time.sleep(0.05)
        r.deiconify()
        r.lift()
        for _ in range(10):
            r.update()
            time.sleep(0.05)
        time.sleep(0.4)
        x, y = r.winfo_rootx(), r.winfo_rooty()
        w, h = r.winfo_width(), r.winfo_height()
        print(f"窗口 rect = ({x},{y}) {w}x{h}  state={r.state()}")
        # 多抓 8px 边(含窗口边框/阴影)
        pad = 8
        img = ImageGrab.grab(bbox=(x - pad, y - pad, x + w + pad, y + h + pad),
                            all_screens=True)
        path = os.path.join(out_dir, "current.png")
        img.save(path)
        print(f"已保存 {path}  {img.size}")
        return 0
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


if __name__ == "__main__":
    sys.exit(main())
