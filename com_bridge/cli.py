"""命令行入口。

    com-bridge server [--side right|left|top|bottom] [--port 24802]
    com-bridge agent  [--host 192.168.10.20] [--keep-audio]
    com-bridge selfcheck
    com-bridge monitors
"""

from __future__ import annotations

import argparse
import sys
import time


def _cmd_monitors(_args) -> int:
    from .input import enumerate_monitors, get_cursor_pos, _virtual_screen

    v = _virtual_screen()
    print(f"虚拟桌面: {v}")
    for i, m in enumerate(enumerate_monitors()):
        print(f"  [{i}] {m}  ({m.width}x{m.height})")
    print(f"光标: {get_cursor_pos()}")
    return 0


def _cmd_selfcheck(_args) -> int:
    """逐项自检, 任一项失败返回非零。"""
    ok = True

    print("== 依赖 ==")
    for mod in ("comtypes", "pycaw", "numpy"):
        try:
            __import__(mod)
            print(f"  [ok] {mod}")
        except ImportError as exc:
            print(f"  [FAIL] {mod}: {exc}")
            ok = False

    print("== 协议编解码 ==")
    try:
        from .protocol import AudioHeader, InputEvent, EV_MOUSE_MOVE_REL

        h = AudioHeader(rate=48000, bits=32, channels=2, chan_mask=3, seq=7, ts_us=1234567890)
        h2 = AudioHeader.unpack(h.pack())
        assert h2 == h, f"{h2} != {h}"
        assert AudioHeader.unpack(
            AudioHeader(rate=44100, bits=16, channels=1, chan_mask=0,
                        seq=1, ts_us=5).pack()
        ).rate == 44100
        e = InputEvent(EV_MOUSE_MOVE_REL, 0, 0, -5, 12, 999)
        assert InputEvent.unpack(e.pack()) == e
        print("  [ok] AudioHeader/InputEvent 往返一致")
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] {exc}")
        ok = False

    print("== 时钟同步 ==")
    try:
        from .clock import ClockSync, JitterBufferController

        # t1=1000(本地发), t2=1500(对端收), t3=1600(对端发), t4=2100(本地收)
        # rtt = (2100-1000)-(1600-1500) = 1000
        # offset = ((1500-1000)+(1600-2100))/2 = 0 -> 两机时钟已对齐(对称延迟)
        cs = ClockSync()
        cs.update(1000, 1500, 1600, 2100)
        assert cs.offset_us == 0.0, cs.offset_us
        assert cs.rtt_us == 1000.0, cs.rtt_us
        assert cs.local_to_remote(2000) == 2000
        # 非对称场景: 对端时钟快 100us(单程 500, 对端处理 100)
        #   t2 = 1000+500+100 = 1600, t3 = 1700, t4 = 2100
        #   offset = remote-local = ((600)+(-400))/2 = 100
        cs2 = ClockSync()
        cs2.update(1000, 1600, 1700, 2100)
        assert cs2.offset_us == 100.0, cs2.offset_us
        assert cs2.rtt_us == 1000.0, cs2.rtt_us
        # offset = remote - local, 所以 local_to_remote 加 offset
        assert cs2.local_to_remote(2000) == 2100
        assert cs2.remote_to_local(2000) == 1900
        jb = JitterBufferController()
        t = jb.update(50.0, 10.0)
        assert 20.0 <= t <= 200.0, t
        print(f"  [ok] offset={cs2.offset_us} rtt={cs2.rtt_us} jitter_target={t:.1f}ms")
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] {exc}")
        ok = False

    print("== 重采样 ==")
    try:
        import numpy as np

        from .audio import Resampler, StreamClockTracker

        rs = Resampler(channels=2, block_align=8, bits=32)
        frames = np.linspace(-1, 1, 1000).astype(np.float32)
        pcm = np.repeat(frames[:, None], 2, axis=1).tobytes()
        n_in = len(frames)

        def total_out(ratio: float, calls: int = 5) -> int:
            """连续喂 calls 次, 返回累计产出帧数(消除首包保留的影响)。"""
            r = Resampler(channels=2, block_align=8, bits=32)
            n = 0
            for _ in range(calls):
                o = r.process(pcm, ratio)
                if o:
                    n += len(o) // 8
            return n

        # 首包即产出(不再有"铺垫包不输出"的旧行为)
        assert len(rs.process(pcm, 1.0)) > 0
        # ratio 越大 -> 读取越快 -> 同样输入产出的帧数越少
        n_slow = total_out(0.998)
        n_mid = total_out(1.0)
        n_fast = total_out(1.002)
        assert n_slow > n_mid > n_fast, (n_slow, n_mid, n_fast)
        # ratio=1 时累计产出应接近输入(允许首尾保留几帧)
        assert abs(n_mid - n_in * 5) <= 120, (n_mid, n_in * 5)

        tr = StreamClockTracker(nominal_rate=48000)
        # 每 1000 帧应在 1000/48000 s = 20833us 到达; 发成 20900us -> 偏慢
        # measured_rate = 1000 * 1e6 / 20900 = 47846.9Hz
        expected = 1000 * 1_000_000 / 20900 / 48000
        r = 1.0
        for i in range(200):
            r = tr.update(i * 20900, 1000)
        assert abs(r - 1.0) > 0.001, r          # 确实偏离了 1.0
        assert abs(r - expected) < 0.005, (r, expected)
        print(f"  [ok] 重采样 + 漂移跟踪 (ratio={r:.6f}, expect={expected:.6f})")
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] {exc}")
        ok = False

    print("== 混音 ==")
    try:
        import numpy as np

        from .audio import Mixer

        m = Mixer(channels=2, block_align=8, bits=32)
        a = np.zeros(4, dtype="<f4")
        b = np.zeros(4, dtype="<f4")
        a[0] = 0.5
        b[0] = 0.5
        out = m.mix([a.tobytes(), b.tobytes()])
        vals = np.frombuffer(out, dtype="<f4")
        assert abs(vals[0] - 1.0) < 1e-6, vals[0]
        assert vals[1] == 0.0
        # 削波
        a[0] = 0.9
        b[0] = 0.9
        out = m.mix([a.tobytes(), b.tobytes()])
        vals = np.frombuffer(out, dtype="<f4")
        assert vals[0] <= 1.0, vals[0]
        print(f"  [ok] 双流混合 + 削波 (peak={vals[0]})")
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] {exc}")
        ok = False

    print("== 越界判定 ==")
    try:
        from .input import EdgeDetector, ScreenRect

        d = EdgeDetector(ScreenRect(0, 0, 1920, 1080), ScreenRect(1920, 0, 3840, 1080),
                         side="right", confirm_frames=2)
        assert d.update(100, 100) is False
        assert d.update(1920, 100) is False   # 第一帧, 未确认
        assert d.update(1921, 100) is True    # 第二帧 -> 越界
        assert d.update(1915, 100) is True    # hysteresis 内仍越界
        assert d.update(1900, 100) is False   # 回到内侧
        print("  [ok] hysteresis + 确认帧")
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] {exc}")
        ok = False

    print("== WASAPI ==")
    try:
        from .audio import LoopbackCapture

        cap = LoopbackCapture()
        cap.start()
        print(
            f"  [ok] loopback: {cap.channels}ch@{cap.rate}Hz {cap.bits}bit "
            f"buf={cap.buffer_frames}帧"
        )
        cap.stop()
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] {exc}")
        ok = False

    print("== 显示器 ==")
    try:
        from .input import enumerate_monitors

        ms = enumerate_monitors()
        print(f"  [ok] {len(ms)} 台: {ms}")
        if not ms:
            ok = False
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] {exc}")
        ok = False

    print("== 权限 ==")
    try:
        from .elevated import is_elevated
        elevated = is_elevated()
        print(f"  [{'ok' if elevated else 'info'}] "
              f"进程{'已提权' if elevated else '未提权'}"
              + (" —— 与 SYSTEM 服务同层级, 钩子竞争不吃亏"
                 if elevated else
                 " —— 可正常运行; 若键盘钩子回调不可达, 先确认本程序"
                 "窗口不在前台(GUI 会自动交还焦点)"))
    except Exception as exc:  # noqa: BLE001
        print(f"  [info] 权限查询不可用: {exc}")

    print()
    print("SELFCHECK: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def _cmd_server(args) -> int:
    from .server import Server

    srv = Server(name=args.name, port=args.port, side=args.side,
                 jitter_ms=args.jitter_ms)
    srv.start()
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print()
    finally:
        srv.stop()
    return 0


def _cmd_agent(args) -> int:
    from .agent import Agent

    ag = Agent(server_host=args.host, name=args.name,
               mute_local=not args.keep_audio,
               discover_timeout_s=args.discover_timeout,
               side=args.side,
               # CLI 默认也是"一直等"(与 GUI 开机自启一致); 用
               # --wait-for-host 可以加上限, 便于脚本里快速失败。
               wait_for_host_s=getattr(args, "wait_for_host", 0.0))
    ag.start()
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print()
    finally:
        ag.stop()
    return 0


def _cmd_loopback(args) -> int:
    from .loopback_test import run

    return run(seconds=args.seconds, jitter_ms=args.jitter_ms,
               tone_hz=args.tone_hz)


def _cmd_kbdcheck(_args) -> int:
    """诊断: 键盘钩子回调此刻是否可达(键盘失效时用)。

    ★ 判据: 装一个只用于探测的键盘钩子, 连续多轮注入带 TAG 的探针键,
    看**自己的回调**能否被调用。

    ★★ 回调不可达的**真实原因**(已用可逆实验确认): **本进程窗口持有
    前台焦点**。实测(tools/exp_window_vs_hook_precise.py):
        窗口有焦点          -> kbd_calls 恒 0
        焦点转移给别的窗口  -> 立刻恢复
    因此若本命令报告不可达, 先看**当前前台窗口是不是 Com Bridge**
    (GUI 版会自动周期交还焦点, 见 gui._focus_watch_tick)。

    ⚠ 历史注记: 早期把原因归为"别的程序的钩子排在链前并吞掉按键"。
      跨进程实测(tools/audit_chain_claim.py)已否定该推测 —— 即使另一
      进程装了"吞一切"的键盘钩子, 我们的回调照样被调用。

    ⚠ 探测用未分配的 0xE8 虚拟键(无扫描码、无字符映射), 不产生任何输入。
    """
    import time

    from .hook import LowLevelHook
    from .input import InputInjector

    print("== 键盘钩子归属诊断 ==")
    h = LowLevelHook(name="KbdCheck", on_key_event=None)
    if not h.start():
        print(f"  [FAIL] 键盘钩子安装失败(last_error={h.last_error})")
        print("         这本身就是问题: 钩子装不上, 键盘无法被吞。")
        return 1
    h.set_relay(True)
    time.sleep(0.4)

    print(f"  自检 probe_keyboard_hook() = {h.probe_keyboard_hook()}")
    rounds, ok_rounds = 6, 0
    for i in range(1, rounds + 1):
        before = h.stats["kbd_calls"]
        # 注入一对带 TAG 的按键探针 —— 带 TAG 的注入不会吞掉/转发任何东西
        n = 0
        for _ in range(3):
            n += InputInjector.key(0xE8, 0, True)
            n += InputInjector.key(0xE8, 0, False)
            time.sleep(0.03)
        time.sleep(0.35)
        d = h.stats["kbd_calls"] - before
        if d > 0:
            ok_rounds += 1
        print(f"  第 {i} 轮: 注入 {n}/6, 我们的回调被调用 +{d}"
              + ("" if d > 0 else "   <<< 回调未被调用"))
    h.set_relay(False)
    h.stop()

    print()
    if ok_rounds == rounds:
        print(f"[结论] 键盘钩子回调可达({ok_rounds}/{rounds} 轮)")
        return 0
    print(f"[结论] !! 键盘钩子回调**不可达**({ok_rounds}/{rounds} 轮)")
    print()
    print("最常见原因: **本进程的窗口是前台窗口** —— Windows 不会把键盘事件")
    print("交给前台窗口所属进程的低级键盘钩子。请检查:")
    print("  1. 前台窗口是不是 Com Bridge?(GUI 版会自动交还焦点)")
    print("  2. 若是 GUI 版仍不可达, 查看日志里有没有")
    print("     '窗口曾抢到前台 -> 已交还焦点' 或 '键盘钩子恢复可达'")
    print("  3. 也可直接点一下别的窗口再重跑本命令, 对比结论是否变化。")
    return 1
    print()
    print("请**结束**下列程序后重试(Deskflow 的 daemon 会自动重启 core, 两个都要停):")
    print("  在**管理员** PowerShell 里:")
    print("    Stop-Process -Name deskflow-core,deskflow-daemon,synergy "
          "-Force -ErrorAction SilentlyContinue")
    print("  若提示拒绝访问, 说明它们以更高权限运行 —— 需在管理员窗口执行,")
    print("  或从服务/计划任务里禁用其自启动。")
    return 1


def _cmd_gui(_args) -> int:
    # ★★ --noconsole 打包下 stdout 是无效句柄, 所有 [server]/[agent]
    #   诊断打印全部丢失(用户机器上排障时我们一个字的证据都拿不到)。
    #   在 GUI 启动**最早期**重定向到文件日志。
    try:
        from . import gui_log
        path = gui_log.install()
        if path:
            print(f"[gui] 诊断日志 -> {path}", flush=True)
    except Exception as exc:  # noqa: BLE001
        # 日志失败绝不能阻止 GUI 启动
        try:
            sys.stderr.write(f"[gui] 文件日志不可用: {exc}\n")
        except Exception:  # noqa: BLE001
            pass
    from .gui import main as gui_main

    return gui_main()


def _no_args_menu() -> int:
    """双击启动(无参数)时给出快速上手信息, 而不是 argparse 的用法报错。

    冻结场景下"按回车关闭窗口"由 launch.py 统一兜底, 这里不暂停。

    ★ 发布版只带 GUI 一个 exe(见 build_exe.ps1), 命令行版 com-bridge.exe
      已不再构建。所以冻结时**不能**提示用户去运行 com-bridge.exe ——
      那个文件根本不存在, 会把人引到死路。源码运行时才给出子命令提示。
    """
    frozen = bool(getattr(sys, "frozen", False))
    print("Com Bridge —— 双机键鼠越界 + 音频混流")
    print()
    if frozen:
        print("请直接运行 com-bridge-gui.exe(图形界面)。")
        print("命令行子命令(server/agent/selfcheck/loopback)仅在源码运行时可用:")
        exe = "python -m com_bridge.cli"
    else:
        print("直接双击不会启动任何角色, 请选择:")
        exe = "python -m com_bridge.cli"
    print(f"  {exe} server --side right     主电脑(物理键鼠所在的那台)")
    print(f"  {exe} agent  --side right     副电脑(声音被采集并送出去)")
    print(f"  {exe} selfcheck               部署前自检")
    print(f"  {exe} monitors                查看显示器布局")
    print(f"  {exe} loopback --tone-hz 440  单机端到端自检")
    print()
    print("说明: --side 指另一台电脑在本机屏幕的方位, 两边必须一致。")
    print()
    return 2


def main(argv: list[str] | None = None) -> int:
    # ★★ 必须在**任何坐标 API 之前**声明 DPI 感知(用户实测 BUG3① 的真正根因)。
    #   未声明时进程是 DPI_UNAWARE, Windows 会把 GetSystemMetrics/GetCursorPos/
    #   SendInput 的坐标按显示缩放虚拟化; 高 DPI 的副机(2560x1600 常见
    #   150%/200%)上, 比例换算得到的"像素"与真实屏幕像素不是同一套坐标系,
    #   落点因此偏移 = "没有等比例对齐"。放在 main 的最前面, 保证 server/
    #   agent/gui/selfcheck 全部入口都生效。
    from .input import _enable_dpi_awareness
    _dpi_mode = _enable_dpi_awareness()

    p = argparse.ArgumentParser(prog="com-bridge", description="双机键鼠越界 + 音频混流")
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("server", help="主电脑")
    ps.add_argument("--name", default="server")
    ps.add_argument("--port", type=int, default=24802)
    ps.add_argument("--side", default="right",
                    choices=["left", "right", "top", "bottom"])
    ps.add_argument("--jitter-ms", type=float, default=60.0)
    ps.set_defaults(func=_cmd_server)

    pa = sub.add_parser("agent", help="副电脑")
    pa.add_argument("--host", default=None, help="主电脑 IP, 省略则自动发现")
    pa.add_argument("--name", default="agent")
    pa.add_argument("--side", default="right",
                    choices=["left", "right", "top", "bottom"],
                    help="与主电脑 --side 保持一致(它决定光标回传的方向)")
    pa.add_argument("--keep-audio", action="store_true",
                    help="不静音本机扬声器(会产生双重延迟)")
    pa.add_argument("--discover-timeout", type=float, default=3.0)
    pa.add_argument("--wait-for-host", type=float, default=0.0,
                    help="等主电脑就绪的最长秒数(默认 0 = 一直等; "
                         "主电脑还没启动时不会再直接报错退出)")
    pa.set_defaults(func=_cmd_agent)

    pc = sub.add_parser("selfcheck", help="自检")
    pc.set_defaults(func=_cmd_selfcheck)

    pm = sub.add_parser("monitors", help="列出显示器与光标位置")
    pm.set_defaults(func=_cmd_monitors)

    pl = sub.add_parser("loopback", help="单机自环端到端测试(server+agent 同进程)")
    pl.add_argument("--seconds", type=float, default=10.0)
    pl.add_argument("--jitter-ms", type=float, default=60.0)
    pl.add_argument("--tone-hz", type=float, default=440.0,
                    help="agent 发送的测试音频率; 0 = 用真实 loopback 采集")
    pl.set_defaults(func=_cmd_loopback)

    pg = sub.add_parser("gui", help="图形界面(角色选择/启动停止/实时状态)")
    pg.set_defaults(func=_cmd_gui)

    pk = sub.add_parser(
        "kbdcheck",
        help="诊断键盘钩子是否被别的程序抢走(键盘同时作用两台时用)")
    pk.set_defaults(func=_cmd_kbdcheck)

    # 无参数(典型场景: 双击 exe)时不走 argparse 的报错路径
    args_list = sys.argv[1:] if argv is None else list(argv)
    if not args_list:
        return _no_args_menu()

    args = p.parse_args(args_list)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
