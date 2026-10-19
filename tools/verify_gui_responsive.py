# -*- coding: utf-8 -*-
"""★★★ 实测: GUI 主线程是否还会被阻塞(用户报的 3 秒锁死)。

## 方法
用 `root.after(10, tick)` 起一个心跳, 每 10ms 记录一次实际间隔。
如果主线程被阻塞, 心跳间隔会出现大空洞 —— 空洞长度就是卡顿时间。

## 场景
1. 启动后的自启状态探测
2. 点击"管理员自启…"按钮(会走提权流程, 这里只验证查询阶段不阻塞)
"""
from __future__ import annotations

import sys
import time
import tkinter as tk

sys.path.insert(0, ".")

gaps: list[float] = []
_last = {"t": 0.0}


def start_heartbeat(root: tk.Tk) -> None:
    def tick():
        now = time.monotonic()
        if _last["t"]:
            gaps.append((now - _last["t"]) * 1000)
        _last["t"] = now
        root.after(10, tick)
    _last["t"] = time.monotonic()
    root.after(10, tick)


def report(label: str) -> None:
    if not gaps:
        print(f"  {label}: 无数据")
        return
    worst = max(gaps)
    over50 = sum(1 for g in gaps if g > 50)
    over200 = sum(1 for g in gaps if g > 200)
    print(f"  {label}: 样本={len(gaps)} 最大间隔={worst:.0f}ms "
          f">50ms={over50} >200ms={over200}")
    gaps.clear()


print("== 构建 GUI(真实启动流程, 含 after(200, _refresh_elev_state)) ==")
from com_bridge.gui import ComBridgeGui

app = ComBridgeGui()
start_heartbeat(app.root)

# 跑 3 秒, 覆盖启动时的自启探测
t0 = time.monotonic()
while time.monotonic() - t0 < 3.0:
    app.root.update()
    time.sleep(0.005)
report("启动阶段(含自启探测)")

print()
print("== 触发一次自启状态刷新(后台线程) ==")
app._refresh_elev_state()
t0 = time.monotonic()
while time.monotonic() - t0 < 2.0:
    app.root.update()
    time.sleep(0.005)
report("自启状态刷新")

print()
print("== 触发第二次(应命中缓存, 更快) ==")
app._refresh_elev_state()
t0 = time.monotonic()
while time.monotonic() - t0 < 1.5:
    app.root.update()
    time.sleep(0.005)
report("二次刷新(缓存)")

print()
print("== 连续点击按钮 3 次(防重复 + 不阻塞) ==")
for _ in range(3):
    app._on_elev_toggle()
t0 = time.monotonic()
while time.monotonic() - t0 < 2.0:
    app.root.update()
    time.sleep(0.005)
report("连点按钮")

app._quit_app()

print()
print("== 判读 ==")
print("  最大间隔应 <100ms(正常 tk 调度抖动), 不应出现 >500ms 的锁死")
