"""审查项 #4: 剪贴板支持注册格式 "PNG"。"""
import pathlib

root = pathlib.Path(__file__).parent
p = root / "com_bridge/clipboard.py"
s = p.read_text(encoding="utf-8")


def sub(old, new):
    global s
    assert s.count(old) == 1, f"未命中/重复: {old[:70]!r} (count={s.count(old)})"
    s = s.replace(old, new, 1)


# 1) 常量 + 新的格式 id
sub(
    """FMT_BITMAP = "bitmap"
FMT_FILES = "files"

FMT_IDS = {FMT_TEXT: 0, FMT_HTML: 1, FMT_BITMAP: 2, FMT_FILES: 3}""",
    """FMT_BITMAP = "bitmap"
FMT_FILES = "files"
# ★ 第 4 种: 注册格式 "PNG"(RegisterClipboardFormatW("PNG"))。
#   审查(2026-10-23)发现的遗漏: 有些程序(部分浏览器/设计工具/截图工具)
#   只把图放进这个格式, **不提供 CF_DIB** —— 以前这类图完全采不到。
#   另外 PNG 比裸 DIB 小一个数量级(4K 截图约 2MB vs 33MB), 有它时优先用它。
FMT_PNG = "png"

FMT_IDS = {FMT_TEXT: 0, FMT_HTML: 1, FMT_BITMAP: 2, FMT_FILES: 3,
           FMT_PNG: 4}""",
)

# 2) 取注册格式 id
sub(
    '''def _read_global(h) -> bytes:''',
    '''def _png_format() -> int:
    """注册格式 "PNG" 的 id(0 = 注册失败)。"""
    try:
        return int(user32.RegisterClipboardFormatW("PNG"))
    except Exception:  # noqa: BLE001
        return 0


def _read_global(h) -> bytes:''',
)

# 3) sample(): 先 PNG, 没有再退回 DIB
sub(
    """        # Bitmap: 规范是裸 DIB(CF_DIB), 与 Deskflow 一致
        dib = _read_dib_locked()
        if dib:
            out[FMT_BITMAP] = dib
        return out or None""",
    """        # 图片: 优先注册格式 "PNG"(小一个数量级, 且不少程序只提供它),
        # 没有再退回裸 DIB(Deskflow 的规范格式)。
        png = _read_png_locked()
        if png:
            out[FMT_PNG] = png
        else:
            dib = _read_dib_locked()
            if dib:
                out[FMT_BITMAP] = dib
        return out or None""",
)

# 4) 读 PNG + PNG->DIB 转换
sub(
    '''def _read_dib_locked() -> bytes:''',
    '''def _read_png_locked() -> bytes:
    """读注册格式 "PNG"(调用方已持有剪贴板锁)。不是合法 PNG 就丢掉。"""
    try:
        pf = _png_format()
        if not pf or not user32.IsClipboardFormatAvailable(pf):
            return b""
        raw = _read_global(user32.GetClipboardData(pf))
        if not raw or len(raw) < 8 or not raw.startswith(b"\\x89PNG"):
            return b""
        if len(raw) > MAX_PAYLOAD:
            return b""
        return raw
    except Exception:  # noqa: BLE001
        return b""


def png_to_dib(png: bytes) -> bytes:
    """把 PNG 解成裸 DIB(BI_RGB), 供写 CF_DIB 用(纯函数, 失败返回空)。

    ★ 为什么接收端还要解一份 DIB: 现代程序读注册的 "PNG", 但只认位图的
      老程序(画图、部分老版本 Office)只吃 CF_DIB。两个都写才能"谁都能粘"。
    """
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(png))
        img.load()
        if img.mode in ("P", "LA", "RGBA", "PA"):
            img = img.convert("RGBA")
        elif img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "BMP")
        bmp = buf.getvalue()
        if len(bmp) <= 14:
            return b""
        return normalise_dib(bmp[14:])       # 去掉 14 字节 BMP 文件头 -> DIB
    except Exception:  # noqa: BLE001
        return b""


def _read_dib_locked() -> bytes:''',
)

# 5) apply(): 写 PNG 格式, 并兼容只认位图的老程序
sub(
    """        bmp = payload.get(FMT_BITMAP)
        if bmp and len(bmp) >= 40:""",
    """        bmp = payload.get(FMT_BITMAP)
        png = payload.get(FMT_PNG)
        if png:
            # ① 注册的 "PNG" 格式(现代程序优先读它, 无损且小)
            pf = _png_format()
            if pf and len(png) <= MAX_PAYLOAD:
                try:
                    h = _alloc_global(png)
                    if h and user32.SetClipboardData(pf, h):
                        wrote = True
                except Exception:  # noqa: BLE001
                    pass
            # ② 同时给 CF_DIB, 让只认位图的老程序也能粘
            if not bmp:
                bmp = png_to_dib(png)
        if bmp and len(bmp) >= 40:""",
)

# 6) describe(): 认识 PNG
sub(
    """    for fmt in (FMT_TEXT, FMT_HTML, FMT_BITMAP, FMT_FILES):""",
    """    for fmt in (FMT_TEXT, FMT_HTML, FMT_BITMAP, FMT_PNG, FMT_FILES):""",
)
sub(
    """        if fmt == FMT_BITMAP:
            w, h, bits = image_size(data)
            parts.append(f"图片 {w}x{h}x{bits} ({len(data)} 字节)")""",
    """        if fmt == FMT_BITMAP:
            w, h, bits = image_size(data)
            parts.append(f"图片 {w}x{h}x{bits} ({len(data)} 字节)")
        elif fmt == FMT_PNG:
            parts.append(f"图片 PNG ({len(data)} 字节)")""",
)

p.write_text(s, encoding="utf-8")
print("clipboard.py: #4 patched")
