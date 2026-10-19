# -*- coding: utf-8 -*-
"""★★★ 验证单实例互斥: 第二个进程必须被拦住并唤起已有窗口。

## 用户报告
"当前程序没有做互斥性检查, 我能同时启动两个甚至多个"

## 本工具
在**同一进程内**模拟两次 acquire(用不同名字验证逻辑), 再用**真实子进程**
验证跨进程互斥:
  1. 进程内: acquire() 第一次 True, 第二次(同进程)仍 True(幂等)
  2. 跨进程: 父进程持有 -> 子进程 acquire() 必须返回 False
  3. 子进程退出后: 新子进程 acquire() 应恢复 True
"""
from __future__ import annotations

import subprocess
import sys
import time

sys.path.insert(0, ".")

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


print("== 1. 进程内 acquire 幂等 ==")
from com_bridge import single_instance as si

a1 = si.acquire()
a2 = si.acquire()
check("首次 acquire 成功", a1 is True)
check("同进程再次 acquire 仍成功(幂等)", a2 is True)

print()
print("== 2. 跨进程: 父进程持有时, 子进程必须失败 ==")
code = (
    "import sys; sys.path.insert(0,'.');"
    "from com_bridge import single_instance as s;"
    "print('ACQUIRED' if s.acquire() else 'BLOCKED')"
)
r = subprocess.run([sys.executable, "-c", code],
                   capture_output=True, text=True, timeout=30)
out = r.stdout.strip()
print(f"  子进程输出: {out!r}")
check("子进程被互斥量拦住", out == "BLOCKED",
      f"期望 BLOCKED, 实际 {out!r}")

print()
print("== 3. 释放后, 新进程应能拿到 ==")
si.release()
time.sleep(0.3)
r2 = subprocess.run([sys.executable, "-c", code],
                    capture_output=True, text=True, timeout=30)
out2 = r2.stdout.strip()
print(f"  子进程输出: {out2!r}")
check("释放后新进程可获取", out2 == "ACQUIRED",
      f"期望 ACQUIRED, 实际 {out2!r}")

print()
print("== 4. 进程被强杀后, 互斥量应自动释放(内核管理) ==")
# 起一个持有互斥量的子进程, 然后强杀它
holder = subprocess.Popen(
    [sys.executable, "-c",
     "import sys, time; sys.path.insert(0,'.');"
     "from com_bridge import single_instance as s;"
     "s.acquire(); print('READY', flush=True); time.sleep(60)"],
    stdout=subprocess.PIPE, text=True)
line = holder.stdout.readline()
print(f"  持有进程: {line.strip()}")
# 确认此时被拦
r3 = subprocess.run([sys.executable, "-c", code],
                    capture_output=True, text=True, timeout=30)
check("持有期间被拦", r3.stdout.strip() == "BLOCKED",
      r3.stdout.strip())
holder.kill()
holder.wait(timeout=10)
time.sleep(0.5)
r4 = subprocess.run([sys.executable, "-c", code],
                    capture_output=True, text=True, timeout=30)
check("强杀后互斥量自动释放", r4.stdout.strip() == "ACQUIRED",
      f"期望 ACQUIRED, 实际 {r4.stdout.strip()!r}")

print()
print(f"结果: {len(FAILED)} 项失败")
for f in FAILED:
    print(f"  - {f}")
sys.exit(1 if FAILED else 0)
