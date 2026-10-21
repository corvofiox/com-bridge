# -*- coding: utf-8 -*-
"""给 GUI 截图(三态各一张) —— 用 PrintWindow 直接抓窗口内容。

★ 为什么不用 ImageGrab 抓屏幕:
  实测抓到全黑 —— 窗口被其它程序遮挡/未真正前置时, 屏幕截图只拿到
  桌面背景或黑屏。`PrintWindow` 让窗口把自己画到指定 DC, 不受遮挡影响。

用法:
    python tools/shot_new_gui.py          # 三态 + 副机角色
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes

from PIL import Image

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int,
                                         ctypes.c_int]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.GetObjectW.argtypes = [wintypes.HGDIOBJ, ctypes.c_int, ctypes.c_void_p]
# ★ 必须声明 argtypes: HBITMAP 是句柄(64 位), 不声明会按 32 位 int 传,
#   报 "OverflowError: int too long to convert"(实测踩到)。
gdi32.GetBitmapBits.argtypes = [wintypes.HBITMAP, ctypes.c_long,
                                ctypes.c_void_p]
gdi32.GetBitmapBits.restype = ctypes.c_long


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long),
                ("bmHeight", ctypes.c_long), ("bmWidthBytes", ctypes.c_long),
                ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                ("bmBits", ctypes.c_void_p)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def capture_window(hwnd: int) -> Image.Image | None:
    """用 PrintWindow 抓窗口位图。"""
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return None
    hdc_screen = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(hdc_screen)
    bmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    old = gdi32.SelectObject(mem, bmp)
    try:
        # PW_RENDERFULLCONTENT = 0x2 (Win 8.1+, 对 DWM 合成窗口必要)
        ok = user32.PrintWindow(hwnd, mem, 0x2)
        if not ok:
            ok = user32.PrintWindow(hwnd, mem, 0)
        bm = BITMAP()
        gdi32.GetObjectW(bmp, ctypes.sizeof(BITMAP), ctypes.byref(bm))
        buf = (ctypes.c_ubyte * (bm.bmWidthBytes * bm.bmHeight))()
        gdi32.GetBitmapBits(bmp, len(buf), buf)
        im = Image.frombuffer("RGBA", (bm.bmWidth, bm.bmHeight), bytes(buf),
                              "raw", "BGRA", bm.bmWidthBytes, 1).convert("RGB")
        return im
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, hdc_screen)


def shot(app, tag: str, out_dir: str, setup=None) -> str | None:
    """先显示窗口(让尺寸确定), 再执行状态设置, 最后截图。

    ★ 顺序很重要: RoundCard 的圆角背景按 `winfo_width()` 渲染。若在
      窗口 withdraw 状态(宽度=1)时调用 set_fill, 会画成 1×1 的图 ——
      实测四张截图完全一样, 就是这么来的。
    """
    r = app.root
    r.deiconify()
    r.lift()
    r.attributes("-topmost", True)
    for _ in range(8):
        r.update()
        time.sleep(0.04)
    # 窗口已映射 -> 尺寸已知, 此时设置状态才能正确重绘背景
    if setup is not None:
        setup()
    for _ in range(20):
        r.update()
        time.sleep(0.04)
    time.sleep(0.6)
    hwnd = int(r.wm_frame(), 16)
    # ★ 等窗口稳定后再抓: 实测偶尔抓到 160x28 的小区域, 是重绘未完成。
    im = capture_window(hwnd)
    for _ in range(3):
        if im is not None and im.width > 400:
            break
        time.sleep(0.3)
        r.update()
        im = capture_window(hwnd)
    r.attributes("-topmost", False)
    if im is None:
        return None
    path = os.path.join(out_dir, f"gui_{tag}.png")
    im.save(path)
    return path


def main() -> int:
    from com_bridge import gui as gui_mod

    gui_mod.ComBridgeGui._restore_and_maybe_autostart = lambda self: None
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "_ui_shots")
    os.makedirs(out_dir, exist_ok=True)

    app = gui_mod.ComBridgeGui()
    try:
        app.root.geometry("+120+60")

        def _enabled(start: bool) -> None:
            """按真实运行状态设置按钮可用性(截图要反映禁用态的真实长相)。"""
            app._start_btn.set_enabled(start)
            app._stop_btn.set_enabled(not start)

        def _autostart(on: bool = True) -> None:
            """直接写自启状态 —— 后台线程经 root.after 投递, 在没有
            mainloop 的截图进程里不会触发(实测), 这里手动设。"""
            app._elev_var.set("已启用 —— 登录后静默启动到托盘" if on else "未启用")
            app._elev_sw.set_on(on)

        def idle():
            app._set_state("未启动")
            app._set_hero_state("idle")
            app._hero_meta.set("选择角色后点「启动服务」")
            _enabled(True)
            _autostart(True)

        def run():
            app._set_state("运行中 · 主电脑")
            app._set_hero_state("run")
            app._hero_meta.set("已连接 192.168.10.201 · 缓冲 61 ms")
            _enabled(False)

        def wait():
            app._set_state("等待主电脑")
            app._set_hero_state("wait")
            app._hero_meta.set("已等 12s · 探测 4 次 · 就绪后自动握手")
            app._stop_btn.set_enabled(True)

        def agent():
            app._select_role("agent", fire=False)
            app._set_state("未启动")
            app._set_hero_state("idle")
            app._hero_meta.set("选择角色后点「启动服务」")
            _enabled(True)

        print(shot(app, "idle", out_dir, idle))
        print(shot(app, "run", out_dir, run))
        print(shot(app, "wait", out_dir, wait))
        print(shot(app, "agent", out_dir, agent))
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
