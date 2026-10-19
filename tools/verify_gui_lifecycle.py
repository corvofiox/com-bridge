"""GUI 启动路径运行时测试 —— 模拟用户在界面上点"启动"。

背景: GUI 重构时 `mon` 变量被删, _start_return 尾部日志行引用它 →
用户点启动立刻 NameError。ast/静态检查抓不到局部名错误, 只有真实调用
worker 才能暴露。

做法: 构造 ComBridgeGui, 填好配置, 直接调 _on_start()(不点按钮),
等 worker 完成, 断言角色真正跑起来了; 然后调 _on_stop() 验证停止路径。
主电脑/副电脑两条路径都测。
"""

from __future__ import annotations

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

from com_bridge.gui import ComBridgeGui   # noqa: E402


def wait_state(gui: ComBridgeGui, want: str, timeout: float = 15.0) -> bool:
    """驱动 Tk 事件循环直到状态栏出现 want(或超时)。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        gui.root.update()
        if want in gui._state_var.get():
            return True
        time.sleep(0.05)
    return False


def main() -> int:
    gui = ComBridgeGui()
    results: list[tuple[str, bool, str]] = []

    # --- 1) 主电脑路径(独占端口, 此时不启动 helper) ---
    gui._role_var.set("server")
    gui._on_role_change()
    gui._jitter_var.set("60")
    gui._side_var.set("right")
    gui.root.update()
    gui._on_start()
    ok1 = wait_state(gui, "运行中", 15)
    running = gui._role is not None
    detail1 = f"state={gui._state_var.get()!r} role={'有' if running else '无'}"
    if running:
        gui._on_stop()
        ok1 = ok1 and wait_state(gui, "已停止", 15)
    gui.root.update()
    time.sleep(1.0)               # 等 server 的端口完全释放
    results.append(("主电脑: 启动→运行→停止", ok1, detail1))

    # --- 2) 副电脑路径(本轮 NameError 的复现路径) ---
    # 需要**持续运行**的主电脑作对端(真实部署即如此)。
    # ★ 单机限制: helper(server)与 agent 都要绑 INPUT_PORT —— 真实双机
    #   各有一份端口空间不存在此问题。这里在 agent 创建接收端之前把它的
    #   绑定端口改开, 并把 helper 的发送目标指过去(与
    #   verify_agent_startup.py 同一手法)。
    from com_bridge.server import Server
    srv = Server(name="helper", side="right", jitter_ms=60)
    srv.start()
    time.sleep(0.5)
    _AG_PORT = 14912
    _orig_rx_init = T.InputReceiver.__init__

    def _rx_init(self, port=_AG_PORT, **kw):
        _orig_rx_init(self, _AG_PORT, **kw)

    T.InputReceiver.__init__ = _rx_init
    gui._role_var.set("agent")
    gui._on_role_change()          # 触发配置区重建
    gui._host_var.set("127.0.0.1")
    gui._jitter_var.set("60")
    gui.root.update()
    gui._on_start()
    # ★ patch 必须保持到 worker 完成之后: _on_start 只是**起线程**,
    #   Agent 的构造与 InputReceiver 的绑定都在 worker 线程里异步发生,
    #   若立刻恢复 patch, worker 会用回默认端口 14911(与 helper 冲突)。
    ok2 = wait_state(gui, "运行中", 15)
    T.InputReceiver.__init__ = _orig_rx_init
    if srv._input_tx is not None:
        srv._input_tx.set_addr("127.0.0.1", _AG_PORT)
    running2 = gui._role is not None
    detail2 = f"state={gui._state_var.get()!r}"
    if running2:
        gui._on_stop()
        ok2 = ok2 and wait_state(gui, "已停止", 15)
    results.append(("副电脑: 启动→运行→停止 (回归 mon NameError)", ok2, detail2))

    # --- 3) 副电脑缺 IP 应被拦截 ---
    gui._host_var.set("")
    gui._on_start()
    ok3 = "缺少主电脑 IP" in gui._state_var.get()
    results.append(("副电脑缺 IP 被拦截", ok3, gui._state_var.get()))

    gui.root.destroy()
    srv.stop()

    print("=== GUI 启动路径运行时测试 ===")
    allok = True
    for name, ok, detail in results:
        allok = allok and ok
        print(f"  {'OK  ' if ok else 'FAIL'} {name}  {detail}")
    print()
    print(f"[结论] {'PASS —— GUI 启动/停止路径完好' if allok else 'FAIL'}")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
