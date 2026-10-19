"""音频帧头 + 输入事件编解码。

音频帧头(15 字节, 兼容 Scream 布局, 便于与现成接收端互通):
    offset  size  field
    0       1     ver/flg   (bit0-3 版本=1, bit7 = 1 表示含扩展时间戳)
    1       1     rateIdx   (bit7 = 基频 0->48kHz / 1->44.1kHz, bit0-6 = 倍率)
    2       1     bits      (16 或 24)
    3       1     chans
    4       2     chanMask  (小端)
    6       2     seq       (小端, 每发送方独立递增)
    8       7     ts_us     (小端, 发送方时钟域的微秒时间戳)
    -- 其后为交错小端 PCM --

输入事件(定长 19 字节):
    0   1   type     (1=MOUSE_MOVE_REL, 2=MOUSE_BUTTON, 3=MOUSE_WHEEL,
                     4=KEY)
    1   1   flags
    2   2   code     (小端; 鼠标 = 按钮号/轴, 键盘 = VK)
    4   4   x 或 scan(小端, int32)
    8   4   y 或 vk  (小端, int32)
    12  7   ts_us    (小端, 发送方时钟域)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

# ---------------------------------------------------------------- 音频

AUDIO_MAGIC = 0x01        # 低 4 位 = 版本号(=1), bit7 = 含扩展时间戳
AUDIO_HEADER_SIZE = 15

FMT_S16 = 16
FMT_S24 = 24


def encode_rate_index(rate: int) -> int:
    """采样率 -> rateIdx 字节(bit7 为基频标志)。

    返回**完整字节**, 与 decode_rate_index() 严格互逆。
    早期版本返回 (mult, base) 元组, 调用方必须自己把 base 揉进 bit7,
    很容易漏; 测试里直接 encode->decode 往返就会丢掉基频标志。
    """
    for base_bit, base_rate in ((0, 48000), (0x80, 44100)):
        if rate == base_rate:
            return base_bit          # 倍率 0 表示"等于基频"
        if rate % base_rate == 0:
            mult = rate // base_rate
            if 1 <= mult <= 0x7F:
                return base_bit | mult
    raise ValueError(f"unsupported sample rate {rate}")


def decode_rate_index(rate_idx: int) -> int:
    base_rate = 44100 if (rate_idx & 0x80) else 48000
    mult = rate_idx & 0x7F
    if mult == 0:
        return base_rate
    return base_rate * mult


@dataclass(frozen=True)
class AudioHeader:
    rate: int
    bits: int
    channels: int
    chan_mask: int
    seq: int
    ts_us: int

    def pack(self) -> bytes:
        head = struct.pack(
            "<BBBBHH",
            AUDIO_MAGIC | 0x80,          # ver=1, 含扩展时间戳
            encode_rate_index(self.rate),
            self.bits,
            self.channels,
            self.chan_mask,
            self.seq & 0xFFFF,
        )
        return head + (self.ts_us & 0xFFFFFFFFFFFFFF).to_bytes(7, "little")

    @classmethod
    def unpack(cls, data: bytes) -> "AudioHeader":
        if len(data) < AUDIO_HEADER_SIZE:
            raise ValueError(f"audio header too short: {len(data)}")
        magic, rate_idx, bits, chans, chan_mask, seq = struct.unpack(
            "<BBBBHH", data[:8]
        )
        ts_us = int.from_bytes(data[8:15], "little")
        if (magic & 0x0F) != 1:
            raise ValueError(f"unsupported audio version {magic & 0x0F}")
        return cls(
            rate=decode_rate_index(rate_idx),
            bits=bits,
            channels=chans,
            chan_mask=chan_mask,
            seq=seq,
            ts_us=ts_us,
        )


def frame_bytes_per_sample(bits: int) -> int:
    if bits == 16:
        return 2
    if bits == 24:
        return 3
    if bits == 32:
        return 4
    raise ValueError(f"unsupported bit depth {bits}")


# ---------------------------------------------------------------- 输入事件

INPUT_HEADER_SIZE = 19

EV_MOUSE_MOVE_REL = 1
EV_MOUSE_BUTTON = 2
EV_MOUSE_WHEEL = 3
EV_KEY = 4
# 心跳/打洞包: 输入包都是 19 字节, 心跳用一个**长度不同**的短包区分,
# 这样无需改协议格式就能让两端共存(旧版本收到它会因长度不足而丢弃)。
EV_HEARTBEAT = 5

# 水平滚轮(倾斜滚轮)。照搬 Deskflow: MSWindowsHook 显式处理 WM_MOUSEHWHEEL
# (含 #ifndef WM_MOUSEHWHEEL 兜底定义), 倾斜滚轮与垂直滚轮地位相同。
# ★ 协议格式不变(仍是 19 字节), 用 flags 位区分方向 —— 旧版本把水平滚轮
#   当垂直滚轮解析会方向错乱, 但不会崩溃; 两台机器同版本部署是前提。
EV_MOUSE_HWHEEL = 7

# EV_MOUSE_WHEEL / EV_MOUSE_HWHEEL 的 flags
WHEEL_VERTICAL = 0x00
WHEEL_HORIZONTAL = 0x01

# 心跳包长度(字节)。刻意取 4, 远小于 INPUT_HEADER_SIZE(19) ——
# 两端都靠"长度 < 19"识别它, 保证与真实输入包不混淆。
#
# 布局: [0]=EV_HEARTBEAT, [1]=方位码(见 SIDE_CODE_*), [2][3]=保留(0)
# 方位码让**主电脑把自己的 side 下发给副电脑** —— 副机因此不必猜测方位,
# 从根上消除"两台机器的方位配置必须填同一个值"这种困惑(实测用户被它
# 卡住: 主机 right + 副机 left 时光标完全进不去副机)。
HEARTBEAT_SIZE = 4

SIDE_CODE_NONE = 0
SIDE_CODE_RIGHT = 1
SIDE_CODE_LEFT = 2
SIDE_CODE_TOP = 3
SIDE_CODE_BOTTOM = 4

SIDE_TO_CODE = {
    "right": SIDE_CODE_RIGHT,
    "left": SIDE_CODE_LEFT,
    "top": SIDE_CODE_TOP,
    "bottom": SIDE_CODE_BOTTOM,
}
CODE_TO_SIDE = {v: k for k, v in SIDE_TO_CODE.items()}

# EV_MOUSE_BUTTON 的 flags
MB_DOWN = 0x01
MB_UP = 0x02

# 专用的"把光标放到指定位置"事件(复用 a/b 两个 int32 承载 x/y)。
#
# ★ 为什么必须有它(多显示器场景的真实故障):
#   鼠标跨屏只发**相对位移**时, 副机光标从"它上次停在哪"开始累积 ——
#   位置完全不可控。实测: 副机有两块屏, 主机一直向右推, 副机光标却停在
#   **右屏的右边缘**蹭, 而不是从最左边缘进入。
#   软件 KVM 必须在**交接那一刻**把副机光标绝对定位到进入边, 否则
#   "从哪进入"这件事无法保证。
EV_MOUSE_PLACE = 6

# 交接待办的修饰键快照事件: 进入穿越态时把主机当前按下的修饰键告知副机。
# 照搬 Deskflow: Server.cpp enter 携带 toggleMask, 副机据此设置修饰键状态。
# 若用户按住 Shift/Ctrl 时穿越, 不发快照的话副机在用户松开前一直不知道
# 修饰键已按下(表现为"按住 Shift 拖过去, 副机选中与主机不一致")。
# 格式: code = 位掩码(MODFLAG_*), a/b/flags = 0。
EV_MOUSE_MODS = 8

# 回程交接定位事件: 副机把光标送回主电脑时, 告知主电脑"把光标放在我这里
# 的哪个相对高度"。与 EV_MOUSE_PLACE 对称(去程用 PLACE, 回程用 RETURN)。
#
# ★ 为什么必须有它(用户实测 BUG3): 回程只发**相对位移**时, 主电脑光标从
#   "它被钉在哪"开始累积 —— 而穿越期间主机光标恰好被钉在**进入时的位置**,
#   于是"从副机哪里回来"这件事完全丢失, 用户看到的是: 光标退回主机的落点
#   总是当初跳去副机时的那个位置, 而不是他在副机上实际移动到的位置。
#   格式: a = 0..65535 的比例(沿副机回传边的切向), flags/code/b = 0。
# 与 EV_MOUSE_PLACE 一样**不**计入 is_mouse, 防止被当成用户输入再转发回环。
EV_MOUSE_RETURN = 9

# 修饰键位掩码(与 Win32 GetKeyState 语义对应)
MODFLAG_SHIFT = 0x01
MODFLAG_CTRL = 0x02
MODFLAG_ALT = 0x04
MODFLAG_WIN = 0x08

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12       # Alt
VK_LWIN = 0x5B
VK_RWIN = 0x5C

# EV_KEY 的 flags
KEY_DOWN = 0x01
KEY_UP = 0x02
KEY_EXTENDED = 0x04


@dataclass(frozen=True)
class InputEvent:
    type: int
    flags: int
    code: int
    a: int
    b: int
    ts_us: int

    def pack(self) -> bytes:
        head = struct.pack(
            "<BBHii",
            self.type,
            self.flags,
            self.code,
            self.a,
            self.b,
        )
        return head + (self.ts_us & 0xFFFFFFFFFFFFFF).to_bytes(7, "little")

    @classmethod
    def unpack(cls, data: bytes) -> "InputEvent":
        if len(data) < INPUT_HEADER_SIZE:
            raise ValueError(f"input event too short: {len(data)}")
        type_, flags, code, a, b = struct.unpack("<BBHii", data[:12])
        ts_us = int.from_bytes(data[12:19], "little")
        return cls(type=type_, flags=flags, code=code, a=a, b=b, ts_us=ts_us)

    @property
    def is_key(self) -> bool:
        return self.type == EV_KEY

    @property
    def is_mouse(self) -> bool:
        return self.type in (EV_MOUSE_MOVE_REL, EV_MOUSE_BUTTON,
                             EV_MOUSE_WHEEL, EV_MOUSE_HWHEEL)


# ---------------------------------------------------------------- 常量

DISCOVERY_PORT = 24800          # 与 Deskflow 默认端口一致, 便于错开时排查
AUDIO_PORT = 4010              # 与 Scream 一致
INPUT_PORT = 24801
CONTROL_PORT = 24802
# 剪贴板同步(参考 Deskflow 的 ClipboardChunk 分块协议)。
# 语义: 哪台机器发生"复制", 就由它发到对端的 CLIPBOARD_PORT。
# 两台机器各自监听该端口, 于是双向复制都能用。
CLIPBOARD_PORT = 24803

AUDIO_MAGIC_BYTES = b"CB"
