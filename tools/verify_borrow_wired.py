"""端到端验证: 握手后 server 的输入发送器是否真的借用了接收 socket。

这是本轮修复的核心断言。若 `_start_input` 与 `_start_discovery` 的顺序
不对(或 use_socket 未生效), `_input_tx._borrowed` 会是 None, 事件就会从
临时端口发出并被端口限制型 NAT 丢弃 —— 症状是"心跳全到、事件全丢"。
"""

from __future__ import annotations

import contextlib
import io
import sys
import time

sys.path.insert(0, ".")

# 避开可能正在运行的 GUI(占 4010/24801/24802), 换整套端口
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
        _d = list(_fn.__defaults__ or ())
        _i = _fn.__code__.co_varnames.index("port") - 1
        if 0 <= _i < len(_d):
            _d[_i] = _ALT_AUDIO if "Audio" in _cls.__name__ else _ALT_INPUT
            _fn.__defaults__ = tuple(_d)

from com_bridge.agent import Agent        # noqa: E402
from com_bridge.server import Server      # noqa: E402


def main() -> int:
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        srv = Server(name="s", port=24941, side="right", jitter_ms=60)
        srv.start()
        time.sleep(0.5)
        ag = Agent(server_host="127.0.0.1", name="a", mute_local=False,
                   discover_timeout_s=1.0, control_port=24941, tone_hz=440.0,
                   enable_return=False)
        ag.start()
        time.sleep(2.0)

        tx = srv._input_tx
        borrowed = getattr(tx, "_borrowed", None) if tx else None
        rx_sock = srv._input_rx._sock if srv._input_rx else None
        print(f"__RESULT__ borrowed_is_rx={borrowed is rx_sock} "
              f"borrowed_none={borrowed is None} "
              f"peer={srv.peer_addr} "
              f"rx_port={srv._input_rx.bound_port if srv._input_rx else None}")
        ag.stop()
        srv.stop()

    for line in log.getvalue().splitlines():
        if "__RESULT__" in line:
            print(line.split("__RESULT__")[1].strip())
    print("\n=== 关键日志 ===")
    for line in log.getvalue().splitlines():
        if any(k in line for k in ("转发目标", "上报本机地址", "心跳", "越界")):
            print("  " + line.strip())

    tx = None
    ok = "borrowed_is_rx=True" in log.getvalue()
    print()
    print(f"[结论] {'PASS —— 发送器已借用接收 socket(NAT 会认回程)' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
