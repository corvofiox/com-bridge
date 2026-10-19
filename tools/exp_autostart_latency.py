# -*- coding: utf-8 -*-
"""★★★ 量化"自启探测"的耗时, 找出锁死 3 秒的元凶。

## 用户报告
"程序分别在启动、开启自启服务、关闭自启服务时有3秒左右的锁死。
 程序启动时的锁死也源于对自启服务的探测。这个过程应该是静默的,
 不应该全局锁死控制。而且时间也显著慢于 deskflow 的实现。"

## 可疑调用
1. `elevated.task_exists()`  -> subprocess.run(["schtasks", "/Query", ...])
2. `elevated.is_elevated()`  -> ctypes OpenProcessToken(应该很快)
3. `_powershell(...)`        -> subprocess.run(["powershell", ...]) 更慢

## 本工具: 逐个测耗时, 并区分"可并行化"与"必须同步"
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

from com_bridge import elevated

print("== 逐个测量自启相关调用的耗时 ==")

# 1. is_elevated (纯 ctypes)
t0 = time.monotonic()
r1 = elevated.is_elevated()
d1 = (time.monotonic() - t0) * 1000
print(f"  is_elevated()  = {r1}  耗时 {d1:7.1f} ms")

# 2. task_exists (schtasks 子进程)
t0 = time.monotonic()
r2 = elevated.task_exists()
d2 = (time.monotonic() - t0) * 1000
print(f"  task_exists()  = {r2}  耗时 {d2:7.1f} ms   <- 主要嫌疑")

# 3. 再来一次, 看是否有缓存效应
t0 = time.monotonic()
r3 = elevated.task_exists()
d3 = (time.monotonic() - t0) * 1000
print(f"  task_exists()  = {r3}  耗时 {d3:7.1f} ms   (第二次)")

# 4. PowerShell 启动开销
t0 = time.monotonic()
rc = elevated._powershell("exit 0")
d4 = (time.monotonic() - t0) * 1000
print(f"  _powershell()  = rc={rc}  耗时 {d4:7.1f} ms  <- 更慢")

print()
print("== 对照: 直接读注册表/计划任务目录(无子进程) ==")
import os
import winreg

# 计划任务在注册表里的位置: 任务名 -> 文件
t0 = time.monotonic()
found = False
try:
    with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
            r"\Schedule\TaskCache\Tree") as k:
        # 枚举子键找我们的任务名
        i = 0
        while True:
            try:
                name = winreg.EnumKey(k, i)
            except OSError:
                break
            if name == elevated.TASK_NAME:
                found = True
                break
            i += 1
except OSError as exc:
    print(f"  注册表读取失败: {exc}")
d5 = (time.monotonic() - t0) * 1000
print(f"  注册表直读      = 存在={found}  耗时 {d5:7.1f} ms")

print()
print("== 结论 ==")
print(f"  is_elevated  : {d1:7.1f} ms  (可同步)")
print(f"  task_exists  : {d2:7.1f} ms  (必须异步!)")
print(f"  powershell   : {d4:7.1f} ms  (必须异步!)")
print(f"  注册表直读   : {d5:7.1f} ms  (可作为快速路径)")
if d5 < 50 and d2 > 200:
    print("  ★★ 注册表直读比 schtasks 快 " f"{d2/max(d5,0.01):.0f} 倍")
