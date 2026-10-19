# -*- coding: utf-8 -*-
"""★ 真实自环端到端: 同一台机器上跑真 Server + 真 Agent, 全链路验证。

背景: 用户报告"完全无效, 之前什么样现在还什么样"—— 而他的 16:06
测试跑在日志损坏的 15:44 exe 上, 没留下任何证据。不再让用户当
测试员, 本机自己复现:

  Server(真钩子/真端口/真越界状态机/真转发)
    + Agent(真注入/真回传边) —— agent 的 server_host 指回本机。

两角色各自吃 24801/24802(自环场景端口共享冲突需要处理: server
绑 0.0.0.0:24801, agent 也想绑 24801 收输入 -> **冲突**)。
查证: agent._start_return 用 INPUT_PORT 绑定……先跑起来看,
端口冲突会立刻报错(设计上"端口被占用"就是检测同机双开的手段)。

流程:
  1. 起 Server -> 起 Agent(连本机) -> 等 agent 连上(控制通道握手)
  2. 注入鼠标位移把光标推到右边界 -> 触发越界 -> server 应转发
  3. 穿越态下注入无 TAG 键盘轰炸(模拟打字) -> 盯:
       server hook: kbd_calls / kbd_eaten / 泵心跳
       agent: 收到的事件数 / 注入数
  4. 退出穿越(注入向左位移 + 回程信号) -> 复核探针
  5. 结论: 每个数字都应该"合理地非零" —— 任何一个环节为 0,
     那就是用户看到"完全无效"的环节。
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

from com_bridge.agent import Agent
from com_bridge.server import Server
from com_bridge.input import get_cursor_pos
from com_bridge.input import _virtual_screen as virtual_screen
from inject_util import PROBE_VK, burst, mouse_move, require_injection

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


print("== 1. 起 Server(真钩子/真端口; accept_synthetic=True 让注入可当物理输入) ==")
srv = Server(name="loop-server", side="right",
             accept_synthetic_input=True)
try:
    srv.start()
    print("  server 就绪")
except Exception as exc:  # noqa: BLE001
    print(f"  server 启动失败: {exc}")
    sys.exit(2)
time.sleep(0.5)

print("== 2. 起 Agent(连本机 = 自环) ==")
# ★ 自环端口冲突: server 已绑 24801(输入接收), agent 的回传监听也要绑
#   24801 —— 设计上这是"同机双开检测"。自环验证关掉 agent 的回传
#   (enable_return=False): server->agent 的转发->注入链路依然完整,
#   只是 agent 不往回发。键盘轰炸正是要验证 server->agent 方向。
ag = Agent(server_host="127.0.0.1", name="loop-agent",
           mute_local=True, discover_timeout_s=3.0, side="right",
           enable_return=False)
try:
    ag.start()
    print("  agent 就绪")
except Exception as exc:  # noqa: BLE001
    print(f"  agent 启动失败: {exc}")
    srv.stop()
    sys.exit(2)
time.sleep(1.5)     # 等 CAPS 握手 + 端口学习

# agent 是否真的连上了 server? 逐层打印 server 内部状态
print(f"  server._input_tx = {getattr(srv, '_input_tx', None)}")
print(f"  server._edge = {getattr(srv, '_edge', None)}")
print(f"  server._punched_host = {getattr(srv, '_punched_host', None)}")
hook = getattr(srv, "_hook", None)
print(f"  server._hook = {'装好' if hook and hook.installed else '没有'}")

print()
print("== 3. 把光标推到右边界触发越界 ==")
vs = virtual_screen()
print(f"  虚拟桌面: {vs}")
# 主屏在最左时右边界在主屏右缘; 直接把光标放到右缘-2 再推 6px
cx, cy = get_cursor_pos()
target_x = vs.left + vs.width - 2     # 单屏场景 = 屏幕右缘
print(f"  当前光标 {cx},{cy}; 用绝对注入跳到右缘 {target_x}…")
# 逐段相对位移推过去(模拟物理移动)
from inject_util import mouse_move as mm
sx, sy = cx, cy
steps = 12
for i in range(steps):
    dx = (target_x - sx) // steps
    mm(dx, 0)
    time.sleep(0.03)
time.sleep(0.3)
outside = bool(srv._edge and srv._edge.is_outside)
d3 = srv._hook.diag() if srv._hook else {}
print(f"  server edge.is_outside = {outside}")
print(f"  server hook: relay={d3.get('relay')} mouse_eaten={d3.get('mouse_eaten')}")
check("越界态触发", outside)

print()
print("== 4. 穿越态键盘轰炸(60 键, 模拟打字) ==")
b = srv._hook.diag() if srv._hook else None
ag_stats0 = dict(ag.stats) if hasattr(ag, "stats") else {}
sent = burst(30, PROBE_VK)
require_injection(sent, 60)
time.sleep(1.0)
a = srv._hook.diag() if srv._hook else None
if a and b:
    d_calls = a["kbd_calls"] - b["kbd_calls"]
    d_eaten = a["kbd_eaten"] - b["kbd_eaten"]
    print(f"  server hook: kbd_calls+{d_calls} kbd_eaten+{d_eaten} "
          f"泵心跳={a.get('hook_pump_age_s', -1):.2f}s")
    check("server 键盘回调活着(穿越态)", d_calls > 0, f"+{d_calls}")
    check("server 吞键(转发给 agent)", d_eaten > 0, f"+{d_eaten}")
ag_fwd = ag.stats.get("events_injected", 0)
print(f"  agent 注入事件数: {ag_fwd} (穿越期间应 > 0)")
check("agent 收到并注入了键盘", ag_fwd > 0)

print()
print("== 5. 交还: 注入向左位移 -> 回程 ==")
for i in range(20):
    mm(-15, 0)
    time.sleep(0.03)
time.sleep(0.6)
d5 = srv._hook.diag() if srv._hook else {}
print(f"  edge.is_outside={bool(srv._edge and srv._edge.is_outside)} "
      f"relay={d5.get('relay')}")
check("穿越退出", not bool(srv._edge and srv._edge.is_outside))

print()
print("== 6. 交还复核探针 ==")
if srv._hook:
    p = srv._hook.probe_keyboard_hook()
    print(f"  probe = {p}")
    check("交还后键盘钩子可达", p is True)

ag.stop()
srv.stop()
print()
print(f"结果: {len(FAILED)} 项失败")
for f in FAILED:
    print(f"  - {f}")
sys.exit(1 if FAILED else 0)
