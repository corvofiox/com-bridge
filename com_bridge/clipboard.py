# -*- coding: utf-8 -*-
"""剪贴板读写(Windows API 封装) —— 参考 Deskflow 的 MSWindowsClipboard。

## Deskflow 的设计要点(已读源码确认)
* `IClipboard::Format` 只支持三种**规范化格式**, 各平台各自转换:
    - `Text`   : UTF-8, 换行为 LF(不是 CRLF)
    - `HTML`   : UTF-8 的 HTML 片段
    - `Bitmap` : BMP 文件内容(24/32bpp, BI_RGB)
  好处: 跨平台/跨机器只需协商这三种, 不用管各系统的私有剪贴板格式。
* `open()` / `empty()` / `add()` / `close()` 的事务式 API:
  Windows 的剪贴板必须"先 OpenClipboard 才能改", 而且**同一时刻只能
  有一个进程持有它** —— 所以每次操作都要重试(Deskflow 也是这么做的)。
* `getTime()` 用 GetClipboardSequenceNumber 的等价物检测变化。

## 我们的实现取舍
* 只同步 **Text / HTML / Bitmap** 三种(与 Deskflow 一致)。
  不做"任意私有格式"转发 —— 那会引入格式不兼容与体积失控。
* 用 `GetClipboardSequenceNumber` 检测变化: 它由系统在**任何**剪贴板
  内容变更时递增, 比轮询内容便宜得多(不需要打开剪贴板)。
* 文本用 UTF-8 双向往返; 写入 Windows 时转成 UTF-16(CF_UNICODETEXT),
  读出来时统一 LF 换行, 避免跨机后多出 \\r。
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# 标准剪贴板格式
CF_TEXT = 1
CF_BITMAP = 2
CF_UNICODETEXT = 13
CF_HTML = None      # 需要 RegisterClipboardFormatW("HTML Format")

GMEM_MOVEABLE = 0x0002

# Deskflow 的三种规范化格式
FMT_TEXT = "text"
FMT_HTML = "html"
FMT_BITMAP = "bitmap"

# 大小上限: 防止一次粘贴几十 MB 的图片把网络/内存打爆。
# ★ 口径说明: 这里是**数据本身**的上限(不含传输层编码头)。
#   传输层另有 MAX_DATA / MAX_TOTAL(见 clipboard_sync), 两者必须一致,
#   否则会出现"采集端认为可以、发送端拒绝"或相反的不一致。
MAX_PAYLOAD = 8 * 1024 * 1024        # 8 MB

# 打开剪贴板的重试参数: 它同一时刻只能被一个进程持有
_OPEN_RETRIES = 8
_OPEN_SLEEP = 0.012


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


def sequence_number() -> int:
    """剪贴板序列号: 内容一变就递增。轮询这个比读内容便宜得多。"""
    try:
        return int(user32.GetClipboardSequenceNumber())
    except Exception:  # noqa: BLE001
        return 0


def _open_clipboard() -> bool:
    """打开剪贴板(带重试)。必须与 close_clipboard 配对。"""
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
    """注册/获取 "HTML Format" 的格式 id(每次调用都要查, 不能缓存)。"""
    try:
        return int(user32.RegisterClipboardFormatW("HTML Format"))
    except Exception:  # noqa: BLE001
        return 0


def _read_global(h) -> bytes:
    """读 HGLOBAL 的完整内容。"""
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


def _alloc_global(data: bytes) -> int:
    """把 bytes 放进 HGLOBAL(调用方交给 SetClipboardData 后不能再释放)。"""
    n = len(data)
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, n + 8)
    if not h:
        return 0
    ptr = kernel32.GlobalLock(h)
    if not ptr:
        return 0
    try:
        ctypes.memmove(ptr, data, n)
        # 双零结尾: 文本/HTML 都需要
        ctypes.memset(ptr + n, 0, min(8, n + 8 - n))
    finally:
        kernel32.GlobalUnlock(h)
    return int(h)


# --- 对外: 采样 ---------------------------------------------------------

def sample() -> dict | None:
    """读取当前剪贴板内容。返回 {fmt: bytes} 或 None(空/不支持)。

    ★ 只读我们要同步的三种格式, 且优先 Text(最常见、最便宜)。
      同一份内容若同时有 Text 和 HTML, 两个都带上 —— 让对端按需选用,
      这是 Deskflow 的做法(它一次 add 多个格式)。
    """
    if not _open_clipboard():
        return None
    try:
        out: dict[str, bytes] = {}
        # Text: 优先 Unicode
        if user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            raw = _read_global(user32.GetClipboardData(CF_UNICODETEXT))
            if raw:
                try:
                    s = raw.decode("utf-16-le", errors="ignore")
                    s = s.split("\x00", 1)[0]
                    # 统一换行为 LF(Deskflow 的规范)
                    s = s.replace("\r\n", "\n").replace("\r", "\n")
                    if s:
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
                    if s:
                        out[FMT_HTML] = s.encode("utf-8")
                except Exception:  # noqa: BLE001
                    pass
        # Bitmap(DIB -> BMP 文件格式, Deskflow 也是这个规范)
        if user32.IsClipboardFormatAvailable(CF_BITMAP):
            bmp = _dib_to_bmp()
            if bmp:
                out[FMT_BITMAP] = bmp
        if not out:
            return None
        # 体积上限
        total = sum(len(v) for v in out.values())
        if total > MAX_PAYLOAD:
            # 优先保留 text/html, 丢掉过大图片
            out.pop(FMT_BITMAP, None)
            if sum(len(v) for v in out.values()) > MAX_PAYLOAD:
                return None
        return out
    finally:
        _close_clipboard()


def _dib_to_bmp() -> bytes:
    """把剪贴板里的 DIB(CF_DIB) 转成完整 BMP 文件字节。

    ★ 为什么用 CF_DIB 而不是 CF_BITMAP:
      CF_BITMAP 给的是 HBITMAP 句柄, 跨进程取像素很麻烦(要 GetDIBits);
      CF_DIB 直接就是 BITMAPINFOHEADER + 像素数据, 只需补一个 14 字节的
      BITMAPFILEHEADER 就成了标准 BMP 文件 —— 这也是 Deskflow 选的形式。
    """
    CF_DIB = 8
    if not user32.IsClipboardFormatAvailable(CF_DIB):
        return b""
    raw = _read_global(user32.GetClipboardData(CF_DIB))
    if not raw or len(raw) < 40:
        return b""
    try:
        import struct
        # BITMAPINFOHEADER: biSize(4) biWidth(4) biHeight(4) biPlanes(2)
        # biBitCount(2) biCompression(4) biSizeImage(4) ...
        bi_size = struct.unpack_from("<I", raw, 0)[0]
        if bi_size < 40 or len(raw) < bi_size:
            return b""
        bi_bitcount = struct.unpack_from("<H", raw, 14)[0]
        bi_compression = struct.unpack_from("<I", raw, 16)[0]
        bi_sizeimage = struct.unpack_from("<I", raw, 20)[0]
        # 只接受 BI_RGB(Deskflow 的 Bitmap 规范要求)
        if bi_compression != 0:
            return b""
        # 调色板大小(<=8bpp 时)
        palette = 0
        if bi_bitcount <= 8:
            colors = struct.unpack_from("<I", raw, 32)[0]
            palette = (colors or (1 << bi_bitcount)) * 4
        pixel_bytes = bi_sizeimage or (len(raw) - bi_size - palette)
        off_bits = 14 + bi_size + palette
        file_size = off_bits + pixel_bytes
        # BITMAPFILEHEADER: 'BM' + 文件大小 + 保留 + 数据偏移
        fh = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, off_bits)
        body = raw[:bi_size + palette] + raw[bi_size + palette:
                                             bi_size + palette + pixel_bytes]
        return fh + body
    except Exception:  # noqa: BLE001
        return b""


# --- 对外: 写入 ---------------------------------------------------------

def apply(payload: dict[str, bytes]) -> bool:
    """把收到的内容写进本机剪贴板。返回是否成功。

    ★ 事务顺序(必须): Open -> Empty -> SetClipboardData * n -> Close。
      Empty 是必需的, 否则旧格式会残留在剪贴板里, 粘贴时可能取到旧数据。
    """
    if not payload:
        return False
    if not _open_clipboard():
        return False
    ok = False
    try:
        if not user32.EmptyClipboard():
            return False
        wrote = False
        # Text -> CF_UNICODETEXT
        txt = payload.get(FMT_TEXT)
        if txt:
            try:
                s = txt.decode("utf-8", errors="ignore")
                # Windows 要 CRLF
                s = s.replace("\n", "\r\n")
                data = (s + "\x00").encode("utf-16-le")
                if len(data) <= MAX_PAYLOAD:
                    h = _alloc_global(data)
                    if h and user32.SetClipboardData(CF_UNICODETEXT, h):
                        wrote = True
            except Exception:  # noqa: BLE001
                pass
        # HTML
        html = payload.get(FMT_HTML)
        hf = _html_format()
        if html and hf:
            try:
                s = html.decode("utf-8", errors="ignore").replace("\n", "\r\n")
                data = (s + "\x00").encode("utf-8")
                if len(data) <= MAX_PAYLOAD:
                    h = _alloc_global(data)
                    if h and user32.SetClipboardData(hf, h):
                        wrote = True
            except Exception:  # noqa: BLE001
                pass
        # Bitmap
        bmp = payload.get(FMT_BITMAP)
        if bmp and len(bmp) > 14:
            try:
                CF_DIB = 8
                # 去掉 14 字节文件头就是 DIB
                dib = bmp[14:]
                if len(dib) <= MAX_PAYLOAD:
                    h = _alloc_global(dib)
                    if h and user32.SetClipboardData(CF_DIB, h):
                        wrote = True
            except Exception:  # noqa: BLE001
                pass
        ok = wrote
    finally:
        _close_clipboard()
    return ok
