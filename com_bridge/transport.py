"""UDP 传输层 + 局域网发现(广播信标)。

设计:
  - 发现: server 在 DISCOVERY_PORT 上周期性广播信标(JSON), agent 收到后
    即知 server 地址与能力(采样率/位深/通道)。零配置。
  - 音频: UDP 单播, 每包 = AudioHeader(15B) + PCM。seq 用于乱序/丢包检测。
  - 输入: UDP 单播, 每包 = InputEvent(24B)。
  - 控制: TCP 单连接, 时钟同步(NTP 四时间戳) + 配置协商 + 状态上报。
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from dataclasses import dataclass, field

from .clock import ClockSync, now_us
from .protocol import (
    AUDIO_PORT,
    CLIPBOARD_PORT,
    CODE_TO_SIDE,
    CONTROL_PORT,
    DISCOVERY_PORT,
    EV_HEARTBEAT,
    HEARTBEAT_SIZE,
    INPUT_PORT,
    INPUT_HEADER_SIZE,
    SIDE_CODE_NONE,
    SIDE_TO_CODE,
    AudioHeader,
    InputEvent,
)

BEACON_MAGIC = "COMBRIDGE1"
BEACON_INTERVAL_S = 1.0

# 控制通道协议版本(照搬 Deskflow 的 Hello/HelloBack 协商):
#   server 发 "HELLO <name> <major> <minor>"
#   client 回 "HELLO <name> <major> <minor>"
# 规则: **major 必须相等**(不兼容), minor 取两端较小值(向后兼容)。
# 之前没有版本协商: 两端程序版本不同时, 行为差异(如新的 SHAPE 消息)会
# 造成静默错位 —— server 等一个永远不会来的消息, 而对端毫无感知。
PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 1


# ---------------------------------------------------------------- 发现

@dataclass
class Beacon:
    host: str
    port: int
    role: str
    name: str
    caps: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "magic": BEACON_MAGIC,
                "host": self.host,
                "port": self.port,
                "role": self.role,
                "name": self.name,
                "caps": self.caps,
            }
        )

    @classmethod
    def from_json(cls, text: str) -> "Beacon | None":
        try:
            d = json.loads(text)
        except (ValueError, TypeError):
            return None
        if d.get("magic") != BEACON_MAGIC:
            return None
        return cls(
            host=d.get("host", ""),
            port=int(d.get("port", CONTROL_PORT)),
            role=d.get("role", "server"),
            name=d.get("name", ""),
            caps=d.get("caps", {}) or {},
        )


class BeaconAdvertiser(threading.Thread):
    """server 侧: 周期性 UDP 广播信标。"""

    def __init__(self, name: str, caps: dict | None = None, port: int = CONTROL_PORT,
                 interval_s: float = BEACON_INTERVAL_S):
        super().__init__(daemon=True, name="BeaconAdvertiser")
        self.name = name
        self.caps = caps or {}
        self.port = port
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._sock: socket.socket | None = None

    def run(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        host = _outbound_ip() or "127.0.0.1"
        beacon = Beacon(host=host, port=self.port, role="server", name=self.name,
                        caps=self.caps)
        payload = beacon.to_json().encode("utf-8")
        while not self._stop.wait(self.interval_s):
            try:
                self._sock.sendto(payload, ("255.255.255.255", DISCOVERY_PORT))
            except OSError:
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


class BeaconListener:
    """agent 侧: 接收广播信标。"""

    def __init__(self, port: int = DISCOVERY_PORT, timeout_s: float = 3.0):
        self.port = port
        self.timeout_s = timeout_s
        self._sock: socket.socket | None = None

    def listen_once(self) -> Beacon | None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _bind_exclusive(s, self.port)
        s.settimeout(self.timeout_s)
        try:
            data, addr = s.recvfrom(2048)
            beacon = Beacon.from_json(data.decode("utf-8", errors="replace"))
            if beacon is not None and not beacon.host:
                beacon = Beacon(host=addr[0], port=beacon.port, role=beacon.role,
                                name=beacon.name, caps=beacon.caps)
            return beacon
        except (socket.timeout, TimeoutError, OSError):
            return None
        finally:
            s.close()


def _outbound_ip() -> str | None:
    """用一个 UDP "连接"拿到本机在默认路由上的 IP(不真的发包)。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def local_ips() -> list[str]:
    """本机所有 IPv4 地址(默认路由地址优先, 其余按系统枚举顺序)。

    ★ 会过滤掉**虚拟网卡地址**(Tailscale/ZeroTier 等的 CGNAT 段、以及
      169.254 链路本地段)。理由: 这些地址只在对应的 VPN 内部可达, 对端
      若不在同一 VPN 里就完全不可用。实测踩到: agent 用 getsockname() 取到
      Tailscale 的 100.95.120.31 上报给主电脑, 主电脑往那里发输入全部丢失。
      需要走 VPN 的用户应显式配置地址, 而不是靠自动选择。
    """
    out: list[str] = []

    def _add(ip: str) -> None:
        if ip and ip not in out and not _is_virtual_ip(ip):
            out.append(ip)

    _add(_outbound_ip() or "")
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET, socket.SOCK_DGRAM):
            _add(info[4][0])
    except (socket.gaierror, OSError):
        pass
    return out


def _is_virtual_ip(ip: str) -> bool:
    """是否是虚拟网卡/VPN 的地址(对普通 LAN 对端不可达)。"""
    try:
        parts = [int(x) for x in ip.split(".")]
    except ValueError:
        return False
    if len(parts) != 4:
        return False
    a, b = parts[0], parts[1]
    # 100.64.0.0/10 —— CGNAT, Tailscale 默认段
    if a == 100 and 64 <= b <= 127:
        return True
    # 169.254.0.0/16 —— 链路本地(自动私有地址)
    if a == 169 and b == 254:
        return True
    return False


def _local_addresses() -> set[str]:
    """本机所有可代表"我自己"的地址(含回环)。

    用于识别"对端地址其实是本机"这种配置错误 —— 它会让输入事件被发回
    自己, 而日志看起来一切正常(见 Server._ensure_input_tx 的说明)。
    还包含 "localhost" 与主机名, 因为用户可能按名字填。
    """
    addrs = set(local_ips())
    addrs.update({"127.0.0.1", "localhost", "0.0.0.0", "::1"})
    try:
        addrs.add(socket.gethostname())
    except OSError:
        pass
    return addrs


# ---------------------------------------------------------------- 音频 UDP

class AudioSender:
    def __init__(self, host: str, port: int = AUDIO_PORT):
        self.addr = (host, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.seq = 0

    def send(self, pcm: bytes, rate: int, bits: int, channels: int,
             chan_mask: int, ts_us: int) -> None:
        self.seq = (self.seq + 1) & 0xFFFF
        hdr = AudioHeader(rate=rate, bits=bits, channels=channels,
                          chan_mask=chan_mask, seq=self.seq, ts_us=ts_us)
        try:
            self._sock.sendto(hdr.pack() + pcm, self.addr)
        except OSError:
            pass

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def _bind_exclusive(sock: socket.socket, port: int) -> None:
    """独占绑定端口。

    **Windows 上不能设 SO_REUSEADDR**: 它的语义与 Unix 不同, 单独它就允许
    另一个进程绑定到同一端口, 两个实例会静默瓜分数据报(音频表现为断续/爆音,
    且完全不报错, 极难排查)。这里用 SO_EXCLUSIVEADDRUSE 明确独占, 端口被
    占用时直接抛 OSError, 让上层把"已在运行"的错误显示给用户。
    """
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        except OSError:
            pass
    sock.bind(("0.0.0.0", port))


class _BoundThread(threading.Thread):
    """后台接收线程的公共基类: 绑定失败要能被前台看见。

    绑定发生在 run() 里(线程内), 若直接抛异常, 线程会静默死亡而
    Server.start() 早已返回成功 —— 界面显示"运行中"但实际没有控制通道,
    非常难排查。所以把绑定提前到 start() 之前完成(构造后由 start_thread
    调用), 失败时立刻抛给调用方。
    """

    def __init__(self, name: str, port: int, kind: int):
        super().__init__(daemon=True, name=name)
        self.port = port
        self._kind = kind            # socket.SOCK_DGRAM / SOCK_STREAM
        self._sock: socket.socket | None = None
        self._stop = threading.Event()

    def bind_now(self) -> None:
        """在调用方线程内完成绑定, 失败直接抛 OSError。"""
        s = socket.socket(socket.AF_INET, self._kind)
        try:
            _bind_exclusive(s, self.port)
        except OSError as exc:
            s.close()
            raise OSError(
                f"端口 {self.port} 绑定失败({exc.strerror or exc}); "
                f"可能已有另一个 Com Bridge 实例在运行"
            ) from exc
        self._sock = s

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


class AudioReceiver(_BoundThread):
    """agent 侧: 收音频帧并回调 (header, pcm)。"""

    def __init__(self, port: int = AUDIO_PORT, on_frame=None):
        super().__init__("AudioReceiver", port, socket.SOCK_DGRAM)
        self._on_frame = on_frame
        self.last_seq = -1
        self.lost = 0
        self.dupes = 0
        self.resyncs = 0
        self.frames_received = 0

    def run(self) -> None:
        s = self._sock
        if s is None:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            _bind_exclusive(s, self.port)
            self._sock = s
        s.settimeout(0.5)
        while not self._stop.is_set():
            try:
                data, _addr = s.recvfrom(65535)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            if len(data) < 15:
                continue
            try:
                hdr = AudioHeader.unpack(data)
            except ValueError:
                continue
            pcm = data[15:]
            self._track_seq(hdr.seq)
            self.frames_received += 1
            if self._on_frame is not None:
                try:
                    self._on_frame(hdr, pcm)
                except Exception:  # noqa: BLE001
                    pass

    # 判定"发送端重启"的阈值: 序号向后跳这么多以上就认为是新流, 而不是丢包
    RESYNC_THRESHOLD = 512

    def _track_seq(self, seq: int) -> None:
        """统计丢包/重复/发送端重启。

        ★ 不能直接把序号差当丢包数。序号是 16 位环绕的, 发送端(agent)一旦
          重启, 新序号从 1 开始, 而接收端还停在旧序号(比如 3897)上, 于是
          `(1 - 3898) & 0xFFFF = 61638` 被记成"丢了 6 万个包" —— 界面显示
          丢包十几万, 让人以为网络极差, 实际一个包都没丢。
          区分办法: 向后跳超过 RESYNC_THRESHOLD 视为发送端重启(记 resync),
          只有小幅前向跳才计入 lost。
        """
        if self.last_seq < 0:
            self.last_seq = seq
            return
        delta = (seq - self.last_seq) & 0xFFFF
        if delta == 0:
            self.dupes += 1
        elif delta == 1:
            pass                                   # 正常连续
        elif delta <= self.RESYNC_THRESHOLD:
            self.lost += delta - 1                 # 真实丢包(小幅前向跳)
        else:
            # 大幅跳变: 发送端重启或严重乱序, 重新对齐而不是虚报丢包
            self.resyncs += 1
        self.last_seq = seq

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


# ---------------------------------------------------------------- 剪贴板 UDP

class ClipboardSender:
    """把剪贴板内容按 MTU 分块发到对端的 CLIPBOARD_PORT(参考 Deskflow)。

    ★ 为什么要自己分块(而不是发一个大包):
      Deskflow 用 ClipboardChunk 做 start/data/end 分块传输。我们必须
      照做, 因为 UDP 包一旦超过 MTU(1500)就会被 IP 分片, 而 **任一分片
      丢失整个数据报就作废** —— 这在音频上已经吃过一次亏(见 agent 的
      SEND_FRAMES=120)。剪贴板图片动辄几 MB, 不分块必然丢。
    """

    def __init__(self, host: str, port: int = CLIPBOARD_PORT):
        self.addr = (host, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # 剪贴板是"尽力而为": 丢一块就整份放弃(接收端会拒绝半份内容),
        # 不重传 —— 重传会让"复制"操作卡住, 而且用户再复制一次即可。
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF,
                                  1 << 20)
        except OSError:
            pass
        self.sent_chunks = 0
        self.failed = 0

    # ★★★ 发送限速(实测必需): 不限速时 8MB 内容的 8391 个包在 0.2 秒内
    #   全塞出去, 接收端 UDP 缓冲直接溢出 —— 实测 8MB 必失败(等 24s 超时),
    #   而 4MB 只需 0.20s。限速后发送端与接收端处理的节奏匹配。
    #   取 20000 包/秒(本机实测可跑 16~20MB/s), 对 8MB 内容约 0.42 秒。
    MAX_PKT_PER_SEC = 20000
    # 每发这么多块让出一次 CPU, 避免长时间占满采集线程
    YIELD_EVERY = 200

    def send_blob(self, blob: bytes, seq: int) -> bool:
        """把整份内容切块发出。返回是否所有块都 sendto 成功。"""
        import time as _t
        from .clipboard_sync import split_payload
        chunks = split_payload(seq, blob)
        budget_s = len(chunks) / float(self.MAX_PKT_PER_SEC)
        # 均匀限速: 每块之前按进度睡一小会儿
        per_chunk = budget_s / max(1, len(chunks))
        ok = True
        for i, chunk in enumerate(chunks):
            if per_chunk > 0:
                _t.sleep(per_chunk)
            try:
                self._sock.sendto(chunk, self.addr)
                self.sent_chunks += 1
            except OSError:
                self.failed += 1
                ok = False
            # 周期性让出 CPU(采集线程还要做别的事)
            if i % self.YIELD_EVERY == 0 and i:
                _t.sleep(0)
        return ok

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


class ClipboardReceiver(_BoundThread):
    """收对端的剪贴板分块, 拼好后回调 (payload: dict[fmt, bytes])。

    ★ 拼装器必须能处理**乱序**(UDP 常态)。见 _Assembler 的说明。
    """

    def __init__(self, port: int = CLIPBOARD_PORT, on_payload=None):
        super().__init__("ClipboardReceiver", port, socket.SOCK_DGRAM)
        from .clipboard_sync import _Assembler
        self._asm = _Assembler()
        self._on_payload = on_payload
        self.received = 0
        self.assembled = 0
        self.bad = 0

    def set_handler(self, fn) -> None:
        self._on_payload = fn

    def run(self) -> None:
        from .clipboard_sync import decode, unpack_chunk
        s = self._sock
        if s is None:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            _bind_exclusive(s, self.port)
            # ★★★ 必须放大接收缓冲(实测 bug): 默认缓冲只有 64KB, 而一份
            #   8MB 内容 = 8391 个包, 发送端在 0.2 秒内就全灌完 ——
            #   缓冲瞬间被撑爆, 接收端还在处理第 100 个包时后面的全丢了,
            #   整份内容永远凑不齐(实测: 4MB 成功 0.20s, 8MB 等 24s 超时)。
            #   放大到 4MB 能吸收绝大部分突发。
            for sz in (4 << 20, 2 << 20, 1 << 20):
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, sz)
                    break
                except OSError:
                    continue
            self._sock = s
        s.settimeout(0.5)
        while not self._stop.is_set():
            try:
                data, _addr = s.recvfrom(65535)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            info = unpack_chunk(data)
            if info is None:
                self.bad += 1
                continue
            self.received += 1
            kind, seq, total, offset, _cseq, payload = info
            blob = self._asm.feed(kind, seq, total, offset, payload)
            if blob is None:
                continue
            self.assembled += 1
            items = decode(blob)
            if not items:
                self.bad += 1
                continue
            if self._on_payload is not None:
                try:
                    self._on_payload(items)
                except Exception:  # noqa: BLE001
                    pass


# ---------------------------------------------------------------- 输入 UDP

class InputSender:
    """把输入事件发到副电脑的 INPUT_PORT(UDP)。

    ★ 发送失败必须**可见**: 这个 socket 是无连接的, sendto 到不可达地址
      通常不会报错(ICMP 端口不可达只在后续 send 才可能反映为 WSAECONNRESET),
      事件就这样静默消失, 用户看到的是"鼠标怎么都过不去"而毫无线索。
      因此这里统计发送次数与失败次数, GUI 可以显示出来。

    ★ 目标地址可以**运行时改写**(set_addr): 主电脑起初只能靠 agent 上报的
      地址猜测回连路径, 而经过 NAT 时那个地址不可达。真实可用的路径要靠
      "agent 主动发包 -> 主电脑看到实际源地址"来确定(见 Server 侧的
      hole-punch 逻辑)。学到真地址后调用 set_addr() 即可, 无需重建 socket。
    """

    def __init__(self, host: str, port: int = INPUT_PORT):
        self.addr = (host, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # 打洞时借用接收端 socket(见 use_socket / _active_sock)
        self._borrowed: socket.socket | None = None
        self._borrowed_rx: InputReceiver | None = None
        self.sent = 0
        self.failed = 0
        self.last_error: str | None = None

    def set_addr(self, host: str, port: int | None = None) -> None:
        """改写发送目标(用于 NAT 打洞学到真实可达地址后切换)。"""
        self.addr = (host, port if port is not None else self.addr[1])

    def use_socket(self, sock: socket.socket | None,
                   receiver: "InputReceiver | None" = None) -> None:
        """改用**外部 socket** 发送(打洞必需), 传入 None 恢复自有 socket。

        为什么需要: 端口限制型 NAT 要求回程包的**源端口**等于对端当初发包的
        目标端口。接收端的 socket 正好绑在那个端口上, 所以事件必须借它发出,
        而不是用本类自己创建的临时端口(那样的包会被 NAT 丢弃)。
        """
        self._borrowed = sock
        self._borrowed_rx = receiver

    @property
    def _active_sock(self) -> socket.socket:
        return self._borrowed if self._borrowed is not None else self._sock

    def send(self, ev: InputEvent) -> None:
        s = self._active_sock
        try:
            if self._borrowed_rx is not None:
                # 借用的接收 socket: 走它的计数与错误处理
                if self._borrowed_rx.send_event(ev, self.addr):
                    self.sent += 1
                else:
                    self.failed += 1
                    self.last_error = self._borrowed_rx.last_send_error
                return
            s.sendto(ev.pack(), self.addr)
            self.sent += 1
        except OSError as exc:
            # 记录而不是吞掉: 打到关闭的端口在 Windows 上会返回
            # WSAECONNRESET(10054), 说明对端根本没在监听。
            self.failed += 1
            self.last_error = f"{type(exc).__name__}: {exc}"

    def send_heartbeat(self) -> None:
        """发一个心跳包(用于 NAT 打洞与链路存活检测)。

        它是 4 字节的短包, 与 19 字节的输入事件在长度上天然区分,
        旧版本收到的处理方式是"长度不足 -> 丢弃", 因此**向下兼容**:
        老 peer 不会因为收到心跳而报错, 只是不会打洞。
        """
        try:
            self._sock.sendto(heartbeat_payload(), self.addr)
        except OSError:
            pass

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def heartbeat_payload(side: str | None = None) -> bytes:
    """构造心跳包。side 非空时把它编码进第 2 个字节下发。

    方位下发是**消除"两台方位配置必须相同"这一困惑**的关键:
    副机拿到主电脑的 side 后自己取对侧作为回传边, 无需用户再猜。
    """
    code = SIDE_TO_CODE.get(side or "", SIDE_CODE_NONE)
    return bytes([EV_HEARTBEAT, code, 0, 0])[:HEARTBEAT_SIZE]


def parse_heartbeat(data: bytes) -> str | None:
    """从心跳包里取出方位(没有则返回 None)。"""
    if len(data) < 2:
        return None
    return CODE_TO_SIDE.get(data[1])


class HeartbeatSender(threading.Thread):
    """周期性向对端发心跳, 用于 NAT 打洞与链路存活检测。

    为什么必须有它(真实故障):
      副电脑经 NAT 连主电脑时, 主电脑无法主动连到副电脑 —— 它从 TCP accept
      学到的"源地址"其实是网关。而音频是副电脑主动发的(NAT 放行)所以正常,
      输入是主电脑主动发的所以全丢, 表现为"声音好、鼠标完全不动"。

    ⚠ 首选做法是**从接收输入的那个 socket 发心跳**(见 InputReceiver.
      send_heartbeat_to): NAT 只为"实际发包的那个源端口"建立映射, 若心跳
      从另一个临时端口发出, 主电脑学到的映射端口指向的是心跳 socket,
      而不是输入接收端口 —— 打洞看起来成功, 输入事件却依然送不到。
      本类仅作为不具备该条件时的后备(例如单机自测)。
    """

    def __init__(self, sender: InputSender, interval_s: float = 1.0):
        super().__init__(daemon=True, name="HeartbeatSender")
        self._sender = sender
        self._interval = interval_s
        self._stop = threading.Event()

    def run(self) -> None:
        # 先立刻发一个: 让对端尽快学到我们的地址(否则要等一个周期)
        self._sender.send_heartbeat()
        while not self._stop.wait(self._interval):
            self._sender.send_heartbeat()

    def stop(self) -> None:
        self._stop.set()


class InputReceiver(_BoundThread):
    """收输入事件的 UDP 监听(server 收回传事件, agent 收转发事件)。

    ★ 同时承担**打洞(on_heartbeat)**: 副电脑经 NAT 时, 主电脑无法主动
      连到它 —— 必须先由副电脑持续发心跳, NAT 才会为这条流建立映射,
      主电脑也才能从"实际收到的源地址"学到真正可用的回程地址。
      on_heartbeat(addr) 会在收到心跳时回调, 由上层决定是否切换发送目标。
    """

    def __init__(self, port: int = INPUT_PORT, on_event=None, on_heartbeat=None,
                 on_peer_side=None):
        super().__init__("InputReceiver", port, socket.SOCK_DGRAM)
        self._on_event = on_event
        self._on_heartbeat = on_heartbeat
        # 对端通过心跳下发的方位(见 protocol.SIDE_CODE_*)
        self._on_peer_side = on_peer_side
        self.peer_side: str | None = None
        # 诊断计数: 用来区分"没人发"和"发了但格式/端口不对"
        self.packets = 0        # 收到的原始 UDP 包数
        self.short_packets = 0  # 长度不足且不是心跳的
        self.bad_packets = 0    # 解包失败的
        self.events = 0         # 成功交付的事件数
        self.heartbeats = 0     # 收到的心跳数
        self.last_from: str | None = None
        # 从本 socket 发出的输入事件统计(打洞路径专用, 见 send_event)
        self.sent_events = 0
        self.send_failed = 0
        self.last_send_error: str | None = None
        # ★ 最近一次收到对端心跳的单调时间(按键卡死防护的判断依据:
        #   对端失联时, 之前转发来的按键可能永远等不到 up)
        self.last_heartbeat_mono: float | None = None

    def run(self) -> None:
        s = self._sock
        if s is None:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            _bind_exclusive(s, self.port)
            self._sock = s
        s.settimeout(0.5)
        while not self._stop.is_set():
            try:
                data, addr = s.recvfrom(2048)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            self.packets += 1
            self.last_from = addr[0]
            # 心跳包: 长度恰好 4 且首字节是 EV_HEARTBEAT。
            # ★ 必须在"长度不足就丢弃"之前判断, 否则心跳会被当成残包丢掉,
            #   打洞永远不会发生(这正是 NAT 环境下输入全丢的原因之一)。
            if len(data) == HEARTBEAT_SIZE and data[0] == EV_HEARTBEAT:
                self.heartbeats += 1
                self.last_heartbeat_mono = time.monotonic()
                # 顺便取出对端下发的方位(主电脑 -> 副电脑, 见 protocol)
                side = parse_heartbeat(data)
                if side and side != self.peer_side:
                    self.peer_side = side
                    if self._on_peer_side is not None:
                        try:
                            self._on_peer_side(side)
                        except Exception:  # noqa: BLE001
                            pass
                if self._on_heartbeat is not None:
                    try:
                        self._on_heartbeat(addr)
                    except Exception:  # noqa: BLE001
                        pass
                continue
            # 输入事件是 19 字节(INPUT_HEADER_SIZE)。这里曾硬编码 24,
            # 于是所有合法事件都被当成残包静默丢弃, 输入功能整体失效。
            if len(data) < INPUT_HEADER_SIZE:
                self.short_packets += 1
                continue
            try:
                ev = InputEvent.unpack(data)
            except ValueError:
                self.bad_packets += 1
                continue
            self.events += 1
            if self._on_event is not None:
                try:
                    self._on_event(ev)
                except Exception:  # noqa: BLE001
                    pass

    def diag(self) -> dict:
        return {
            "packets": self.packets,
            "short": self.short_packets,
            "bad": self.bad_packets,
            "events": self.events,
            "heartbeats": self.heartbeats,
            "last_from": self.last_from,
        }

    def send_heartbeat_to(self, addr: tuple[str, int],
                          side: str | None = None) -> bool:
        """**从接收输入用的这个 socket** 向 addr 发心跳(打洞的关键)。

        为什么必须用同一个 socket:
          NAT 只为"实际发出包的那个源端口"建立映射。若用另一个 socket
          (例如 InputSender 的临时端口)发心跳, 对端学到的映射端口指向的是
          那个临时端口, 而我们的输入接收端在 INPUT_PORT 上 —— 对端把事件
          发到临时端口, 我们永远收不到。用同一 socket 才能让映射与接收端
          口一致, 打洞才真正有效。

        side: 主电脑用它把自己的方位下发给副电脑(见 heartbeat_payload)。

        必须在 bind_now()/start() 之后调用(那时 self._sock 才存在)。
        """
        s = self._sock
        if s is None:
            return False
        try:
            s.sendto(heartbeat_payload(side), addr)
            return True
        except OSError:
            return False

    def send_event(self, ev: InputEvent, addr: tuple[str, int]) -> bool:
        """**从接收输入用的这个 socket** 发送输入事件。

        ★ 这是打洞能否真正送达到事件的关键, 也是最后一处踩到的坑。
          端口限制型 NAT(OpenWrt 默认 masquerade 即是)的 conntrack 条目
          是双向绑定的:
              内网 192.168.10.201:24801 -> 主电脑 192.168.10.12:24801
              回程必须是 192.168.10.12:**24801** -> 192.168.10.1:37103
          回程包的**源端口必须等于对端当初发包的目标端口**, 否则 conntrack
          不匹配, 包被丢弃。
          实测症状极具指向性: **心跳(从 24801 发)全部到达, 输入事件
          (从 InputSender 的临时端口发)全部丢失** —— 两者目标地址完全相同,
          唯一差别就是发送用的 socket。
        """
        s = self._sock
        if s is None:
            return False
        try:
            s.sendto(ev.pack(), addr)
            self.sent_events += 1
            return True
        except OSError as exc:
            self.send_failed += 1
            self.last_send_error = f"{type(exc).__name__}: {exc}"
            return False

    @property
    def bound_port(self) -> int | None:
        """本 socket 实际绑定的本地端口(绑在 0 时由系统分配)。"""
        s = self._sock
        if s is None:
            return None
        try:
            return s.getsockname()[1]
        except OSError:
            return None

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


# ---------------------------------------------------------------- 控制 TCP

class ControlClient:
    """TCP 控制通道: 时钟同步 + 文本消息。"""

    def __init__(self, host: str, port: int = CONTROL_PORT, timeout: float = 5.0):
        self.addr = (host, port)
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self.clock = ClockSync()
        # 连接建立后本端使用的地址 (ip, port)。**这是最可靠的本机地址** ——
        # 它就是"能到达主电脑的那个接口地址", 比靠网段猜测或默认路由准确。
        # 主电脑需要拿它当输入转发目标(见 Agent.start 的 adv_host 说明)。
        self.local_addr: tuple[str, int] | None = None
        # 版本协商结果: 协商后的 minor 版本(None = 未协商/失败)。
        # 见 connect() 里的 HELLO/HELLOACK 交换。
        self.negotiated_minor: int | None = None

    def connect(self) -> bool:
        try:
            s = socket.create_connection(self.addr, timeout=self.timeout)
            s.settimeout(self.timeout)
            self._sock = s
            try:
                self.local_addr = s.getsockname()
            except OSError:
                self.local_addr = None
        except OSError:
            return False
        # 必须先吃掉 server 的 "HELLO <name>" 问候行, 否则它会在第一次
        # sync_clock 里被当作 SYNCREPLY 读走, 使请求/回复整体错位一轮
        # (表现为 offset 恰好偏差一个轮询间隔, 例如 -50ms)。
        try:
            self._recv_line()
        except OSError:
            pass
        # ★ 协议版本协商(照搬 Deskflow 的 HelloBack): 回报本端版本。
        #   server 校验 major 相同才继续; 不匹配时会发 EVERSION 并断开,
        #   客户端能立刻看到"版本不兼容"而不是莫名超时。
        try:
            self._send_line(f"HELLO {socket.gethostname()} "
                            f"{PROTOCOL_MAJOR} {PROTOCOL_MINOR}")
            reply = self._recv_line()
            if reply.startswith("EVERSION"):
                self.negotiated_minor = None
                return False
            if reply.startswith("HELLOACK"):
                parts = reply.split()
                if len(parts) >= 3:
                    self.negotiated_minor = min(
                        PROTOCOL_MINOR, int(parts[2]))
        except OSError:
            return False
        return True

    def _send_line(self, line: str) -> None:
        assert self._sock is not None
        self._sock.sendall(line.encode("utf-8") + b"\n")

    def _recv_line(self) -> str:
        assert self._sock is not None
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = self._sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf.decode("utf-8", errors="replace").strip()

    def sync_clock(self, rounds: int = 8) -> ClockSync:
        """NTP 四时间戳对时。客户端为 time1/time4, server 为 time2/time3。

        线格式(单行, 空格分隔):
            请求:  "SYNC <t1>"
            回复:  "SYNCREPLY <t1> <t2> <t3>"

        **必须用 now_us()(单调时钟), 不能用 time.time_ns()**: 音频路径上的
        时间戳全部来自单调时钟, 若对时用墙钟而打戳用单调钟, offset 属于
        不同时间域, 换算出的 ts 毫无意义。单调时钟的纪元任意, 但两台机器
        走时速率一致, NTP 偏移量在任何同速时钟域上都成立。
        """
        if self._sock is None:
            raise ConnectionError("control channel not connected")
        for _ in range(rounds):
            t1 = now_us()
            self._send_line("SYNC %d" % t1)
            line = self._recv_line()
            t4 = now_us()
            parts = line.split()
            if len(parts) == 4 and parts[0] == "SYNCREPLY":
                _t1, t2, t3 = int(parts[1]), int(parts[2]), int(parts[3])
                self.clock.update(t1, t2, t3, t4)
            time.sleep(0.05)
        return self.clock

    def send_caps(self, caps: dict) -> None:
        self._send_line("CAPS " + json.dumps(caps))

    def send_shape(self, x: int, y: int, w: int, h: int) -> None:
        """上报本机桌面几何(照搬 Deskflow 的 DInfo/CInfoAck)。

        ★ 双方都必须知道对方的桌面矩形: 跨屏的进入/退出位置要映射到
          对端的坐标系, 没有这份数据就只能靠"猜"(实测副机双屏时位置错乱)。
          Deskflow 里 client 连上后立刻发 DInfo, server 回 CInfoAck ——
          几何是**握手的一部分**, 不是可选项。
        """
        self._send_line(f"SHAPE {x} {y} {w} {h}")

    def recv_shape(self) -> tuple[int, int, int, int] | None:
        """读对端桌面几何(等待最多数行, 跳过 SHAPEACK 等非数据行)。

        新版对端回 "SHAPE x y w h"; 旧版只回 "SHAPEACK"(没有几何) ——
        此时返回 None, 调用方退化为"不做对端几何校验"。
        """
        for _ in range(4):
            line = self._recv_line()
            if line.startswith("SHAPE ") and len(line.split()) == 5:
                try:
                    x, y, w, h = (int(p) for p in line[6:].split())
                    return x, y, w, h
                except ValueError:
                    continue
            if line.startswith("SHAPEACK"):
                continue          # 旧版对端: 没有几何可给
            if not line:
                break
        return None

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


class ControlServer(_BoundThread):
    """server 侧控制通道(单客户端)。

    on_sync(offset_us) 在每次收到 SYNC 请求时回调:
        offset = client_clock - server_clock
    """

    def __init__(self, port: int = CONTROL_PORT, name: str = "server",
                 caps: dict | None = None, on_sync=None, on_client=None,
                 on_shape=None):
        super().__init__("ControlServer", port, socket.SOCK_STREAM)
        self.name = name
        self.caps = caps or {}
        self._on_sync = on_sync
        self._on_client = on_client
        # 副机桌面上报完成时回调(Deskflow DInfo 语义)
        self._on_shape = on_shape
        self.client_addr: tuple[str, int] | None = None
        self.client_clock = ClockSync()
        self.client_caps: dict = {}
        # 副机桌面几何(x, y, w, h) —— 由 SHAPE 消息上报(见 _serve)
        self.client_shape: tuple[int, int, int, int] | None = None
        # 版本协商结果(与 ControlClient.negotiated_minor 对应)
        self.negotiated_minor: int | None = None
        # 本机桌面几何: set_shape() 设置后, SHAPE 交换时会把它发回给副机
        self.own_shape: tuple[int, int, int, int] | None = None

    def set_shape(self, x: int, y: int, w: int, h: int) -> None:
        """设置本机桌面几何(在 bind_now 前调用), SHAPE 交换时会发回对端。"""
        self.own_shape = (x, y, w, h)

    @staticmethod
    def _recv_line_conn(conn: socket.socket) -> str:
        """从指定连接读一行(供握手阶段使用, 此时还没有逐行缓冲)。"""
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf.decode("utf-8", errors="replace").strip()

    def run(self) -> None:
        # 绑定已在 bind_now() 里完成(基类存 self._sock)。若这里再 bind 一次,
        # 独占绑定下必然 EADDRINUSE 并让本线程静默死亡 —— 控制通道看起来
        # "启动成功"却永远连不上。
        s = self._sock
        if s is None:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            _bind_exclusive(s, self.port)
            self._sock = s
        s.listen(1)
        s.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, addr = s.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            self.client_addr = addr
            if self._on_client is not None:
                try:
                    self._on_client(addr, self.client_caps)
                except Exception:  # noqa: BLE001
                    pass
            try:
                self._serve(conn)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
                self.client_addr = None

    def _serve(self, conn: socket.socket) -> None:
        # ★ 握手超时 30s(照搬 ClientProxyUnknown 的 30.0s):
        #   agent 若连上却迟迟不完成版本协商/对时, 连接会被关闭 ——
        #   否则这个坏连接会一直占着"单客户端"的名额, 真正的 agent
        #   重连时反而被拒。握手完成后放宽到普通的读超时。
        conn.settimeout(30.0)
        buf = b""
        conn.sendall(b"HELLO " + self.name.encode("utf-8") + b"\n")

        # ── 版本协商(照搬 Deskflow: Hello -> HelloBack -> 校验 major)──
        #   之前没有协商: 两端版本不同时行为差异(如新的 SHAPE 消息)会造成
        #   静默错位。major 不匹配必须明确拒绝, 让对端"看到"不兼容。
        try:
            line = self._recv_line_conn(conn)
        except OSError:
            return
        hello_parts = line.split()
        # 兼容旧版客户端: HELLO 后没有版本字段(只有名字)时按 minor=0 处理
        if hello_parts and hello_parts[0].upper() == "HELLO" \
                and len(hello_parts) >= 3:
            try:
                c_major, c_minor = int(hello_parts[-2]), int(hello_parts[-1])
            except ValueError:
                c_major, c_minor = PROTOCOL_MAJOR, 0
            if c_major != PROTOCOL_MAJOR:
                try:
                    conn.sendall(b"EVERSION %d %d\n"
                                 % (PROTOCOL_MAJOR, PROTOCOL_MINOR))
                except OSError:
                    pass
                print(f"[server] 客户端协议版本 {c_major}.{c_minor} 与本机 "
                      f"{PROTOCOL_MAJOR}.{PROTOCOL_MINOR} 不兼容(major 不同), "
                      f"已拒绝", flush=True)
                return
            self.negotiated_minor = min(PROTOCOL_MINOR, c_minor)
            try:
                conn.sendall(b"HELLOACK %d %d\n"
                             % (PROTOCOL_MAJOR, self.negotiated_minor))
            except OSError:
                return
        # 协商完成, 恢复常规读超时
        conn.settimeout(10.0)
        while not self._stop.is_set():
            try:
                chunk = conn.recv(4096)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                parts = text.split(None, 1)
                cmd = parts[0].upper()
                if cmd == "SYNC" and len(parts) > 1:
                    try:
                        t1 = int(parts[1])
                    except ValueError:
                        continue
                    t2 = now_us()
                    t3 = now_us()
                    # 记录: local(t1) -> remote(t2/t3) -> local(t4)
                    self._last_t1 = t1
                    self._last_t2 = t2
                    self._last_t3 = t3
                    conn.sendall(b"SYNCREPLY %d %d %d\n" % (t1, t2, t3))
                    if self._on_sync is not None:
                        try:
                            self._on_sync(self.client_clock.offset_us)
                        except Exception:  # noqa: BLE001
                            pass
                elif cmd == "SHAPE" and len(parts) > 1:
                    # 副机上报桌面几何(Deskflow 的 DInfo)。server 必须知道
                    # 副机的桌面矩形, 跨屏位置映射才不会错位。
                    try:
                        x, y, w, h = (int(p) for p in parts[1].split())
                        self.client_shape = (x, y, w, h)
                    except ValueError:
                        self.client_shape = None
                    # ★ 同时把**本机桌面**发回去(双向交换, 与 Deskflow 一致):
                    #   agent 需要它来校验主电脑的方位假设与比例映射基准。
                    try:
                        own = getattr(self, "own_shape", None)
                        if own:
                            conn.sendall(b"SHAPE %d %d %d %d\n" % own)
                        else:
                            conn.sendall(b"SHAPEACK\n")
                    except OSError:
                        break
                    if self._on_shape and self.client_shape:
                        try:
                            self._on_shape(self.client_shape)
                        except Exception:  # noqa: BLE001
                            pass
                    if self._on_client is not None:
                        try:
                            self._on_client(self.client_addr, self.client_caps)
                        except Exception:  # noqa: BLE001
                            pass
                elif cmd == "CAPS" and len(parts) > 1:
                    try:
                        self.client_caps = json.loads(parts[1])
                    except ValueError:
                        self.client_caps = {}
                    # ★ 收到 CAPS 后**再次**通知上层: 客户端会在 CAPS 里
                    #   报告它自己的真实地址(adv_host)。此前 _on_client 只在
                    #   TCP accept 时触发过, 那时只有 TCP 源地址可用 —— 而
                    #   经过 NAT(例如主路由是虚拟机里的 OpenWrt)时, 源地址
                    #   是**网关**而不是副电脑, 拿它当转发目标会让输入事件
                    #   全部丢失(实测: 主电脑日志"输入转发目标 -> 192.168.10.1",
                    #   那正是 OpenWrt 网关, 副电脑"注入 0 事件")。
                    if self._on_client is not None:
                        try:
                            self._on_client(self.client_addr, self.client_caps)
                        except Exception:  # noqa: BLE001
                            pass
                    # 副机桌面几何到齐后通知一次(可能晚于 SHAPE 到达)。
                    # server 用它做跨屏位置映射的基准(Deskflow 的 DInfo)。
                    if self.client_shape is not None and self._on_shape:
                        try:
                            self._on_shape(self.client_shape)
                        except Exception:  # noqa: BLE001
                            pass

    def stop(self) -> None:
        # 复用基类的关闭逻辑(关 self._sock 并置 None)
        super().stop()
