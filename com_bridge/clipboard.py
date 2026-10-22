# -*- coding: utf-8 -*-
"""剪贴板读写(Windows API 封装) —— 对齐 Deskflow 的 IClipboard / MSWindowsClipboard。

## 照搬 Deskflow 的部分(读了 `src/lib/deskflow/IClipboard.h`、
## `src/lib/platform/MSWindowsClipboard*.cpp` 之后确定的)

1. **只同步规范化格式**, 各端自己做本地转换。Deskflow 的 `IClipboard::Format`:
       Text   = 0   UTF-8, 换行 LF
       HTML   = 1   UTF-8 HTML 片段, 换行 LF
       Bitmap = 2   **BMP 去掉 14 字节文件头**(即从 BITMAPINFOHEADER 开始), 24/32bpp BI_RGB
   ★ 我们之前传的是"带 14 字节文件头的完整 BMP 文件", 与 Deskflow 的规范不一致 ——
     这里改成 **裸 DIB**, 与 `MSWindowsClipboardBitmapConverter::getWin32Format()`
     直接写 `CF_DIB` 的做法一致。

2. **所有权标记**(`MSWindowsClipboard::getOwnershipFormat`):
       `s_ownershipFormat = RegisterClipboardFormat("Deskflow Ownership")`
   写入时把该格式也放进剪贴板, 之后用 `IsClipboardFormatAvailable` 判断
   "这份内容是我们写的" —— 这比"写完后 1.2 秒内不采样"的时间窗可靠得多,
   而且对"大图片/文件要传好几百毫秒"的场景是必需的(时间窗早就过期了)。
   我们注册 `"Com Bridge Ownership"`。

3. **序列化格式**(`IClipboard::marshall`, IClipboard.cpp):
       4 字节 format 数 | 每个格式: 4 字节 formatID + 4 字节长度 + 数据
   我们照它来(不传 ASCII 格式名了)。

## 比 Deskflow 多出的一种格式(因为桌面剪贴板复制文件是 CF_HDROP)

    Files = 3   文件清单: `u16 个数` + 每个路径 UTF-8 + `\\0`(Deskflow 的
                `kMsgDDragInfo` 就是这个编码: "Number of files (2 bytes) |
                Null-separated file paths")
    ★ 注意: Deskflow 的**文件拖放**实现已经废弃(ProtocolTypes.h 里
      `kMsgDFileTransfer` / `kMsgDDragInfo` 都标了 "@deprecated File drag and
      drop is no longer implemented"), 所以没有可直接照搬的文件传输代码;
      我们只沿用它的消息语义(先清单、再按文件传 content), 传输本身走 TCP。
"""
from __future__ import annotations

import ctypes
import json
import struct
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32

# 标准剪贴板格式
CF_TEXT = 1
CF_BITMAP = 2
CF_DIB = 8
CF_UNICODETEXT = 13
CF_HDROP = 15
CF_HTML = None      # 需要 RegisterClipboardFormatW("HTML Format")

GMEM_MOVEABLE = 0x0002

# 规范化格式(与 Deskflow 的 Format 枚举同值同序)
FMT_TEXT = "text"
FMT_HTML = "html"
FMT_BITMAP = "bitmap"
FMT_FILES = "files"

FMT_IDS = {FMT_TEXT: 0, FMT_HTML: 1, FMT_BITMAP: 2, FMT_FILES: 3}
ID_FMTS = {v: k for k, v in FMT_IDS.items()}

# 体积上限: DIB 是**未压缩**的, 4K 截图 = 3840*2160*4 ≈ 33MB, 8K 翻四倍。
# 传输走 TCP(见 clip_transfer.py), 所以可以给得比原来的 8MB 宽松得多。
# Deskflow 的做法是由对端协商上限(`m_maximumClipboardSize`), 我们先用固定值,
# 需要时在 config.json 里调 clipboard_max_file_mb / clipboard_max_image_mb。
MAX_PAYLOAD = 64 * 1024 * 1024       # 64 MB(单个格式的上限)
MAX_TEXT = 16 * 1024 * 1024          # 文本/HTML 单独收一个更紧的口径

# 打开剪贴板的重试参数: 它同一时刻只能被一个进程持有
_OPEN_RETRIES = 8
_OPEN_SLEEP = 0.012

_OWNERSHIP_FORMAT_NAME = "Com Bridge Ownership"
_s_ownership = 0


# --- API 签名(64 位必须显式声明, 否则句柄被截断) ----------------------
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.GetClipboardSequenceNumber.argtypes = []
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wintypes.UINT
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalSize.restype = ctypes.c_size_t
shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                   wintypes.LPWSTR, wintypes.UINT]
shell32.DragQueryFileW.restype = wintypes.UINT


def sequence_number() -> int:
    """剪贴板序列号: 内容一变就递增。轮询这个比读内容便宜得多。"""
    try:
        return int(user32.GetClipboardSequenceNumber())
    except Exception:  # noqa: BLE001
        return 0


def _open_clipboard() -> bool:
    for _ in range(_OPEN_RETRIES):
        if user32.OpenClipboard(None):
            return True
        time.sleep(_OPEN_SLEEP)
    return False


def _close_clipboard() -> None:
    try:
        user32.CloseClipboard()
    except Exception:  # noqa: BLE001
        pass


def _html_format() -> int:
    try:
        return int(user32.RegisterClipboardFormatW("HTML Format"))
    except Exception:  # noqa: BLE001
        return 0


def ownership_format() -> int:
    """注册(一次)"Com Bridge Ownership" 格式, 返回 id。"""
    global _s_ownership
    if not _s_ownership:
        try:
            _s_ownership = int(
                user32.RegisterClipboardFormatW(_OWNERSHIP_FORMAT_NAME))
        except Exception:  # noqa: BLE001
            _s_ownership = 0
    return _s_ownership


def is_owned_by_us() -> bool:
    """当前剪贴板内容是不是我们自己刚写进去的(Deskflow 的所有权标记法)。"""
    fmt = ownership_format()
    if not fmt:
        return False
    try:
        return bool(user32.IsClipboardFormatAvailable(fmt))
    except Exception:  # noqa: BLE001
        return False


def _read_global(h) -> bytes:
    if not h:
        return b""
    try:
        size = int(kernel32.GlobalSize(wintypes.HGLOBAL(h)))
        if size <= 0:
            return b""
        ptr = kernel32.GlobalLock(wintypes.HGLOBAL(h))
        if not ptr:
            return b""
        try:
            return ctypes.string_at(ptr, size)
        finally:
            kernel32.GlobalUnlock(wintypes.HGLOBAL(h))
    except Exception:  # noqa: BLE001
        return b""


def _alloc_global(data: bytes, zero_pad: int = 8) -> int:
    """把 bytes 放进 HGLOBAL(交给 SetClipboardData 后不能再释放)。"""
    n = len(data)
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, n + zero_pad)
    if not h:
        return 0
    ptr = kernel32.GlobalLock(h)
    if not ptr:
        return 0
    try:
        ctypes.memmove(ptr, data, n)
        if zero_pad:
            ctypes.memset(ptr + n, 0, zero_pad)
    finally:
        kernel32.GlobalUnlock(h)
    return int(h)


# --- 采样 -------------------------------------------------------------

def sample() -> dict | None:
    """读取当前剪贴板内容。返回 {fmt: bytes} 或 None(空/不支持)。

    优先级: 有文件(CF_HDROP) 就只取文件 —— 复制文件时剪贴板里往往还有
    文本/路径, 混发会让对端既写文件又改文本剪贴板。
    """
    if not _open_clipboard():
        return None
    try:
        out: dict[str, bytes] = {}
        # ★ 文件不在这里产出: 清单需要**本地绝对路径**(发送方才能读盘),
        #   由 ClipboardSync 通过 sample_files() 取路径后自行组装 manifest。
        #   这里若同时有 CF_HDROP 和文本(Explorer 复制文件时常带路径文本),
        #   仍照常返回文本 —— 发送方会优先走文件通道。
        # Text
        if user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            raw = _read_global(user32.GetClipboardData(CF_UNICODETEXT))
            if raw:
                try:
                    s = raw.decode("utf-16-le", errors="ignore")
                    s = s.split("\x00", 1)[0]
                    s = s.replace("\r\n", "\n").replace("\r", "\n")
                    if s and len(s.encode("utf-8")) <= MAX_TEXT:
                        out[FMT_TEXT] = s.encode("utf-8")
                except Exception:  # noqa: BLE001
                    pass
        # HTML
        hf = _html_format()
        if hf and user32.IsClipboardFormatAvailable(hf):
            raw = _read_global(user32.GetClipboardData(hf))
            if raw:
                try:
                    s = raw.decode("utf-8", errors="ignore")
                    s = s.split("\x00", 1)[0]
                    s = s.replace("\r\n", "\n").replace("\r", "\n")
                    if s and len(s.encode("utf-8")) <= MAX_TEXT:
                        out[FMT_HTML] = s.encode("utf-8")
                except Exception:  # noqa: BLE001
                    pass
        # Bitmap: 规范是裸 DIB(CF_DIB), 与 Deskflow 一致
        dib = _read_dib_locked()
        if dib:
            out[FMT_BITMAP] = dib
        return out or None
    finally:
        _close_clipboard()


BI_RGB = 0
BI_BITFIELDS = 3


def normalise_dib(raw: bytes) -> bytes:
    """把剪贴板里的 DIB **规范化成 BI_RGB**(纯函数, 可单测)。

    ★★★ 这一步是照搬 Deskflow 的 `normaliseMalformedMacDib()`, 而不是我原先
      以为的"只接受 BI_RGB"。真发现(2026-10-23): 用户复制的截图在剪贴板里是

          biSize=40 biBitCount=32 biCompression=3(BI_BITFIELDS)  长度 = 40+12+像素

      也就是头部后面**多了 12 字节的三个颜色掩码**。当时的实现直接把它当
      "不支持的格式"丢掉了 —— 于是"图片不能跨机粘贴", 而其它格式(文字/文件)
      都正常。

      Deskflow 的做法是转换而不是丢弃:
        * 只针对 `biSize == 40 && biPlanes == 1 && biBitCount == 32 &&
          biCompression == BI_BITFIELDS && 长度恰好是 40 + w*h*4`(即
          INFOHEADER + 掩码 + BGRA 像素) 这一种"macOS 风格"的情况;
        * 做法: 把头部里的 biCompression 改回 BI_RGB, 并**丢掉那 12 字节掩码**
          (因为 header 只有 40 字节、没有掩码字段, 掩码只能跟在后面; 若保留,
          Windows 会把前 12 字节像素当成掩码读)。

    BI_RGB 的 DIB 原样返回; 其它压缩方式(BI_JPEG/BI_PNG 之类, 内容不是像素)
    仍然返回空 —— 那些没法直接当位图用。
    """
    if not raw or len(raw) < 40:
        return b""
    bi_size = struct.unpack_from("<I", raw, 0)[0]
    if bi_size < 40 or len(raw) < bi_size or len(raw) > MAX_PAYLOAD:
        return b""
    bi_width = struct.unpack_from("<i", raw, 4)[0]
    bi_height = struct.unpack_from("<i", raw, 8)[0]
    bi_planes = struct.unpack_from("<H", raw, 12)[0]
    bi_bits = struct.unpack_from("<H", raw, 14)[0]
    bi_compression = struct.unpack_from("<I", raw, 16)[0]
    if bi_compression == BI_RGB:
        return raw
    pixels = abs(int(bi_width)) * abs(int(bi_height)) * 4
    if (bi_size == 40 and bi_planes == 1 and bi_bits == 32
            and bi_compression == BI_BITFIELDS
            and len(raw) >= bi_size + 12 + pixels):
        head = bytearray(raw[:bi_size])
        struct.pack_into("<I", head, 16, BI_RGB)
        return bytes(head) + raw[bi_size + 12:bi_size + 12 + pixels]
    return b""


def _read_dib_locked() -> bytes:
    """读 CF_DIB(调用方已持有剪贴板锁)并规范化成 BI_RGB。"""
    try:
        if not user32.IsClipboardFormatAvailable(CF_DIB):
            return b""
        return normalise_dib(_read_global(user32.GetClipboardData(CF_DIB)))
    except Exception:  # noqa: BLE001
        return b""


def _read_hdrop_locked() -> list[str]:
    """读 CF_HDROP 的文件列表(调用方已持有剪贴板锁)。"""
    try:
        if not user32.IsClipboardFormatAvailable(CF_HDROP):
            return []
        h = user32.GetClipboardData(CF_HDROP)
        if not h:
            return []
        n = int(shell32.DragQueryFileW(wintypes.HANDLE(h), 0xFFFFFFFF,
                                       None, 0))
        out: list[str] = []
        for i in range(min(n, 4000)):
            need = int(shell32.DragQueryFileW(wintypes.HANDLE(h), i, None, 0))
            if need <= 0:
                continue
            buf = ctypes.create_unicode_buffer(need + 2)
            got = int(shell32.DragQueryFileW(wintypes.HANDLE(h), i, buf,
                                             need + 1))
            if got > 0 and buf.value:
                out.append(buf.value)
        return out
    except Exception:  # noqa: BLE001
        return []


def sample_files() -> list[str]:
    """单独取文件列表(供发送方展开目录用)。"""
    if not _open_clipboard():
        return []
    try:
        return _read_hdrop_locked()
    finally:
        _close_clipboard()


# --- 文件清单编码 ------------------------------------------------------
#
# ★ 与 Deskflow 的差异(有意为之): 它的 `kMsgDDragInfo` 用
#   "u16 个数 + null 分隔的绝对路径", 但那份实现已经废弃, 而且绝对路径
#   表达不了"复制一个文件夹"的树结构。我们改用 JSON 清单,
#   每条带**相对路径 + 字节数**, 于是文件夹复制也能原样还原。
#
#   {"files": [{"name": "照片/a.jpg", "size": 123}, ...], "total": 123}

def pack_file_manifest(entries: list[dict]) -> bytes:
    """entries: [{"name": 相对路径, "size": 字节}] -> JSON bytes。"""
    items = [{"name": str(e.get("name", "")),
              "size": int(e.get("size", 0))} for e in entries
             if e.get("name")]
    doc = {"files": items, "total": sum(i["size"] for i in items)}
    return json.dumps(doc, ensure_ascii=False).encode("utf-8")


def unpack_file_manifest(raw: bytes) -> dict:
    """解析清单; 不合规返回 {"files": [], "total": 0}。"""
    try:
        doc = json.loads(raw.decode("utf-8"))
        files = doc.get("files")
        if not isinstance(files, list):
            return {"files": [], "total": 0}
        out = []
        for f in files:
            if not isinstance(f, dict):
                continue
            name = str(f.get("name", ""))
            if not name:
                continue
            out.append({"name": name, "size": int(f.get("size", 0) or 0)})
        return {"files": out, "total": sum(f["size"] for f in out)}
    except Exception:  # noqa: BLE001
        return {"files": [], "total": 0}


# --- 写入 -------------------------------------------------------------

def apply(payload: dict[str, bytes], timeout_s: float = 0.0) -> bool:
    """把收到的内容写进本机剪贴板。返回是否成功。

    ★ 事务顺序(必须): Open -> Empty -> SetClipboardData * n -> Close。
    ★ 写完之后还会放一个"Com Bridge Ownership"标记(Deskflow 的所有权法),
      用于防回环 —— 见 clipboard_sync 的说明。
    """
    if not payload:
        return False
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        if _open_clipboard():
            break
        if time.monotonic() >= deadline:
            return False
        time.sleep(_OPEN_SLEEP)
    ok = False
    try:
        if not user32.EmptyClipboard():
            return False
        wrote = False
        txt = payload.get(FMT_TEXT)
        if txt:
            try:
                s = txt.decode("utf-8", errors="ignore").replace("\n", "\r\n")
                data = (s + "\x00").encode("utf-16-le")
                if len(data) <= MAX_TEXT:
                    h = _alloc_global(data)
                    if h and user32.SetClipboardData(CF_UNICODETEXT, h):
                        wrote = True
            except Exception:  # noqa: BLE001
                pass
        html = payload.get(FMT_HTML)
        hf = _html_format()
        if html and hf:
            try:
                s = html.decode("utf-8", errors="ignore").replace("\n", "\r\n")
                data = (s + "\x00").encode("utf-8")
                if len(data) <= MAX_TEXT:
                    h = _alloc_global(data)
                    if h and user32.SetClipboardData(hf, h):
                        wrote = True
            except Exception:  # noqa: BLE001
                pass
        bmp = payload.get(FMT_BITMAP)
        if bmp and len(bmp) >= 40:
            try:
                if len(bmp) <= MAX_PAYLOAD:
                    h = _alloc_global(bmp)
                    if h and user32.SetClipboardData(CF_DIB, h):
                        wrote = True
            except Exception:  # noqa: BLE001
                pass
        # FMT_FILES 在**传输时**是清单(相对路径+大小); 落到本机剪贴板时
        # 由调用方先把文件写进磁盘, 再把实际路径放进同名的 payload 键
        # (见 clip_transfer 的 _apply_files)。这里只处理路径列表。
        files = payload.get(FMT_FILES)
        if files:
            try:
                paths = unpack_paths(files)
                if paths and _set_hdrop_locked(paths):
                    wrote = True
            except Exception:  # noqa: BLE001
                pass
        if wrote:
            # 所有权标记: 只有 1 字节, 目的就是让 is_owned_by_us() 认出来
            of = ownership_format()
            if of:
                h = _alloc_global(b"\x01")
                if h:
                    user32.SetClipboardData(of, h)
        ok = wrote
    finally:
        _close_clipboard()
    return ok


def set_files(paths: list[str]) -> bool:
    """只往剪贴板放文件列表(CF_HDROP)。"""
    if not paths:
        return False
    if not _open_clipboard():
        return False
    try:
        if not user32.EmptyClipboard():
            return False
        ok = _set_hdrop_locked(paths)
        if ok:
            of = ownership_format()
            if of:
                h = _alloc_global(b"\x01")
                if h:
                    user32.SetClipboardData(of, h)
        return ok
    finally:
        _close_clipboard()


def _set_hdrop_locked(paths: list[str]) -> bool:
    """构造 DROPFILES + 双零结尾宽字符列表, 写入 CF_HDROP(持有锁时调用)。"""
    try:
        # DROPFILES: pFiles(4) pt(8) fNC(4) fWide(4) = 20
        head = struct.pack("<Iii ii", 20, 0, 0, 0, 1)
        body = b"".join(
            p.encode("utf-16-le") + b"\x00\x00" for p in paths) + b"\x00\x00"
        data = head + body
        if len(data) > MAX_PAYLOAD:
            return False
        h = _alloc_global(data)
        if not h:
            return False
        return bool(user32.SetClipboardData(CF_HDROP, h))
    except Exception:  # noqa: BLE001
        return False


# --- 序列化(与 Deskflow 的 IClipboard::marshall 同构) -----------------

def marshall(payload: dict[str, bytes]) -> bytes:
    """`u32 格式数` + 每个格式 `u32 id + u32 长度 + 数据`。"""
    items = [(FMT_IDS[k], v) for k, v in payload.items()
             if v and k in FMT_IDS]
    if not items:
        return b""
    out = bytearray(struct.pack("<I", len(items)))
    for fid, data in items:
        out += struct.pack("<II", fid, len(data))
        out += data
    return bytes(out)


def unmarshall(blob: bytes, max_total: int = MAX_PAYLOAD) -> dict[str, bytes]:
    """解析 marshall 的产物。任何一步不合规都返回 {} (绝不崩)。

    ★ 未知的 formatID 直接跳过(Deskflow 的注释说明了原因: 两端支持的格式
      数量可能不同, 遇到没有的格式不应该让整份内容作废)。
    """
    try:
        if len(blob) < 4:
            return {}
        (n,) = struct.unpack_from("<I", blob, 0)
        if n <= 0 or n > 64:
            return {}
        i = 4
        out: dict[str, bytes] = {}
        for _ in range(n):
            if i + 8 > len(blob):
                return {}
            fid, size = struct.unpack_from("<II", blob, i)
            i += 8
            if size > max_total or i + size > len(blob):
                return {}
            fmt = ID_FMTS.get(fid)
            if fmt is not None:
                out[fmt] = blob[i:i + size]
            i += size
        return out
    except Exception:  # noqa: BLE001
        return {}


def pack_paths(paths: list[str]) -> bytes:
    """`u16 个数` + 每个路径 UTF-8 + `\\0`(Deskflow kMsgDDragInfo 的编码)。"""
    out = bytearray(struct.pack("<H", min(len(paths), 0xFFFF)))
    for p in paths[:0xFFFF]:
        out += p.encode("utf-8", errors="replace") + b"\x00"
    return bytes(out)


def unpack_paths(raw: bytes) -> list[str]:
    """解析 pack_paths 的产物; 不合规返回 []。"""
    try:
        if len(raw) < 2:
            return []
        (n,) = struct.unpack_from("<H", raw, 0)
        i = 2
        out: list[str] = []
        for _ in range(n):
            j = raw.find(b"\x00", i)
            if j < 0:
                break
            out.append(raw[i:j].decode("utf-8", errors="replace"))
            i = j + 1
        return out
    except Exception:  # noqa: BLE001
        return []


def describe(payload: dict[str, bytes]) -> str:
    """把 {格式: 字节} 说成一句人话, 便于日志里确认"到底传了什么"。

    现场排障时这行很关键: 用户报"图片传不过去"时, 一眼就能看出是
    **没采到图**(只有 text), 还是采到了但体积异常。
    """
    parts = []
    for fmt in (FMT_TEXT, FMT_HTML, FMT_BITMAP, FMT_FILES):
        data = payload.get(fmt)
        if not data:
            continue
        if fmt == FMT_BITMAP:
            w, h, bits = image_size(data)
            parts.append(f"图片 {w}x{h}x{bits} ({len(data)} 字节)")
        elif fmt == FMT_FILES:
            doc = unpack_file_manifest(data)
            parts.append(f"文件 {len(doc['files'])} 个 ({doc['total']} 字节)")
        elif fmt == FMT_TEXT:
            parts.append(f"文字 {len(data)} 字节")
        else:
            parts.append(f"HTML {len(data)} 字节")
    return ", ".join(parts) or "(空)"


def image_size(dib: bytes) -> tuple[int, int, int]:
    """从 DIB 头取 (宽, 高, 位深), 供日志显示。失败返回 (0,0,0)。"""
    try:
        w, h = struct.unpack_from("<ii", dib, 4)
        bits = struct.unpack_from("<H", dib, 14)[0]
        return int(w), abs(int(h)), int(bits)
    except Exception:  # noqa: BLE001
        return 0, 0, 0
