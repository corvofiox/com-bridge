# -*- coding: utf-8 -*-
"""★★★ 审计: GUI 主线程里有哪些阻塞调用会造成"锁死"?

## 用户报告
"启动、开启自启服务、关闭自启服务时有3秒左右的锁死。
 程序启动时的锁死也源于对自启服务的探测。"

## 方法
静态扫描 GUI 代码, 找出在**主线程/定时回调**里调用的阻塞 API:
  * subprocess (进程创建, 打包 exe 里尤其慢)
  * 网络阻塞调用(connect / recv)
  * time.sleep
  * wait / join

并区分它们是被 after() 直接调用, 还是在后台线程里。
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BLOCKING = {
    "subprocess.run", "subprocess.Popen", "subprocess.call",
    "subprocess.check_output", "time.sleep",
    "Event.wait", "thread.join", "socket.connect",
}
# 这些方法由 after() 调度 -> 跑在主线程
MAIN_THREAD_CALLERS = {
    "_poll", "_refresh_elev_state", "_check_watchdog",
    "_focus_watch_tick", "_restore_and_maybe_autostart",
    "_autostart_now", "_set_dot", "_sync_tray", "collect_status",
    "_on_user_activity", "_on_elev_toggle",
}

targets = [ROOT / "com_bridge" / f
           for f in ("gui.py", "gui_windows.py", "elevated.py",
                     "tray.py", "config.py")]

print("== 扫描 GUI 主线程路径上的阻塞调用 ==\n")
for path in targets:
    if not path.exists():
        continue
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        fname = node.name
        # 收集该函数体内所有调用
        calls: list[tuple[str, int]] = []
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            fn = sub.func
            if isinstance(fn, ast.Attribute):
                base = fn.value
                name = ""
                if isinstance(base, ast.Attribute):
                    name = f"{base.attr}.{fn.attr}"
                elif isinstance(base, ast.Name):
                    name = f"{base.id}.{fn.attr}"
                else:
                    name = fn.attr
                if name in BLOCKING:
                    calls.append((name, sub.lineno))
            elif isinstance(fn, ast.Name) and fn.id:
                # 直接调用本模块函数(可能是 blocking 的包装器)
                if fn.id in ("task_exists", "_powershell",
                             "_do_register", "_do_unregister"):
                    calls.append((fn.id, sub.lineno))
        if not calls:
            continue
        marker = ""
        if fname in MAIN_THREAD_CALLERS:
            marker = "  <== 主线程(after 回调)!"
        print(f"{path.name}:{node.lineno} def {fname}{marker}")
        for name, ln in calls:
            print(f"    L{ln}: {name}")

print()
print("== 说明 ==")
print("  标记 '<== 主线程' 的函数由 root.after() 调度, 其中任何阻塞调用")
print("  都会冻住整个界面(3 秒卡顿就是它)。修法: 移到后台线程。")
