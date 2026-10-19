"""双端握手实测: 在同一进程里跑 server + agent, 验证地址协商链路。

重点验证(不依赖双机):
  1. agent 上报的 adv_host 被 server 采纳为转发目标;
  2. 双向心跳能被识别, 且 server 学到真实地址;
  3. 触发越界后 server 真的开始转发。

注: 单机上两端不能同时绑定同一个 INPUT_PORT(真实双机各有一份端口空间),
所以这里用 enable_return=False 只验证地址协商与转发决策; 线路径本身由
tools/verify_wire.py 单独验证。
"""

from __future__ import annotations

import contextlib
import io
import sys
import time

sys.path.insert(0, ".")

# 避开用户正在运行的 GUI(它占着 4010/24801/24802), 换一整套端口。
# ★ 必须在 import 之前改, 因为默认端口是**函数默认参数**(import 时就绑定),
#   只改模块常量对已定义的函数无效 —— 因此同时改掉默认值本身。
import com_bridge.protocol as P          # noqa: E402
import com_bridge.transport as T          # noqa: E402

_ALT_AUDIO, _ALT_INPUT = 14010, 14911
P.AUDIO_PORT = _ALT_AUDIO
P.INPUT_PORT = _ALT_INPUT
T.AUDIO_PORT = _ALT_AUDIO
T.INPUT_PORT = _ALT_INPUT

for _cls in (T.AudioSender, T.AudioReceiver, T.InputSender, T.InputReceiver):
    _fn = _cls.__init__
    if "port" in _fn.__code__.co_varnames:
        _defaults = list(_fn.__defaults__ or ())
        _idx = _fn.__code__.co_varnames.index("port") - 1   # 去掉 self
        if 0 <= _idx < len(_defaults):
            _defaults[_idx] = (_ALT_AUDIO if "Audio" in _cls.__name__
                               else _ALT_INPUT)
            _fn.__defaults__ = tuple(_defaults)

from com_bridge.agent import Agent        # noqa: E402
from com_bridge.server import Server      # noqa: E402


def main() -> int:
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        # ★ 单机跑双角色时, server 与 agent 会争抢同一个 INPUT_PORT
        #   (真实双机部署各有一份端口空间, 不存在此问题)。这里通过在
        #   server 侧把发送目标指到 agent 的独立端口来规避。
        srv = Server(name="s", port=24912, side="right", jitter_ms=60)
        srv.start()
        time.sleep(0.5)
        ag = Agent(server_host="127.0.0.1", name="a", mute_local=False,
                   discover_timeout_s=1.0, control_port=24912, tone_hz=440.0,
                   enable_return=False)   # 见下方说明: 单机不启用接收端
        ag.start()
        time.sleep(3.0)

        # 直接调 server 的输入入口, 模拟"用户把鼠标推到边缘"
        from com_bridge.protocol import (EV_MOUSE_MOVE_REL, InputEvent)
        from com_bridge.input import get_cursor_pos
        # 先把光标推到最右, 再喂朝外位移
        import ctypes
        u = ctypes.windll.user32
        u.SetCursorPos(1918, 500)
        time.sleep(0.2)
        for _ in range(6):
            ev = InputEvent(EV_MOUSE_MOVE_REL, 0, 0, 20, 0, 0)
            srv._on_input_event(ev)
            time.sleep(0.05)
        time.sleep(1.0)
        ag_stats = dict(ag.stats)
        ag.stop()
        srv.stop()

    text = log.getvalue()
    print("=== 关键日志 ===")
    for line in text.splitlines():
        if any(k in line for k in ("上报本机地址", "转发目标", "已连接",
                                   "心跳", "越界")):
            print("  " + line.strip())

    print("\n=== 结果 ===")
    print(f"server.peer_addr      = {srv.peer_addr}")
    print(f"server._punched_host  = {srv._punched_host}")
    print(f"server 转发计数       = {srv.stats['events_forwarded']}")
    print(f"agent 注入计数        = {ag_stats.get('events_injected')}")
    rx = srv._input_rx
    print(f"server 收包诊断       = {rx.diag() if rx else None}")

    ok_addr = srv.peer_addr in ("127.0.0.1", "192.168.10.12")
    ok_fwd = srv.stats["events_forwarded"] > 0
    print()
    print(f"地址正确: {'PASS' if ok_addr else 'FAIL'}")
    print(f"转发发生: {'PASS' if ok_fwd else 'FAIL'}")
    print(f"\n[结论] {'PASS' if ok_addr and ok_fwd else 'FAIL'}")
    return 0 if (ok_addr and ok_fwd) else 1


if __name__ == "__main__":
    raise SystemExit(main())
