"""截取两个角色视图的实际窗口截图, 供人工确认视觉设计。"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

from com_bridge.gui import ComBridgeGui


def shoot(gui: ComBridgeGui, path: str) -> None:
    gui.root.update_idletasks()
    gui.root.update()
    # ★ 必须先把窗口提到最上层并激活, 否则截到的是别的应用
    #   (实测第一次截图抓到的是浏览器页面)
    gui.root.lift()
    gui.root.attributes("-topmost", True)
    gui.root.focus_force()
    gui.root.update()
    time.sleep(0.4)
    x = gui.root.winfo_rootx()
    y = gui.root.winfo_rooty()
    w = gui.root.winfo_width()
    h = gui.root.winfo_height()
    # 用 PIL 抓屏(Pillow 随打包环境提供)
    from PIL import ImageGrab
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
    img.save(path)
    gui.root.attributes("-topmost", False)
    print(f"  已保存 {path} ({w}x{h})")


def main() -> int:
    gui = ComBridgeGui()
    gui.root.update()
    time.sleep(0.5)

    gui._role_var.set("server")
    gui._on_role_change()
    gui.root.update()
    shoot(gui, "ui_server.png")

    gui._role_var.set("agent")
    gui._on_role_change()
    gui.root.update()
    shoot(gui, "ui_agent.png")

    gui.root.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
