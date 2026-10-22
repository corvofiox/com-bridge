"""主电脑(server)角色。

职责:
  1. UDP 广播信标, 供副电脑发现。
  2. TCP 控制通道: 对时 + 能力协商(时间基准)。
  3. 本机 loopback 采集 -> 混音 -> 本机渲染(主电脑音频输出)。
  4. 接收副电脑音频流, 与本机音频混合。
  5. 输入: 本机原始输入捕获 + 越界判定, 越界后事件转发给副电脑;
     副电脑光标回来时, 副电脑发回的事件在本机注入。
"""

from __future__ import annotations

import ctypes
import os
import threading
import time

from ctypes import wintypes

from .audio import (
    AlignedStreamBuffer,
    LoopbackCapture,
    LoopbackRenderer,
)
from .clock import disable_high_res_timer, enable_high_res_timer, now_us
from .input import (
    EdgeDetector,
    InputInjector,
    RawInputListener,
    ScreenRect,
    enumerate_monitors,
    fraction_to_pixel,
    get_cursor_pos,
    virtual_desktop,
)
from .cursor import CursorHider
from .hook import PASSTHROUGH_VKS, LowLevelHook
from .protocol import (
    CLIPBOARD_PORT,
    EV_KEY,
    EV_MOUSE_BUTTON,
    EV_MOUSE_MODS,
    EV_MOUSE_MOVE_REL,
    EV_MOUSE_PLACE,
    EV_MOUSE_RETURN,
    EV_MOUSE_WHEEL,
    INPUT_PORT,
    MB_DOWN,
    KEY_DOWN,
    KEY_EXTENDED,
    KEY_UP,
    MODFLAG_ALT,
    MODFLAG_CTRL,
    MODFLAG_SHIFT,
    MODFLAG_WIN,
    VK_CONTROL,
    VK_MENU,
    VK_SHIFT,
    VK_LWIN,
    VK_RWIN,
    InputEvent,
)
from .transport import (
    AudioReceiver,
    BeaconAdvertiser,
    ControlServer,
    InputReceiver,
    InputSender,
    _local_addresses,
)


def _current_input_desktop_name() -> str | None:
    """当前输入桌面名(锁屏/安全桌面防护用)。查询失败返回 None。

    Windows 桌面层级: 锁屏/UAC 时输入桌面从 Default 切到 Winlogon(或
    安全桌面), 低级钩子在安全桌面上收不到事件。SessionGuard 用桌面名
    变化检测这类切换(照搬 Deskflow checkDesk 的 OpenInputDesktop 轮询)。
    """
    try:
        hdesk = ctypes.windll.user32.OpenInputDesktop(0, False, 0x0100)
        if not hdesk:
            return None
        try:
            need = wintypes.DWORD()
            # 两步查询: 先取所需字节数(此时必然返回 0 + ERROR_INSUFFICIENT_BUFFER,
            # 是预期行为), 再真正读名字。need 仍为 0 才是失败。
            ctypes.windll.user32.GetUserObjectInformationW(
                hdesk, 2, None, 0, ctypes.byref(need))
            if not need.value:
                return None
            buf = ctypes.create_unicode_buffer(need.value)
            if not ctypes.windll.user32.GetUserObjectInformationW(
                    hdesk, 2, buf, need, ctypes.byref(need)):
                return None
            return buf.value
        finally:
            ctypes.windll.user32.CloseDesktop(hdesk)
    except Exception:  # noqa: BLE001
        return None


class Server:
    """主电脑(server)角色: 捕获本机音频/输入, 把跨屏输入转发给副电脑。"""

    # ★ 穿越态**没有**时间兜底(与 Deskflow 一致): 用户把光标停在副机上
    #   不动是完全合法的状态 —— 曾经按"活动间隙 2.5s"强制退出, 用户在
    #   副机上阅读几秒就被拽回主机(实测)。真正要防的是"副电脑掉线、
    #   回程信号永远不来"导致用户被困, 那由**心跳丢失看门狗**处理:
    #   副电脑每 HEARTBEAT_INTERVAL_S(1s) 发一次心跳, 连续这么久(秒)
    #   收不到才判定失联, 强制解除穿越态。触发条件是"对端失联",
    #   与用户是否停止活动完全无关。
    PEER_LOSS_S = 5.0

    # ★★★ 强制交回控制权之后的"禁止再进入"窗口(秒)。
    #   为什么需要它(2026-10-21 事故复盘, 日志实证):
    #   看门狗每 1s 轮询, 只在"处于穿越态"时才动作。它调 edge.reset()
    #   把穿越态清掉, 但只要用户的手还在往屏幕边外推, **下一次位移**
    #   就会立刻重新触发穿越 —— 日志里就是这么写的:
    #       03:27:46.427 光标越界
    #       03:27:46.994 !! 92s 未收到副电脑心跳, 判定失联 -> 强制交回
    #       03:27:47.097 光标越界            <- 100ms 后又进去了
    #       03:27:47.997 !! 93s 未收到副电脑心跳 ...
    #   结果不是"交回控制权", 而是每秒一次的**抢控制权抖动**:
    #   每次都要 抢前台 + 全屏窗 + SetCapture + 隐藏光标 + 注入抖动,
    #   再全部撤销。用户的键鼠一半时间被吞, 全屏程序被反复抢焦点。
    #   修法: 强制交回后设一个冷静期, 期间**不许**进入穿越态。
    ENTRY_LOCKOUT_S = 2.0

    # 反向心跳间隔(秒)。打洞期间需要维持 NAT 映射, 太长会让映射过期。
    HEARTBEAT_INTERVAL_S = 1.0

    # 钩子自愈上限: 检测到"穿越态光标位移"(= 钩子被系统静默摘除)时先
    # 请求重装(通常一次就恢复), 累计超过这个次数才判定为"根本装不上"
    # 并退化为钉住方案。见 _on_input_event 里的健康自检。
    HOOK_REINSTALL_LIMIT = 3

    # 钩子健康自检的位移容差(像素): 排除 hider 自己的 ±1px 提交抖动,
    # 只把"光标跟着物理鼠标明显移动"当作摘钩证据。
    HOOK_PROBE_TOLERANCE_PX = 2

    # ★ WASAPI 渲染缓冲"见底"阈值(帧, 48kHz 下 96 帧 = 2ms)。
    #   padding 低于它 => 设备随时可能在我写入前读空 => 可听咔哒。
    #   这是主机侧唯一**确定性**的断口判据(见 _render_loop 的说明)。
    STARVE_PAD_FRAMES = 96

    # ★ 起播前等待启动突发落定的时间(秒)。见 _render_loop 起播段说明:
    #   连接瞬间 agent 会把采集缓冲存量一次性发来, 等它到齐再开播,
    #   就能在"一声未出"的状态下把多余水位抹平, 避免起播阶段的持续削波。
    PRIME_SETTLE_S = 0.6

    # ★ 启动宽限期内**禁止削波**(秒)。削波=丢真实样本=可听咔哒, 而启动
    #   阶段前馈还没收敛, 水位本来就偏高; 此时削波是纯损失。
    TRIM_WARMUP_S = 20.0

    # (已废弃 2026-10-20) KBD_LEAK_REINSTALL_LIMIT 连同"RawInput 键盘泄漏
    #   检测"整体移除: 物理按键实测证明其判据错误(hider 持前台 + 键盘
    #   RawInput 并存时, 钩子收不到键是系统正常行为, 不是钩子坏)。
    #   根因修复 = 移除键盘 RawInput 注册(register_keyboard=False)。

    # 键盘钩子自愈参数: 重装后等这么久再验证回调是否可达(给系统时间把
    # 新钩子挂上链), 最多尝试这么多次。
    KBD_HEAL_INTERVAL_S = 0.35
    KBD_HEAL_ATTEMPTS = 3

    # 判定"这次位移是不是我们自己注入造成的"的时间窗(秒)。
    HOOK_PROBE_INJECT_WINDOW_S = 0.25

    # ★ 键盘钩子自检结论的**有效期**(秒)。超过就复检 —— 因为钩子可用性
    #   会随"本进程窗口是否前台"变化(实测: 窗口有焦点 -> 回调恒不调用;
    #   焦点转移 -> 立刻恢复)。GUI 启动时窗口有焦点, 自检必然失败;
    #   之后主动放弃焦点才恢复, 所以结论**必须**能更新。
    #   取 1.0s: 足够快地跟随焦点变化, 又不会在输入热路径上频繁注入探针。
    KBD_VERIFY_TTL_S = 1.0

    # 边界现场取证开关(与 Agent 同一套): 环境变量 COM_BRIDGE_DIAG_EDGE=1
    # 或存在标记文件 %LOCALAPPDATA%\ComBridge\diag_edge.flag。改完重启生效。
    # 只为排"边缘附近指针不动"这类手感问题服务, 平时零开销。
    DIAG_EDGE = (bool(os.environ.get("COM_BRIDGE_DIAG_EDGE"))
                 or os.path.exists(os.path.join(
                     os.environ.get("LOCALAPPDATA") or "", "ComBridge",
                     "diag_edge.flag")))
    DIAG_MAX_LINES = 20000

    # ★ 全屏程序(游戏/播放器/远程桌面)在前台时**禁止穿越**(2026-10-22 用户要求):
    #   "主机在游戏中鼠标还是能穿梭到副机上, 我需要游戏中主机的鼠标靠近边缘
    #    也不要穿梭"。游戏里鼠标经常贴边平移, 一穿过去键鼠就全跑到对面。
    #   判定结果缓存一会儿(每事件都查前台窗口太重), 见 _game_blocks_crossing。
    GAME_BLOCK_CHECK_S = 0.2

    def __init__(self, name: str = "server", port: int = 24802,
                 jitter_ms: float = 60.0, side: str = "right",
                 bind_addr: str = "0.0.0.0", capture_local: bool = False,
                 accept_synthetic_input: bool = False):
        self.name = name
        self.port = port
        self.jitter_ms = jitter_ms
        self.side = side
        self.bind_addr = bind_addr
        # capture_local 默认 False: 见 _render_loop 顶部的反馈回路说明。
        # 开启只用于诊断(会引入回声), 生产路径不要开。
        self.capture_local = capture_local
        # ★ 测试后门(默认 False): 传给 RawInputListener.accept_synthetic,
        #   仅供自环/活体验收用注入模拟物理输入。生产(双机)永远 False,
        #   否则副机回声/别的程序注入会进入越界判定。
        self._accept_synthetic_input = bool(accept_synthetic_input)

        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._fmt_ready = threading.Event()
        self._threads: list[threading.Thread] = []
        self._start_mono = time.monotonic()
        self._peer_priming = 0.0
        # 副电脑心跳最近一次到达的时刻(看门狗用, 见 PEER_LOSS_S)
        self._last_peer_heartbeat = time.monotonic()
        self._cap: LoopbackCapture | None = None
        self._ren: LoopbackRenderer | None = None
        self._adv: BeaconAdvertiser | None = None
        self._ctrl: ControlServer | None = None
        self._audio_rx: AudioReceiver | None = None
        self._input_rx: InputReceiver | None = None
        self._input_tx: InputSender | None = None
        # 剪贴板跨屏同步(参考 Deskflow 的 Clipboard + ClipboardChunk)。
        #   _clip_rx: 收对端发来的内容
        #   _clip_tx: 把本机复制的内容发给对端(地址在 peer 确定后才知道)
        #   _clip_sync: 监听本机剪贴板变化
        self._clip_rx = None
        self._clip_tx = None          # ClipOutbox(TCP 串行发送队列)
        self._clip_sync = None
        self._listener: RawInputListener | None = None
        self._edge: EdgeDetector | None = None
        # ★ 必须在 __init__ 预置: _start_input 里 listener 就绪到 hider
        #   创建之间有窗口期, 用户此刻推鼠标越界会触发 _on_input_event,
        #   若属性不存在 -> AttributeError 被消息循环吞掉 -> 光标不隐藏
        #   (与 agent 侧 '_hider=None' 预置是同一个教训)。
        self._hider: CursorHider | None = None
        # 低级钩子: 穿越期间**吞掉**本机物理键鼠输入(见 _acquire_control)。
        # 这是"点击落到副机而不是主机"的关键, RawInput 做不到这件事。
        self._hook: LowLevelHook | None = None
        # 钩子不可用时退化为"注入钉住"(见 _ensure_hook)
        self._pin_fallback = False
        # 穿越期间的**钉住点**(交接那一刻定下, 之后每个事件都钉回同一点)。
        # 见 _edge_pin_point / _pin_cursor_to_edge 的说明。
        self._pin_point: tuple[int, int] | None = None
        # 钩子健康自检的上次光标位置(穿越态下位移非零 = 钩子被摘)。
        self._last_probe: tuple[int, int] = (0, 0)
        # 钩子重装次数(自愈; 超过 HOOK_REINSTALL_LIMIT 才退化为钉住)
        self._hook_reinstalls = 0
        # ★ 键盘钩子自检结果: None=未测/无法判定, True=可达, False=不可达。
        #   False 时**禁止转发键盘**(否则键同时落两台, 用户实测症状:
        #   "在副机打字, 主机也同步输入")。
        self._kbd_hook_verified: bool | None = None
        # 上次自检的时间(结论有效期见 KBD_VERIFY_TTL_S)
        self._kbd_verify_mono = 0.0
        # 异步自检线程(见 _kick_kbd_verify): 绝不让探针的 0.5s 等待
        # 发生在输入线程上
        self._kbd_verify_thread: threading.Thread | None = None
        # 启动自检线程的锁: 探针会改全局计数, 两个并发探针会互相污染判据
        self._kbd_verify_lock = threading.Lock()
        # 边界现场取证的已输出行数与节流时刻(见 _diag_edge_event)
        self._diag_lines = 0
        self._diag_mono = 0.0
        # 全屏程序抑制穿越: 缓存(结论, 检查时刻)与告警节流
        self._game_check = (False, 0.0)
        self._game_blocks = 0
        self._game_log_mono = 0.0
        # 键盘钩子自愈(见 _kbd_heal_loop): 同一时刻只允许一轮在跑
        self._kbd_healing = False
        self._kbd_heal_lock = threading.Lock()

        self._local: AlignedStreamBuffer | None = None
        self._peer: AlignedStreamBuffer | None = None
        self._clock_offset_us = 0.0     # local(agent) - remote(server)

        self.peer_addr: str | None = None
        # 心跳打洞学到的副电脑真实地址与端口(优先于 TCP 源地址, 见 _on_peer_heartbeat)
        self._punched_host: str | None = None
        self._punched_port: int | None = None
        # 反向心跳线程只启动一次(地址更新时不能重复起线程)
        self._rx_hb_started = False
        self.stats = {
            "local_frames": 0,
            "peer_frames": 0,
            "peer_lost": 0,
            "rendered_frames": 0,
            "events_forwarded": 0,
            # ★ 回程事件注入计数。此前只在 _on_return_event 里 `+= 1`,
            #   从未初始化 —— 副机回送任意一个事件就抛 KeyError
            #   (日志实证: 03:16:23/31/34 三次)。异常被上层吞掉, 所以
            #   表现为"回程时偶发一次点击/按键失效", 极难发现。
            "events_injected": 0,
            # 钩子不可用时被丢弃的键盘事件(见 _keyboard_relay_ready)
            "keys_dropped_no_hook": 0,
        }
        # 穿越态禁止进入的截止时刻(见 ENTRY_LOCKOUT_S 的说明)
        self._entry_blocked_until = 0.0

    # -- 启动 ----------------------------------------------------------

    def start(self) -> None:
        # 15.6ms 的系统节拍会把渲染循环的 sleep/wait 量化, 水位随之抖动;
        # 音频路径要求亚毫秒调度, 必须先调高定时器分辨率
        enable_high_res_timer()
        self._start_audio()
        # ★ 顺序很重要: **先 _start_input 再 _start_discovery**。
        #   _start_discovery 会启动控制服务端, agent 一连上就会触发
        #   _on_client_connect -> _ensure_input_tx -> use_socket(接收端)。
        #   若此时 _input_rx 还没创建, use_socket 什么也借不到, 输入事件就会
        #   从 InputSender 的**临时端口**发出 —— 端口限制型 NAT 会把它丢弃。
        #   症状极具误导性: 心跳(从 24801 发)全部到达, 事件全丢。
        self._start_input()
        self._start_discovery()
        print("[server] 就绪。Ctrl+C 停止。", flush=True)

    def _start_audio(self) -> None:
        # 只构造对象, start() 留给各自线程(COM 线程亲和)。
        # 格式由**渲染器**决定(不是采集器): 我们输出到本机声卡,
        # 所以 peer 缓冲的格式必须跟渲染端点一致。
        self._ren = LoopbackRenderer()
        if self.capture_local:
            self._cap = LoopbackCapture()

        self._audio_rx = AudioReceiver(on_frame=self._on_peer_audio)
        # 在启动线程**之前**完成绑定: 端口被占用时立刻抛错, 而不是让线程
        # 静默死亡、界面误报"运行中"
        self._audio_rx.bind_now()
        self._audio_rx.start()

        t_cap = None
        if self.capture_local:
            # 采集线程: COM 亲和, LoopbackCapture 的 start()/read() 必须同线程
            t_cap = threading.Thread(target=self._capture_loop, daemon=True,
                                     name="CaptureLoop")
            t_cap.start()
        # 渲染线程: LoopbackRenderer 的 start()/write() 必须同线程
        t = threading.Thread(target=self._render_loop, daemon=True, name="RenderLoop")
        t.start()
        self._threads = [x for x in (t_cap, t) if x is not None]

    def _capture_loop(self) -> None:
        """诊断用: 采集本机音频(默认关闭, 见 _render_loop 反馈回路说明)。"""
        assert self._cap is not None
        self._cap.start()
        with self._lock:
            self._local = AlignedStreamBuffer(
                channels=self._cap.channels, block_align=self._cap.block_align,
                bits=self._cap.bits, rate=self._cap.rate, name="local",
                target_ms=self.jitter_ms,
            )
        print(
            f"[server] (诊断)本机采集 {self._cap.channels}ch@{self._cap.rate}Hz "
            f"{self._cap.bits}bit",
            flush=True,
        )
        while not self._stop.is_set():
            r = self._cap.read(timeout_s=0.05)
            if r is None:
                continue
            pcm, flags, _qpc = r
            if not (flags & 0x2):          # 非 SILENT
                with self._lock:
                    if self._local is not None:
                        self._local.push(now_us(), pcm)
                self.stats["local_frames"] += 1
        self._cap.stop()

    def _on_client_shape(self, shape: tuple[int, int, int, int]) -> None:
        """收到副机桌面上报: 记录并打印(跨屏位置映射的基准)。"""
        x, y, w, h = shape
        print(f"[server] 副电脑桌面 {w}x{h} @({x},{y}) —— "
              f"跨屏进入位置将按比例映射到该桌面", flush=True)

    def _start_discovery(self) -> None:
        caps = {
            "role": "server",
            "name": self.name,
            "audio_port": 4010,
            "input_port": 24801,
            "control_port": self.port,
        }
        self._adv = BeaconAdvertiser(name=self.name, caps=caps, port=self.port)
        self._adv.start()
        self._ctrl = ControlServer(port=self.port, name=self.name,
                                   on_client=self._on_client_connect,
                                   on_shape=self._on_client_shape)
        # 本机桌面几何: SHAPE 交换时发给副机(它要用主电脑的尺寸做比例映射)
        from .input import virtual_desktop
        _d = virtual_desktop()
        self._ctrl.set_shape(_d.left, _d.top, _d.width, _d.height)
        # 同 _start_audio: 先绑定再起线程, 端口冲突要立刻暴露
        self._ctrl.bind_now()
        self._ctrl.start()

    def _on_peer_heartbeat(self, addr) -> None:
        """收到副电脑的心跳: 学到它**真实可用**的地址并切换发送目标。

        为什么这是必需的(真实故障):
          副电脑经 NAT(主路由是跑在虚拟机里的 OpenWrt)连接时, TCP accept
          给出的源地址是**网关**(实测 192.168.10.1), 拿它转发输入会让事件
          全部落在网关上 —— 而音频是副电脑主动发的(NAT 放行)所以完全正常,
          于是表现为"声音好、鼠标一动不动", 极难自查。

        ★★★ 但**绝不能无条件信任**心跳源地址 —— 这是用户现场实测的 bug:
          日志显示 `由心跳学到副电脑真实地址 192.168.10.1:32802
          (原 192.168.10.201:24801) -> 切换输入转发目标`
          agent 上报的 192.168.10.201 是**同网段直连地址**(实测 ping 可达),
          而心跳学习把它覆盖成了**主路由的 NAT 映射地址**。后者要经
          OpenWrt 的 conntrack 才能转回副机, 多一跳且可能完全不可达。
          即: 学习机制把**更好的直连地址**覆盖成了**更差的 NAT 地址**。

        判据(按优先级, 只允许"更好"的地址覆盖"更差"的):
          1. 已是**同网段直连**(与对方 adv_host 同 /24, 且不是网关)
             -> 永不覆盖。直连永远优于 NAT 中转。
          2. 心跳源地址 == TCP 源地址(都是网关) -> 无新信息, 不覆盖。
          3. 其余情况(跨网段部署, 只能靠打洞) -> 才用心跳地址。

        ★ 必须用**完整的 (IP, 端口)**, 不能只取 IP 配固定端口:
          NAT 映射后的源端口几乎不是 24801(实测会变成随机高位端口)。
        """
        host = addr[0] if addr else None
        port = addr[1] if addr and len(addr) > 1 else None
        if not host or not port:
            return
        # 看门狗: 任何心跳都证明副电脑还活着(与穿越态无关, 常态也刷新)
        self._last_peer_heartbeat = time.monotonic()

        # ★★ 判据1: 当前目标已是"同网段直连"时, 拒绝被 NAT 地址覆盖。
        #   同网段 = 前 24 位相同且不是网关地址(网关由 _is_gateway_like 判)。
        cur = self._input_tx.addr if self._input_tx is not None else None
        if cur is not None and self._is_lan_peer(cur[0], host):
            if (host, port) != cur:
                print(f"[server] 心跳源 {host}:{port} 是 NAT 映射地址, 而当前"
                      f"目标是直连 {cur[0]}:{cur[1]} —— 保持直连(不切换)",
                      flush=True)
            return

        # 记住打洞学到的地址(仅在允许覆盖时)
        first_time = self._punched_host != host
        self._punched_host = host
        self._punched_port = port
        if self._input_tx is None:
            # ★ 还没有发送器: 只记录打洞地址, **不在这里建目标**。
            #   真正的目标由 _ensure_input_tx(带 adv_host)决定 —— 因为
            #   心跳源在 NAT 下就是网关, 单凭它建目标会发错地方。
            #   (用户现场: 心跳来自 192.168.10.1:32802, 那不是副机地址)
            if self._is_gateway_like(host):
                print(f"[server] 心跳源 {host}:{port} 疑似 NAT 网关 —— "
                      f"不据此建转发目标, 等待 agent 上报 adv_host",
                      flush=True)
            return
        if self._input_tx.addr != (host, port):
            old = self._input_tx.addr
            self._input_tx.set_addr(host, port)
            self.peer_addr = host
            print(f"[server] 由心跳学到副电脑真实地址 {host}:{port} "
                  f"(原 {old[0]}:{old[1]}) -> 切换输入转发目标", flush=True)
        elif first_time:
            print(f"[server] 副电脑心跳来自 {host}:{port} (转发目标一致)",
                  flush=True)

    @staticmethod
    def _is_gateway_like(host: str) -> bool:
        """判断地址是否"像网关"(用于拒绝把 NAT 映射地址当副机地址)。

        ★ 判据(任一成立即视为网关):
          1. 以 .1 结尾(绝大多数家用/办公路由器的地址, 含本项目现场
             的 192.168.10.1 —— OpenWrt 跑在 Hyper-V 里做 NAT)
          2. 与本机地址不同网段(说明是路由器转发来的, 不是直连对端)

        保守起见只用第 1 条做主判据(第 2 条在跨网段部署时会误杀),
        并允许 _pick_input_target 的"同网段优先"逻辑兜底。
        """
        try:
            if not host:
                return False
            return host.split(".")[-1] == "1"
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _is_lan_peer(cur_host: str, hb_host: str) -> bool:
        """当前目标是否"与心跳源同网段"(= 直连优于 NAT)。

        ★ 判据用**前 24 位相同**: 家用/办公 LAN 几乎都是 /24。
        """
        try:
            if not cur_host or not hb_host or cur_host == hb_host:
                return False
            return (".".join(cur_host.split(".")[:3])
                    == ".".join(hb_host.split(".")[:3]))
        except Exception:  # noqa: BLE001
            return False

    def _pick_input_target(self, peer_host: str) -> tuple[str, int]:
        """决定输入事件的转发目标 —— **直连优先于 NAT 中转**。

        ★★★ 这是用户现场实测 bug 的修复点。原逻辑是"心跳学到的永远优先",
          日志显示它把 agent 上报的**同网段直连地址** 192.168.10.201
          覆盖成了**主路由的 NAT 映射** 192.168.10.1:32802:
              agent 上报地址 192.168.10.201 -> 用上报地址转发输入
              由心跳学到副电脑真实地址 192.168.10.1:32802 (原 .201:24801)
          后果: 输入事件全发给路由器, 需经 conntrack 转回, 多一跳且可能
          完全不可达 —— 表现为"鼠标能过去但副机没反应"。

        优先级:
          1. peer_host(agent 上报的 adv_host)与心跳源**同网段**
             -> 用 peer_host(直连), 忽略 NAT 映射地址
          2. 无 peer_host(心跳先到)或跨网段 -> 用心跳学到的(打洞唯一出路)
          3. 都没有 -> peer_host + 默认端口
        """
        hb = ((self._punched_host, self._punched_port)
              if self._punched_host and self._punched_port else None)
        if peer_host:
            if hb is None:
                return (peer_host, INPUT_PORT)
            # 上报地址与心跳源同网段 => 上报的是直连地址, 优于 NAT 映射
            if self._is_lan_peer(peer_host, hb[0]):
                return (peer_host, INPUT_PORT)
            # 跨网段: 只能靠打洞
            return hb
        if hb is not None:
            return hb
        return (peer_host, INPUT_PORT)


    def _on_client_connect(self, addr, caps: dict) -> None:
        """agent 连上控制通道(或随后上报 CAPS): 记下地址, 准备转发输入。

        ★ 地址优先级: **agent 上报的 adv_host > TCP 源地址**。
          TCP 源地址经过 NAT 时会变成网关(实测主路由是虚拟机里的 OpenWrt
          时源地址是 192.168.10.1), 拿它当转发目标会让输入事件全部落到
          网关上, 副电脑一个也收不到 —— 而音频是副电脑主动发的, NAT 放行,
          于是出现"声音正常、鼠标完全不动"这种极具迷惑性的现象。
          agent 在 CAPS 里报告自己与主电脑同网段的地址, 那才是可回连的。

        ★★★ caps 为空时**绝不能**用 TCP 源地址建目标 —— 用户现场实测:
              [server] agent 已连接 192.168.10.1 caps={}
              [server] 输入转发目标 -> 192.168.10.1:24801
          空 caps 意味着对方还没上报 adv_host(首包/重连时序), 此刻的
          TCP 源地址在 NAT 环境下**就是网关**。若据此建目标, 输入事件
          会全部发给路由器。正确做法: 等到带 adv_host 的 CAPS 再建。
        """
        src = addr[0] if addr else None
        adv = (caps or {}).get("adv_host") or None
        if not adv:
            # ★ 没有 adv_host: 不建立/不改动转发目标。
            #   真正的目标要等 agent 上报 CAPS, 或等心跳学到
            #   (此时 _pick_input_target 会做"直连优先"判定)。
            print(f"[server] agent 已连接 {src} (尚未上报 adv_host, "
                  f"等待 CAPS 后再决定输入转发目标)", flush=True)
            return
        host = adv
        if src and adv != src:
            print(f"[server] agent 上报地址 {adv} (TCP 源地址 {src} 应为 NAT "
                  f"网关) -> 用上报地址转发输入", flush=True)
        else:
            print(f"[server] agent 已连接 {host} caps={caps}", flush=True)
        self._ensure_input_tx(host)

    def _start_input(self) -> None:
        monitors = enumerate_monitors()
        if not monitors:
            raise RuntimeError("未枚举到显示器")
        self_mon = max(monitors, key=lambda r: r.width * r.height)
        # ★ 进入判定的帧数用 EdgeDetector 的默认值(2), 与用户认可的手感一致。
        #   2026-10-22 曾临时改成 1(以为"慢推进不了副机"卡在这个帧数上), 但真正的
        #   原因是注入坐标归一化丢像素(见 InputInjector.to_absolute 的长注释),
        #   与帧数无关 —— 归一化修好后按用户要求改回默认值。
        #   ⚠ 已知取舍: 要求"连续 2 帧外向", 而 dx<=0 的事件(含 dx=0 的静止/纯
        #   竖直上报)会把计数清零, 所以**极慢**的推法可能需要稍微果断一点。
        self._edge = EdgeDetector(
            self_rect=self_mon, peer_rect=ScreenRect(0, 0, 0, 0), side=self.side
        )
        # 接收 agent 回传的输入事件(光标拖回主电脑时, 副电脑发事件过来)
        # ★ 同时用 on_heartbeat 做**NAT 打洞**: 收到副电脑的心跳时, 我们
        #   看到的是它**真实可用的源地址**(而 TCP accept 学到的是网关),
        #   于是把输入发送目标切过去。见 _on_peer_heartbeat。
        self._input_rx = InputReceiver(on_event=self._on_return_event,
                                       on_heartbeat=self._on_peer_heartbeat)
        self._input_rx.bind_now()
        self._input_rx.start()
        # ★ RawInput 走 _on_raw_input_event 而不是直接 _on_input_event:
        #   前者兼作**键盘钩子存活检测**(穿越态收到 RawInput 键盘事件 =
        #   钩子没吞住 = 键盘钩子已被系统摘除)。见该方法的长注释。
        #
        # ★★★ register_keyboard=False —— **这是键盘跨屏失效的根因修复**。
        #   物理按键实测(用户亲手按, 三组对照):
        #     不注册任何 RawInput   -> hider 隐藏光标后键盘**正常**(+26)
        #     只注册鼠标 0x02       -> 隐藏光标后键盘**正常**(+24)
        #     只注册键盘 0x06       -> 隐藏光标后键盘**全灭**(+0)
        #   机制: 本进程窗口(hider)在前台 + 注册了键盘 RawInput 时,
        #   本进程的 WH_KEYBOARD_LL 钩子收不到任何键盘事件。而穿越期间
        #   hider 必须抢前台才能隐藏光标(SetCapture 依赖前台), 于是必然触发
        #   -> 键不吞不转发 -> 两机同时响应 -> 自愈重装无效 -> 暂停键盘转发
        #   (正是用户日志里那一串)。
        #   Deskflow 从未踩坑, 因为它**全程不注册 RawInput**。
        #   保留鼠标 RawInput: 越界判定/回声过滤/TAG 判据都依赖它, 且实测无害。
        self._listener = RawInputListener(
            on_event=self._on_raw_input_event,
            accept_synthetic=self._accept_synthetic_input,
            register_keyboard=False)
        self._listener.start()
        if not self._listener.wait_ready():
            raise RuntimeError("原始输入监听启动失败")
        # ★ 光标隐藏器(照搬 Deskflow 的 MSWindowsDesks hider window):
        #   越界交给副机时隐藏本机光标(否则本机光标被钳在边缘可见/可点,
        #   用户会看到"两个光标"), 回到本机时恢复。
        self._hider = CursorHider(hide_on_motion=False, name="ServerHider")
        self._hider.start()
        # ★ 低级钩子: 穿越期间吞掉本机物理键鼠输入。
        #   这是"点击落到副机而不是主机"的关键 —— RawInput 只观察不消费,
        #   没有它就会出现用户实测的症状: "进入副机后鼠标点击还是落位在主机"。
        self._ensure_hook()
        # ★ 启动即打印 DPI 感知模式与**物理**桌面几何: 方位偏差最可能的
        #   原因就是进程 DPI-unaware 导致坐标被系统虚拟化(高 DPI 副机上
        #   尤其明显)。把这两个数写进日志, 现场一眼就能判断"是不是这套
        #   坐标系"的问题, 不用再猜。
        from .input import dpi_awareness, virtual_desktop
        _d = virtual_desktop()
        print(
            f"[server] 本机屏幕 {self_mon}; 越界边={self.side}; "
            f"显示器数={len(monitors)}\n"
            f"         DPI 感知={dpi_awareness()} (应为 per-monitor; "
            f"unaware 会让坐标被系统虚拟化)\n"
            f"         虚拟桌面={_d} ({_d.width}x{_d.height} 物理像素)",
            flush=True,
        )

    def _ensure_input_tx(self, peer_host: str) -> None:
        """懒建 InputSender: 只有真正知道 agent 地址后才转发。

        ★ 必须校验"对端地址不是本机": 若 agent 把"主电脑 IP"填成了自己的
          地址(或两台机器角色配反), 控制连接会来自本机, 于是输入事件被发
          回自己 —— 用户看到的是"鼠标怎么都过不去", 而日志里一切正常。
          实测踩到: server 日志显示 `输入转发目标 -> 192.168.10.1`, 而那
          正是它自己的地址, 事件全打到自己身上, 副电脑一个都收不到。
        """
        if peer_host in _local_addresses():
            print(
                f"[server] !! 警告: 对端地址 {peer_host} 是本机地址。\n"
                f"         这说明副电脑上的'主电脑 IP'填成了它自己, 或两台\n"
                f"         机器的角色配反了。输入事件会发回本机, 副电脑收不到。\n"
                f"         本机地址: {', '.join(sorted(_local_addresses()))}",
                flush=True,
            )
        if self._input_tx is not None and self.peer_addr == peer_host:
            return
        if self._input_tx is not None:
            # ★ 已存在发送器时**必须更新地址**而不是直接返回。
            #   曾写成"已有就不重建"(想保护打洞学到的地址), 结果 agent 后来
            #   上报的正确地址被完全忽略 —— 日志显示"用上报地址", 实际目标
            #   仍是网关, 用户看到鼠标依然不动。
            #
            # ★★★ 优先级修正(用户现场实测 bug): 曾经无条件
            #   "心跳学到的 > 上报地址"。日志显示这会把**同网段直连地址**
            #   192.168.10.201 覆盖成**主路由的 NAT 映射** 192.168.10.1:32802
            #   —— 后者要多经一跳 OpenWrt conntrack, 且可能完全不可达。
            #   正确优先级:
            #     1. 上报地址与当前心跳源**同网段**(说明上报的是直连地址)
            #        -> 用上报地址(直连永远优于 NAT 中转)
            #     2. 否则 -> 用心跳学到的(跨网段部署时它才是有用的打洞结果)
            want = self._pick_input_target(peer_host)
            if self._input_tx.addr != want:
                old = self._input_tx.addr
                self._input_tx.set_addr(*want)
                self.peer_addr = want[0]
                print(f"[server] 更新输入转发目标 {old[0]}:{old[1]} -> "
                      f"{want[0]}:{want[1]}", flush=True)
            return
        # ★ 若心跳已经先到(打洞成功), 需要判断哪个地址更好(见 _pick_input_target)
        target = self._pick_input_target(peer_host)
        self._input_tx = InputSender(*target)
        self.peer_addr = target[0]
        # ★ 让发送器**借用输入接收端的 socket**。
        #   端口限制型 NAT(OpenWrt 默认 masquerade 即是)的 conntrack 条目
        #   要求回程包的**源端口 == 对端当初发包的目标端口**(即 24801)。
        #   若用 InputSender 自建的临时端口发出, 包会被 NAT 直接丢弃 ——
        #   实测症状极有指向性: **心跳(从 24801 发)全部到达, 输入事件
        #   (从临时端口发)全部丢失**, 两者目标地址完全相同。
        if self._input_rx is not None:
            self._input_tx.use_socket(self._input_rx._sock, self._input_rx)
        # 反向心跳: 让副电脑也能学到本机的可达地址(同样从接收 socket 发)
        if self._input_rx is not None:
            self._start_rx_heartbeat()
        # ★ 剪贴板跨屏同步: peer 地址确定后立刻启用(参考 Deskflow)。
        #   双向: 本机复制 -> 发到 peer; peer 复制 -> 本机接收并写入。
        self._ensure_clipboard(target[0])
        # 看门狗: 副电脑失联时强制交回控制权(穿越态唯一的自动退出路径)
        if self._input_rx is not None:
            self._start_peer_watchdog()
        # 会话锁防护: 锁屏/安全桌面切换时强制交回控制权(照搬 checkDesk)
        self._start_session_guard()
        # ★ 日志必须**如实说明地址来源** —— 曾经无条件写"(心跳学到)",
        #   但当 agent 上报的直连地址优先时, 那个措辞会误导排障
        #   (用户会以为程序又用了 NAT 地址)。
        if target[0] == peer_host and target[1] == INPUT_PORT:
            src = "agent 上报的直连地址"
        elif self._punched_host and target == (self._punched_host,
                                               self._punched_port):
            src = "心跳打洞学到"
        else:
            src = "默认端口"
        print(f"[server] 输入转发目标 -> {target[0]}:{target[1]} ({src})",
              flush=True)

    def _start_rx_heartbeat(self) -> None:
        """用输入接收 socket 周期性反向发心跳(打洞用), 并**下发本机方位**。

        方位下发让副电脑不必再猜"主电脑在我哪一侧", 从根上消除"两台机器的
        方位配置必须填同一个值"这种困惑(实测用户被它卡住: 主机 right +
        副机 left 时光标完全进不去副机 —— 副机把回传边算反了, 于是把主电脑
        转发来的每一个位移都当成"用户要回主机"而原样退回)。
        """
        if self._input_rx is None or self._rx_hb_started:
            return
        self._rx_hb_started = True

        def _loop() -> None:
            while not self._stop.is_set():
                # ★ 每次都读**当前**地址: 打洞/上报会改写发送目标, 若在
                #   循环外捕获一次地址, 心跳就会一直发往旧目标(例如最初
                #   学到的 NAT 网关), 副电脑永远收不到方位下发。
                if self._input_rx is not None and self._input_tx is not None:
                    self._input_rx.send_heartbeat_to(self._input_tx.addr,
                                                     side=self.side)
                if self._stop.wait(self.HEARTBEAT_INTERVAL_S):
                    break

        t = threading.Thread(target=_loop, daemon=True, name="ServerHeartbeat")
        t.start()
        self._threads.append(t)

    # -- 剪贴板跨屏同步(参考 Deskflow) ---------------------------------

    def _ensure_clipboard(self, peer_host: str) -> None:
        """启动剪贴板双向同步。幂等 —— 可重复调用。

        ★ 参考 Deskflow 的设计(已读源码):
          * 只同步 **Text(UTF-8/LF) / HTML / Bitmap(BMP)** 三种规范化格式,
            各平台自己做本地格式转换(见 clipboard.py)。
          * 用**序列号**检测变化, 不轮询内容。
          * 分块传输(ClipboardChunk 的 start/data/end + seq),
            我们按 MTU 切成 1000 字节的块(见 clipboard_sync.py)。
          * **防回环**: 自己写进剪贴板的内容不能再发回去,
            否则两台机器会无限互发同一份内容。
        """
        from .clipboard_sync import ClipboardSync
        from .clip_transfer import ClipInbox, ClipOutbox

        # 接收端(TCP 监听, 收 peer 复制的内容): 只启一次
        if self._clip_rx is None:
            try:
                inbox = ClipInbox(on_payload=self._on_clipboard_recv)
                inbox.bind_now()          # 绑定失败要当场抛出来
                inbox.start()
                self._clip_rx = inbox
                print("[server] 剪贴板接收已启动"
                      f"(TCP 端口 {inbox.port})", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[server] 剪贴板接收启动失败: {exc}", flush=True)
                self._clip_rx = None

        # 发送端(串行发送队列): peer 地址变了要重建
        if self._clip_tx is None or \
                getattr(self._clip_tx, "host", None) != peer_host:
            try:
                if self._clip_tx is not None:
                    self._clip_tx.stop()
                self._clip_tx = ClipOutbox(peer_host)
                self._clip_tx.start()
            except Exception as exc:  # noqa: BLE001
                print(f"[server] 剪贴板发送初始化失败: {exc}", flush=True)
                self._clip_tx = None

        # 本机剪贴板监听: 只启一次
        if self._clip_sync is None and self._clip_tx is not None:
            try:
                self._clip_sync = ClipboardSync(self._on_local_copy,
                                                on_files=self._on_local_files)
                self._clip_sync.start()
                print("[server] 剪贴板同步已启用(双向, 含图片/文件)", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[server] 剪贴板同步启动失败: {exc}", flush=True)
                self._clip_sync = None

    def _on_local_copy(self, blob: bytes) -> None:
        """本机发生"复制" -> 放进串行发送队列(TCP)。"""
        tx = self._clip_tx
        if tx is not None:
            tx.put(blob)

    def _on_local_files(self, blob: bytes, entries: list) -> None:
        """本机复制了文件/文件夹 -> 剪贴板内容(带清单) + 文件内容一起发。"""
        tx = self._clip_tx
        if tx is not None:
            tx.put(blob, entries)

    def _on_clipboard_recv(self, payload: dict) -> None:
        """收到 peer 的剪贴板 -> 写进本机。

        ★ 写之前必须**登记自写标记**, 否则我们刚写进去的内容会被
          本机监听器当成"用户复制"再发回去 -> 两台机器无限互发。
          这是 Deskflow 用 setClipboard/grabClipboard 解决的问题,
          我们用一个时间窗实现(见 ClipboardSync.note_self_write)。
        """
        from . import clipboard as cb
        if not payload:
            return
        # 先登记(即使 apply 失败也无害: 只是多跳过一轮)
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

    def _start_peer_watchdog(self) -> None:
        """★ 心跳丢失看门狗 —— 穿越态唯一的**自动**退出路径。

        取代旧的"活动间隙 2.5s 兜底"(那次改动是真正的回归: 用户把光标
        停在副机上阅读几秒就被拽回主机, 因为"停止活动"本来是合法状态)。
        真正需要防被困的场景只有一种: **副电脑掉线**(进程崩溃/拔网线),
        回程信号永远来不了。副电脑每 HEARTBEAT_INTERVAL_S(1s) 发一次心跳,
        本线程连续 PEER_LOSS_S(5s) 收不到才判定失联, 强制解除穿越态 ——
        触发条件是"对端失联", 与用户是否停止活动无关(照搬 Deskflow:
        它同样只在连接断开时才把光标交回主屏)。
        """
        def _loop() -> None:
            while not self._stop.is_set():
                if self._stop.wait(1.0):
                    break
                edge = self._edge
                if edge is None or not edge.is_outside:
                    continue
                silent = time.monotonic() - self._last_peer_heartbeat
                if silent > self.PEER_LOSS_S:
                    self._force_release(
                        f"!! {silent:.0f}s 未收到副电脑心跳, 判定失联")

        t = threading.Thread(target=_loop, daemon=True, name="PeerWatchdog")
        t.start()
        self._threads.append(t)

    def _start_session_guard(self) -> None:
        """★ 会话锁/桌面切换防护(照搬 Deskflow checkDesk 的语义, 简化版)。

        Deskflow 每 0.2s 轮询 OpenInputDesktop 对比桌面名, 检测到安全桌面
        (Winlogon/默认桌面切换)/屏保就做桌面切换; Server.cpp onScreensaver
        在屏保激活时立即跳回主机。我们没有安全桌面注入能力, 但必须处理
        一个真实场景: **穿越态跨锁屏存续** —— 低级钩子在安全桌面上收不到
        事件(Winlogon 是独立桌面, 钩子不跟随), 解锁后本机第一段物理输入
        仍被 relay 吞掉并转发给副机; 锁屏期间心跳若继续到达, 看门狗也不会
        触发。修法: 穿越态期间轮询当前输入桌面名, 变化(=发生锁屏/解锁/
        UAC)即强制交回控制权。非穿越态零开销(只在穿越时才查)。
        """
        def _loop() -> None:
            prev: str | None = None
            while not self._stop.is_set():
                if self._stop.wait(0.5):
                    break
                edge = self._edge
                if edge is None or not edge.is_outside:
                    prev = None
                    continue
                name = _current_input_desktop_name()
                if name is None:
                    continue    # 查询失败: 不误判
                if prev is not None and name != prev:
                    self._force_release(
                        f"!! 输入桌面已切换 ({prev} -> {name}), 判定发生锁屏/安全桌面")
                prev = name

        t = threading.Thread(target=_loop, daemon=True, name="SessionGuard")
        t.start()
        self._threads.append(t)

    # -- 音频 ----------------------------------------------------------

    def _on_peer_audio(self, hdr, pcm: bytes) -> None:
        with self._lock:
            if self._peer is None:
                self.stats["peer_dropped"] = self.stats.get("peer_dropped", 0) + 1
                return
            self.stats["peer_frames"] += 1
            if (self._ren is not None and self._ren.rate
                    and (hdr.rate != self._ren.rate
                         or hdr.bits != self._ren.bits)):
                # 格式不符: 记录但不混入(全网统一 48kHz 是前提)
                self.stats["peer_rejected"] = self.stats.get("peer_rejected", 0) + 1
                return
            # ts 已在 agent 侧换算到 server 时钟域, 直接入队
            # ★★★ 入站时间线连续性检查(2026-10-21 新增)
            #   "上一包 ts + 上一包时长 == 本包 ts" 必须成立(副机按帧偏移
            #   打戳)。不成立只有两种可能:
            #     delta > 0 -> 中间这段数据**没到**(丢包/被丢弃) -> 断口
            #     delta < 0 -> 重复/乱序投递
            #   两种情况都会在播放流里留下缺口 = 可听咔哒。
            #   ★ 这是"副机打戳之后到主机入队之前"唯一能抓住丢数据的判据;
            #     若它恒为 0, 说明入站流严格连续, 断口不在这段路径。
            _frames_in = len(pcm) // self._ren.block_align
            _prev_ts = getattr(self, "_prev_in_ts", 0)
            if _prev_ts:
                _exp = _prev_ts + int(
                    getattr(self, "_prev_in_frames", 0) * 1_000_000
                    / self._ren.rate)
                _d_us = hdr.ts_us - _exp
                _ad = abs(_d_us)
                if _ad > getattr(self, "_in_max_delta_us", 0):
                    self._in_max_delta_us = _ad
                if _ad > 3000:            # >3ms 视为真实断口
                    self.stats["in_ts_gaps"] = \
                        self.stats.get("in_ts_gaps", 0) + 1
                    _n = self.stats["in_ts_gaps"]
                    if _n <= 5 or _n % 50 == 0:
                        print(f"[server] !! 入站时间线断口(第 {_n} 次): "
                              f"ts 偏差={_d_us / 1000.0:+.1f}ms "
                              f"(上一包 {getattr(self, '_prev_in_frames', 0)} 帧)",
                              flush=True)
            self._prev_in_ts = hdr.ts_us
            self._prev_in_frames = _frames_in
            self._peer.push(hdr.ts_us, pcm)
            self._peer.note_packet(hdr.ts_us, len(pcm) // self._ren.block_align)

    def _render_loop(self) -> None:
        """把**副电脑的音频**渲染到本机默认输出端点。

        为什么这里不混本机采集的音频:
          主电脑自己的音频(播放器/游戏/浏览器)本来就由 Windows 直接送到
          这块声卡, 已经能听见。如果我们再 loopback 采集它、跟 peer 混一遍
          写回同一块声卡, 就等于把本机声音送回自己的输出 —— 形成闭环:
          采集到"混合结果" -> 又混一次 -> 又写回 -> 幅度每圈累加, 只能靠
          削波压住, 结果就是持续失真的自激。所以本机音频**不参与软件混音**,
          混音在声卡端点里由 Windows 完成(acoustic mixing)。
          这样本机音频零额外延迟, 副电脑音频才付抖动缓冲的代价。
        """
        self._starve_pad_frames = self.STARVE_PAD_FRAMES
        self._pad_min_frames = None
        # 起播等待用(见 _render_loop 的起播段说明)
        self._peer_first_mono = 0.0
        self._prime_level = 0.0
        # ★ 边界淡入淡出器(消除起播/停止的 pop), 见 BoundaryFader 的说明
        from .audio import BoundaryFader
        self._fader: BoundaryFader | None = None
        # ★ 音频渲染线程提权(见 clock.raise_audio_thread_priority 的说明)
        from .clock import raise_audio_thread_priority
        print(f"[server] 渲染线程优先级: {raise_audio_thread_priority()}",
              flush=True)
        self._ren.start()
        self._fader = BoundaryFader(
            channels=self._ren.channels,
            block_align=self._ren.block_align,
            bits=self._ren.bits,
            rate=self._ren.rate,
            fade_ms=5.0,
        )
        with self._lock:
            self._peer = AlignedStreamBuffer(
                channels=self._ren.channels,
                block_align=self._ren.block_align,
                bits=self._ren.bits,
                rate=self._ren.rate,
                name="peer",
                target_ms=self.jitter_ms,
            )
            self._fmt_ready.set()
        print(
            f"[server] 渲染 {self._ren.channels}ch@{self._ren.rate}Hz "
            f"{self._ren.bits}bit, 抖动目标={self.jitter_ms:.0f}ms "
            f"(本机音频由 Windows 直接混音, 不经过软件)",
            flush=True,
        )
        block = self._ren.block_align
        target_frames = int(self._ren.rate * self.jitter_ms / 1000)
        lookahead_us = int(25_000)
        # ── 启动水位建立 ────────────────────────────────────────────
        # 起播前先攒 target_frames 帧的数据(60ms), 让渲染一开始就有
        # 完整的抖动缓冲。没有这一步: 水位从 0 起步, 供给与消费恰好
        # 相等时水位永远贴 0, 渲染端偶发"队列空"→ 静音簇。
        primed = False
        # ── 漂移补偿的闭环控制 ──────────────────────────────────────
        # 每轮渲染读出渲染缓冲的水位误差, 换算成"本次多读/少读几帧",
        # 累积成 ratio 微调量。这是**闭环**: 水位高(发送快于消费)→少产出,
        # 水位低→多产出, 与发送端速率估计无关, 长期必然收敛。
        # 之前依赖 StreamClockTracker 的开环估计: 发送端 Event.wait 的
        # 实际节奏(20.104ms)与设备时钟(20.000ms)的 ~0.5% 失配, 加上
        # EWMA 滞后与对时误差, 长期积累成渲染缓冲 overflow → 每秒几十次
        # 均匀小爆音。闭环直接消灭这个失配。
        #
        # ★ ratio 必须**平滑变化**(每轮限幅 RATIO_SLEW):
        #   实测 ratio 阶跃时, Resampler 相邻输出样本的插值映射会移动
        #   整帧, 表现为**成簇的密集小跳变**(簇间隔≈闭环时间常数) ——
        #   正是用户听到的持续杂音。限幅后 τ≈2s, 闭环仍收敛但无突变
        #   (tests/test_resampler_stream.py 的平滑 ratio 用例零跳变)。
        #
        # ★ 起播攒水位 + FIFO 取数后, **不要**再用 padding 做 ratio 闭环:
        #   padding 由渲染循环自己的写入决定(自反馈), 会与 peer 水位
        #   打架形成极限环(实测 ratio 饱和到 1.03、水位 0~60ms 震荡)。
        #   漂移的修正改为**直接读 peer 水位**: peer 水位 > 目标 → 多读
        #   (ratio<1), < 目标 → 少读(ratio>1)。单控制器, 无自反馈。
        ratio = 1.0
        # ±1.5%: 给 PI 足够调节裕度。
        #   ★ 范围不能太窄: 实测净流入(供给-消费)可达 0.2%, 若下限只到
        #     0.995(=消耗降 0.5%)看似够用, 但闭环还要同时吸收 Resampler
        #     本身约 0.05% 的结构性泄漏, 叠加后 PI 会长期贴在下限 ——
        #     表现为"水位永远到不了目标且 ratio 顶死", 而 ratio 顶死意味
        #     着音调被固定压低, 失去调节能力。
        #   ±1.5% 对应的最大音调偏差约 1.5%(≈1/4 半音), 只在极端失配时
        #   短暂触及; 稳态 ratio 应落在 1.000±0.002。
        ratio_min, ratio_max = 0.985, 1.015
        # 比例项最大提速(见下面 want 的计算处): 0.8% 采样率偏移
        # ≈ 每秒消化 8ms 积压, 音调偏移 <1/8 半音(听不出)。
        RATIO_P_MAX = 0.008
        RATIO_SLEW = 0.00002          # 每轮(≈10ms)最大变化, τ≈2s
        # 慢速修正增益: 极小(时间常数数十秒), 只用来消除前馈的残余稳态
        # 误差, 不与水位噪声耦合 —— 增益一大就会重新变成震荡的纯 PI。
        RATIO_KI_SLOW = 0.00002
        # 死区: 水位偏差在 ±15% 内视为正常噪声, 不驱动积分器。
        # (水位本身有 ±15ms 噪声, 对应 ±25%; 取 15% 让积分器只对真正的
        #  长期偏离动作 —— 目标 60ms 时即 ±9ms 内不动作。)
        RATIO_DEADZONE = 0.15
        # 积分项权限上限。
        # ★★★ 从 ±0.2% 放宽到 ±1.0%(实测依据
        #   tools/exp_integral_authority.py):
        #   前馈 ratio_ff = arrival_rate/48000 测的是"agent 每秒发来多少帧",
        #   而**消费速率由 WASAPI 设备时钟决定**, 两者可能差 0.2~0.5%
        #   (共享模式尤其明显)。这个失配前馈**完全看不见**, 只能靠积分吸收。
        #   原来只给 ±0.2%, 一旦真实失配超过它, 积分就饱和 ->
        #   水位单向爬升 -> 撞 max_ms(用户实测"一直加, 加到上限就不走了")。
        #   模拟(消费慢 0.3%): ±0.2% 时水位 10→370ms 单调上涨;
        #   ±1.5% 时能回落到 17ms。这里取 ±1.0%: 足够覆盖常见设备失配,
        #   又留出前馈的调节裕度(总 ratio 仍被 ratio_min/max 钳在 ±1.5%)。
        RATIO_TRIM_LIMIT = 0.010
        self._ratio_integral = 0.0
        # 渲染缓冲补到接近满(留 5ms 余量防 write 溢出):
        # ★ 这是**抗调度抖动**的关键。渲染循环是 Python 轮询(2ms sleep +
        #   numpy 拷贝), 偶发调度延迟(GC/GIL/系统负载)可达几十毫秒;
        #   若渲染缓冲只留 30ms, 一次这样的延迟就让它耗尽 → WASAPI 欠载
        #   (共享模式下它会重复上一小段样本)→ 均匀小爆音。
        #   实测: render_target=30ms 时录到 13 处/s 跳变;
        #         放到接近 100ms 后降到 1~2 处/s(仅剩启动瞬间)。
        #   代价: 端到端延迟增加约 70ms(总延迟 ≈ 100ms 渲染 + 60ms 抖动)。
        #   抖动缓冲仍独立留在 peer 队列里, 两者互不挤占。
        render_target = int(self._ren.buffer_frames) - int(self._ren.rate * 0.005)
        empty_since = None
        while not self._stop.is_set():
            avail = self._ren.available_frames()
            padding = self._ren.buffer_frames - avail
            with self._lock:
                peer_buf = self._peer
                level_ms = peer_buf.buffered_ms() if peer_buf else 0.0
                if not primed:
                    # 起播: 等 peer 队列里**真的有数据**再开始写。
                    # ★ 不能用 avail > 0 当条件 —— 渲染缓冲初始就是空的,
                    #   avail 立即为真, 等于没有等待(会出现一段静音填充)。
                    #   这里要求队列至少有一包数据(约 20ms), 保证首帧就是
                    #   真实音频而不是静音。若 1s 内没有数据(对端未起播),
                    #   也照常进入渲染循环(写静音由 empty_since 分支负责)。
                    # ★★★ 起播时机(2026-10-21 重做, 修"刚连接水位上涨->爆音"):
                    #   旧逻辑是"队列里有一包就开播", 于是开播后 agent 才把
                    #   采集缓冲里的存量(~100ms)灌进来, 水位冲高并一直压在
                    #   削波阈值之上, trim_to 持续丢样本 -> 持续爆音, 要靠
                    #   ratio 慢慢排 40 秒才安静(用户描述的现象)。
                    #   新做法: **等启动突发落定再开播** —— 至少等
                    #   PRIME_SETTLE_S 秒, 且队列不再明显增长; 开播前把
                    #   多余水位一次性抹平。此刻设备缓冲还是空的(一声未出),
                    #   所以丢样本完全不可闻。
                    if not primed and level_ms > 0:
                        if self._peer_first_mono == 0.0:
                            self._peer_first_mono = time.monotonic()
                        _settle = time.monotonic() - self._peer_first_mono
                        _growing = level_ms > getattr(self, "_prime_level", 0) + 3
                        self._prime_level = level_ms
                        if _settle < self.PRIME_SETTLE_S or _growing:
                            self._peer_priming = round(level_ms, 1)
                            time.sleep(0.01)
                            continue
                    if level_ms > 0 or \
                            time.monotonic() - self._start_mono > 1.0:
                        primed = True
                        print(f"[server] 开始渲染(队列 {level_ms:.0f}ms)",
                              flush=True)
                        if level_ms > self.jitter_ms * 1.5 and peer_buf:
                            _snap = peer_buf.trim_to(self.jitter_ms,
                                                    max_skip_ms=1000.0)
                            level_ms = peer_buf.buffered_ms()
                            print(f"[server] 起播抹平: 丢弃 {_snap} 帧启动"
                                  f"积压(此时尚未出声, 不可闻), "
                                  f"水位 -> {level_ms:.0f}ms", flush=True)
                    else:
                        self._peer_priming = round(level_ms, 1)
                        time.sleep(0.005)
                        continue
                # ★★★ 真·断口判据(2026-10-21 新增): **WASAPI 渲染缓冲见底**。
                #
                #   为什么必须单独统计这一项(此前所有指标都不覆盖它):
                #     队列水位 / ratio / 欠载 / 丢包 全都是**间接**指标; 而
                #     设备里真正排队待播的样本量是 `padding`。padding 一旦
                #     触到 0, WASAPI 共享模式就会播放静音或重复上一小段
                #     —— 那就是用户听到的"咔哒/噼啪", 100% 可听。
                #     所以: **只要 render_starved 在涨, 断口就在主机侧渲染,
                #     与网络/副机无关**; 若它恒为 0 却仍在响, 断口就一定来自
                #     送进来的 PCM 内容本身(源头或传输), 排查方向立刻二分。
                #
                #   阈值取 2ms(96 帧): 低于它设备随时可能在我写入前读空。
                #   启动/起播阶段(primed 刚为真)不算, 用 start 后的宽限跳过。
                if time.monotonic() - self._start_mono > 2.0:
                    _pad_min = getattr(self, "_pad_min_frames", None)
                    if _pad_min is None or padding < _pad_min:
                        self._pad_min_frames = padding
                    if padding <= self._starve_pad_frames:
                        self.stats["render_starved"] = \
                            self.stats.get("render_starved", 0) + 1
                        self.stats["render_starved_pad"] = padding
                        # 只在首次与每 50 次打印, 避免刷屏
                        _n = self.stats["render_starved"]
                        if _n == 1 or _n % 50 == 0:
                            print(f"[server] !! 渲染缓冲见底(第 {_n} 次): "
                                  f"padding={padding}帧 "
                                  f"({padding * 1000 / max(1, self._ren.rate):.1f}ms) "
                                  f"peer水位={level_ms:.0f}ms "
                                  f"空转={getattr(self, '_render_empty_loops', 0)}",
                                  flush=True)
                # ── 漂移补偿: 前馈为主 + 慢速修正 ──────────────────────
                #
                # resampler 语义(已用数值实验确认):
                #   每个输出帧让读指针前进 ratio 个输入帧, 故
                #   **输入消耗速率 = 设备输出速率(恒定 48000) × ratio**。
                #   ratio > 1 -> 消耗输入更快 -> 队列被抽干(水位降);
                #   ratio < 1 -> 消耗更慢   -> 队列积压  (水位升)。
                #
                # ★ 为什么不用纯 PI(曾经的做法, 实测会震荡):
                #   水位反馈有天然滞后(数据先到队列、再被消耗), 而且水位
                #   噪声大(±15ms)。用纯 PI 去追它, 增益调小则收敛慢(实测
                #   起步顶下限 9 秒), 调大则过冲震荡(实测 ratio 在
                #   0.985↔1.015 满量程摆动, 周期约 6s) —— 而 ratio 就是
                #   读取速率, 摆动等于音调持续抖动, 听感即"持续小杂音"。
                #
                # ★ 正确做法: **直接量出供给速率, 前馈算出所需 ratio**。
                #   要让队列水位稳定, 输入消耗速率必须等于到达速率:
                #       48000 × ratio = arrival_rate
                #   => ratio_ff = arrival_rate / 48000
                #   这是一步到位的解, 不需要迭代, 也不会震荡。
                #   再叠加一个**很小的**积分项去消除残余稳态误差(晶振长期
                #   漂移、到达速率估计偏差), 时间常数取数十秒, 只做慢修正,
                #   不与水位噪声耦合。
                arrival = peer_buf.arrival_rate if peer_buf else 0.0
                if arrival > 0:
                    ratio_ff = arrival / float(self._ren.rate)
                else:
                    ratio_ff = 1.0
                # 慢速修正(带死区): 水位偏差的长时间积累。
                #   ★ 死区是必需的: 水位本身有 ±15ms 噪声, 若让积分器一直
                #     追它, 积分会持续累积并在 ±限幅间饱和摆动, 把前馈算好
                #     的 ratio 也拖着一起来回走(实测周期 15~20s、幅度 ±1.5%)
                #     —— ratio 就是读取速率, 这种摆动等于音调持续抖动,
                #     听感即"持续小杂音"。
                #   死区内完全不动, 只修正真正的长期偏离(晶振漂移)。
                pi_err = (level_ms - self.jitter_ms) / max(1.0, self.jitter_ms)
                if abs(pi_err) > RATIO_DEADZONE:
                    # 超出死区的部分才计入(避免在边界反复触发)
                    excess = pi_err - (RATIO_DEADZONE if pi_err > 0
                                       else -RATIO_DEADZONE)
                    self._ratio_integral += excess * RATIO_KI_SLOW
                    # ★ 积分权限**收窄到 ±0.2%**: 前馈已经给出了正确的
                    #   ratio(实测 ratio_ff 稳定在 1.000~1.0015), 积分项只需
                    #   补偿晶振长期漂移这种慢变残差。若给它 ±1.5% 的全量程
                    #   (曾如此), 它会在死区边界外持续累积直到饱和, 把前馈
                    #   算好的值也拖着一起来回走 —— 实测 ratio 在
                    #   0.985↔1.015 满量程摆动、周期约 12s, 而 ratio 就是
                    #   读取速率, 这种摆动听感正是"持续小杂音"。
                    lim = RATIO_TRIM_LIMIT
                    self._ratio_integral = max(-lim,
                                               min(lim, self._ratio_integral))
                want = ratio_ff + self._ratio_integral
                # ★★★ 比例项(P): 水位超目标就让 ratio 变大 —— 这是
                #   **唯一不丢音频**的水位控制手段。
                #
                #   机制: `pop_aligned` 里 `input_budget = max_frames × ratio`,
                #   所以 ratio 越大, 本轮从队列**真正取走**的输入帧越多
                #   (而产出帧数仍受 max_frames 限制) -> 队列水位下降。
                #   多取的样本被重采样器按比例"压掉"(等价于以略高的
                #   速率播出), **一个样本都不丢**, 波形连续。
                #
                #   ★ 为什么之前我说它"无效": 我在模拟脚本里手动维护
                #     `rendered` 计数器, 那个模型不准。用**真实 WASAPI**
                #     实测(tools/verify_ratio_real_wasapi.py, 供给快 2%):
                #        ratio=1.000 -> 水位峰值 55ms
                #        ratio=1.008 -> 水位峰值 **20ms**, trim=0, 丢音频 0
                #     这才是真实行为。
                #
                #   权限 0.8%(RATIO_P_MAX): 音调偏移 <1/8 半音, 听不出;
                #   但每秒可多消化约 8ms 积压, 足以把几十 ms 的超调在
                #   几秒内拉回目标。
                if level_ms > self.jitter_ms:
                    over = min(1.0, (level_ms - self.jitter_ms)
                               / max(1.0, self.jitter_ms))
                    want += RATIO_P_MAX * over
                want = max(ratio_min, min(ratio_max, want))
                # 平滑逼近(限制单步变化), 避免插值映射突变。
                # ★ 超调时放宽单步限制: 水位已经偏高, 需要更快响应,
                #   否则 RATIO_SLEW 的 τ≈2s 会让积压继续增长到触发丢弃。
                #   稳态仍用细步进(避免相位突变产生可听杂音), 只在
                #   "明显超目标"时加速逼近 —— 那时用户更在意别爆音,
                #   而不是 0.8% 的瞬时音调变化。
                slew = RATIO_SLEW
                if level_ms > self.jitter_ms * 1.2:
                    slew = RATIO_SLEW * 8
                ratio += max(-slew, min(slew, want - ratio))
                ratio = max(ratio_min, min(ratio_max, ratio))
                self.stats["render_ratio"] = round(ratio, 6)
                self.stats["ratio_ff"] = round(ratio_ff, 6)
                self.stats["arrival_rate"] = round(arrival, 1)

                # ★★★ 水位硬保护: 超过软阈值就**加速排水**, 越线更深才弃帧。
                #   为什么必须要有它(与积分项互补):
                #     积分项靠调 ratio 慢慢削水位, 时间常数数十秒; 若失配较大
                #     或偶发突发(agent 补发/系统卡顿), 水位仍可能摸到上限,
                #     一旦触顶就只能靠 push() 的丢弃(那是被动的、按包丢,
                #     且丢在数据流中间 → 听感上就是"突然一段杂音/卡顿")。
                #
                #   ★ 2026-10-19 重做(用户实测: "水位设定 60ms 后依然偶尔
                #     突破 200ms, 并在破 200ms 时出现零碎杂音, 回落后正常"):
                #     旧方案(240ms 才一次性 trim 回 60ms)有两个问题:
                #       a) 阈值太高: 水位在 60~240ms 区间完全没人管, 缓慢
                #          堆积可以一路爬到 200ms+ 才被砍, 用户全程看得到;
                #       b) 排水速度不够: budget 被钉死在"补满渲染缓冲",
                #          高水位只能靠积分项以数十秒的时间常数慢慢削,
                #          削的过程中每轮渲染间隙的突发反复顶高水位。
                #   ★★★ 最终方案(2026-10-19 第三次修正, 依据用户的关键证据
                #   "爆音阈值始终紧跟水位上限 300/200/80"):
                #     噪音不是来自"排水慢", 而是来自**一次性硬切**。
                #     所以现在只有**一种**控制手段:
                #       每轮检查水位, 超过软阈值(1.5x 目标)就由
                #       `trim_to(max_skip_ms=2.0)` 丢一小步(≤2ms),
                #       见下面紧邻的代码块。
                #     budget 回到**常规**逻辑(只补渲染缓冲), 不再叠加
                #     超调量 —— 那会产出一大段 PCM, 放不下的部分被
                #     WASAPI 截掉, 又是一次硬切。
                budget = max(0, min(avail, render_target - padding))

                # ★★★ 送进 Resampler 的 max_out 必须 >= 本轮**可产出量**,
                #   不能直接用 budget(它受 WASAPI 剩余空间限制)。
                #
                #   实测根因(diag_sinc_threshold.py + diag_audio_80ms.py):
                #   水位高时队列里积压了大量待播数据, 但 WASAPI 渲染缓冲
                #   已接近满(render_target - padding ≈ 0), 于是 budget 很小。
                #   `Resampler.process(max_out=小)` 就把 out_n 卡住 ->
                #   每轮消费的输入帧远少于队列里的量 -> `_pos` 推进极慢 ->
                #   **抽头窗口始终贴着缓冲末端** -> 越来越多抽头被零填充 ->
                #   波形被截断成宽带噪声(听感: 微小沙沙声)。
                #   这**正好**解释用户观察到的 "水位超过 ~80ms 就出现杂音":
                #   水位越高, 积压越多, 末端截断比例越大。
                #
                # ★ 排水预算: 不要"超调就一次猛灌", 那会产出一大段 PCM,
                #   而 WASAPI 放不下的部分会被截掉 = 又一次硬切。
                #   水位控制统一交给上面的 **trim_to 小步滑窗**, 这里只做
                #   常规的"补满渲染缓冲"。
                pop_budget = max(budget, 0)
                # ★★★ 水位控制: **持续小步滑窗**, 不是"越限就大砍一刀"。
                #
                #   用户提供的决定性证据: "起初水位会持续增长到 390ms,
                #   并在超过 300ms 时出现爆音; 后来限制到 240ms, 就超过
                #   200ms 出现爆音; 现在限制到 85, 于是超过 80ms 出现爆音。"
                #   —— **爆音阈值始终紧跟水位上限**, 证明噪音来自"执行
                #   上限的机制", 而非重采样或积压。
                #
                #   旧机制: `level > max_level_ms()` 时调 trim_to(一次性
                #   削到 target)。事件稀疏但每次都是**几十毫秒的硬切**,
                #   听感 = 爆音, 且频率正比于"越过上限的频率"。
                #
                #   新机制: 每轮都检查, 只要高于**软阈值**就丢一小步
                #   (max_skip_ms=2ms, 由 trim_to 内部限幅)。断口极小,
                #   且因为每轮都在做, 水位被稳定压住而不是反复冲高。
                #
                #   软阈值取 **1.15x 目标**(60ms -> 69ms): 让超调在很小的
                #   幅度就被削掉, 而不是等到 90/240ms。削得越早, 每次要削
                #   的量越小, 断口越不明显; 且水位不会呈现"缓慢爬升-突然
                #   掉落"的锯齿, 听感更稳定。
                # ★ 软阈值取 **1.8× 目标**(60ms -> 108ms)。
                #   曾试过放宽到 3.0× 以减少"丢样本造成的咔哒", 但实测
                #   (纯音 20s)水位会一路涨到 180ms 并卡住: 前馈项在低到达率
                #   读数下把 ratio 压向 0.985(消耗更慢), 形成正反馈。
                #   所以维持 1.8×, 让削波及时把水位压回目标。
                soft_ms = self.jitter_ms * 1.8
                # ★★★ 启动宽限: **还没测出到达率之前不许削波**。
                #   削波(trim_to)是丢真实样本 -> 每丢一次就是一声爆音。
                #   刚连接时前馈尚未收敛(见 audio.note_packet 的说明),
                #   水位会短暂冲高; 此时若立刻开削, 用户听到的就是
                #   "刚连上一直爆音, 过一会儿自己好"。
                #   等 arrival_rate 有读数(说明前馈已接管)且过了启动宽限
                #   时间, 再允许削波 —— 削波是丢样本, 早期触发必然被听见。
                _warmup = time.monotonic() - self._start_mono < self.TRIM_WARMUP_S
                if (peer_buf is not None and level_ms > soft_ms
                        and arrival > 0 and not _warmup):
                    # ★ max_skip_ms = **0.5ms**(24 帧): 断口小到接近
                    #   听不见(0.5ms 的接缝在 1kHz 音上约半个周期,
                    #   听感是极轻的"沙"); 同时每轮都削, 每秒最多
                    #   500×0.5 = 250ms, 足以压住任何实际积压速率。
                    dropped_frames = peer_buf.trim_to(
                        target_ms=self.jitter_ms, max_skip_ms=0.5) \
                        if hasattr(peer_buf, "trim_to") else 0
                    if dropped_frames:
                        self.stats["level_trims"] = \
                            self.stats.get("level_trims", 0) + 1
                        self.stats["trimmed_frames"] = \
                            self.stats.get("trimmed_frames", 0) + dropped_frames
                        # 削完之后队列变短, 下一轮的 level 会自然变小,
                        # 这里不再重复计算(避免同一轮内连削)。
                        level_ms = peer_buf.buffered_ms()

                # ★ 写入预算 = 只把渲染缓冲补到 render_target, **不是**填满它。
                #   曾直接用 avail: 第一轮就把攒下的水位全部灌进 WASAPI,
                #   队列随即见底, 之后每轮 pop 都取不到数据 → WASAPI 欠载
                #   (共享模式下它会重复上一小段样本)→ 每秒几十次均匀爆音。
                #
                #   ★ 实测结论(tools/exp_budget_matrix.py, 真实渲染器 12s,
                #   三种策略对比): target / target+floor / fill 三种写法的
                #   跳变、内部积压、写入间隔**完全相同**。所以"预算小导致
                #   重采样器饥饿"并不成立 —— 真正的问题在 Resampler 内部
                #   对 max_out 的处理(已单独修复, 见 audio.Resampler.process)。
                #   这里保持原逻辑, 不引入不必要的风险。

                peer = None
                if peer_buf is not None and pop_budget > 0:
                    peer = peer_buf.pop_aligned(now_us(), pop_budget,
                                                lookahead_us,
                                                ratio_override=ratio)
                    if peer is None:
                        lag = peer_buf.head_lag_ms(now_us())
                        if lag is not None and lag > self.jitter_ms * 3.0:
                            peer = peer_buf.pop_any(pop_budget)
                            self.stats["resync_popped"] = \
                                self.stats.get("resync_popped", 0) + 1
                    peer_buf.drain_level_ms = level_ms
                    # ★ pop_budget == budget(已受 avail 约束), 所以正常情况
                    #   下这里不会截断。保留兜底: 若确实超出, 把尾部交给
                    #   下一轮(不丢), 而不是硬切。
                    if peer is not None:
                        max_bytes = max(0, avail) * block
                        carry = getattr(self, "_render_carry", b"") or b""
                        if carry:
                            # ★★★ 残留**优先且完整**地排在前面(保持顺序)。
                            #   历史 bug: 旧代码在 len(peer)>max_bytes 时直接
                            #   `self._render_carry = peer[max_bytes:]` —— 那是
                            #   **赋值, 会把上一轮残留原地丢弃**, 播放流随即
                            #   出现断口(咔哒)。现在改成"先拼旧残留, 再整体
                            #   截断到本轮可写量, 剩余继续顺延"。
                            peer = carry + peer
                        if len(peer) > max_bytes:
                            self._render_carry = peer[max_bytes:]
                            peer = peer[:max_bytes]
                        else:
                            self._render_carry = b""
                        # ★ 兜底上限: 正常不会触发(上游产出速率受控)。
                        #   若真的累积超过 ~400ms, 说明出现持续失配, 此时
                        #   丢**最旧**的(而不是最新的), 让延迟不无界增长。
                        _cap = int(self._ren.rate * 0.4) * block
                        if len(self._render_carry) > _cap:
                            _drop = len(self._render_carry) - _cap
                            self._render_carry = self._render_carry[_drop:]
                            self.stats["carry_dropped_frames"] = \
                                self.stats.get("carry_dropped_frames", 0) + \
                                _drop // block
                        if not peer:
                            peer = None

            if peer is None:
                # 队列暂无数据(包在路上): 先什么都不写, 让 WASAPI 把缓冲里
                # 已写入的 ~30ms 播完 —— 这段时间通常足够下一包到达。
                # ★ 绝不写静音填充: 静音会顶占渲染缓冲、并在静音/数据交界
                #   处造成波形断裂(实测正是"持续均匀小杂音"的主源)。
                # ★★ 诊断: 统计"渲染循环取不到数据"的次数与累计时长。
                #   这是**周期性噼啪声最直接的证据** —— 每次取不到数据都
                #   可能让渲染缓冲见底 -> 设备时钟停摆 -> 波形断裂。
                #   之前没有这个计数, 导致只能靠推理猜根因(历史教训)。
                self._render_empty_loops = \
                    getattr(self, "_render_empty_loops", 0) + 1
                if empty_since is None:
                    empty_since = time.monotonic()
                elif time.monotonic() - empty_since > 0.2:
                    # 持续 200ms 无数据(对端真的没声音/断流): 这时才补静音,
                    # 否则 WASAPI 缓冲彻底耗尽会让设备时钟停摆。
                    silent = b"\x00" * (max(0, min(avail,
                                                   render_target - padding))
                                        * block)
                    if silent:
                        # ★ 静音填充也要经过 fader: 否则 fader 里暂存的
                        #   5ms 尾巴会被直接丢掉, 且"音乐->静音"没有淡出,
                        #   照样是一声 pop。
                        s_out = self._fader.push(silent) \
                            if self._fader is not None else silent
                        if s_out:
                            self.stats["silence_frames"] = \
                                self.stats.get("silence_frames", 0) + \
                                self._ren.write(s_out)
                time.sleep(0.004)
                continue

            empty_since = None
            # ★★★ 边界淡入淡出: 消除"起播/停止那一刻的小爆音"。
            #   用户实测: "开始播放声音和结束播放声音的那一刻会小爆一下"
            #   —— 那是音乐↔数字零之间的采样级跳变。fader 会暂存 5ms 尾巴,
            #   等看到下一块再决定是否淡出/淡入(代价: 5ms 额外延迟)。
            shaped = self._fader.push(peer) if self._fader is not None else peer
            if shaped:
                written = self._ren.write(shaped)
                self.stats["rendered_frames"] += written
            time.sleep(0.002)

            # ★★★ 周期性诊断快照(每 5 秒一行) —— 排障必须靠数据, 不能猜。
            #   关注: peer 水位(应 >= jitter_ms)、渲染缓冲水位(应接近
            #   render_target)、render_empty_loops(取不到数据的次数)、
            #   underruns(队列空)、level_trims(水位削波)、ratio。
            #   典型故障特征:
            #     peer 水位远低于目标 + render_empty_loops 持续增长
            #         -> 供给/消费失配, 队列被抽干, 渲染缓冲周期性见底
            _now = time.monotonic()
            _last_snap = getattr(self, "_last_audio_snapshot", 0.0)
            if _now - _last_snap >= 5.0:
                self._last_audio_snapshot = _now
                _pb = peer_buf
                print(
                    f"[server] 音频: peer水位="
                    f"{_pb.buffered_ms() if _pb else 0:.0f}ms/"
                    f"{self.jitter_ms:.0f}ms "
                    f"渲染缓冲={(self._ren.buffer_frames - avail) / self._ren.rate * 1000:.0f}ms"
                    f"/{render_target / self._ren.rate * 1000:.0f}ms "
                    f"空转={getattr(self, '_render_empty_loops', 0)} "
                    f"欠载={_pb.underruns if _pb else 0} "
                    f"预算饿={getattr(_pb, 'starved_budget', 0) if _pb else 0} "
                    f"削波={self.stats.get('level_trims', 0)} "
                    f"静音帧={self.stats.get('silence_frames', 0)} "
                    f"重同步={self.stats.get('resync_popped', 0)} "
                    f"carry丢帧={self.stats.get('carry_dropped_frames', 0)} "
                    f"★见底={self.stats.get('render_starved', 0)} "
                    f"重采样积压={_pb._rs.backlog_frames if (_pb is not None and _pb._rs is not None) else 0}"
                    f"(排空{getattr(_pb._rs, '_backlog_drains', 0) if (_pb is not None and _pb._rs is not None) else 0}"
                    f"/回退{getattr(_pb._rs, '_backlog_resets', 0) if (_pb is not None and _pb._rs is not None) else 0}) "
                    f"最低padding={getattr(self, '_pad_min_frames', -1) if getattr(self, '_pad_min_frames', None) is not None else -1}帧 "
                    f"ratio={ratio:.5f} 到达率={arrival:.0f}",
                    flush=True,
                )
                # 最低 padding 每 5 秒清零, 让下一个窗口重新取最小值
                self._pad_min_frames = None
                # ★★★ 第二行: **真丢包/时序**统计 —— 本地闭环永远测不出
                #   网络丢包(UDP 无重传, 每丢 1 个 2.5ms 的包 = 1 个咔哒,
                #   周期性丢失 = 周期性噼啪), 必须在真机上看。
                _rx = getattr(self, "_audio_rx", None)
                if _rx is not None:
                    print(
                        f"[server] 丢包: 累计丢={_rx.lost} 重复={_rx.dupes} "
                        f"重启={_rx.resyncs} "
                        f"★入站断口={self.stats.get('in_ts_gaps', 0)} "
                        f"最大偏差={getattr(self, '_in_max_delta_us', 0.0) / 1000.0:.1f}ms "
                        f"carry滞留={len(getattr(self, '_render_carry', b'') or b'') // block}",
                        flush=True,
                    )
                    self._in_max_delta_us = 0.0
        self._ren.stop()

    # -- 输入 ----------------------------------------------------------

    def max_level_ms(self) -> float:
        """水位硬保护阈值: 超过它就主动弃帧回落。

        取 max_ms 的 60%(默认 400 × 0.6 = 240ms)。为什么留这么大余量:
        正常波动(±15ms)+ 短暂突发(agent 补发 1~2 包)不应触发弃帧;
        只有真正失控(持续净流入)才会越过它。
        """
        buf = self._peer
        if buf is None:
            return 1e9
        return float(buf.max_ms) * 0.6

    def _game_blocks_crossing(self) -> bool:
        """前台是不是"别的进程的全屏程序"(游戏/播放器/远程桌面)。

        是的话本机**不进入穿越态**: 用户在游戏里把鼠标推到屏幕边不应把键鼠
        交给副电脑。结果缓存 `GAME_BLOCK_CHECK_S`, 避免每个事件都查窗口。
        """
        now = time.monotonic()
        verdict, ts = self._game_check
        if now - ts < self.GAME_BLOCK_CHECK_S:
            return verdict
        try:
            from .cursor import foreign_fullscreen_foreground
            verdict = bool(foreign_fullscreen_foreground())
        except Exception:  # noqa: BLE001
            verdict = False
        self._game_check = (verdict, now)
        if verdict:
            self._game_blocks += 1
            if now - self._game_log_mono >= 5.0:
                self._game_log_mono = now
                print("[server] 前台是全屏程序 -> 本次不穿越"
                      f"(累计拦截 {self._game_blocks} 次)", flush=True)
        return verdict

    def _diag_edge_event(self, ev: InputEvent) -> None:
        """边界现场取证(COM_BRIDGE_DIAG_EDGE / diag_edge.flag 时开启)。

        现场问题(2026-10-22): "副机光标在靠主机那一侧的边缘附近时, 朝屏内
        慢慢移动离不开边缘, 用力拉才行"。要判断这到底是
          ① 主机根本没把位移转发过去(判定/状态问题), 还是
          ② 转发了但副机注不进去(注入问题),
        就得看**主机这一侧**每个事件的原始位移与当时的真实光标位置。
        这里只在光标距跨越边 60px 内记录, 并限制总行数, 平时零开销。
        """
        # 诊断绝不能抛异常打断输入热路径: 有些测试/嵌入式调用不经过 __init__
        # (见 tests 里的 _bare_server, 它的 _edge 是假的), 所以整段兜住。
        if not self.DIAG_EDGE:
            return
        try:
            self._diag_edge_event_now(ev)
        except Exception:  # noqa: BLE001
            return

    def _diag_edge_event_now(self, ev: InputEvent) -> None:
        if getattr(self, "_diag_lines", 0) >= self.DIAG_MAX_LINES:
            return
        # ★ 只在**非穿越态**记录: 穿越期间主机光标被钩子冻住不动, 记它毫无
        #   意义而且 1000 事件/秒会瞬间把行数配额烧光(实测: 6000 行只够 6 秒,
        #   恰好把真正要看的"交回控制权之后"那一段全挤掉了)。
        if self._edge is not None and self._edge.is_outside:
            return
        now = time.monotonic()
        if now - getattr(self, "_diag_mono", 0.0) < 0.002:   # 最多 500 行/秒
            return
        self._diag_mono = now
        x, y = get_cursor_pos()
        r = self._edge.self_rect if self._edge is not None else None
        if r is None:
            return
        side = self.side
        if side == "right":
            near = (r.right - 1 - x) <= 60
        elif side == "left":
            near = (x - r.left) <= 60
        elif side == "top":
            near = (y - r.top) <= 60
        else:
            near = (r.bottom - 1 - y) <= 60
        if not near:
            return
        self._diag_lines = getattr(self, "_diag_lines", 0) + 1
        print(f"[server][edge] dx={ev.a} dy={ev.b} 光标=({x},{y}) "
              f"穿越={self._edge.is_outside}", flush=True)

    def _on_input_event(self, ev: InputEvent) -> None:
        """主电脑侧: 判定光标是否越界, 越界后把事件转发给副电脑。

        ★★★ 控制权转移的完整语义(这是"点击落到哪台机器"的核心):

        穿越期间我们做三件事, 缺一就会表现为"点击还是落在主机":
          1. `hook.set_relay(True)` —— 低级钩子**吞掉**物理鼠标/键盘事件,
             使它们**不落到主机的焦点窗口**。只靠 RawInput 是做不到的:
             RawInput 是"只观察不消费"的, 按下鼠标时 Windows 照样把点击
             送给本机应用(用户实测症状: "进入副机后鼠标点击还是落位在主机")。
          2. 事件转发给副电脑(副机用 SendInput 注入, 点击就落在副机了)。
          3. 隐藏本机光标(否则用户看到"两个光标")。

        回到本机时把 1 撤销(`set_relay(False)`), 物理输入立刻恢复正常 ——
        因此**退出路径必须覆盖所有分支**(回程信号/兜底/异常), 否则用户会
        面对一台"键鼠全无响应"的电脑。

        ★ 设计要点(这些是踩过坑之后才定下来的, 改动前请先读完):

        1) **穿越判定只看相对位移方向, 不看光标绝对坐标。**
           绝对坐标在这里完全不可靠: 越界期间会注入钉住事件, 注入本身
           会改变坐标; 而位移事件的到达与注入的生效存在竞态, 读到的坐标
           可能是"钉住前"或"钉住后"的任意一个。一旦据此判定"光标退回屏内
           了", 就会退出越界态、光标弹回本机, 用户再推又触发 —— 表现为
           **光标在边缘被疯狂弹回**。
           位移方向不受钉住影响(Windows 先应用位移再钳制, 而 lLastX
           记录的是原始位移), 是唯一稳定的信号。

        2) 进入穿越态用"抵边 + 持续朝外推"两个条件, 不用绝对坐标抵边。

        3) 退出穿越态由 agent 的回程信号决定(见 _on_return_event), 本地
           只保留一个时间兜底, 防止回程信号丢失时用户被困。

        4) **钩子吞掉位移后, 主机光标根本不会移动** —— 因此通常**不需要**
           任何"钉住"注入。这从根上消灭了一整类 bug(归一化丢像素、钉读回
           坐标导致沿边缘爬角落)。
           `_pin_cursor_to_edge` 仅作为**钩子不可用时的退化路径**
           (见 `_ensure_hook` 与 `_pin_fallback`)。
        """
        if self._edge is None or self._input_tx is None:
            return

        if ev.type == EV_MOUSE_MOVE_REL:
            self._diag_edge_event(ev)
            dx, dy = ev.a, ev.b
            # ★★ 刚被强制交回(失联/桌面切换/用户逃生键)的冷静期内
            #   **不许**再进穿越态 —— 否则用户的手只要还贴着边往外推,
            #   下一个位移就又把控制权抢走, 变成每秒一次的前台/捕获抖动
            #   (日志实证见 ENTRY_LOCKOUT_S)。
            if not self._edge.is_outside and self._entry_blocked():
                self._edge.reset()
                return
            pushing_out = self._is_outward(dx, dy)

            if not self._edge.is_outside:
                # 未穿越: 需要"抵边 + 朝外推"才进入。
                # ⚠ 顺序很重要: **必须先判方向, 再喂 EdgeDetector**。
                #   EdgeDetector.update() 会**修改内部状态**(置 _outside),
                #   若写成 `update(...) and pushing_out`, 那么方向不对时
                #   短路求值虽然不转发, 状态却已被污染成"已穿越" —— 下一个
                #   事件就会走进穿越分支, 表现为光标莫名跳到对面。
                if not pushing_out:
                    self._edge.reset()      # 朝内移动: 连"抵边计数"也清零
                    return
                # ★ 前台是全屏程序(游戏/播放器/远程桌面)时不许穿越:
                #   放在"抵边计数"之前, 连计数都不累, 退出游戏后从干净状态开始。
                if self._game_blocks_crossing():
                    self._edge.reset()
                    return
                x, y = get_cursor_pos()
                if self._edge.update(x, y):
                    print(f"[server] 光标越界({self.side}) -> 开始转发输入",
                          flush=True)
                    # 先定下钉住点(仅钩子不可用的退化路径会用), 再发定位事件
                    self._pin_point = self._edge_pin_point(x, y)
                    # 自检基准: 从交接一刻起, 主机光标不该再动
                    self._last_probe = (x, y)
                    # ★ 每轮穿越重新计重装次数: 钩子偶发被摘是**一次性**事件,
                    #   不该跨轮累积成"永久退化"(否则用一整天后必然退到钉住,
                    #   键盘再也无人吞 = BUG1 复现)。真正的"根本装不上"会在
                    #   单轮内就连续触发超限。
                    self._hook_reinstalls = 0
                    self._send_place_event(x, y)
                    # 修饰键快照(照搬 Deskflow enter 的 toggleMask):
                    # 用户按住 Shift/Ctrl 时穿越, 副机需要知道
                    self._send_modifier_snapshot()
                    # ★★ 夺取控制权: 开始**吞掉**物理输入 —— 这一步让点击/
                    #   按键真正落到副机。顺序上先开 relay 再隐藏光标, 避免
                    #   中间一小段物理点击仍落到本机。
                    self._acquire_control()
                else:
                    return
            else:
                # 已在穿越态。
                # 钩子可用时位移已被吞掉、主机光标不会移动, 无需钉住;
                # 钩子不可用时退回旧的"钉固定点"方案。
                if self._pin_fallback:
                    self._pin_cursor_to_edge()
                else:
                    # ★ 钩子健康自检(照搬 Deskflow 的 checkDesk 思路):
                    #   Windows 会在回调超时(~300ms)后**静默摘掉**低级钩子,
                    #   摘除后位移不再被吞 —— 主机光标开始真实移动, 而
                    #   edge.is_outside 仍为 True, 事件照旧转发, 用户看到的
                    #   是"两个光标各动各的"。
                    #
                    #   ★ 关键: 摘钩**不等于**钩子不可用 —— 重装一次通常
                    #   立刻恢复(Deskflow 也是这么做的)。此前一检测到位移
                    #   就直接转 _pin_fallback 钉住, 于是键盘侧再也无人吞键:
                    #   物理键同时落到主机焦点应用和副机 —— 正是用户报的
                    #   "主机和副机键盘同步生效"。所以先请求重装, 只有反复
                    #   重装仍失效才退化为钉住。
                    px, py = self._last_probe
                    cx, cy = get_cursor_pos()
                    # ★ 判据必须排除**我们自己造成的**位移, 否则会误判:
                    #   ① hider.hide() 为了提交光标可见性会注入一次 ±1px
                    #      抖动(_do_hide), 探测恰好落在两次注入之间时会读到
                    #      1px 位移;
                    #   ② 退化路径的钉住注入本身也在动光标。
                    #   真"钩子被摘"的表现是光标**跟着物理鼠标大幅移动**,
                    #   所以要求位移超过容差, 且不处于注入窗口期内。
                    moved = (abs(cx - px) > self.HOOK_PROBE_TOLERANCE_PX
                             or abs(cy - py) > self.HOOK_PROBE_TOLERANCE_PX)
                    if moved and not self._is_own_injection():
                        self._hook_reinstalls += 1
                        print("[server] !! 检测到主机光标在穿越态发生位移 -> "
                              f"钩子疑似被系统摘除, 请求重装"
                              f"(第 {self._hook_reinstalls} 次)", flush=True)
                        self._hook.request_reinstall()
                        if self._hook_reinstalls > self.HOOK_REINSTALL_LIMIT:
                            print("[server] !! 重装多次仍失效 -> 退化为注入"
                                  "钉住模式(键盘将无法被吞, 请退出 Deskflow/"
                                  "Synergy 等占用低级钩子的程序)", flush=True)
                            self._pin_fallback = True
                    self._last_probe = (cx, cy)

                # ★ 穿越态**没有时间兜底**(与 Deskflow 一致): 用户把光标
                #   停在副机上不动是合法状态, 曾经按"活动间隙 2.5s"强制
                #   退出, 用户在副机上阅读几秒就被拽回主机(实测)。
                #   防被困改由**心跳丢失看门狗**负责: 副电脑每 1s 发心跳,
                #   连续 PEER_LOSS_S 秒收不到才判定失联并强制解除 ——
                #   触发条件是"对端真的掉线", 与用户是否停止活动无关。

        # ★★★ 转发**鼠标与键盘** —— 但**只在穿越态**。
        #
        #   ⚠ 穿越态门槛是必须的: 未穿越时鼠标位移有早退保护(上面已经
        #     return), 但键盘/按键/滚轮会落到这里。若不加门槛, 用户在
        #     主机上正常打字/点击, 每个事件也被发给副机注入一遍 ——
        #     实测症状: "键盘指令总是在两台机器同时生效, 而不取决于当前
        #     鼠标聚焦在哪台机器"。控制权在谁手里, 输入就归谁。
        #
        #   主电脑自己不处理任何键盘事件(不在本机注入): 穿越期间所有输入
        #   都归属副机, 本机再注入会造成"两边同时收到按键"。
        #
        # ★★★ 键盘的转发必须**以钩子可用为前提**(用户实测的兜底修复):
        #   钩子失效时若照样转发键盘, 结果是**两台同时收到按键**:
        #     - 钩子没吞 -> 键落到主机焦点窗口(输入法被唤起)
        #     - 我们又转发 -> 副机也收到
        #   这正是用户报的"在副机打字, 主机也同步输入"。
        #   正确行为: 钩子不可用时**只转发鼠标、不转发键盘** ——
        #   键只落在主机(用户至少能正常打字), 并在 GUI/日志明确告警。
        #   鼠标不受影响(鼠标钩子通常还活着, 且鼠标泄漏的后果轻得多)。
        if (ev.is_mouse or ev.is_key) and self._edge.is_outside:
            if ev.is_key and not self._keyboard_relay_ready():
                self.stats["keys_dropped_no_hook"] = \
                    self.stats.get("keys_dropped_no_hook", 0) + 1
                return
            self._input_tx.send(ev)
            self.stats["events_forwarded"] += 1
            if ev.is_key:
                self.stats["keys_forwarded"] = \
                    self.stats.get("keys_forwarded", 0) + 1
            else:
                self.stats["mouse_forwarded"] = \
                    self.stats.get("mouse_forwarded", 0) + 1

    def _keyboard_relay_ready(self) -> bool:
        """键盘转发是否安全(钩子能吞键)。

        ★★★ 2026-10-19 物理按键实测后的**重要修正**: 穿越期间不能再用
          probe 否定键盘转发。原因:
            probe 靠 SendInput 注入探针键; 而**穿越期间 hider 必然占着
            前台**(隐藏光标的必要条件, SetCapture 依赖前台)。前台是本进程
            窗口时, 注入的键不一定能反映物理键的真实流向 —— 更重要的是,
            只要 hider 持前台, 我们**本来就不该**把"probe 失败"当成
            "键盘会泄漏"的证据:
              * 若钩子真的收不到键 -> 键不会被吞, 但也**不会被转发**
                (钩子是键盘事件的唯一来源), 所以**不会**出现"两台同时响应";
              * 若钩子收得到键 -> 正常吞+转发。
            两种情况都不会造成泄漏, 所以在穿越态因 probe 失败而丢弃键盘
            只会白白把键盘功能关掉(用户报的"键盘跨屏完全失效")。

        ★ 判据:
          1. 钩子未装 / relay 未开 -> False
          2. **不在穿越态**(hider 未隐藏光标): probe 结论才可信, 过期则复检,
             明确失败 -> False(此时是本进程别的窗口抢了前台, 真的会泄漏)
          3. **在穿越态**: 不看 probe, 只要钩子已装 + relay 已开 -> True。
             钩子若真收不到键, 事件根本不会到这里, 不会泄漏。
        """
        h = self._hook
        if h is None:
            return False
        if not (h.installed and h.relay):
            return False
        # ★ 穿越态(hider 持前台): probe 不可信, 也不需要用 —— 见上面的说明
        hider = getattr(self, "_hider", None)
        if hider is not None and getattr(hider, "hidden", False):
            return True
        # ★ 复检: 结论会过期(焦点可能已变化)
        now = time.monotonic()
        if (self._kbd_hook_verified is None
                or now - self._kbd_verify_mono > self.KBD_VERIFY_TTL_S):
            self._reverify_keyboard_hook()
        if self._kbd_hook_verified is False:
            return False
        return True

    def _reverify_keyboard_hook(self) -> None:
        """重新自检键盘钩子并更新 `_kbd_hook_verified`。

        ★ 只在**必要时**调用(判据过期或穿越开始), 不放在输入热路径上
          —— 探针要注入并等待最多 0.5s。
        """
        h = self._hook
        self._kbd_verify_mono = time.monotonic()
        if h is None or not h.installed:
            self._kbd_hook_verified = False
            return
        try:
            probe = h.probe_keyboard_hook()
        except Exception:  # noqa: BLE001
            return              # 保持原结论
        if probe is None:
            return              # 注入失败: 判据不可用, 保持原结论
        prev = self._kbd_hook_verified
        self._kbd_hook_verified = bool(probe)
        if prev is False and probe is True:
            print("[server] 键盘钩子恢复可达 -> 重新启用键盘转发"
                  "(焦点已离开本程序窗口)", flush=True)

    # -- 控制权(穿越期间 "吞掉本机输入" 的开关) --------------------------

    def _entry_blocked(self, now: float | None = None) -> bool:
        """此刻是否处于"刚被强制交回"的冷静期(不许再进穿越态)。"""
        t = time.monotonic() if now is None else now
        return t < getattr(self, "_entry_blocked_until", 0.0)

    def _force_release(self, reason: str) -> bool:
        """强制交回控制权(看门狗/桌面切换/用户逃生键的**唯一**出口)。

        与 `_release_control` 的区别: 这里还负责**清状态 + 设冷静期**,
        避免"交回后立刻又被同一个边缘动作抓回去"(见 ENTRY_LOCKOUT_S)。

        返回是否真的从穿越态退出(调用方据此决定是否打印)。
        """
        edge = self._edge
        if edge is None:
            return False
        was_outside = bool(edge.is_outside)
        edge.reset()
        self._pin_point = None
        self._release_control()
        if was_outside:
            # 只有真的从穿越态被拽回来才设冷静期: 常态下调用(误按逃生键)
            # 不得产生副作用, 否则用户会莫名卡住下一次穿越。
            self._entry_blocked_until = time.monotonic() + self.ENTRY_LOCKOUT_S
            print(f"[server] {reason} -> 控制权已交回本机 "
                  f"(接下来 {self.ENTRY_LOCKOUT_S:.0f}s 内忽略越界, "
                  f"避免抢控制权抖动)", flush=True)
        return was_outside

    def _on_user_abort(self) -> None:
        """用户按下 Ctrl+Alt+Esc: 无条件交还控制权(逃生通道)。

        ★ 只在**穿越态**下生效: 常态按到它不产生任何副作用(不改冷静期),
          否则用户平时按 Ctrl+Alt+Esc(部分编辑器/终端用)会莫名卡一下穿越。
        """
        if self._edge is not None and self._edge.is_outside:
            self._force_release("用户按下 Ctrl+Alt+Esc")

    def _acquire_control(self) -> None:
        """夺取控制权: 吞掉本机物理输入 + 隐藏本机光标。

        ★ 顺序: **先开 relay 再隐藏光标**。
          若先隐藏光标, 中间那一小段(直到 relay 生效)用户按下的点击仍会
          落到本机 —— 用户会看到"偶尔点错一下"。relay 是纯布尔标志, 钩子
          线程下一个事件就生效, 快得多。

        ★ 与 Deskflow 一致: 切换只用 `setMode()`(我们对应 `set_relay`),
          **不重装钩子**。历史注记: 代码里曾写过"每次穿越都要重装以夺回
          链首", 那是基于"后装的先调用、前面吞掉链就断"的错误推论 ——
          跨进程实测( tools/audit_chain_claim.py )证明: 即使另一个进程装了
          "吞一切"的键盘钩子, 我们的回调**照样被调用**。真实故障原因是
          **本进程窗口持有前台焦点**(见 gui._release_focus_for_hook)。
        """
        if self._hook is not None:
            self._hook.set_relay(True)
        # ★★ 穿越开始时**强制复检键盘钩子** —— 这是最需要键盘可用的时刻。
        #   为什么必须在这里: 钩子可用性取决于"本进程窗口是否前台"。
        #   GUI 启动时窗口有焦点(自检必然失败), 之后 GUI 放弃焦点才恢复;
        #   若只信启动时的结论, 键盘会一直被误禁用(上一版的回归:
        #   用户报"键盘跨屏完全失效")。
        #
        # ★★★ 但必须**异步**做(2026-10-22 修, 竞态实测):
        #   本方法运行在**原始输入回调线程**上(越界判定 -> _acquire_control)。
        #   而 `probe_keyboard_hook()` 要注入一个探针键并**轮询等最多 0.5s**。
        #   同步调用等于每次越界都把主机的输入线程堵住最多半秒: 期间物理
        #   鼠标的 RawInput 事件全在窗口队列里排队, 堵完后一次性倒给副机
        #   —— 副机看到的是"指针先僵住、然后猛地窜一段", 一串陈旧位移
        #   把光标怼到回传边上 -> 立刻被交回主机。用户报的"指针被往主机
        #   那一侧边缘吸"主要就是这条链。
        #   异步不影响判定: 穿越期间是否转发键盘由 `_keyboard_relay_ready`
        #   的"hider 已隐藏"分支直接放行, 不依赖本结论; 本结论只用于
        #   **非穿越期**的泄漏判断(它有 KBD_VERIFY_TTL_S 有效期, 到点自会重测)。
        self._kick_kbd_verify()
        if self._hider is not None:
            self._hider.hide()

    def _kick_kbd_verify(self) -> None:
        """在后台线程里复核键盘钩子(绝不在输入线程上阻塞)。

        同一时刻只允许一个在跑: `probe_keyboard_hook` 的注入会改动全局计数,
        两个线程同时探针会互相干扰(把 `before` 快照算错, 误报"可达")。
        """
        self._spawn_kbd_verify(self._reverify_keyboard_hook)


    def _release_control(self) -> None:
        """交还控制权: **立刻放行**本机物理输入, 并恢复光标。

        ★ relay 必须最先关: 它是"用户能不能用自己的键鼠"的开关。若先做别的
          (恢复光标、发事件…), 中间用户的所有点击/按键都还在被吞 —— 表现
          为"鼠标回来了但点不动"。
        """
        if self._hook is not None:
            self._hook.set_relay(False)
        # ★★★ 先恢复光标, **再**去复核键盘钩子(2026-10-22 修):
        #   探针要注入一个键并轮询等结果(最多 0.5s, 实测常在 10~20ms)。
        #   它原先卡在这里同步执行, 于是**每次交还控制权, 光标都要多等
        #   一次探针才显示、回程绝对定位也被推迟** —— 用户看到的是
        #   "已经推回来了, 指针却愣一下才出现"。
        #   而且探针的判据与"本进程窗口是不是前台"强相关, 在 hider 还
        #   占着前台时问它, 结论本身就不可信。放到恢复光标之后、后台线程
        #   里问, 三个问题一起解决。
        self._show_cursor()
        self._verify_kbd_on_release()

    def _verify_kbd_on_release(self) -> None:
        """交还控制权后**在后台**复核键盘钩子; 失败则重装一次(下一轮生效)。

        ★ 探针判据(`probe_keyboard_hook`)不依赖用户打字: 注入一个带
          TAG 的 0xE8(未分配虚拟键, 无字符/无指示灯副作用), 看**自己的
          回调**能否看到它。TAG 分支在任何过滤之前, 所以回调活着就计数。
          返回 None(注入本身失败)时**不报警** —— 判据不可用时不制造噪声。

        ★ 为什么要复核(历史原因保留): 穿越期间键盘钩子死亡的两条路 ——
          被系统静默摘除(约 300ms 超时, 句柄不置空) / 被别的程序先吞掉
          (键到不了我们, RawInput 也看不到) —— 都不会触发现有检测, 于是
          用户穿越时"键同时落两台", 退回后日志里却一个字都没有。
        """
        if self._hook is None or not self._hook.installed:
            return
        self._spawn_kbd_verify(self._verify_kbd_on_release_now)

    def _spawn_kbd_verify(self, target) -> None:
        """起一个后台自检线程(同一时刻只允许一轮)。"""
        with self._kbd_verify_lock:
            t = self._kbd_verify_thread
            if t is not None and t.is_alive():
                return          # 已有一轮在跑: 同一时刻只需要一个结论
            t = threading.Thread(target=target, daemon=True,
                                 name="KbdHookVerify")
            self._kbd_verify_thread = t
            t.start()

    def _verify_kbd_on_release_now(self) -> None:
        """`_verify_kbd_on_release` 的实际工作体(后台线程入口)。"""
        # 稍等焦点归还: hider 收起前台是异步的(消息投递到 hider 线程),
        # 探针在"本进程仍占前台"时必然问出假失败。
        time.sleep(0.15)
        h = self._hook
        if h is None or not h.installed:
            return
        try:
            probe = h.probe_keyboard_hook()
        except Exception:  # noqa: BLE001
            return
        if probe is False:
            print("[server] !! 上一轮穿越期间键盘钩子失效(回调从未被调用)。\n"
                  "         症状: 键同时落到主机和副机(主机输入法被唤起)。\n"
                  "         已自动重装; 若反复出现, 说明有别的程序在抢"
                  "键盘钩子\n"
                  "         (Deskflow/Synergy/输入法热键/宏工具) —— "
                  "请让它退出, 或以管理员运行 Com Bridge。", flush=True)
            h.request_reinstall()

    def _ensure_hook(self) -> None:
        """安装低级钩子; 失败则退化为"每事件注入钉住"的旧路径。

        ⚠ 退化路径的正确性远不如钩子: 它靠注入抵消物理位移, 存在丢像素与
          沿边缘漂移的风险(见 _pin_cursor_to_edge 的历史注释)。但"钩子被
          别的程序占用"时它仍是可用的兜底, 总比完全不工作好。
        """
        self._hook = LowLevelHook(
            name="ServerHook",
            # ★ 键盘事件由钩子自产(被 LL 键盘钩子吞掉的按键不会产生
            #   RawInput, 实测 tools/diag_hook_kbd_rawinput.py) —— 把
            #   InputEvent 字段直接喂进 _on_input_event, 复用同一条
            #   转发链(穿越态门槛/计数都在那里)。
            on_key_event=self._on_hook_key_event,
            # ★ 逃生通道: 穿越期间 Ctrl+Alt+Esc 强制作废(见 hook.PanicCombo)
            on_panic=self._on_user_abort,
        )
        if self._hook.start():
            self._pin_fallback = False
            print("[server] 低级钩子已安装: 穿越期间将吞掉本机键鼠输入",
                  flush=True)
            # ★★ 立刻自检**键盘钩子是否真的能被调用**(见 probe_keyboard_hook)。
            #   WH_KEYBOARD_LL 是链式钩子, 别的程序排在前面并返回非零时,
            #   我们的回调一次都不会执行 —— 用户症状是"鼠标计数正常、
            #   键盘恒为 0, 且主机输入法被同步唤起"。在启动时就说清楚,
            #   比让用户在 GUI 上猜好得多。
            probe = self._hook.probe_keyboard_hook()
            # ★★ 记录自检结果: 键盘转发依赖它(见 _keyboard_relay_ready)。
            #   失败时**禁止转发键盘**, 避免"两台同时收到按键"。
            #   ⚠ 但这只是**当时的**结论: GUI 启动时窗口有焦点 -> 自检
            #   必然失败; 之后 GUI 主动放弃焦点, 钩子会恢复。所以结论
            #   带有效期(KBD_VERIFY_TTL_S), 由 _keyboard_relay_ready 复检。
            self._kbd_hook_verified = (probe is True)
            self._kbd_verify_mono = time.monotonic()
            if probe is False:
                # ★★ 明确报告**当前是否提权** —— 这决定处置方式:
                #   未提权: Deskflow 服务(SYSTEM)的钩子天然在我们之前,
                #           提权后可能解决(用户实测选择此方案)。
                #   已提权: 提权都无效 -> 只能停掉 Deskflow 服务。
                elevated = self._is_elevated()
                print("[server] !! 键盘钩子自检失败: 我们的键盘回调**从未被"
                      "调用**。\n"
                      f"         当前进程: {'已提权' if elevated else '未提权'}\n"
                      "         原因: WH_KEYBOARD_LL 是链式钩子, 有别的程序"
                      "排在我们前面并吞掉了按键\n"
                      "         (最常见: Deskflow/Synergy/Barrier 的 core "
                      "以 SYSTEM 运行,\n"
                      "          它的钩子天然高于任何用户态程序)。\n"
                      "         后果: 键盘不会被吞, 键会**同时**落到主机和"
                      "副机(主机输入法被唤起)。\n"
                      + ("         处置: 提权已无效, 请**停掉 Deskflow 服务**"
                         "(管理员):\n"
                         "           Stop-Service Deskflow\n"
                         "           Set-Service Deskflow -StartupType Disabled"
                         "\n"
                         if elevated else
                         "         处置: 二选一 ——\n"
                         "           a) 以管理员运行 Com Bridge"
                         "(GUI 里点「管理员自启…」)\n"
                         "           b) 停掉 Deskflow 服务(管理员):\n"
                         "              Stop-Service Deskflow\n"
                         "              Set-Service Deskflow "
                         "-StartupType Disabled\n"),
                      flush=True)
            elif probe is True:
                print("[server] 键盘钩子自检通过(回调可达)", flush=True)
        else:
            self._pin_fallback = True
            print(f"[server] !! 低级钩子安装失败(err={self._hook.last_error}), "
                  f"退化为注入钉住模式。\n"
                  f"         此模式下穿越期间本机点击仍会落到主机。\n"
                  f"         常见原因: 被其它键鼠共享软件(Deskflow/Synergy 等)\n"
                  f"         占用, 或本进程被安全软件限制 —— 请先退出它们\n"
                  f"         再启动 Com Bridge。", flush=True)

    @staticmethod
    def _is_elevated() -> bool:
        """当前进程是否已提权(仅用于诊断信息, 不改变行为)。

        ★ 唯一实现放在 `elevated.is_elevated()`, 这里只是薄委托 ——
          此前 server / cli / elevated 三处各写了一遍同样的
          OpenProcessToken + GetTokenInformation 调用。
        """
        try:
            from .elevated import is_elevated
            return is_elevated()
        except Exception:  # noqa: BLE001
            return False

    def _on_hook_key_event(self, ev_type, flags, code, a, b, ts_us) -> None:
        """钩子自产的键盘事件入口(见 _ensure_hook)。

        组装成 InputEvent 后走与 RawInput 完全相同的 _on_input_event,
        转发门槛(`is_outside`)与计数都在那里, 不在此重复判定。
        """
        try:
            self._on_input_event(InputEvent(ev_type, flags, code, a, b,
                                            ts_us))
        except Exception as exc:  # 钩子线程内绝不让异常外泄
            print(f"[server] 钩子键盘事件处理失败: {exc}", flush=True)

    def _on_raw_input_event(self, ev: InputEvent) -> None:
        """RawInput 事件入口(现在**只有鼠标**走这里)。

        ★★★ 历史注记(2026-10-19, 已被物理按键实测推翻, 不要恢复):
          这里原本还有一段"键盘钩子失效检测": 判据是"relay=True 时若
          RawInput 仍报来键盘事件, 说明钩子没吞住"。它还配了一套
          `_start_kbd_heal()` 重装自愈。

          但这套机制的**前提是错的**, 而且它本身就是故障放大器:
            1. 我们注册键盘 RawInput(RIDEV_INPUTSINK) 之后, 只要 hider
               抢前台隐藏光标, 本进程的 WH_KEYBOARD_LL **就再也收不到
               任何键**(物理键实测 kbd_calls +0, 而 RawInput +36) ——
               也就是说"RawInput 有键、钩子没键"根本不代表钩子坏了,
               它只是这个组合的**正常表现**。
            2. 于是检测疯狂触发重装, 而重装改变不了前台归属 -> 必然无效
               (用户日志里"自愈第 1/2/3 次未生效"+"暂停键盘转发"就是它)。
            3. 更糟: 它把键盘转发**自己停掉**了, 而当时唯一的问题只是
               我们多注册了一条不该注册的键盘 RawInput。

          修复方式不是改判据, 而是**不再注册键盘 RawInput**
          (见 RawInputListener.register_keyboard)。症状(键不吞/不转发)
          的根因随之消失, 这段检测自然也不需要了。

        ⚠ 若将来要恢复某种"钩子存活检测", 判据必须是**钩子自己的回调计数**
          是否推进(见 gui 状态行 / probe_keyboard_hook), 绝不能用
          "RawInput 是否收到键盘"来推断。
        """
        self._on_input_event(ev)

    def _start_kbd_heal(self) -> None:
        """启动(或复用)键盘钩子自愈线程 —— 见 _on_raw_input_event 的说明。"""
        with self._kbd_heal_lock:
            if self._kbd_healing:
                return                      # 已有一轮在跑, 不重复启动
            self._kbd_healing = True
        t = threading.Thread(target=self._kbd_heal_loop, daemon=True,
                             name="KbdHookHeal")
        t.start()

    def _kbd_heal_loop(self) -> None:
        """重装键盘钩子并**验证**回调真的可达; 失败则退避重试。

        ★ 为什么要"验证"而不是"重装完就算": 重装可能因为句柄竞争、
          时机窗口等原因没真正生效。用 `probe_keyboard_hook()`
          (注入带 TAG 的键, 看自己的回调能否看到)可以**确定性**地判断
          是否真的修好了 —— 不靠猜。
        """
        try:
            for attempt in range(1, self.KBD_HEAL_ATTEMPTS + 1):
                if self._stop.is_set():
                    return
                self._hook.request_reinstall()
                time.sleep(self.KBD_HEAL_INTERVAL_S)
                probe = self._hook.probe_keyboard_hook()
                if probe is True:
                    self._kbd_hook_verified = True
                    print(f"[server] 键盘钩子自愈成功(第 {attempt} 次重装后"
                          f"回调可达) -> 恢复键盘转发", flush=True)
                    return
                if probe is None:
                    # 注入失败 -> 判据不可用, 不要据此报"失败"
                    print("[server] 键盘钩子自愈: 自检无法判定(注入失败), "
                          "已重装但未验证", flush=True)
                    return
                print(f"[server] 键盘钩子自愈第 {attempt} 次未生效, 继续重试",
                      flush=True)
            # ★ 自愈彻底失败: 标记为不可达 -> 停止转发键盘(只转发鼠标),
            #   避免"键同时落两台"。用户至少能在主机正常打字。
            self._kbd_hook_verified = False
            print("[server] !! 键盘钩子自愈失败: 重装多次后回调仍不可达。\n"
                  "         >>> 已**暂停键盘转发**(键只落在主机, 不再同时"
                  "送副机) <<<\n"
                  "         鼠标转发不受影响, 仍可跨屏操作副机。\n"
                  "         说明有别的程序先收到了按键 —— 请退出 "
                  "Deskflow/Synergy/Barrier、\n"
                  "         输入法全局热键组件、游戏辅助/宏工具后重启 "
                  "Com Bridge。", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[server] 键盘钩子自愈线程异常: {exc}", flush=True)
        finally:
            with self._kbd_heal_lock:
                self._kbd_healing = False

    def _is_own_injection(self) -> bool:
        """最近是否有**我们自己**的注入移动(见钩子健康自检的容差说明)。

        复用 InputInjector 的注入时间戳 —— 与 CursorHider 判"注入 vs 物理"
        用的是同一个信号, 避免两处各写一套判据。
        """
        return InputInjector.last_move_age() <= self.HOOK_PROBE_INJECT_WINDOW_S

    def _edge_pin_point(self, x: int, y: int) -> tuple[int, int]:
        """由交接位置算出**钉住点**: 边缘坐标 + 交接那一刻的切向坐标。

        ★ 钉住点只在越界那一刻计算一次(照搬 Deskflow 存 m_xCenter/m_yCenter
          的做法)。此后每个事件都钉回**同一个点** —— 若每个事件都传读回的
          当前切向坐标, 垂直方向等于没有约束, 顶墙猛推时光标会沿边缘一路
          爬到角落(用户实测的"爬到右上角")。
        """
        r = self._edge.self_rect if self._edge else virtual_desktop()
        if self.side == "right":
            return (r.right - 1, y)
        if self.side == "left":
            return (r.left, y)
        if self.side == "top":
            return (x, r.top)
        return (x, r.bottom - 1)

    def _pin_cursor_to_edge(self) -> None:
        """把本机光标钉回**交接时定下的固定点**(注入绝对定位)。

        ★ 这是"两台鼠标协同一体"的前提(照搬 Deskflow 的模型):
          越界期间主机光标被钉住, 用户在副机上可以双向自由移动,
          回程只由"副机光标抵达回传边"触发。

        被用户踩到的错误版本:
          * 用 clamp_to_edge(x,y) 想"钉住"—— 它只把坐标限制在屏幕内,
            对已在屏内的坐标原样返回, 是**无效空操作**(实测从未改变过
            光标位置), 只会制造回声事件(表现为边缘疯狂弹回);
          * 完全不注入 —— 主机光标随物理鼠标自由移动, 用户在副机上往回
            拉时主机光标也跟着回移, 一超过迟滞带主机就退出穿越态
            (表现为副机只能朝一个方向移动);
          * 钉"读回的当前切向坐标" —— 切向零约束, 光标沿边缘爬到角落。
        """
        if self._edge is None:
            return
        if self._pin_point is None:
            self._pin_point = self._edge_pin_point(*get_cursor_pos())
        InputInjector.move_absolute(*self._pin_point)

    def _send_place_event(self, x: int | None = None,
                          y: int | None = None) -> None:
        """交接时告知副电脑"把光标放到这里"(绝对定位)。

        ★ 为什么必须绝对定位(照搬 Deskflow 的 `enter(x, y)`):
          只发相对位移时, 副机光标从"它上次停在哪"开始累积, 进入位置完全
          不可控。实测(副机双屏): 主机一直向右推, 副机光标却停在**右屏的
          右边缘**蹭, 而不是从最左边缘进入。
          Deskflow 在 `switchComputer()` 里调用 `m_active->enter(x, y, ...)`
          把坐标**绝对告知**目标机, 这是跨屏位置可控的前提。

        位置换算: 沿跨越边的切向按**比例**传递(见 input.position_fraction),
        这样两台分辨率不同也能从同一相对高度进入。
        x, y: 交接那一刻的光标位置(判定越界时读到的) —— 用它而不是再读一次,
        避免被随后的钉住注入污染。
        side: 副机位于主电脑哪一侧 -> 副机应从**对侧**的边缘进入。
        """
        if self._input_tx is None:
            return
        try:
            from .input import position_fraction
            if x is None or y is None:
                x, y = get_cursor_pos()
            f = position_fraction(x, y, self.side)
            # 换算成 0..65535 的定点值承载(协议里 a/b 是 int32;
            # 副机按自己的桌面尺寸还原成像素)
            frac_q = int(round(min(max(f, 0.0), 1.0) * 65535))
            ev = InputEvent(EV_MOUSE_PLACE, 0, 0, frac_q, 0, now_us())
            self._input_tx.send(ev)
            # ★ 打印交接位置与比例: 用户报"方位没有等比例对齐"时, 这一行
            #   能直接区分是**主机算错比例**还是**副机还原错像素** ——
            #   主机侧的 (x,y)/f 与副机侧的落点日志对照即可定位。
            _d = virtual_desktop()
            print(f"[server] 越界交接 -> 本机 ({x},{y}) 比例 f={f:.4f} "
                  f"[本机桌面 {_d.width}x{_d.height}, 跨越边={self.side}]",
                  flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[server] 发送进入定位失败: {exc}", flush=True)

    def _send_modifier_snapshot(self) -> None:
        """把主机当前按下的修饰键快照发给副机(照搬 Deskflow toggleMask)。

        EV_MOUSE_MODS, code = MODFLAG_* 位掩码。副机据此注入还按着的修饰键,
        用户"按住 Shift 跨屏"时副机的选择/拖拽行为才与主机一致。
        """
        if self._input_tx is None:
            return
        try:
            user32 = ctypes.windll.user32
            mask = 0
            if user32.GetKeyState(VK_SHIFT) & 0x8000:
                mask |= MODFLAG_SHIFT
            if user32.GetKeyState(VK_CONTROL) & 0x8000:
                mask |= MODFLAG_CTRL
            if user32.GetKeyState(VK_MENU) & 0x8000:
                mask |= MODFLAG_ALT
            if (user32.GetKeyState(VK_LWIN) | user32.GetKeyState(VK_RWIN)) \
                    & 0x8000:
                mask |= MODFLAG_WIN
            ev = InputEvent(EV_MOUSE_MODS, 0, mask, 0, 0, now_us())
            self._input_tx.send(ev)
        except Exception as exc:  # noqa: BLE001
            print(f"[server] 发送修饰键快照失败: {exc}", flush=True)

    def _is_outward(self, dx: int, dy: int) -> bool:
        """位移是否指向副电脑(穿越方向)。"""
        if self.side == "right":
            return dx > 0
        if self.side == "left":
            return dx < 0
        if self.side == "top":
            return dy < 0
        return dy > 0

    def _is_inward(self, dx: int, dy: int) -> bool:
        """位移是否指向本机屏内(退出穿越态的方向)。"""
        if self.side == "right":
            return dx < 0
        if self.side == "left":
            return dx > 0
        if self.side == "top":
            return dy > 0
        return dy < 0

    def _moving_back(self, dx: int, dy: int) -> bool:
        """兼容旧接口名: 相对位移是否指向"回到主电脑屏幕内"。

        ★ 仅被 tools/sim_input_flow.py(状态机模拟)使用; 生产代码用
          `_is_inward`。保留是为了不破坏该模拟工具的语义。
        """
        return self._is_inward(dx, dy)

    def _stop_cursor_hider(self) -> None:
        hider = getattr(self, "_hider", None)
        if hider is not None:
            hider.show()
            hider.stop()
            self._hider = None

    def _on_return_event(self, ev: InputEvent) -> None:
        """agent 判定"用户想回主电脑"后把事件送回来, 在这里注入本机。

        ★ 收到任何回程事件都意味着**控制权已经交还**: 副电脑那边光标抵达
          回传边且继续朝主电脑移动, 由它决定何时回来。主电脑在此释放穿越
          态并重置兜底计时, 之后的光标由本机物理鼠标接管。
          这样"谁在控制"只有一个判定者(光标所在的那一端), 不会出现两端
          各按自己的计时器抢控制权 —— 那是"边缘弹回"的另一种成因。
        """
        if self._edge is not None and self._edge.is_outside:
            self._edge.reset()
            self._pin_point = None      # 下次交接重新定点
            # 自检复位: 新一轮穿越重新建立"光标不动"基准
            self._last_probe = get_cursor_pos()
            # ★ 交还控制权: **必须先放行本机物理输入**(见 _release_control),
            #   否则回程后用户的点击/按键还在被钩子吞掉, 表现为"鼠标回来了
            #   但点不动、打不了字"。
            self._release_control()
            print("[server] 收到回程信号 -> 控制权交回本机", flush=True)

        inj = InputInjector
        try:
            if ev.type == EV_MOUSE_RETURN:
                # ★★ BUG3② 修复: 副机报来"它是从哪个相对高度回去的"。
                #   必须**绝对定位**(而不是靠相对位移累积): 穿越期间本机
                #   光标被钉在进入时的位置, 相对累积的落点永远是当初跳过去
                #   的那一点, 用户在副机上的实际位置完全丢失。
                #   比例沿跨越边的切向, 与去程 _send_place_event 对称。
                f = ev.a / 65535.0
                px, py = fraction_to_pixel(f, self.side)
                inj.move_absolute(px, py)
                print(f"[server] 回程定位 -> ({px},{py}) "
                      f"[边={self.side}, f={f:.3f}]", flush=True)
            elif ev.type == EV_MOUSE_MOVE_REL:
                inj.move_relative(ev.a, ev.b)
            elif ev.type == EV_MOUSE_BUTTON:
                inj.button(ev.code, bool(ev.flags & MB_DOWN))
            elif ev.type == EV_MOUSE_WHEEL:
                inj.wheel(ev.b)
            elif ev.type == EV_KEY:
                inj.key(ev.code, ev.a, bool(ev.flags & KEY_DOWN),
                        bool(ev.flags & KEY_EXTENDED))
            self.stats["events_injected"] += 1
        except OSError:
            pass

    # -- 停止 ----------------------------------------------------------

    def _show_cursor(self) -> None:
        """恢复本机光标(回程或兜底退出穿越态时)。"""
        if self._hider is not None:
            self._hider.show()

    def stop(self) -> None:
        self._stop.set()
        disable_high_res_timer()
        # ★★ 退出前必须**先放行物理输入**再销毁 hider:
        #   若钩子还处于 relay(吞事件)状态而进程退出, 用户会面对一台
        #   "键鼠全无响应"的电脑 —— 最坏的用户体验, 且极难自查。
        #   钩子本身随进程销毁会被系统自动摘掉, 但显式关掉更稳妥。
        if self._hook is not None:
            self._hook.set_relay(False)
            self._hook.stop()
            self._hook = None
        self._show_cursor()
        self._stop_cursor_hider()
        # ★ 剪贴板资源: 先停监听器(否则它还在读剪贴板), 再关收发
        if self._clip_sync is not None:
            try:
                self._clip_sync.stop()
            except Exception:  # noqa: BLE001
                pass
            self._clip_sync = None
        for obj in (self._adv, self._ctrl, self._audio_rx, self._input_rx,
                    self._input_tx, self._clip_rx, self._clip_tx):
            if obj is None:
                continue
            if hasattr(obj, "stop"):
                obj.stop()
            elif hasattr(obj, "close"):
                obj.close()
        self._clip_rx = None
        self._clip_tx = None
        if self._listener is not None:
            self._listener.stop()
        for obj in (self._cap, self._ren):
            if obj is not None:
                obj.stop()
        print(
            f"[server] 停止: local={self.stats['local_frames']} "
            f"peer={self.stats['peer_frames']} rendered={self.stats['rendered_frames']} "
            f"silence={self.stats.get('silence_frames', 0)} "
            f"forwarded={self.stats['events_forwarded']}",
            flush=True,
        )
        for stream in (self._local, self._peer):
            if stream is not None:
                print(f"[server] {stream.stats()}", flush=True)
