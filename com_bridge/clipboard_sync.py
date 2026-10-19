# -*- coding: utf-8 -*-
"""剪贴板跨屏同步 —— 参考 Deskflow 的 Clipboard + ClipboardChunk 设计。

## Deskflow 的做法(已读源码)
1. **只同步三种规范化格式**: Text(UTF-8/LF) / HTML / Bitmap(BMP)。
   各平台各自做本地格式转换, 网络上只走这三种 —— 避免格式矩阵爆炸。
2. **用序列号检测变化**: 不轮询内容, 只比较"剪贴板版本号"。
3. **分块传输**(ClipboardChunk: start/data/end + sequence):
   大内容切成小块发, 每块独立可校验, 避免 UDP 分片/大包阻塞。
   我们上一轮已在音频上吃过"包超 MTU"的亏, 这里必须一次做对。
4. **防回环**: 自己写进剪贴板的内容不能再被当成"用户复制"发回去。
   否则两台机器会无限互发同一份内容。

## 我们的设计
* `ClipboardSync`: 跑在**采集侧**(复制发生时的那台机器),
  周期检查序列号; 变化且**不是自己写的** -> 采集内容 -> 交给发送回调。
* 分块: 每块 **1000 字节**(加协议头远小于 MTU 1500), start/data/end。
* 防回环: 自己 apply() 之前记录"期望序列号", 之后几次轮询跳过。
  另外维护**内容哈希**: 与最近一次发送/接收的哈希相同则忽略 ——
  这比单纯比序列号可靠(用户可能重复复制同样的内容)。
"""
from __future__ import annotations

import hashlib
import struct
import threading
import time

from . import clipboard as cb

# 协议: 每个剪贴板分块消息的头部
#   magic(2) | kind(1) | seq(4) | total(4) | offset(4) | chunk_seq(2) | len(2)
#   合计 19 字节 -> 1000+19 = 1019 < 1500(MTU), 不分片
CLIP_MAGIC = b"CB"
KIND_START = 1
KIND_DATA = 2
KIND_END = 3

#        magic kind  seq   total  offset  chunk  len
HDR = struct.Struct("<2s B I I I H H")
CHUNK_SIZE = 1000
MAX_TOTAL = 8 * 1024 * 1024      # 8MB 上限

# 轮询间隔: 剪贴板变化检测很便宜(只读序列号), 但也别太频繁
POLL_INTERVAL_S = 0.25
# 自己写入后, 忽略变化的时间窗(防回环)
SELF_WRITE_GRACE_S = 1.2


def pack_chunk(kind: int, seq: int, total: int, offset: int,
               chunk_seq: int, payload: bytes) -> bytes:
    return HDR.pack(CLIP_MAGIC, kind, seq, total, offset, chunk_seq,
                    len(payload)) + payload


def unpack_chunk(data: bytes) -> tuple[int, int, int, int, int, bytes] | None:
    if len(data) < HDR.size:
        return None
    try:
        magic, kind, seq, total, offset, chunk_seq, n = HDR.unpack_from(data, 0)
    except struct.error:
        return None
    if magic != CLIP_MAGIC:
        return None
    payload = data[HDR.size:HDR.size + n]
    if len(payload) != n:
        return None
    if total > MAX_TOTAL:
        return None
    return kind, seq, total, offset, chunk_seq, payload


def split_payload(seq: int, blob: bytes) -> list[bytes]:
    """把整份剪贴板内容切成可发送的块列表(start + data* + end)。"""
    out = [pack_chunk(KIND_START, seq, len(blob), 0, 0, b"")]
    off = 0
    cs = 0
    while off < len(blob):
        piece = blob[off:off + CHUNK_SIZE]
        cs = (cs + 1) & 0xFFFF
        out.append(pack_chunk(KIND_DATA, seq, len(blob), off, cs, piece))
        off += len(piece)
    out.append(pack_chunk(KIND_END, seq, len(blob), 0, 0, b""))
    return out


class _Assembler:
    """接收侧: 把分块拼回完整内容。

    ★ UDP 会乱序, 所以**不能假设 START 先到、END 后到**(实测踩到:
      乱序时 END 先到被直接判失败)。正确做法:
        * 任何块到达时, 若 seq 与当前不同则重置(新一轮);
        * DATA 按 offset 落位(不 append), 这样乱序也能拼对;
        * END **只标记"声明长度已知"**, 真正交付要等 bytes 收齐。
      这样无论什么到达顺序, 只要没丢块就能拼出完整内容。
    """

    def __init__(self) -> None:
        self.seq = -1
        self.total = 0
        self.buf = bytearray()
        # ★ 用**位图**记录哪些字节已落位, 而不是累加计数。
        #   累加计数在"同一块重复到达"时会多算, 导致误判收齐(实测乱序
        #   测试随机失败)。位图天然去重。
        self._have = bytearray()
        self.active = False
        self.saw_end = False

    def _reset(self, seq: int, total: int) -> None:
        self.seq = seq
        self.total = total
        self.buf = bytearray()
        self._have = bytearray(total if 0 < total <= MAX_TOTAL else 0)
        self.active = 0 < total <= MAX_TOTAL
        self.saw_end = False

    def feed(self, kind: int, seq: int, total: int, offset: int,
             payload: bytes) -> bytes | None:
        """喂入一块; 收齐且见过 END 时返回整份内容, 否则 None。"""
        if seq != self.seq:
            self._reset(seq, total)
        if not self.active:
            return None

        if kind == KIND_START:
            # ★ START **不清空已收数据**(实测 bug 的最终修法)。
            #
            #   我们走 UDP, 而 START 完全可能**中途才到**(乱序):
            #   若它一来就清空 buf/_have, 先前收到的 DATA 就白收了
            #   -> 永远凑不齐 -> 随机失败(实测 200 次乱序只成功 54 次)。
            #
            #   而且清空本身没有意义: 本轮的总长在 `_reset` 里已按 seq
            #   设好(每个块都带 total), 所以 START 只是个"标记开始"的
            #   冗余信号 —— 它唯一的作用是让接收方知道"这是新一轮"。
            #   既然 seq 已经保证了轮次区分, START 就不需要做任何事。
            #
            #   (Deskflow 的 ClipboardChunk 不会遇到这个问题, 因为它走
            #    TCP —— 有序可靠。我们走 UDP 必须自己处理乱序。)
            return None

        if kind == KIND_DATA:
            end = offset + len(payload)
            if end > self.total:
                self.active = False
                return None
            if len(self.buf) < end:
                self.buf.extend(b"\x00" * (end - len(self.buf)))
            self.buf[offset:end] = payload
            for i in range(offset, end):
                self._have[i] = 1
            return self._maybe_complete()

        if kind == KIND_END:
            self.saw_end = True
            return self._maybe_complete()
        return None

    def _maybe_complete(self) -> bytes | None:
        """收齐(且已见 END)才交付。"""
        if self.saw_end and self._have and all(self._have):
            self.active = False
            self.saw_end = False
            return bytes(self.buf[:self.total])
        return None


class ClipboardSync:
    """采集侧: 监听本机剪贴板变化, 交给发送回调。

    发送回调 `on_copy(blob: bytes)` 由调用方决定怎么传(我们走 UDP 分块)。
    blob 的编码由 _encode/_decode 负责(多格式打包)。
    """

    def __init__(self, on_copy, enabled: bool = True):
        self._on_copy = on_copy
        self._enabled = enabled
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_seq = 0
        self._last_hash = b""
        self._self_write_until = 0.0
        self._msg_seq = 0
        self.stats = {"sent": 0, "skipped_self": 0, "skipped_same": 0,
                      "skipped_big": 0, "errors": 0}

    # -- 生命周期 ----------------------------------------------------

    def start(self) -> None:
        self._last_seq = cb.sequence_number()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="ClipboardSync")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None

    def note_self_write(self) -> None:
        """自己刚写过剪贴板: 在宽限期内不要把它当成"用户复制"。"""
        self._self_write_until = time.monotonic() + SELF_WRITE_GRACE_S
        # 同时把序列号基线推到当前, 避免宽限期后立刻误报
        self._last_seq = cb.sequence_number()

    # -- 主循环 ------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception:  # noqa: BLE001
                self.stats["errors"] = self.stats.get("errors", 0) + 1
            self._stop.wait(POLL_INTERVAL_S)

    def _poll_once(self) -> None:
        if not self._enabled:
            return
        seq = cb.sequence_number()
        if seq == self._last_seq:
            return
        self._last_seq = seq
        # 自己刚写的 -> 跳过(防回环)
        if time.monotonic() < self._self_write_until:
            self.stats["skipped_self"] = \
                self.stats.get("skipped_self", 0) + 1
            return
        payload = cb.sample()
        if not payload:
            return
        blob = _encode(payload)
        if not blob:
            return
        h = hashlib.sha1(blob).digest()
        # 与上次发送的内容相同 -> 跳过(用户可能重复复制同一份)
        if h == self._last_hash:
            self.stats["skipped_same"] = \
                self.stats.get("skipped_same", 0) + 1
            return
        if len(blob) > MAX_TOTAL:
            self.stats["skipped_big"] = self.stats.get("skipped_big", 0) + 1
            return
        self._last_hash = h
        self._msg_seq = (self._msg_seq + 1) & 0xFFFFFFFF
        try:
            self._on_copy(blob, self._msg_seq)
            self.stats["sent"] = self.stats.get("sent", 0) + 1
        except Exception:  # noqa: BLE001
            self.stats["errors"] = self.stats.get("errors", 0) + 1


# --- 多格式打包(与 Deskflow 的 add() 多格式一致) ----------------------
#
# 网络编码: 每个格式一段
#   count(1) | [fmt_len(1) fmt_utf8 data_len(4) data] * count
# 这样一份内容可以同时带 text + html + bitmap, 对端按需选用。

def _encode(payload: dict[str, bytes]) -> bytes:
    items = [(k, v) for k, v in payload.items() if v]
    if not items:
        return b""
    out = bytearray()
    out.append(len(items) & 0xFF)
    for fmt, data in items:
        fb = fmt.encode("ascii")
        out.append(len(fb) & 0xFF)
        out += fb
        out += struct.pack("<I", len(data))
        out += data
    return bytes(out)


def decode(blob: bytes) -> dict[str, bytes]:
    """解析 _encode 的产物。任何一步不合规就返回 {} (绝不崩)。"""
    try:
        if not blob:
            return {}
        i = 0
        n = blob[i]
        i += 1
        out: dict[str, bytes] = {}
        for _ in range(n):
            fl = blob[i]
            i += 1
            fmt = blob[i:i + fl].decode("ascii", errors="ignore")
            i += fl
            (dl,) = struct.unpack_from("<I", blob, i)
            i += 4
            if dl > MAX_TOTAL or i + dl > len(blob):
                return {}
            out[fmt] = blob[i:i + dl]
            i += dl
        return out
    except Exception:  # noqa: BLE001
        return {}
