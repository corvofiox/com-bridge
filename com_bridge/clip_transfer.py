# -*- coding: utf-8 -*-
"""剪贴板内容的跨机传输 —— **TCP**, 512KB 分块, 照搬 Deskflow 的 StreamChunker。

## 为什么是 TCP(这是 Deskflow 的成熟做法, 我们之前走错了)

Deskflow 的剪贴板走它那条 **TCP 客户端流**, 分块参数在
`src/lib/deskflow/StreamChunker.cpp`:

    static const size_t g_chunkSize = 512 * 1024;   // 512 KB
    ClipboardChunk: id(1) | sequence(4) | mark(1) | data
    mark: DataStart(1)(里面是总长度) / DataChunk(2) / DataEnd(3)

顺序、可靠性、流控全由 TCP 保证, 所以它**不需要**序号、重传、位图、限速。

我们原来的剪贴板走 UDP + 1000 字节分块 + 逐字节位图, 那是为小文本设计的。
一旦要传图片(裸 DIB, 4K 截图 ≈ 33MB)和文件(几百 MB), UDP 这条路必然崩:
几十万个包、丢一个整份作废、接收端还要一字节一位图的缓冲。

所以这里照 Deskflow 换成 TCP, 并把它那套 start/data/end 拆帧方式扩展到文件。

## 帧格式

    magic "CB" | kind(1) | len(u32 LE) | payload

    kind  1 CLIP_START   payload = 剪贴板内容总长(u64)
          2 CLIP_DATA    原始字节(每帧 ≤ 512KB)
          3 CLIP_END
          4 FILE_START   payload = u16 序号 + u64 字节数
          5 FILE_DATA    原始字节(每帧 ≤ 512KB)
          6 FILE_END
          7 DONE
          8 ACK          payload = u8 结果 + 说明文本

一条连接承载**一次复制**的全过程: 先剪贴板内容(含文件清单), 再逐个文件。
文件清单在剪贴板内容的 `files` 格式里(JSON: 相对路径 + 字节数), 所以接收端
在收到第一个 FILE_START 之前就知道该生成哪些文件名。
"""
from __future__ import annotations

import os
import socket
import struct
import threading
import time

from . import clipboard as cb
from .protocol import CLIPBOARD_PORT
from .transport import _BoundThread

MAGIC = b"CB"
HDR = struct.Struct("<2s B I")
CHUNK = 512 * 1024            # 照搬 Deskflow 的 g_chunkSize

K_CLIP_START = 1
K_CLIP_DATA = 2
K_CLIP_END = 3
K_FILE_START = 4
K_FILE_DATA = 5
K_FILE_END = 6
K_DONE = 7
K_ACK = 8

# 上限: 图片(裸 DIB)可以很大; 文件总量远大于剪贴板内容。两者都要有明确口径,
# 否则对端一个畸形头就能把磁盘/内存打爆。
MAX_CLIP_BYTES = 96 * 1024 * 1024        # 剪贴板内容(含清单)上限
MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024  # 一次复制所有文件之和上限
MAX_FILES = 4000                         # 文件个数上限
CONNECT_TIMEOUT_S = 5.0
IO_TIMEOUT_S = 60.0
ACK_TIMEOUT_S = 300.0                    # 对端写盘可能很慢(2GB ≈ 20s, 留足)
KEEP_BATCHES = 30                        # 接收目录保留最近多少批


# --- 路径消毒(接收侧强制, 不能信对端) --------------------------------

_RESERVED = frozenset(
    "CON PRN AUX NUL COM1 COM2 COM3 COM4 COM5 COM6 COM7 COM8 COM9 "
    "LPT1 LPT2 LPT3 LPT4 LPT5 LPT6 LPT7 LPT8 LPT9".split())
MAX_DEPTH = 8
MAX_NAME_LEN = 200


def sanitize_name(raw: str) -> str:
    """压成安全的**文件名**(不含目录)。不可用时返回空串。"""
    if not raw:
        return ""
    s = str(raw).replace("\\", "/").rsplit("/", 1)[-1]
    s = "".join(ch for ch in s if ch.isprintable())
    s = s.strip().strip(".")
    if not s:
        return ""
    stem, dot, ext = s.partition(".")
    if stem.upper() in _RESERVED:
        s = "_" + s
    if len(s) > MAX_NAME_LEN:
        keep = MAX_NAME_LEN - (len(ext) + 1 if dot else 0)
        s = s[:max(1, keep)] + (("." + ext) if dot else "")
    return s


def sanitize_relpath(raw: str) -> str:
    """相对路径消毒成 ``a/b/c.txt``; 有任何一段不合法就整体拒收。"""
    if not raw:
        return ""
    s = str(raw).replace("\\", "/").strip("/")
    parts = [p for p in s.split("/") if p not in ("", ".")]
    if not parts or len(parts) > MAX_DEPTH:
        return ""
    out = []
    for p in parts:
        if p == "..":
            return ""
        clean = sanitize_name(p)
        if not clean:
            return ""
        out.append(clean)
    return "/".join(out)


# --- 收集要发送的文件(展开目录) --------------------------------------

def collect_sources(paths: list[str]) -> tuple[list[dict], int, list[str]]:
    """把用户复制的路径展开成可发送条目 ``[{"name","size","src"}]``。

    目录**递归展开**: 复制一个文件夹到对面, 对面得到一个同名文件夹
    (相对路径保留)。返回 ``(entries, 总字节, 警告)``。
    """
    entries: list[dict] = []
    total = 0
    warnings: list[str] = []
    seen: set[str] = set()

    def add(rel: str, src: str, size: int) -> None:
        nonlocal total
        name = sanitize_relpath(rel)
        if not name:
            warnings.append(f"跳过非法路径: {rel!r}")
            return
        base = name
        i = 2
        while name.lower() in seen:
            stem, dot, ext = base.rpartition(".")
            name = (f"{stem} ({i}).{ext}" if dot else f"{base} ({i})")
            i += 1
        seen.add(name.lower())
        entries.append({"name": name, "size": int(size), "src": src})
        total += int(size)

    for p in paths:
        try:
            if os.path.isdir(p):
                root = os.path.basename(os.path.normpath(p)) or "folder"
                for dirpath, _dirs, files in os.walk(p):
                    for fn in files:
                        full = os.path.join(dirpath, fn)
                        try:
                            rel = os.path.join(root, os.path.relpath(full, p))
                            add(rel, full, os.path.getsize(full))
                        except OSError as exc:
                            warnings.append(f"读不到 {full}: {exc}")
            elif os.path.isfile(p):
                add(os.path.basename(p), p, os.path.getsize(p))
            else:
                warnings.append(f"不存在: {p}")
        except OSError as exc:
            warnings.append(f"读不到 {p}: {exc}")
    return entries, total, warnings


# --- 帧读写 -----------------------------------------------------------

def pack_frame(kind: int, payload: bytes = b"") -> bytes:
    return HDR.pack(MAGIC, kind, len(payload)) + payload


def send_frame(sock: socket.socket, kind: int, payload: bytes = b"") -> None:
    sock.sendall(HDR.pack(MAGIC, kind, len(payload)) + payload)


def read_frame(sock: socket.socket) -> tuple[int, bytes] | None:
    head = _recv_exact(sock, HDR.size)
    if head is None:
        return None
    magic, kind, n = HDR.unpack(head)
    if magic != MAGIC or n > MAX_CLIP_BYTES + MAX_FILE_BYTES + 1024:
        return None
    body = _recv_exact(sock, n) if n else b""
    if body is None:
        return None
    return kind, body


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(min(CHUNK, n - len(buf)))
        except (socket.timeout, TimeoutError):
            return None
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


# --- 接收目录 ---------------------------------------------------------

def default_recv_root() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "ComBridge", "received")


def _prune_batches(root: str, keep: int = KEEP_BATCHES) -> None:
    """只留最近 keep 批(更早的没人再粘贴了, 删掉免得磁盘无限涨)。"""
    try:
        names = sorted(n for n in os.listdir(root)
                       if os.path.isdir(os.path.join(root, n)))
    except OSError:
        return
    for n in (names[:-keep] if len(names) > keep else []):
        path = os.path.join(root, n)
        try:
            for dirpath, dirs, files in os.walk(path, topdown=False):
                for fn in files:
                    os.remove(os.path.join(dirpath, fn))
                for d in dirs:
                    os.rmdir(os.path.join(dirpath, d))
            os.rmdir(path)
        except OSError:
            pass


# --- 接收 -------------------------------------------------------------

class ClipInbox(_BoundThread):
    """接收对端"复制"的内容: 剪贴板数据 + (可选)文件, 落盘后回调。

    回调 ``on_payload(payload)``: 与原来一样, payload 是 {fmt: bytes},
    交给 clipboard.apply() 写进本机剪贴板。文件落盘后额外回调一次
    ``{files: pack_paths(实际落盘路径)}``。
    """

    def __init__(self, port: int = CLIPBOARD_PORT, on_payload=None,
                 recv_root: str | None = None,
                 max_clip: int = MAX_CLIP_BYTES,
                 max_files: int = MAX_FILE_BYTES):
        super().__init__("ClipInbox", port, socket.SOCK_STREAM)
        self._on_payload = on_payload
        self.recv_root = recv_root or default_recv_root()
        self.max_clip = int(max_clip)
        self.max_files = int(max_files)
        self.stats = {"links": 0, "payloads": 0, "file_sets": 0,
                      "files": 0, "bytes": 0, "rejected": 0}
        self.last_error: str | None = None
        self.last_recv_dir: str | None = None

    def set_handler(self, fn) -> None:
        self._on_payload = fn

    def bind_now(self) -> None:
        super().bind_now()
        assert self._sock is not None
        if self.port == 0:
            self.port = self._sock.getsockname()[1]
        try:
            self._sock.listen(1)
        except OSError:
            pass

    def run(self) -> None:
        s = self._sock
        if s is None:
            return
        s.settimeout(0.5)
        try:
            os.makedirs(self.recv_root, exist_ok=True)
            _prune_batches(self.recv_root)
        except OSError:
            pass
        while not self._stop.is_set():
            try:
                conn, _addr = s.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            self.stats["links"] += 1
            try:
                self._handle(conn)
            except Exception as exc:  # noqa: BLE001
                self.stats["rejected"] += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(IO_TIMEOUT_S)
        clip = bytearray()
        clip_total: int | None = None
        manifest: list[dict] = []
        batch_dir = os.path.join(
            self.recv_root,
            time.strftime("%Y%m%d-%H%M%S") +
            f"-{int(time.time() * 1000) % 1000:03d}")
        saved: list[str] = []
        total_written = 0
        cur_fh = None
        cur_left = 0
        ok = False
        try:
            while True:
                fr = read_frame(conn)
                if fr is None:
                    self.last_error = "帧读取失败"
                    break
                kind, payload = fr
                if kind == K_CLIP_START:
                    (clip_total,) = struct.unpack("<Q", payload[:8])
                    clip.clear()
                    if clip_total > self.max_clip:
                        self.stats["rejected"] += 1
                        self.last_error = f"剪贴板内容过大({clip_total})"
                        break
                elif kind == K_CLIP_DATA:
                    clip += payload
                    if len(clip) > self.max_clip:
                        self.last_error = "剪贴板内容超限"
                        break
                elif kind == K_CLIP_END:
                    items = cb.unmarshall(bytes(clip))
                    if not items:
                        self.last_error = "内容解码失败"
                        break
                    files_raw = items.pop(cb.FMT_FILES, b"")
                    if files_raw:
                        doc = cb.unpack_file_manifest(files_raw)
                        manifest = doc["files"]
                    if items:
                        print(f"[clip] 收到 {cb.describe(items)} / "
                              f"{len(clip)} 字节", flush=True)
                        self._emit(items)
                        self.stats["payloads"] += 1
                elif kind == K_FILE_START:
                    idx, size = struct.unpack("<HQ", payload[:10])
                    if idx >= len(manifest) or size > self.max_files:
                        self.last_error = "文件帧头部非法"
                        break
                    entry = manifest[idx]
                    rel = sanitize_relpath(entry.get("name", ""))
                    if not rel:
                        self.stats["rejected"] += 1
                        self.last_error = f"非法路径 {entry.get('name')!r}"
                        break
                    os.makedirs(batch_dir, exist_ok=True)
                    dest = os.path.join(batch_dir, *rel.split("/"))
                    os.makedirs(os.path.dirname(dest) or batch_dir,
                                exist_ok=True)
                    cur_fh = open(dest, "wb")
                    cur_left = int(size)
                    saved.append(dest)
                elif kind == K_FILE_DATA:
                    if cur_fh is None:
                        self.last_error = "文件数据先于头部"
                        break
                    cur_fh.write(payload)
                    cur_left -= len(payload)
                    total_written += len(payload)
                    if total_written > self.max_files:
                        self.last_error = "文件总量超限"
                        break
                elif kind == K_FILE_END:
                    if cur_fh is not None:
                        cur_fh.close()
                        cur_fh = None
                    if cur_left > 0:
                        self.last_error = f"文件长度不足(差 {cur_left} 字节)"
                        break
                elif kind == K_DONE:
                    ok = True
                    break
            if ok:
                send_frame(conn, K_ACK, b"\x01" + b"ok")
                if saved:
                    self.stats["file_sets"] += 1
                    self.stats["files"] += len(saved)
                    self.stats["bytes"] += total_written
                    self.last_recv_dir = batch_dir
                    print(f"[clip] 收到 {len(saved)} 个文件"
                          f"({total_written} 字节) -> {batch_dir}",
                          flush=True)
                    self._emit({cb.FMT_FILES: cb.pack_paths(saved)})
            else:
                send_frame(conn, K_ACK, b"\x00" + (self.last_error or "失败")
                           .encode("utf-8")[:200])
                # ★ 拒收时对端可能还在灌数据。直接 close 会因为"接收缓冲里还有
                #   未读数据"而发 RST, 把刚发出去的 ACK 一起冲掉 —— 发送端只
                #   看到连接被重置, 日志里就成了含糊的"对端没有确认"(实测)。
                #   这里先短时间把对端数据读掉再关, 让 ACK 能正常送达。
                _drain(conn)
                if self.last_error:
                    print(f"[clip] !! 接收失败: {self.last_error}", flush=True)
        finally:
            if cur_fh is not None:
                try:
                    cur_fh.close()
                except OSError:
                    pass

    def _emit(self, payload: dict) -> None:
        if self._on_payload is None or not payload:
            return
        try:
            self._on_payload(payload)
        except Exception:  # noqa: BLE001
            pass


# --- 发送 -------------------------------------------------------------

def _drain(conn: socket.socket, budget_s: float = 0.4,
           max_bytes: int = 8 << 20) -> None:
    """把对端还在发来的数据读掉一点, 避免 close 触发 RST 冲掉我们发出去的 ACK。"""
    deadline = time.monotonic() + budget_s
    total = 0
    try:
        conn.settimeout(0.05)
        while time.monotonic() < deadline and total < max_bytes:
            try:
                chunk = conn.recv(65536)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            if not chunk:
                return
            total += len(chunk)
    except OSError:
        pass


class ClipOutbox:
    """把"要发出去的内容"排队, 由**单一线程**串行发送。

    ★ 为什么要有队列: 复制文件可能是几百 MB, 绝不能占着剪贴板轮询线程;
      而两次复制若各起一个线程, 对端可能同时收到两份内容(剪贴板只该有一个
      当前值)。串行队列保证"后一次复制排在前一次之后", 语义干净。
    """

    def __init__(self, host: str, port: int = CLIPBOARD_PORT):
        self.host = host
        self.port = port
        self._q: list[tuple[bytes, list[dict]]] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="ClipOutbox")
        self.sent = 0
        self.failed = 0
        self.last_error: str | None = None

    def start(self) -> None:
        self._thread.start()

    def put(self, blob: bytes, entries: list[dict] | None = None) -> None:
        with self._lock:
            self._q.append((blob, entries or []))
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(0.5)
            self._wake.clear()
            while True:
                with self._lock:
                    if not self._q:
                        break
                    blob, entries = self._q.pop(0)
                ok, _sent, msg = push_clipboard(self.host, self.port,
                                                blob, entries)
                if ok:
                    self.sent += 1
                else:
                    self.failed += 1
                    self.last_error = msg
                    print(f"[clip] !! 发送失败: {msg}", flush=True)


def push_clipboard(host: str, port: int, blob: bytes,
                   entries: list[dict] | None = None,
                   timeout: float = ACK_TIMEOUT_S) -> tuple[bool, int, str]:
    """把一份剪贴板内容(可选带文件)推给对端。返回 (成功, 发送字节, 说明)。"""
    entries = entries or []
    if not blob:
        return False, 0, "空内容"
    if len(blob) > MAX_CLIP_BYTES:
        return False, 0, f"内容过大({len(blob)} 字节)"
    total_files = sum(int(e.get("size", 0)) for e in entries)
    if total_files > MAX_FILE_BYTES:
        return False, 0, f"文件总量过大({total_files} 字节)"
    if len(entries) > MAX_FILES:
        return False, 0, f"文件数过多({len(entries)})"
    try:
        sock = socket.create_connection((host, port),
                                        timeout=CONNECT_TIMEOUT_S)
    except OSError as exc:
        return False, 0, f"连不上 {host}:{port} ({exc})"
    sent = 0
    try:
        sock.settimeout(IO_TIMEOUT_S)
        send_frame(sock, K_CLIP_START, struct.pack("<Q", len(blob)))
        for off in range(0, len(blob), CHUNK):
            piece = blob[off:off + CHUNK]
            send_frame(sock, K_CLIP_DATA, piece)
            sent += len(piece)
        send_frame(sock, K_CLIP_END)
        for idx, e in enumerate(entries):
            src = e.get("src")
            size = int(e.get("size", 0))
            if not src:
                return False, sent, f"缺少源路径: {e.get('name')}"
            send_frame(sock, K_FILE_START, struct.pack("<HQ", idx, size))
            with open(src, "rb") as fh:
                left = size
                while left > 0:
                    piece = fh.read(min(CHUNK, left))
                    if not piece:
                        break
                    send_frame(sock, K_FILE_DATA, piece)
                    left -= len(piece)
                    sent += len(piece)
                if left > 0:
                    return False, sent, f"源文件读不满: {src}"
            send_frame(sock, K_FILE_END)
        send_frame(sock, K_DONE)
        try:
            sock.settimeout(timeout)
            fr = read_frame(sock)
        except (socket.timeout, TimeoutError):
            return False, sent, "等对端确认超时"
        if fr is None or fr[0] != K_ACK:
            return False, sent, "对端没有确认"
        ok = bool(fr[1][:1]) and fr[1][:1] == b"\x01"
        msg = fr[1][1:].decode("utf-8", errors="replace") or (
            "已送达" if ok else "对端拒收")
        return ok, sent, msg
    except OSError as exc:
        return False, sent, f"传输中断: {exc}"
    finally:
        try:
            sock.close()
        except OSError:
            pass
