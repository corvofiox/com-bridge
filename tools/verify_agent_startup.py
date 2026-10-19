"""运行时冒烟测试: 真实执行 Agent.start() 的完整路径。

背景: GUI 重构时 `mon = _main_monitor()` 被删, 但 _start_return 尾部的
日志行还在引用它 —— 静态检查(symtable/ast)抓不到这种局部名错误,
只有**真正调用**才会炸(用户在副机上点启动就遇到了)。

本测试在一个真实 server 后面启动完整 agent(不 mock), 走完
控制连接 -> 对时 -> 桌面交换 -> 输入接收 -> 回传监听 的全部路径。
"""

from __future__ import annotations

import contextlib
import io
import sys
import time

sys.path.insert(0, ".")

import com_bridge.protocol as P          # noqa: E402
import com_bridge.transport as T          # noqa: E402

_ALT_AUDIO, _ALT_INPUT = 14010, 14911
P.AUDIO_PORT, P.INPUT_PORT = _ALT_AUDIO, _ALT_INPUT
T.AUDIO_PORT, T.INPUT_PORT = _ALT_AUDIO, _ALT_INPUT
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
    ok = False
    detail = ""
    with contextlib.redirect_stdout(log):
        srv = Server(name="s", port=24995, side="right", jitter_ms=60)
        srv.start()
        time.sleep(0.4)
        # ★ 单机上 server 与 agent 不能同时绑同一 INPUT_PORT(真实双机各有
        #   一份端口空间)。在 agent 创建接收端之前临时改写其绑定端口,
        #   并把 server 的发送目标指过去 —— 目的是让 agent 的完整启动
        #   路径(含 _start_return)能真实执行。
        _AG_PORT = 14912
        _orig_init = T.InputReceiver.__init__

        def _init(self, port=_AG_PORT, **kw):
            _orig_init(self, _AG_PORT, **kw)

        T.InputReceiver.__init__ = _init
        ag = Agent(server_host="127.0.0.1", name="a", mute_local=False,
                   discover_timeout_s=1.0, control_port=24995, tone_hz=440.0,
                   side="right", enable_return=True)
        try:
            ag.start()
            T.InputReceiver.__init__ = _orig_init
            if srv._input_tx is not None:
                srv._input_tx.set_addr(srv._input_tx.addr[0], _AG_PORT)
            time.sleep(2.0)       # 让回传监听/心跳/对时全部就绪
            ok = (ag._return_edge is not None
                  and ag._return_listener == "enabled"
                  and ag._control.clock.synced)
            detail = (f"return_edge={ag._return_edge.side if ag._return_edge else None} "
                      f"listener={ag._return_listener} "
                      f"synced={ag._control.clock.synced}")
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
        finally:
            T.InputReceiver.__init__ = _orig_init
            ag.stop()
            srv.stop()

    print("=== agent 完整启动冒烟 ===")
    for line in log.getvalue().splitlines():
        if any(k in line for k in ("回传已启用", "上报本机地址", "桌面几何",
                                   "时钟同步", "失败", "Error", "Traceback")):
            print("  " + line.strip())
    print(f"\n  {detail}")
    print(f"\n[结论] {'PASS —— agent 完整启动无异常' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
