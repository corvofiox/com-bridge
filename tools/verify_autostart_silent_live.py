# -*- coding: utf-8 -*-
"""★★★ 真机端到端(用真实 mainloop): 通过 GUI 开关开机自启。

上一版用 root.update() 驱动, 导致 after(0) 投递的回调不执行
(主线程看起来"卡在处理中"其实是测试驱动方式不对)。
本版用真实 mainloop + after 轮询, 与生产环境一致。
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


from com_bridge import autostart

print("== 准备: 确保关闭状态 ==")
autostart.disable()
print(f"  当前: {autostart.status_text()}")

print()
print("== 构建 GUI + 真实 mainloop ==")
from com_bridge.gui import ComBridgeGui

app = ComBridgeGui()
result = {}


def step1():
    """点击启用。"""
    print(f"  点击前: 状态={app._elev_var.get()!r} "
          f"按钮={app._elev_btn.cget('text')!r}")
    result["t0"] = time.monotonic()
    app._on_elev_toggle()
    app.root.after(50, poll_enable)


def poll_enable():
    """轮询直到启用完成或超时。"""
    done = (not getattr(app, "_elev_toggling", False)
            and autostart.is_enabled())
    if not done and time.monotonic() - result["t0"] < 6.0:
        app.root.after(50, poll_enable)
        return
    result["enable_ms"] = (time.monotonic() - result["t0"]) * 1000
    app.root.after(200, after_enable)


def after_enable():
    print(f"  耗时 {result['enable_ms']:.0f}ms")
    check("启用成功(注册表已写入)", autostart.is_enabled())
    check("★ 1 秒内完成(无 UAC 阻塞)", result["enable_ms"] < 1000,
          f"{result['enable_ms']:.0f}ms")
    print(f"  界面状态: {app._elev_var.get()!r}")
    print(f"  按钮文字: {app._elev_btn.cget('text')!r}")
    check("界面显示已启用", "已启用" in app._elev_var.get())
    check("按钮变为「关闭自启」", app._elev_btn.cget("text") == "关闭自启")
    print(f"  登记命令: {autostart.stored_command()}")
    app.root.after(100, step2)


def step2():
    """点击关闭。"""
    result["t1"] = time.monotonic()
    app._on_elev_toggle()
    app.root.after(50, poll_disable)


def poll_disable():
    done = (not getattr(app, "_elev_toggling", False)
            and not autostart.is_enabled())
    if not done and time.monotonic() - result["t1"] < 6.0:
        app.root.after(50, poll_disable)
        return
    result["disable_ms"] = (time.monotonic() - result["t1"]) * 1000
    app.root.after(200, after_disable)


def after_disable():
    print()
    print(f"== 关闭 ==  耗时 {result['disable_ms']:.0f}ms")
    check("关闭成功", not autostart.is_enabled())
    check("★ 关闭也在 1 秒内", result["disable_ms"] < 1000,
          f"{result['disable_ms']:.0f}ms")
    print(f"  界面状态: {app._elev_var.get()!r}")
    print(f"  按钮文字: {app._elev_btn.cget('text')!r}")
    check("界面显示未启用", "未启用" in app._elev_var.get())
    check("按钮回到「开机自启」",
          app._elev_btn.cget("text") == "开机自启")
    app.root.after(100, finish)


def finish():
    autostart.disable()
    app._quit_app()


# 用真实 mainloop 驱动(与生产一致)
app.root.after(300, step1)
app.root.mainloop()

print()
print(f"结果: {len(FAILED)} 项失败")
for f in FAILED:
    print(f"  - {f}")
sys.exit(1 if FAILED else 0)
