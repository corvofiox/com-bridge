"""副电脑(agent)角色。

职责:
  1. 发现主电脑(UDP 广播信标)。
  2. 时钟同步(TCP 控制通道, NTP 四时间戳)。
  3. loopback 采集本机音频 -> 打 server 时钟域时间戳 -> UDP 发送。
  4. 副电脑本地扬声器静音(用户已接受)。
  5. 接收主电脑回传的输入事件(当用户把光标拖回主电脑时), 注入本机。
"""

from __future__ import annotations

import socket
import sys
import threading
import time

from .audio import LoopbackCapture
from .clock import disable_high_res_timer, enable_high_res_timer, now_us
from .input import (InputInjector, enumerate_monitors, get_cursor_pos,
                    position_fraction, return_edge_side)
from .transport import (
    AudioSender,
    BeaconListener,
    ClipboardReceiver,
    ClipboardSender,
    ControlClient,
    InputReceiver,
    InputSender,
    _is_virtual_ip,
    _local_addresses,
    local_ips,
)
from .cursor import CursorHider
from .protocol import (
    CONTROL_PORT,
    EV_HEARTBEAT,
    EV_KEY,
    EV_MOUSE_BUTTON,
    EV_MOUSE_HWHEEL,
    EV_MOUSE_MODS,
    EV_MOUSE_MOVE_REL,
    EV_MOUSE_PLACE,
    EV_MOUSE_RETURN,
    EV_MOUSE_WHEEL,
    HEARTBEAT_SIZE,
    INPUT_PORT,
    InputEvent,
    MB_DOWN,
    KEY_DOWN,
    KEY_EXTENDED,
    KEY_UP,
    MODFLAG_ALT,
    MODFLAG_CTRL,
    MODFLAG_SHIFT,
    MODFLAG_WIN,
    VK_CONTROL,
    VK_LWIN,
    VK_MENU,
    VK_SHIFT,
)
# 回环地址: 单机自测的合法用法(见 Agent.start 里的地址校验)
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _main_monitor():
    """本机主显示器(桌面坐标)。越界回传只需要一条基准边。"""
    mons = enumerate_monitors()
    if not mons:
        raise RuntimeError("未枚举到显示器")
    return max(mons, key=lambda r: r.width * r.height)


def _pick_advertised_host(server_host: str) -> str:
    """选一个"主电脑能访问到"的本机地址, 用于上报给主电脑转发输入。

    为什么需要 (真实踩到的坑):
      副电脑经 NAT(主路由是跑在虚拟机里的 OpenWrt)连主电脑时, 主电脑
      看到的 TCP 源地址是**网关**, 而不是副电脑。若主电脑拿那个地址回发
      输入事件, 事件全部落在网关上 —— 表现为"音频正常(副电脑主动发,
      NAT 放行)但鼠标完全不动(主电脑主动发, NAT 不转发)"。

    ⚠ 不要直接用控制连接的 getsockname(): 本机若装了 Tailscale 之类的
      VPN, 而到主电脑的路由恰好走了 VPN 接口, 取到的是 100.64.0.0/10 的
      CGNAT 地址。**主电脑若不在同一 VPN 里就完全不可达**, 上报它等于
      把输入事件送进黑洞(实测: 上报 100.95.120.31 后主电脑仍发不通)。
      local_ips() 已过滤这类虚拟地址。

    选法:
      1. 与主电脑**同网段**的本机地址(最可能被直接路由到);
      2. 否则取默认路由地址(已排除虚拟网卡);
      3. 单机自测用回环。
    """
    if server_host in _local_addresses():
        return "127.0.0.1"
    candidates = local_ips()
    if not candidates:
        return socket.gethostname()
    # 同网段匹配(按 /24 粗略比对 —— 家用/办公 LAN 几乎都是 /24)
    try:
        server_prefix = ".".join(server_host.split(".")[:3])
    except Exception:  # noqa: BLE001
        server_prefix = ""
    if server_prefix:
        for ip in candidates:
            if ".".join(ip.split(".")[:3]) == server_prefix:
                return ip
    # 没有同网段地址(跨网段部署): 用默认路由地址
    return candidates[0]


class Agent:
    """副电脑(agent)角色: 采集本机音频发给主电脑, 注入主电脑转发的输入。"""

    # ★ 已移除 RETURN_INTENT_S("持续往回拉 0.7 秒就回传")。
    #   那是个 workaround: 起因是光标进入副机时位置不可控, 只好用"拉多久"
    #   来猜用户意图。它带来明显误判 —— 用户在副机上正常向左移动 0.7 秒
    #   就被拽回主机; 副机双屏(桌面宽 3840)时几乎必然触发。
    #
    #   现在照搬 Deskflow: **交接时绝对定位光标到进入边**(EV_MOUSE_PLACE),
    #   于是进入位置确定, 用户往主电脑方向移动一点点就自然越过回传边,
    #   回传判定只需看**位置**, 不需要时间条件。
    #   Deskflow 的 onMouseMoveSecondary 也是纯位置判定(比较 m_x 与计算机
    #   边界), 全程没有时间条件。

    # 打洞心跳间隔(秒)。太大会让 NAT 映射过期, 太小没必要。
    HEARTBEAT_INTERVAL_S = 1.0

    # 主电脑心跳超时(秒) -> 释放残留按键。主电脑每 1s 发一次心跳,
    # 容忍 4 次丢失(server.PEER_LOSS_S 同款阈值)。
    STUCK_KEY_AFTER_S = 5.0

    def __init__(self, server_host: str | None = None, name: str = "agent",
                 mute_local: bool = True, discover_timeout_s: float = 3.0,
                 prefer_rate: int = 48000, control_port: int | None = None,
                 tone_hz: float = 0.0, side: str = "right",
                 enable_return: bool = True,
                 enable_clipboard: bool = True):
        self.name = name
        self.mute_local = mute_local
        self.discover_timeout_s = discover_timeout_s
        self.prefer_rate = prefer_rate
        self.control_port = control_port
        # tone_hz > 0 时用合成正弦代替 WASAPI 采集(自环测试用, 单机无法靠
        # 扬声器验证时也能跑通"采集->传输->对齐->混音->渲染"整条链路)
        self.tone_hz = tone_hz
        # 本机相对主电脑的方位(主电脑是 "right" 则本机在它左边)。
        # 用于光标从副电脑拖回主电脑时的方向: 只有靠近主电脑那一侧的移动
        # 才会触发回传, 往远处屏幕移动不应该把光标"送回"主电脑。
        self.side = side
        # 输入回传要在本机绑定 INPUT_PORT 收主电脑回传的事件。单机自环测试
        # 时 server 与 agent 在同一进程会争抢该端口(独占绑定下必然失败),
        # 且单机上回传毫无意义, 因此可关闭。
        self.enable_return = enable_return
        # 剪贴板同步开关: 便于 A/B 验证与用户按需关闭
        # (用户报过"剪贴板引入后键盘失效", 有开关才能快速隔离)
        self.enable_clipboard = enable_clipboard
        self._tone_phase = 0.0

        self.server_host = server_host
        self._cap: LoopbackCapture | None = None
        self._audio_tx: AudioSender | None = None
        self._input_rx: InputReceiver | None = None
        self._control: ControlClient | None = None
        # 剪贴板跨屏同步(参考 Deskflow, 与 server 侧对称)
        self._clip_rx = None
        self._clip_tx: ClipboardSender | None = None
        self._clip_sync = None
        # 输入回传(光标从本机拖回主电脑): _start_return() 里初始化
        self._return_edge = None
        # 本轮穿越是否已发过回程方位(见 _on_input_event 的"只发一次"说明)
        self._return_place_sent = False
        # 已发出的打洞心跳数(GUI 用它确认打洞在持续进行)
        self._punch_heartbeats = 0
        self._adv_host: str | None = None
        # 主电脑下发的方位(权威值; None = 旧版主机未下发, 退回本机配置)
        # 语义: 副电脑位于主电脑的哪一侧。见 _start_return 的长注释。
        self._server_side: str | None = None
        # 主电脑桌面几何(x, y, w, h), 由控制通道 SHAPE 交换获得
        self._server_shape: tuple[int, int, int, int] | None = None
        self._input_tx = None
        self._return_listener_obj = None
        # 光标隐藏器(enable_return=True 时创建; 见 start 里 4a 的说明)
        self._hider: CursorHider | None = None
        # 光标回传开关: 开启后 _on_input_event 会判断"这次相对移动会不会把
        # 光标带回主电脑", 会的话就不注入, 留给 _on_return_event 处理。
        self._return_listener = "enabled"
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        # ★ 按键卡死防护(照搬 Deskflow updateKeysCB/leave() 的语义):
        #   UDP 会丢包, 若 EV_KEY 的 down 送达而 up 丢失, 副机上这个键
        #   就**永久按住**(实际症状: 文档里选中的文字被一直拖动、打字
        #   全是大写)。Deskflow 的对策是主端周期补发 key release + 回
        #   主机时释放残留键; 我们在**副机**侧跟踪仍按住的键, 在两个时机
        #   主动释放: (a) 控制通道重连成功(断线期间丢失的 up 无法追回);
        #   (b) 心跳超时(对端疑似失联)。
        #   键: (vk, extended); 鼠标: 1..5。value 记 down 时的时间戳(诊断用)。
        self._down_keys: dict[tuple[int, bool], float] = {}
        self._down_buttons: dict[int, float] = {}
        self._down_guard_lock = threading.Lock()
        self.stats = {"frames_sent": 0, "bytes_sent": 0, "events_injected": 0}

    # -- 发现 ----------------------------------------------------------

    def discover(self) -> str:
        if self.server_host:
            return self.server_host
        print(f"[agent] 正在搜索局域网主电脑 ({self.discover_timeout_s}s)...", flush=True)
        beacon = BeaconListener(timeout_s=self.discover_timeout_s).listen_once()
        if beacon is None:
            raise RuntimeError(
                "未发现主电脑。请确认主电脑已运行 `com-bridge server`, "
                "且两台机器在同一局域网/同一网段。"
            )
        print(f"[agent] 发现主电脑 {beacon.name} @ {beacon.host}", flush=True)
        return beacon.host

    # -- 主流程 --------------------------------------------------------

    def start(self) -> None:
        # 同 server: 20ms 发送间隔不能被 15.6ms 系统节拍量化
        enable_high_res_timer()
        host = self.discover()
        self.server_host = host

        # ★ 主电脑 IP 填成本机地址是最容易犯、也最难自查的配置错误:
        #   控制通道会"连上"(其实是连到自己), 时钟同步、音频也都正常,
        #   唯独主电脑把输入事件发回它自己 —— 用户看到鼠标怎么都过不去。
        #   实测踩到: agent 填了 192.168.10.12(本机), 而主电脑是
        #   192.168.10.1; 主电脑日志显示"输入转发目标 -> 192.168.10.1"
        #   (它自己), 副电脑"注入 0 事件"。
        #
        #   回环地址(127.0.0.1/localhost)是**合法的单机自测**用法, 放行;
        #   但填本机的**真实网卡地址**几乎必然是配错了(如果真要单机测,
        #   用 127.0.0.1 即可), 这里直接报错并给出本机地址清单。
        if host not in _LOOPBACK and host in _local_addresses():
            raise RuntimeError(
                f"主电脑 IP 填成了本机地址 {host}。\n"
                f"  本机地址: {', '.join(sorted(_local_addresses() - _LOOPBACK))}\n"
                f"  请填写**另一台**电脑(主电脑)的 IP —— 两台机器不能填同一个。\n"
                f"  (单机自测请用 127.0.0.1)"
            )

        # 1. 控制通道 + 对时
        ctrl = ControlClient(host, self.control_port or CONTROL_PORT)
        if not ctrl.connect():
            raise RuntimeError(f"无法连接主电脑控制通道 {host}")
        # ★ 在 CAPS 里报告**本机真实地址**: 主电脑要用它当输入转发目标。
        #   不能只靠 TCP 源地址 —— 经过 NAT(如主路由是虚拟机里的 OpenWrt)
        #   时源地址会变成网关, 主电脑把输入事件发到网关就全丢了。
        #
        #   ⚠ 也不是直接用 getsockname(): 到主电脑的路由若走了 Tailscale
        #     之类的 VPN 接口, 取到的是 100.64.0.0/10 的 CGNAT 地址, 主电脑
        #     若不在同一 VPN 里完全不可达(实测上报 100.95.120.31 后仍不通)。
        #     因此只在 getsockname 给出**非虚拟**地址时才采用它。
        adv_host = None
        if ctrl.local_addr and ctrl.local_addr[0]:
            cand = ctrl.local_addr[0]
            if not _is_virtual_ip(cand):
                adv_host = cand
        if adv_host is None:
            adv_host = _pick_advertised_host(host)
        self._adv_host = adv_host       # 重连时要再次上报同一个地址
        ctrl.send_caps({"role": "agent", "name": self.name,
                        "adv_host": adv_host})
        print(f"[agent] 上报本机地址 {adv_host} (供主电脑转发输入)",
              flush=True)
        # ★ 交换桌面几何(照搬 Deskflow 的 DInfo/CInfoAck):
        #   跨屏的进入/退出位置要映射到对端坐标系, 双方都必须知道对方的
        #   桌面矩形。主电脑用它做比例映射的基准, 副机用它校验主电脑的
        #   方位假设(如"副机在主机的右边"对应副机的哪条边)。
        from .input import virtual_desktop
        d = virtual_desktop()
        ctrl.send_shape(d.left, d.top, d.width, d.height)
        self._server_shape = ctrl.recv_shape()
        print(f"[agent] 桌面几何已交换: 本机 {d.width}x{d.height}, "
              f"主电脑 {self._server_shape}", flush=True)
        clock = ctrl.sync_clock()
        print(
            f"[agent] 时钟同步完成: offset={clock.offset_us / 1000:.2f}ms "
            f"rtt={clock.rtt_us / 1000:.2f}ms samples={clock.samples}",
            flush=True,
        )
        self._control = ctrl

        # 2. loopback 采集 —— 只构造对象, start() 留到采集线程内(COM 线程亲和)
        self._cap = LoopbackCapture()

        # 3. 本地静音(用户已接受)
        if self.mute_local:
            self._mute_local()

        # 4. 发送/接收线程
        self._audio_tx = AudioSender(host)
        # ★ 剪贴板跨屏同步(参考 Deskflow, 与 server 侧对称):
        #   副机复制 -> 发到主机的 CLIPBOARD_PORT;
        #   主机复制 -> 副机接收并写入本机剪贴板。
        if self.enable_clipboard:
            self._ensure_clipboard(host)
        if self.enable_return:
            # on_heartbeat: 主电脑也可能反向发心跳(它同样可能位于 NAT 后,
            # 例如虚拟机网络)。收到后把**回传**目标切到实际源地址, 保证
            # "从副电脑把光标送回主电脑"这条路也能走通。
            self._input_rx = InputReceiver(on_event=self._on_input_event,
                                           on_heartbeat=self._on_host_heartbeat,
                                           on_peer_side=self._on_server_side)
            # 先绑定再起线程: 端口被占用(例如主电脑与副电脑跑在同一台机器上)
            # 要立刻抛错, 而不是让线程静默死亡
            self._input_rx.bind_now()
            self._input_rx.start()

            # 4a. ★ 光标隐藏器(照搬 Deskflow 的 hider window):
            #   光标回到主机时隐藏本机光标(用户此时在看主机屏幕,
            #   副机光标停在角落若仍可见, 用户会觉得"有两只鼠标")。
            #   hide_on_motion=True: 副机自己的物理键鼠一动就恢复可见
            #   (与 Deskflow 的 secondaryDeskProc 行为一致)。
            self._hider = CursorHider(hide_on_motion=True, name="AgentHider")
            self._hider.start()

            # 4b. 输入回传: 光标从本机拖回主电脑时把事件发回去
            self._start_return(host)

            # 4c. ★ 打洞心跳必须**从输入接收 socket 发出**。
            #   NAT 只为"实际发包的那个源端口"建立映射; 若用另一个 socket
            #   (临时端口)发心跳, 主电脑学到的映射端口指向那个临时端口,
            #   而我们的输入接收端在 INPUT_PORT 上 —— 主电脑把事件发到临时
            #   端口, 我们永远收不到。用同一 socket 才能让映射端口与接收
            #   端口一致, 打洞才真正有效。
            #   旧的独立 HeartbeatSender 无法满足这一点, 故改为在此循环发。
            self._start_punch_heartbeat()

        cap_thread = threading.Thread(
            target=self._capture_loop, daemon=True, name="AgentCapture"
        )
        cap_thread.start()
        self._threads = [cap_thread]

        # 5. 周期性重新对时
        sync_thread = threading.Thread(
            target=self._resync_loop, daemon=True, name="AgentResync"
        )
        sync_thread.start()
        self._threads.append(sync_thread)

    def _capture_loop(self) -> None:
        assert self._cap is not None and self._audio_tx is not None
        cap = self._cap
        cap.start()          # 必须在本线程调用(COM 线程亲和)
        print(
            f"[agent] loopback 就绪: {cap.channels}ch @{cap.rate}Hz {cap.bits}bit "
            f"buffer={cap.buffer_frames}帧"
            f"({cap.buffer_frames * 1_000_000 // max(1, cap.rate)}ms)",
            flush=True,
        )
        clock = self._control.clock if self._control else None
        if self.tone_hz > 0:
            print(f"[agent] 测试音模式 {self.tone_hz}Hz (跳过采集)", flush=True)
            self._tone_loop(cap, clock)
            cap.stop()
            return
        # ★★★ 静音补包必须按**实时速率**补足, 否则队列会被抽干。
        #
        #   实测根因(2026-10-20, tools/verify_audio_capture_path.py):
        #     旧实现每次 read 超时只补**一个** 120 帧包(2.5ms),
        #     而 read 每 30ms 超时一次 -> 供给速率 ≈ 2.5ms/30ms = 83ms/s,
        #     而主机消费速率是 1000ms/s。缺口 12 倍!
        #     实测水位: 0→2→4→6... 每 200ms 只涨 2ms, 29% 的时间
        #     低于 20ms(目标 60ms), 欠载 1275 次/20s。
        #     队列被抽干 -> 渲染缓冲见底 -> 波形断裂 = **周期性噼啪声**。
        #
        #   ★ 与 _tone_loop 同一个道理: 必须用 **catch-up 语义** ——
        #     "自上次发包以来过了多少真实时间, 就补多少帧",
        #     一次补齐。供给速率因此严格等于时钟速率, 与调度抖动无关。
        #
        #   注意: 有声音时不能补静音(会盖掉真实音频), 所以只在
        #   连续读不到数据(silence_ticks >= 2)时进入补包分支;
        #   但一旦进入, 就必须一次补齐这段时间的全部缺口。
        SEND_FRAMES = 120
        pkt_dur_us = int(SEND_FRAMES * 1_000_000 / cap.rate)
        self._capture_t0 = time.monotonic()
        # 补包用的"上次补到的时间点"(音频时钟域)
        self._silence_last_ts = 0
        while not self._stop.is_set():
            r = cap.read(timeout_s=0.03)
            if r is None:
                # ★★★ 静音时 WASAPI **不返回任何包**(实测: read 全是 None)。
                #   若此时什么都不发, 主机的音频时间轴就出现**空洞** ——
                #   主机的 `pop_aligned` 按时间戳取数据, 它看到的下一包
                #   时间戳突然跳到很远的将来, 于是把这段当成"巨大滞后",
                #   触发重复/跳过样本 -> 可听杂音; 而且水位在静音期间
                #   掉到 0, 恢复有声时又要重新攒 -> 每次静音后都有一下响。
                #
                #   修法: 静音期间**主动补发全零包**, 保持时间轴连续。
                #   只在"确实静音"时补(连续多次 read 超时), 且带正确的
                #   时间戳, 让主机的队列看到连续的流。
                self._silence_ticks = getattr(self, "_silence_ticks", 0) + 1
                if self._silence_ticks >= 2:
                    # ★★★ catch-up: 算出"自上次补包以来过了多少真实时间",
                    #   一次补足那么多帧(按 120 帧一片切分)。
                    #   这样供给速率 == 消费速率, 队列不会掉。
                    now_mono = time.monotonic()
                    last_mono = getattr(self, "_silence_last_mono", None)
                    if last_mono is None:
                        last_mono = now_mono
                    elapsed = now_mono - last_mono
                    # 至少补 1 片, 但**不无限补**(极端卡顿时别一次性
                    # 灌几百 ms, 那会造成主机侧队列突增 -> 削波爆音)
                    need_frames = int(elapsed * cap.rate)
                    max_frames = cap.rate // 4      # 上限 250ms
                    if need_frames < SEND_FRAMES:
                        need_frames = SEND_FRAMES
                    if need_frames > max_frames:
                        need_frames = max_frames
                    self._silence_last_mono = now_mono

                    silent = b"\x00" * (need_frames * cap.block_align)
                    # ★ 时间戳必须**接在上一包之后**(严格递增),
                    #   不能用 now_us() —— 那可能与上一包乱序, 而主机侧
                    #   pop_aligned 依赖时间戳单调, 乱序会被当成"滞后"。
                    last = getattr(self, "_last_audio_ts", 0)
                    base = last if last else now_us()
                    sent_f = 0
                    while sent_f < need_frames:
                        n = min(SEND_FRAMES, need_frames - sent_f)
                        piece_ts = base + int(sent_f * 1_000_000 / cap.rate)
                        self._audio_tx.send(
                            b"\x00" * (n * cap.block_align),
                            rate=cap.rate,
                            bits=cap.bits,
                            channels=cap.channels,
                            chan_mask=0x3,
                            ts_us=piece_ts,
                        )
                        sent_f += n
                    self._last_audio_ts = base + int(
                        need_frames * 1_000_000 / cap.rate)
                    self.stats["silence_sent"] = \
                        self.stats.get("silence_sent", 0) + sent_f
                continue
            self._silence_ticks = 0
            pcm, flags, qpc = r
            # ★★★ 静音包**必须照发**, 不能丢(2026-10-19 修正)。
            #
            #   旧实现 `if flags & 0x2: continue` 把静音包直接扔掉。
            #   后果: 音频的**时间轴出现空洞** —— 主机的 `pop_aligned`
            #   按时间戳取数据, 它看到的"下一包"突然跳到很远之后,
            #   于是把多出来的时间全当成"该跳过的滞后", 触发重复/跳过
            #   样本 -> 可听杂音。而且静音期间水位会掉到 0, 恢复有声时
            #   又要重新攒 —— 表现为"每次静音后都有一下爆音"。
            #   静音包是**合法的音频内容**(全零), 必须按原样发送,
            #   时间轴才连续。
            #
            # ★★★ 时间戳必须用 **QPC 设备位置**(2026-10-19 修正)。
            #
            #   旧实现用 `now_us()` —— 那是"读到数据的时刻", 与音频真正
            #   被采集的时刻之间隔着不确定的延迟(WASAPI 缓冲、调度)。
            #   这个抖动会让主机的 `pop_aligned` 的 horizon 计算偏移,
            #   进而重复或跳过样本 -> 杂音。
            #   `qpc` 是 WASAPI 给的**设备时钟位置**(100ns 单位), 它
            #   严格按采样率线性推进, 是真正的时间轴基准。
            if qpc and qpc > 0:
                # qpc 单位 100ns -> us
                ts = int(qpc) // 10
                # 转到主机时钟域(两边设备时钟可能有偏移, 由 clock 校正)
                if clock is not None and clock.synced:
                    ts = clock.local_to_remote(ts)
            else:
                ts = now_us()
                if clock is not None and clock.synced:
                    ts = clock.local_to_remote(ts)
            # ★★★ 时间戳必须**严格单调递增**。
            #
            #   场景: 静音期我们按 catch-up 推进了时间轴(_last_audio_ts),
            #   恢复有声时 WASAPI 给的 qpc 可能落在我们已推进的位置**之前**
            #   (设备时钟与本地时钟有微小偏移, 加上补包量的取整误差)。
            #   一旦时间戳倒退, 主机侧 `pop_aligned` 会把它当成"乱序/滞后",
            #   触发重复或跳过样本 -> **爆音**。
            #   修法: 若 ts <= 上一个时间戳, 就从上一个继续推进。
            _prev_ts = getattr(self, "_last_audio_ts", 0)
            if _prev_ts and ts <= _prev_ts:
                ts = _prev_ts + 1
            # 静音->有声 切换时, 补包的"catch-up 基准"要重置,
            # 否则下一次进入静音分支会从过时的时间点起算, 补出过量音频。
            self._silence_last_mono = None
            # ★★★ 按 MTU 分包发送(2026-10-19 关键修正)。
            #
            #   根因: 一包 480 帧 x 2ch x 32bit = **3840 字节**,
            #   加 IP(20)+UDP(8) = **3868 字节**, 而以太网 MTU 只有 1500
            #   -> 每个音频包被切成 **3 个 IP 分片**。
            #   UDP 无重传, **任一分片丢失 -> 整个数据报被丢弃**
            #   -> 10ms 音频完全消失, 贴包率被放大 ~3 倍,
            #   听感正是"零碎杂音/爆音"。
            #
            #   修复: 每包最多 SEND_FRAMES(120 帧 = 960 字节,
            #   加头 988 < 1500, **不分片**)。每包带精确递增时间戳,
            #   主机侧时间轴完全不受影响。
            #
            #   为什么是 120 帧: 48kHz 下 = 2.5ms, 网络包数量增到 4 倍,
            #   但换来"零分片"; 局域网带宽完全够(每声道 ~1.5Mbps)。
            #   权衡: 包更小 -> 单包丢失影响更短(2.5ms vs 10ms) -> 更不易听出。
            SEND_FRAMES = 120
            per = SEND_FRAMES * cap.block_align
            total_frames = len(pcm) // cap.block_align
            sent = 0
            base_ts = ts
            while sent < total_frames:
                n = min(SEND_FRAMES, total_frames - sent)
                piece = pcm[sent * cap.block_align:
                            (sent + n) * cap.block_align]
                # 每片的时间戳按帧偏移推进(微秒)
                piece_ts = base_ts + int(sent * 1_000_000 / cap.rate)
                self._audio_tx.send(
                    piece,
                    rate=cap.rate,
                    bits=cap.bits,
                    channels=cap.channels,
                    chan_mask=0x3,
                    ts_us=piece_ts,
                )
                sent += n
            # ★ 记录本块**末包**时间戳, 供静音补包接续(严格递增)
            self._last_audio_ts = base_ts + int(
                total_frames * 1_000_000 / cap.rate)
            self.stats["frames_sent"] += total_frames
            self.stats["bytes_sent"] += len(pcm)
            self.stats["packets_split"] = \
                self.stats.get("packets_split", 0) + \
                ((total_frames + SEND_FRAMES - 1) // SEND_FRAMES)

            # ★★★ 周期性诊断(每 5 秒) —— 排障靠数据, 不靠猜。
            #   关键量:
            #     cap_buffer_ms  : WASAPI 采集缓冲深度(应 >= 20ms;
            #                      太浅 -> Python 调度抖动就会丢音频)
            #     empty_reads    : GetBuffer 拿不到数据的次数(高=缓冲被抽干)
            #     avg_chunk_ms   : 每次 read 拿到的平均时长
            #   典型故障: avg_chunk_ms 明显小于 cap_buffer_ms 且
            #   empty_reads 很高 -> 采集端在丢帧 -> 主机听到周期性杂音。
            _now = time.monotonic()
            _last_snap = getattr(self, "_last_capture_snapshot", 0.0)
            if _now - _last_snap >= 5.0:
                self._last_capture_snapshot = _now
                _elapsed = max(1e-6, _now - getattr(
                    self, "_capture_t0", _now))
                _sent = self.stats.get("frames_sent", 0)
                _cap_ms = cap.buffer_frames * 1000.0 / max(1, cap.rate)
                _avg_ms = (_sent / max(1, cap.rate) * 1000.0 / _elapsed)
                _clk = f" offset跳变={clock.offset_jumps} 最大={clock.max_offset_jump_us / 1000:.1f}ms" if (
                    clock is not None and getattr(clock, "offset_jumps", 0)) else ""
                print(
                    f"[agent] 采集: 缓冲={_cap_ms:.0f}ms "
                    f"空读={cap.empty_reads} "
                    f"读次数={cap.read_calls} "
                    f"实供={_avg_ms:.0f}ms/s "
                    f"需要={1000.0:.0f}ms/s "
                    f"静包={self.stats.get('silence_sent', 0)}"
                    f"{_clk}",
                    flush=True,
                )
        cap.stop()

    def _tone_loop(self, cap, clock) -> None:
        """合成正弦, 按 cap 的实际格式打包发送(自环测试用)。

        用时间驱动: 应发帧数由**已过去的真实时间**决定(catch-up 语义)。

        ★ 旧实现 `next_t += period; 若落后则 next_t = now 重置` 在系统
          卡顿一次后就**永久丢失追赶配额** —— 每次卡顿少发的帧永远缺失,
          供给速率低于设备消费速率, 主电脑端渲染缓冲被缓慢抽干, 每 2~3s
          一次 underrun = 用户听到的周期性杂音簇。
          正确做法: 目标帧数 = 已过时间 × 采样率, 落后多少补多少(一次
          补齐), 这样长期供给速率严格等于时钟速率, 与调度抖动无关。
        """
        import numpy as np

        assert self._audio_tx is not None
        ch = cap.channels
        rate = cap.rate
        frames_per_pkt = max(1, int(rate * 0.02))   # 目标 20ms 一包
        dtype = "<f4" if cap.bits == 32 else "<i2"
        start_t = time.monotonic()
        sent_frames = 0
        PKT_US = int(frames_per_pkt / rate * 1e6)
        # ★ 供给速率只比设备消费高 0.1%: 正好抵消 Event.wait 的调度超调
        #   (实测实际间隔约 20.10ms 而非 20.00ms, 即慢约 0.1%)。
        #   uplift 与音调直接相关: 接收端要用 ratio>1 吸收净流入, 而 ratio
        #   就是读取速率 —— 净流入越大, 稳态 ratio 越偏离 1, 音调被压低
        #   越多(uplift=0.15% 时实测 440Hz 变 438Hz, 可闻)。
        SUPPLY_UPLIFT = 0.0010
        eff_rate = rate * (1.0 + SUPPLY_UPLIFT)
        # ★ 起播预发: 一次性把抖动缓冲垫到**目标水位**再匀速发送。
        #   ⚠ 预发量必须 ≈ 接收端目标水位(60ms): 预发 90ms 而目标 60ms 时,
        #     闭环会长期压低 ratio 去消耗多出的 30ms —— 而 ratio 就是读取
        #     速率, 压低它等于把音调拉低(实测偏 0.45%)。与目标对齐后闭环
        #     只需 ±0.1% 微调, 音调自然准确。
        prebuffer_frames = int(rate * getattr(self, "prebuffer_s", 0.06))
        while not self._stop.is_set():
            # catch-up: 目标帧数 = 已过时间 × 有效供给速率 + 起播预发量
            now = time.monotonic()
            target = int((now - start_t) * eff_rate) + prebuffer_frames
            if target <= sent_frames:
                # 精确睡到下一包的到期时刻(按帧数折算), 而不是固定 2ms —
                # 固定粒度在 target<=sent 分支反复触发时会造成 ~10% 供给缺口
                ahead = (sent_frames - target) / rate
                self._stop.wait(max(0.001, ahead - 0.002))
                continue
            # 一次补齐落后的帧数(封顶 100ms 的量, 防止异常卡顿后雪崩)
            batch = min(target - sent_frames, rate // 10)
            idx = np.arange(sent_frames, sent_frames + batch,
                            dtype=np.float64)
            # ★ 波形必须按**标称采样率** rate 生成, 不能用 eff_rate:
            #   eff_rate 只描述"每秒发多少帧"的节奏(含 uplift), 若拿它当
            #   正弦分母, 生成出的音调就被系统性压低 uplift 的比例
            #   (实测 440Hz 变 439.3Hz, 与 server 端 ratio 叠加后录到 437Hz)。
            wave = np.sin(2.0 * np.pi * self.tone_hz * idx / rate) * 0.25
            sent_frames += batch
            if cap.bits == 32:
                block = wave.astype(np.float32)
            else:
                block = np.clip(wave * 32767.0, -32768, 32767).astype(np.int16)
            pcm = np.repeat(block, ch).astype(dtype).tobytes()

            # 长包拆成 ≤frames_per_pkt 的 UDP 包(避免超过 MTU)
            frame_bytes = (4 if cap.bits == 32 else 2) * ch
            pkt_frames = frames_per_pkt
            off = 0
            while off < batch:
                take = min(pkt_frames, batch - off)
                b0 = off * frame_bytes
                piece = pcm[b0: b0 + take * frame_bytes]
                ts = now_us()
                if clock is not None and clock.synced:
                    ts = clock.local_to_remote(ts)
                self._audio_tx.send(
                    piece, rate=rate, bits=cap.bits, channels=ch,
                    chan_mask=0x3, ts_us=ts,
                )
                self.stats["frames_sent"] += 1
                self.stats["bytes_sent"] += len(piece)
                off += take

            # 下一包的到期时刻: 按 sent_frames 折算, 精确等待
            due = start_t + sent_frames / eff_rate
            delay = due - time.monotonic()
            if delay > 0:
                self._stop.wait(min(delay, PKT_US / 1e6))

    def _resync_loop(self) -> None:
        """周期性对时; 断线时**自动重连**而不是退出线程。

        ★ 为什么必须重连(照搬 Deskflow 的行为):
          Deskflow 的 ClientListener 常驻, 客户端断线后重连即重新握手 ——
          主电脑重启/网络闪断后几十秒内自动恢复。
          此前这里 `except ConnectionError: break`: 主电脑一重启, 本线程
          退出, 副机变成"看起来在运行但永远不会再同步"的僵尸 —— 时钟漂移
          无限累积, 音频逐渐错位, 用户只能手动重启两台机器的程序。

        重连内容: 控制通道(TCP)+ 对时 + 桌面几何重交换。
        打洞心跳与音频 UDP 无需重建(它们各自持续发送, NAT 映射靠心跳维持)。
        """
        assert self._control is not None
        backoff = 2.0
        while not self._stop.is_set():
            if self._stop.wait(5.0):
                break
            try:
                self._control.sync_clock(rounds=4)
                backoff = 2.0
                continue
            except (ConnectionError, OSError):
                # 控制通道断了: 尝试重建
                pass
            host = self.server_host
            if not host:
                break
            print(f"[agent] 控制通道断开 -> {backoff:.0f}s 后重连 {host}",
                  flush=True)
            if self._stop.wait(backoff):
                break
            # ★ 指数退避, 上限 **5s**(用户要求: "副机在重连主机连接时的
            #   请求间隔要压在5秒之内, 30秒太长了")。
            #   上限 30s 时, 主机短暂重启/网络抖动后要等半分钟才能恢复,
            #   用户会觉得"程序卡死了"。5s 足够避免对端被压垮, 又能让
            #   恢复几乎无感。2 -> 4 -> 5 -> 5 ...
            backoff = min(backoff * 2, 5.0)
            if self._stop.is_set():
                break
            try:
                self._control.close()
                ctrl = ControlClient(host, self.control_port or CONTROL_PORT)
                if not ctrl.connect():
                    print("[agent] 重连失败(主电脑未就绪?), 稍后重试",
                          flush=True)
                    continue
                ctrl.send_caps({"role": "agent", "name": self.name,
                                "adv_host": self._adv_host or host})
                ctrl.sync_clock(rounds=4)
                from .input import virtual_desktop
                d = virtual_desktop()
                ctrl.send_shape(d.left, d.top, d.width, d.height)
                self._server_shape = ctrl.recv_shape()
                self._control = ctrl
                # ★ 按键卡死防护: 重连成功即释放残留按键。
                #   断线期间丢失的 key-up 无法追回, 干净起步(照搬 Deskflow
                #   的 cleanupComputer -> disable 语义)。
                self._release_stuck_inputs("控制通道重连")
                print(f"[agent] 控制通道已重连 {host}, 对时完成", flush=True)
                backoff = 2.0
            except (ConnectionError, OSError) as exc:
                print(f"[agent] 重连异常: {exc}", flush=True)
                try:
                    self._control.close()
                except Exception:  # noqa: BLE001
                    pass

    def _on_input_event(self, ev) -> None:
        """把主电脑转发来的输入事件注入本机。

        注意鼠标移动是**相对**移动(SendInput MOUSEEVENTF_MOVE 无 ABSOLUTE),
        所以注入前必须判断"这个相对移动是否会把光标带回主电脑"。主电脑在自己
        那一侧已经做了 clamp_to_edge() 把光标钉在边界上, 若本机也照搬 clamp,
        光标永远回不去 —— 回传的条件应该是: 光标当前贴着本机朝向主电脑的
        那一条边, 且移动方向是朝主电脑的。get_cursor_pos 是全局虚拟桌面坐标,
        越界判定只关心靠近主电脑的那一侧。
        """
        inj = InputInjector
        try:
            # ★ 交接定位: 主电脑在越界那一刻告知"把光标放到这里"。
            #   必须**绝对定位**, 否则副机光标从上次位置开始累积,
            #   进入位置完全不可控(实测双屏时停在右屏右边缘蹭)。
            #   照搬 Deskflow 的 enter(x, y)。
            if ev.type == EV_MOUSE_PLACE:
                from .input import fraction_to_pixel, return_edge_side, \
                    virtual_desktop
                eff = self._server_side or self.side
                entry_side = return_edge_side(eff)   # 副机的进入边 = 回传边
                f = ev.a / 65535.0
                px, py = fraction_to_pixel(f, entry_side)
                InputInjector.move_absolute(px, py)
                if self._return_edge is not None:
                    self._return_edge.reset()   # 交接后从干净状态开始判定
                # 新一轮穿越: 允许再发一次回程方位(见 _on_input_event 的闸门)
                self._return_place_sent = False
                d = virtual_desktop()
                # ★ 主机光标进入: 恢复本机光标可见(它就是当前活动光标)。
                #   若之前回传时隐藏了, 这里必须恢复 —— 否则用户在副机上
                #   看不到光标(实测踩到: 光标隐藏后进入副机看不见位置)。
                if self._hider is not None:
                    self._hider.show()
                print(f"[agent] 交接定位 -> ({px},{py}) "
                      f"[桌面 {d.width}x{d.height}, 进入边={entry_side}, "
                      f"f={f:.3f}]", flush=True)
                self.stats["events_injected"] += 1
                return
            if ev.type == EV_MOUSE_MODS:
                # 修饰键快照(照搬 Deskflow enter 的 toggleMask): 主电脑
                # 进入穿越态时告知它当前按着哪些修饰键, 本机补注入,
                # 使"按住 Shift 跨屏"时副机的选择/拖拽与主机一致。
                import ctypes
                user32 = ctypes.windll.user32
                mask = ev.code
                injected = []
                for flag, vk in ((MODFLAG_SHIFT, VK_SHIFT),
                                 (MODFLAG_CTRL, VK_CONTROL),
                                 (MODFLAG_ALT, VK_MENU),
                                 (MODFLAG_WIN, VK_LWIN)):
                    if mask & flag and not (user32.GetKeyState(vk) & 0x8000):
                        InputInjector.key(vk, 0, True)
                        injected.append(vk)
                        with self._down_guard_lock:
                            self._down_keys[(vk, False)] = time.monotonic()
                if injected:
                    print(f"[agent] 同步主电脑修饰键快照: vk={injected}",
                          flush=True)
                return
            if ev.type == EV_MOUSE_MOVE_REL and self._return_listener is not None:
                # ★ 回传判定**只看位置**, 不看时间(照搬 Deskflow)。
                #
                #   Deskflow 的 onMouseMoveSecondary 把位移累加到 m_x/m_y,
                #   然后检查是否越出**本计算机的边界**:
                #       m_x < ax             -> 从左边离开 -> 回主电脑
                #       m_x > ax + aw - 1    -> 从右边离开
                #       否则                  -> 仍在机内, 把光标 clamp 到边界
                #   全程没有任何"持续拉动 N 秒"的条件。
                #
                #   为什么它不需要时间条件: 因为**进入时已把光标绝对定位在
                #   边缘**(enter(x,y)), 用户往主电脑方向移动一点点就会越出
                #   边界, 自然回传。
                #
                #   我此前加的 RETURN_INTENT_S(0.7s) 是个 workaround ——
                #   为绕过"位置不可控"而用时间猜意图。它带来误判: 用户在
                #   副机上正常向左移动 0.7 秒就被拽回主机, 双屏时尤其明显
                #   (桌面宽 3840, 向左移动几乎必然超过 0.7 秒)。
                #   现在绝对定位已实现, 时间条件可以而且应该去掉。
                toward_host = self._is_toward_host(ev.a, ev.b)
                if toward_host:
                    x, y = get_cursor_pos()
                    if self._at_return_edge(x, y, ev.a, ev.b):
                        if self._input_tx is not None:
                            # ★★ BUG3② 修复: 先把**回程方位**发给主电脑,
                            #   再发这条位移。主电脑的光标在整个穿越期间
                            #   被钉在**进入时的位置**, 只靠相对位移累积
                            #   的话落点永远是当初跳过来的那一点 —— 用户
                            #   在副机上移动到哪个高度回来完全丢失。
                            #   这里把光标沿回传边的切向比例报过去, 主机
                            #   用它做绝对定位(与去程的 EV_MOUSE_PLACE 对称)。
                            #
                            #   ★ 只发**一次**: `_at_return_edge` 在越过之后
                            #   会持续返回 True(边缘态一直成立), 若不设闸就
                            #   会每个事件都发一条, 主机会被反复绝对定位,
                            #   反而把光标钉死在同一个切向坐标上。
                            if not self._return_place_sent:
                                self._send_return_place(x, y)
                                self._return_place_sent = True
                            self._input_tx.send(ev)
                            self.stats["events_returned"] = \
                                self.stats.get("events_returned", 0) + 1
                            print("[agent] 光标离开回传边 -> 交回主电脑控制权",
                                  flush=True)
                            # ★ 控制权回到主机: 隐藏本机光标
                            #   (主机光标恢复可见是主机侧 crossing 时做的)。
                            if self._hider is not None:
                                self._hider.hide()
                        return
            if ev.type == EV_MOUSE_MOVE_REL:
                inj.move_relative(ev.a, ev.b)
            elif ev.type == EV_MOUSE_BUTTON:
                down = bool(ev.flags & MB_DOWN)
                inj.button(ev.code, down)
                # 按键卡死防护: 记录按住的鼠标键 / 释放时移除
                if ev.code in (1, 2, 3, 4, 5):
                    with self._down_guard_lock:
                        if down:
                            self._down_buttons[ev.code] = time.monotonic()
                        else:
                            self._down_buttons.pop(ev.code, None)
            elif ev.type == EV_MOUSE_WHEEL:
                inj.wheel(ev.b)
            elif ev.type == EV_MOUSE_HWHEEL:
                # 倾斜滚轮: delta 语义与垂直相同, 正=向右
                inj.wheel(ev.b, horizontal=True)
            elif ev.type == EV_KEY:
                down = bool(ev.flags & KEY_DOWN)
                inj.key(ev.code, ev.a, down,
                        bool(ev.flags & KEY_EXTENDED))
                # 按键卡死防护: 记录按住的键 / 释放时移除
                with self._down_guard_lock:
                    k = (ev.code, bool(ev.flags & KEY_EXTENDED))
                    if down:
                        self._down_keys[k] = time.monotonic()
                    else:
                        self._down_keys.pop(k, None)
            self.stats["events_injected"] += 1
        except OSError:
            pass
        except Exception as exc:  # noqa: BLE001
            # ★★★ 必须捕获**所有**异常, 不能只捕 OSError(用户实测的回归):
            #
            #   旧代码只有 `except OSError: pass`。于是任何其它异常
            #   (ValueError / AttributeError / TypeError ...) 都会一路上抛到
            #   `InputReceiver` 的 `self._on_event(ev)`, 而那里是
            #   `except Exception: pass` —— **静默吞掉, 日志里一个字都没有**。
            #   表现就是"键盘完全无输入, 且没有任何错误线索", 极难排查。
            #
            #   现在: 捕获所有异常 + 计数 + 首次打印详情(类型与消息),
            #   这样用户报问题时有据可查, 而且单个事件失败不会让整条
            #   通路静默停摆。
            n = self.stats.get("event_inject_errors", 0) + 1
            self.stats["event_inject_errors"] = n
            if n <= 5:
                print(f"[agent] !! 注入事件失败(第 {n} 次) "
                      f"type=0x{ev.type:02X} code=0x{ev.code:02X} "
                      f"a=0x{ev.a:02X}: {type(exc).__name__}: {exc}",
                      flush=True)

    def _release_stuck_inputs(self, reason: str) -> None:
        """把所有仍按住的键/鼠标键注入 up(按键卡死防护的补发动作)。

        照搬 Deskflow: updateKeysCB 周期对比按键状态补发 release,
        leave() 时对 m_primaryKeyDownList 逐键 fakeLocalKey 释放。
        注入 key-up/button-up 是幂等的: 本来就没按住也无害。
        """
        with self._down_guard_lock:
            keys = list(self._down_keys.keys())
            buttons = list(self._down_buttons.keys())
            self._down_keys.clear()
            self._down_buttons.clear()
        if not keys and not buttons:
            return
        try:
            for vk, extended in keys:
                # 扫描码对 key-up 注入无关紧要(Windows 按 vk 匹配),
                # 但给 0 不如照常给 extended 标志稳妥。
                InputInjector.key(vk, 0, False, extended)
            for code in buttons:
                InputInjector.button(code, False)
        except OSError:
            pass
        print(f"[agent] 释放残留按键 ({reason}): "
              f"键={len(keys)} 鼠标键={len(buttons)}", flush=True)

    def _is_toward_host(self, dx: int, dy: int) -> bool:
        """位移是否指向主电脑(即副电脑上的"回程方向")。"""
        side = self._return_edge.side if self._return_edge else None
        if side == "left":
            return dx < 0
        if side == "right":
            return dx > 0
        if side == "top":
            return dy < 0
        if side == "bottom":
            return dy > 0
        return False

    def _at_return_edge(self, x: int, y: int, dx: int, dy: int) -> bool:
        """光标是否已贴着朝向主电脑的那条边, 且仍在朝主电脑方向移动。

        直接复用 EdgeDetector 的抵边几何, 避免两处各写一遍边界条件 ——
        之前这里用 `mon.right - x > h` 判定, 与 EdgeDetector 修复前的
        `x >= right` 是同一类错误(拿光标到不了的坐标当边界)。

        ⚠ 顺序: **先判方向, 再喂 EdgeDetector**。update() 会修改内部
        状态(置 _outside); 若先 update 再判方向, 那么"贴着边但朝屏内
        移动"的事件也会把状态污染成"已抵边", 下一个事件就会被误判为
        "要回主电脑"而把控制权交出去 —— 表现为光标在边界反复跳回主电脑。
        """
        if self._return_edge is None:
            return False
        # 先判方向: 不朝主电脑方向移动就谈不上"回传"
        back_side = self._return_edge.side
        toward_host = (
            (back_side == "left" and dx < 0)
            or (back_side == "right" and dx > 0)
            or (back_side == "top" and dy < 0)
            or (back_side == "bottom" and dy > 0)
        )
        if not toward_host:
            self._return_edge.reset()
            return False
        # 方向正确, 再看是否确实贴在那条边上
        return self._return_edge.update(x, y)

    # -- 输入回传 ------------------------------------------------------

    def _on_host_heartbeat(self, addr) -> None:
        """收到主电脑心跳: 把回传目标切到它实际使用的地址。

        动机与主电脑侧相同(见 Server._on_peer_heartbeat): 双方任一端位于
        NAT 之后时, 配置里填的地址未必是可达的回程地址; 而"主动发来的包"
        其源地址一定是通的, 用它最可靠。
        """
        host = addr[0] if addr else None
        if not host or self._input_tx is None:
            return
        if self._input_tx.addr[0] == host:
            return
        old = self._input_tx.addr[0]
        self._input_tx.set_addr(host)
        print(f"[agent] 由主电脑心跳学到地址 {host} (原 {old}) "
              f"-> 切换回传目标", flush=True)

    def _on_server_side(self, side: str) -> None:
        """主电脑下发方位: 记录并按其重建回传边。

        这是**消除方位配置困惑**的关键: 副机不必再猜"主电脑在我哪一侧",
        直接用主电脑的权威值取对侧作为回传边。
        若回传边已经建好(心跳晚于 start_return 到达), 这里就地重建。
        """
        self._server_side = side
        if self._return_edge is None:
            return          # _start_return 会用到它
        back_side = return_edge_side(side)
        if self._return_edge.side == back_side:
            return
        from .input import EdgeDetector, ScreenRect, virtual_desktop
        # 同 _start_return: 基准用整个虚拟桌面, 且 confirm_frames=1 保证跟手
        self._return_edge = EdgeDetector(
            self_rect=virtual_desktop(), peer_rect=ScreenRect(0, 0, 0, 0),
            side=back_side, confirm_frames=1,
        )
        print(f"[agent] 按主电脑下发的方位 {side} 重建回传边 -> {back_side}",
              flush=True)

    def _start_punch_heartbeat(self) -> None:
        """从输入接收 socket 周期性向主电脑发心跳, 维持 NAT 映射。

        见 start() 里 4c 的说明: 必须用接收输入的同一个 socket, 否则
        NAT 映射的端口与我们的接收端口不一致, 打洞无效。
        目标为主电脑的 INPUT_PORT; 若已从主电脑心跳学到别的端口则用学到的。
        """
        if self._input_rx is None:
            return

        def _loop() -> None:
            while not self._stop.is_set():
                target = None
                if self._input_tx is not None:
                    target = self._input_tx.addr          # 学了就用学到的
                elif self.server_host:
                    target = (self.server_host, INPUT_PORT)
                if target and self._input_rx is not None:
                    self._input_rx.send_heartbeat_to(target)
                self._punch_heartbeats += 1
                if self._stop.wait(self.HEARTBEAT_INTERVAL_S):
                    break

        t = threading.Thread(target=_loop, daemon=True,
                             name="AgentPunchHeartbeat")
        t.start()
        self._threads.append(t)
        self._start_stuck_key_watchdog()

    def _start_stuck_key_watchdog(self) -> None:
        """主电脑心跳超时 -> 释放残留按键(按键卡死防护第二时机)。

        主电脑每 1s 发一次心跳(server 侧 _start_rx_heartbeat)。若连续
        STUCK_KEY_AFTER_S 秒收不到, 说明对端疑似失联 —— 之前转发来的
        down 事件对应的 up 可能永远来不了, 主动释放(照搬 Deskflow
        updateKeysCB 的"对活动 client 补发 key release"精神)。
        有 down 状态需要兜底时才值得看; 平时仅轻量检查时间戳。
        """
        def _loop() -> None:
            while not self._stop.is_set():
                if self._stop.wait(1.0):
                    break
                if not (self._down_keys or self._down_buttons):
                    continue
                rx = self._input_rx
                last = rx.last_heartbeat_mono if rx is not None else None
                if last is None:
                    continue    # 从未收到过心跳(单机自测), 不判失联
                if time.monotonic() - last > self.STUCK_KEY_AFTER_S:
                    self._release_stuck_inputs("主电脑心跳超时")

        t = threading.Thread(target=_loop, daemon=True,
                             name="StuckKeyWatchdog")
        t.start()
        self._threads.append(t)

    def _start_return(self, server_host: str) -> None:
        """监听本机输入, 在光标从副电脑拖回主电脑时把事件发给主电脑。

        ★ side 的来源(这里曾经极易配错):
          side 的语义是"**副电脑位于主电脑的哪一侧**", 与副机自己的方位无关。
          主电脑从它的配置得知并随 CAPS 下发(见 _serve 的 SERVER_CAPS),
          副机**优先采用**它 —— 用户只需在主电脑上配一次, 副机不再需要
          猜测, 也就不会出现"两台填同一个值才能用"的困惑。

          此前副机用自己配的 side 取对侧作为回传边:
            主机 right(副机在右) + 副机填 left
            -> 副机认为"主电脑在我右边", 回传边算成 right;
               而物理上主电脑在副机左边 => 回传边完全搞反。
               后果: 主机向右推(dx>0)时副机判为"用户要回主机", 立刻把事件
               原样退回, **光标根本进不去副机**(用户实测报告)。
          若用户两台都填 right 则恰好正确 —— 于是表现为"必须填相同值",
          这正是把"配置项语义不清"误当成"必须同值"的根源。
        """
        from .input import (EdgeDetector, RawInputListener, ScreenRect,
                            return_edge_side, virtual_desktop)

        # ★ 边界基准必须是**整个虚拟桌面**, 不是单块显示器。
        #   副机有两块屏时, `_main_monitor()` 只返回面积最大的那一块,
        #   回传边会被算在**那块屏的边缘**(例如右屏的左边缘 x=1920),
        #   而整台机器的最左边缘是 x=0。后果(用户实测):
        #   "在右屏上往左移动一点就被拽回主机" —— 因为一越过 1920 就被
        #   判定为"已到回传边"。
        #   Deskflow 用的是计算机的完整 shape(所有显示器合并),
        #   这里照搬: virtual_desktop()。
        desktop = virtual_desktop()
        # 优先用主电脑下发的 side; 没有(旧版主机)才用本机配置
        effective_side = self._server_side or self.side
        if self._server_side and self._server_side != self.side:
            print(f"[agent] 采用主电脑下发的方位 {self._server_side} "
                  f"(本机配置为 {self.side})", flush=True)
        back_side = return_edge_side(effective_side)
        # ★ confirm_frames=1: 回传要**跟手**。
        #   抵边几何本身已经精确(光标真的贴在最外一列/行), 再要求连续两帧
        #   只是增加延迟。用户常常是"甩一下鼠标到边缘就停手" —— 若需要两帧
        #   而他只产生了一个事件, 就得再动一下才能回传, 手感很别扭。
        #   主电脑侧的进入判定仍保留 confirm_frames=2(那里误触发的代价是
        #   光标意外跳到副机, 更烦人), 两侧策略按各自代价取舍。
        self._return_edge = EdgeDetector(
            self_rect=desktop, peer_rect=ScreenRect(0, 0, 0, 0),
            side=back_side, confirm_frames=1,
        )
        self._input_tx = InputSender(server_host)
        # ★ 回传也必须借用**输入接收端的 socket**: 我们把事件发回主电脑时,
        #   源端口要等于主电脑当初发包的目标端口(24801), 否则主电脑侧的
        #   NAT/conntrack 不认(与主电脑转发输入是同一个道理)。
        if self._input_rx is not None:
            self._input_tx.use_socket(self._input_rx._sock, self._input_rx)
        # ★★★ register_keyboard=False: 与 server 侧同一个根因修复。
        #   副机也用 CursorHider 隐藏光标(hide_on_motion=True), 而
        #   「hider 持前台 + 键盘 RawInput」会让本进程的低级键盘钩子收不到键。
        #   副机的回程判定只需要**鼠标** RawInput, 键盘事件由主电脑转发注入,
        #   不需要键盘 RawInput。
        #   实测依据见 RawInputListener.register_keyboard 的长注释。
        self._return_listener_obj = RawInputListener(
            on_event=self._on_return_event, register_keyboard=False)
        self._return_listener_obj.start()
        if not self._return_listener_obj.wait_ready():
            print("[agent] 输入回传监听启动失败", flush=True)
            return
        self._return_listener = "enabled"
        # ★ 打印 DPI 感知模式: 副机若是高 DPI 屏(2560x1600 常见 150%/200%),
        #   进程 DPI-unaware 会让坐标被系统虚拟化, 进入方位随之偏移。
        from .input import dpi_awareness
        print(
            f"[agent] 输入回传已启用: 本机桌面 {desktop}; 回传边={back_side}\n"
            f"        DPI 感知={dpi_awareness()} (应为 per-monitor)",
            flush=True,
        )

    def _on_return_event(self, ev) -> None:
        """本机物理键鼠(若有)把光标拖到对侧边界时, 把事件发回主电脑。

        常见部署里键鼠只接在主电脑上, 本机收到的鼠标事件全是主电脑注入的
        SendInput 合成事件, 已被 RawInputListener 的合成事件过滤器(标记 +
        hDevice)排除, 所以这条路径平时不触发; 只有副电脑也接了键鼠才生效。

        ⚠ 退出判定同样必须"先判方向, 再看坐标": 若先用 update() 再看坐标,
        本机光标被钳在边界时 update() 会一直返回 True, 回传永不停止 ——
        表现为用户在副电脑上怎么拉都回不去(或反复跳回)。
        """
        if self._return_edge is None or self._input_tx is None:
            return
        # ★ 只处理鼠标事件。这条路径是"副机自己接了物理键鼠、把光标拖到
        #   回传边"的手势通道; 键盘不经它转发(理由见下方 send 处的注释)。
        if not ev.is_mouse:
            return
        side = self._return_edge.side
        if ev.type == EV_MOUSE_MOVE_REL:
            toward_host = (
                (side == "left" and ev.a < 0)
                or (side == "right" and ev.a > 0)
                or (side == "top" and ev.b < 0)
                or (side == "bottom" and ev.b > 0)
            )
            if not toward_host:
                # 往回拉(离主电脑远去): 立刻停止回传, 控制权留在本机
                if self._return_edge.is_outside:
                    self._return_edge.reset()
                    print("[agent] 光标已回到本机 -> 停止回传", flush=True)
                return

        x, y = get_cursor_pos()
        if not self._return_edge.is_outside:
            if not self._return_edge.update(x, y):
                return
            # ★★ BUG3②: 越过回传边的**那一刻**把方位报给主电脑(只发一次,
            #   在状态翻转处)。之后每个事件都只转发位移, 不重复发方位 ——
            #   否则主机会被反复绝对定位, 反而钉死在同一个切向坐标上。
            self._send_return_place(x, y)
            print("[agent] 光标拖回主电脑 -> 回传输入", flush=True)
        # 主电脑的光标已被它自己的钩子冻住(不再随物理鼠标移动), 收到这里的
        # 相对移动后会自然往屏内推; 在本机做绝对定位既无必要也会干扰本机
        # 光标位置(它应该停在边界上, 作为下次进入的入口)。
        #
        # ★ 只回传**鼠标位移**(即"拖到边界"这个手势本身)。
        #   绝不能在这里回传键盘/按键: 本机没有钩子, 物理按键会**同时**
        #   落到本机焦点应用(本机没吞)和主电脑(这里又发一份) —— 表现为
        #   在副机打字时主机也跟着收到一遍, 或两边各打一半, 极易误判为
        #   "键盘错乱"。控制权在谁手里, 键盘就归谁: 主机侧的键盘转移由
        #   主机的钩子+转发完成(见 Server._on_input_event)。
        self._input_tx.send(ev)
        self.stats["events_returned"] = self.stats.get("events_returned", 0) + 1

    def _send_return_place(self, x: int, y: int) -> None:
        """把"从副机哪一点回去"的比例发给主电脑(见调用处的 BUG3② 说明)。

        与去程 EV_MOUSE_PLACE 完全对称: 那边由主机算 `position_fraction`,
        这边由副机算。比例沿**回传边**的切向 —— 副机的回传边与主机的
        跨越边是同一根轴(见 return_edge_side), 所以主机拿到后直接
        `fraction_to_pixel(f, self.side)` 就能还原到自己的桌面坐标。
        """
        if self._return_edge is None or self._input_tx is None:
            return
        try:
            f = position_fraction(x, y, self._return_edge.side)
            frac_q = int(round(min(max(f, 0.0), 1.0) * 65535))
            self._input_tx.send(InputEvent(EV_MOUSE_RETURN, 0, 0,
                                           frac_q, 0, now_us()))
            print(f"[agent] 回程方位 -> f={f:.3f} (边={self._return_edge.side})",
                  flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[agent] 发送回程方位失败: {exc}", flush=True)

    # -- 剪贴板跨屏同步(参考 Deskflow, 与 server 侧对称) ----------------

    def _ensure_clipboard(self, host: str) -> None:
        """启动剪贴板双向同步(幂等)。设计与 server 侧完全一致。

        ★ Deskflow 要点:
          * 只同步 Text(UTF-8/LF) / HTML / Bitmap(BMP) 三种规范化格式;
          * 序列号检测变化, 不轮询内容;
          * 分块传输(按 MTU 切 1000 字节);
          * 防回环: 自己写进去的不能再发出去。
        """
        from .clipboard_sync import ClipboardSync
        from .transport import ClipboardReceiver, ClipboardSender

        if self._clip_rx is None:
            try:
                self._clip_rx = ClipboardReceiver(
                    on_payload=self._on_clipboard_recv)
                self._clip_rx.start()
                print("[agent] 剪贴板接收已启动", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[agent] 剪贴板接收启动失败: {exc}", flush=True)
                self._clip_rx = None

        if self._clip_tx is None or \
                getattr(self._clip_tx, "addr", (None,))[0] != host:
            try:
                if self._clip_tx is not None:
                    self._clip_tx.close()
                self._clip_tx = ClipboardSender(host)
            except Exception as exc:  # noqa: BLE001
                print(f"[agent] 剪贴板发送初始化失败: {exc}", flush=True)
                self._clip_tx = None

        if self._clip_sync is None and self._clip_tx is not None:
            try:
                self._clip_sync = ClipboardSync(self._on_local_copy)
                self._clip_sync.start()
                print("[agent] 剪贴板同步已启用(双向)", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[agent] 剪贴板同步启动失败: {exc}", flush=True)
                self._clip_sync = None

    def _on_local_copy(self, blob: bytes, seq: int) -> None:
        tx = self._clip_tx
        if tx is None:
            return
        if not tx.send_blob(blob, seq):
            print(f"[agent] 剪贴板发送部分失败"
                  f"(累计 {tx.failed} 次)", flush=True)

    def _on_clipboard_recv(self, payload: dict) -> None:
        """收到主机的剪贴板 -> 写进本机(写前登记自写标记防回环)。"""
        from . import clipboard as cb
        if not payload:
            return
        if self._clip_sync is not None:
            try:
                self._clip_sync.note_self_write()
            except Exception:  # noqa: BLE001
                pass
        ok = cb.apply(payload)
        self.stats["clip_received"] = self.stats.get("clip_received", 0) + 1
        if not ok:
            self.stats["clip_apply_failed"] = \
                self.stats.get("clip_apply_failed", 0) + 1

    def _mute_local(self) -> None:
        try:
            from comtypes import CLSCTX_ALL, CoInitialize
            from pycaw.api.endpointvolume import IAudioEndpointVolume
            from pycaw.utils import AudioUtilities

            CoInitialize()
            dev = AudioUtilities.GetSpeakers()
            vol = dev._dev.Activate(
                IAudioEndpointVolume._iid_, CLSCTX_ALL, None
            ).QueryInterface(IAudioEndpointVolume)
            vol.SetMute(1, None)
            print("[agent] 本机扬声器已静音(用户已接受)", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[agent] 本机静音失败: {exc}", flush=True)

    def unmute_local(self) -> None:
        try:
            from comtypes import CLSCTX_ALL, CoInitialize
            from pycaw.api.endpointvolume import IAudioEndpointVolume
            from pycaw.utils import AudioUtilities

            CoInitialize()
            dev = AudioUtilities.GetSpeakers()
            vol = dev._dev.Activate(
                IAudioEndpointVolume._iid_, CLSCTX_ALL, None
            ).QueryInterface(IAudioEndpointVolume)
            vol.SetMute(0, None)
            print("[agent] 本机扬声器已取消静音", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[agent] 取消静音失败: {exc}", flush=True)

    def stop(self) -> None:
        self._stop.set()
        disable_high_res_timer()
        for t in self._threads:
            t.join(timeout=2.0)
        if self._return_listener_obj is not None:
            self._return_listener_obj.stop()
        # ★ 退出前释放残留按键: 进程一退, 副机上再没有人为这些键发 up。
        self._release_stuck_inputs("退出")
        # ★ 必须在销毁 hider 窗口之前恢复光标(停止时若光标仍隐藏,
        #   用户会面对一个没有光标的桌面)
        if self._hider is not None:
            self._hider.show()
            self._hider.stop()
            self._hider = None
        # 剪贴板资源先停监听器(否则它还在读剪贴板), 再关收发
        if self._clip_sync is not None:
            try:
                self._clip_sync.stop()
            except Exception:  # noqa: BLE001
                pass
            self._clip_sync = None
        for obj in (self._input_rx, self._audio_tx, self._control,
                    self._input_tx, self._clip_rx, self._clip_tx):
            if obj is not None:
                obj.stop() if hasattr(obj, "stop") else obj.close()
        self._clip_rx = None
        self._clip_tx = None
        if self._cap is not None:
            self._cap.stop()
        if self.mute_local:
            self.unmute_local()
        print(
            f"[agent] 停止: sent={self.stats['frames_sent']} 帧 "
            f"{self.stats['bytes_sent']} 字节, injected={self.stats['events_injected']} "
            f"事件, 回传={self.stats.get('events_returned', 0)} 事件",
            flush=True,
        )
