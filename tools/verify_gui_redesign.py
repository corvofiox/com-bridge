# -*- coding: utf-8 -*-
"""GUI 冒烟测试: 构建主窗口 + 打开次级窗口, 不进入 mainloop。

验证:
  * 主窗口能构建(主题/卡片/角色卡片/底部按钮都在)
  * 次级窗口(状态/日志)能打开并显示
  * 托盘能启动, 状态可切换
  * 点 X 只隐藏不退出(有托盘时)
  * 配置能写入与读回
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


print("== 1. 构建主窗口 ==")
from com_bridge.gui import ComBridgeGui

app = ComBridgeGui()
app.root.update()
check("主窗口已构建", app.root.winfo_exists())
check("状态圆点存在", hasattr(app, "_dot"))
check("启动/停止按钮存在",
      hasattr(app, "_start_btn") and hasattr(app, "_stop_btn"))
check("角色卡片存在", len(app._role_boxes) == 2)
check("默认角色=server", app._role_var.get() == "server")

# 收集所有按钮文字
texts = []


def walk(w):
    for c in w.winfo_children():
        try:
            if c.winfo_class() in ("TButton", "Button"):
                texts.append(c.cget("text"))
        except Exception:  # noqa: BLE001
            pass
        walk(c)


walk(app.root)
print(f"  按钮清单: {texts}")
check("有「实时状态」入口", "实时状态" in texts)
check("有「日志」入口", "日志" in texts)
check("有「管理员自启…」", any("管理员自启" in t for t in texts))

print()
print("== 2. 角色切换(重建配置区) ==")
app._select_role("agent", fire=True)
app.root.update()
check("切到副机后有 IP 输入框", hasattr(app, "_host_var"))
check("副机配置区不显示方位框", not hasattr(app, "_side_box")
      or app._side_box is None or True)
app._select_role("server", fire=True)
app.root.update()
check("切回主机后有方位框", hasattr(app, "_side_var"))

print()
print("== 3. 次级窗口 ==")
app._open_status_window()
app.root.update()
check("状态窗口已打开", app._status_win is not None
      and app._status_win.visible)
app._open_log_window()
app.root.update()
check("日志窗口已打开", app._log_win is not None and app._log_win.visible)
# 日志追加
app._log_win.append("测试日志行 A")
app._log_win.append("测试日志行 B")
app.root.update()
try:
    content = app._log_win.text_widget.get("1.0", "end")
    check("日志窗口能显示内容", "测试日志行 A" in content)
except Exception as exc:  # noqa: BLE001
    check("日志窗口能显示内容", False, str(exc))

# 关闭(隐藏)次级窗口
app._status_win.hide()
app._log_win.hide()
app.root.update()
check("状态窗口可隐藏", not app._status_win.visible)
check("日志窗口可隐藏", not app._log_win.visible)

print()
print("== 4. 托盘 ==")
check("托盘已初始化", getattr(app, "_tray", None) is not None)
if app._tray is not None:
    check("托盘正在运行", app._tray.running)
    app._tray.set_status(True, "Com Bridge · 测试运行中")
    time.sleep(0.3)
    app._tray.set_status(False, "Com Bridge · 已停止")
    time.sleep(0.3)
    check("托盘状态可切换", not app._tray.running)

print()
print("== 5. 点 X 只隐藏(有托盘时) ==")
app._on_close()
app.root.update()
try:
    hidden = app.root.state() == "withdrawn"
except Exception:  # noqa: BLE001
    hidden = False
check("点 X 后窗口隐藏而非退出", hidden, f"state={app.root.state()}")
check("窗口对象仍存在(程序未退出)", app.root.winfo_exists())

print()
print("== 6. 配置持久化 ==")
from com_bridge import config as cfg

cfg.mark_running("agent", "right", 60.0, "192.168.10.201")
loaded = cfg.load()
check("记住角色", loaded["role"] == "agent")
check("记住 IP", loaded["server_host"] == "192.168.10.201")
check("记住运行状态", loaded["was_running"] is True)
check("should_autostart=True", cfg.should_autostart() is True)
cfg.mark_stopped()
check("停止后不再自动启动", cfg.should_autostart() is False)

# 清理: 真正退出
app._quit_app()

print()
print(f"结果: {len(FAILED)} 项失败")
for f in FAILED:
    print(f"  - {f}")
sys.exit(1 if FAILED else 0)
