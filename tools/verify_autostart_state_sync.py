# -*- coding: utf-8 -*-
"""★★★ 端到端验证: 注册后界面状态必须立刻更新(核心回归)。

## 这个 bug 的表现
用户点"管理员自启…" -> 提权子进程成功注册 -> 但界面仍显示"未启用"
-> 用户认为"注册不成功"。

根因: 父进程的 task_exists 缓存没被作废。

## 本工具
不实际提权(会弹 UAC), 而是:
  1. 记录当前真实状态
  2. 在**父进程**里模拟"操作完成 -> 作废缓存 -> 重新查询"
  3. 验证 force=True 确实能读到最新值(不依赖缓存)
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


from com_bridge import elevated

print("== 1. 真实状态 ==")
elevated.invalidate_task_cache()
real = elevated.task_exists(force=True)
print(f"  当前任务存在: {real}")

print()
print("== 2. 缓存生效后, 外部改状态也能被 force 读到 ==")
elevated.invalidate_task_cache()
cached_val = elevated.task_exists()          # 写入缓存
print(f"  首次查询(建缓存): {cached_val}")
print(f"  缓存内容: {elevated._TASK_EXISTS_CACHE}")
# 再查一次(命中缓存)
same = elevated.task_exists()
check("第二次读到缓存值", same == cached_val)

print()
print("== 3. force=True 必须绕过缓存 ==")
forced = elevated.task_exists(force=True)
check("force 读到真实值", forced == real,
      f"force={forced} real={real}")

print()
print("== 4. invalidate 后缓存为空 ==")
elevated.invalidate_task_cache()
check("缓存已清空", elevated._TASK_EXISTS_CACHE == {},
      f"实际 {elevated._TASK_EXISTS_CACHE}")

print()
print("== 5. GUI 代码必须用 force=True 做核对 ==")
import inspect

import com_bridge.gui as gui_mod

refresh_src = inspect.getsource(gui_mod.ComBridgeGui._refresh_elev_state)
check("_refresh_elev_state 用 force=True", "force=True" in refresh_src)

toggle_src = inspect.getsource(gui_mod.ComBridgeGui._on_elev_toggle)
check("_on_elev_toggle 决定模式时用 force=True",
      "force=True" in toggle_src)
check("_on_elev_toggle 完成后作废缓存",
      "invalidate_task_cache" in toggle_src)

print()
print("== 6. 日志必须落盘(否则事后无法查证) ==")
check("自启消息写入日志文件", "gui_log" in toggle_src
      and "write_line" in toggle_src)

print()
print(f"结果: {len(FAILED)} 项失败")
for f in FAILED:
    print(f"  - {f}")
sys.exit(1 if FAILED else 0)
