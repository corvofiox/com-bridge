"""剪贴板注册格式 "PNG" 的测试(审查项 #4)。

现场问题: 有些程序只把图放进注册格式 "PNG", 不提供 CF_DIB —— 这类图以前
完全采不到。另外 PNG 体积比裸 DIB 小一个数量级, 有它时优先用它。
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from com_bridge import clipboard as cb  # noqa: E402


def _png(w=4, h=3, mode="RGB", color=(10, 200, 30)):
    from PIL import Image
    img = Image.new(mode, (w, h), color if mode == "RGB" else None)
    if mode != "RGB":
        img = Image.new("RGB", (w, h), color).convert(mode)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- 格式表
def test_png_has_its_own_wire_id():
    assert cb.FMT_IDS[cb.FMT_PNG] == 4
    assert cb.ID_FMTS[4] == cb.FMT_PNG
    # 老格式的 id 不能动(跨版本兼容)
    assert cb.FMT_IDS[cb.FMT_TEXT] == 0
    assert cb.FMT_IDS[cb.FMT_BITMAP] == 2
    assert cb.FMT_IDS[cb.FMT_FILES] == 3


def test_png_survives_marshall_roundtrip():
    png = _png()
    blob = cb.marshall({cb.FMT_PNG: png})
    back = cb.unmarshall(blob)
    assert back[cb.FMT_PNG] == png


def test_describe_reports_png():
    png = _png()
    txt = cb.describe({cb.FMT_PNG: png})
    assert "PNG" in txt
    assert str(len(png)) in txt


# ---------------------------------------------------------------- PNG -> DIB
def test_png_to_dib_produces_valid_bitmapinfoheader():
    png = _png(4, 3)
    dib = cb.png_to_dib(png)
    assert len(dib) >= 40
    import struct
    bi_size, bi_w, bi_h = struct.unpack("<Iii", dib[:12])
    planes, bits, comp = struct.unpack("<HHI", dib[12:20])
    assert bi_size == 40
    assert planes == 1
    assert comp == cb.BI_RGB          # 规范化后必须是 BI_RGB
    assert bits in (24, 32)
    assert (bi_w, abs(bi_h)) == (4, 3)


def test_png_to_dib_rgba_input_ok():
    png = _png(2, 2, mode="RGBA")
    dib = cb.png_to_dib(png)
    assert dib


@pytest.mark.parametrize("bad", [b"", b"not a png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 4])
def test_png_to_dib_rejects_garbage(bad):
    assert cb.png_to_dib(bad) == b""


def test_png_smaller_than_dib_for_realistic_image():
    """优先级依据: 同一张图 PNG 应远小于裸 DIB。"""
    png = _png(400, 300)
    dib = cb.png_to_dib(png)
    assert dib and len(png) < len(dib)


# ---------------------------------------------------------------- 采样优先级
def test_sample_prefers_png_over_dib(monkeypatch):
    """两者都在时用 PNG(小), 不要再退回 DIB。"""
    png = _png()
    monkeypatch.setattr(cb, "_open_clipboard", lambda: True)
    monkeypatch.setattr(cb, "_close_clipboard", lambda: True)
    monkeypatch.setattr(cb, "_read_png_locked", lambda: png)
    monkeypatch.setattr(cb, "_read_dib_locked",
                        lambda: (_ for _ in ()).throw(AssertionError("不该读 DIB")))
    # 剪贴板里没有任何文本/HTML
    import ctypes
    monkeypatch.setattr(cb.user32, "IsClipboardFormatAvailable", lambda f: 0)
    out = cb.sample()
    assert out == {cb.FMT_PNG: png}


def test_sample_falls_back_to_dib_when_no_png(monkeypatch):
    dib = b"\x28\x00\x00\x00" + b"\x00" * 36 + b"\x00" * 16
    monkeypatch.setattr(cb, "_open_clipboard", lambda: True)
    monkeypatch.setattr(cb, "_close_clipboard", lambda: True)
    monkeypatch.setattr(cb, "_read_png_locked", lambda: b"")
    monkeypatch.setattr(cb, "_read_dib_locked", lambda: dib)
    monkeypatch.setattr(cb.user32, "IsClipboardFormatAvailable", lambda f: 0)
    out = cb.sample()
    assert out == {cb.FMT_BITMAP: dib}


# ---------------------------------------------------------------- 落盘路径
def test_apply_writes_png_format_and_dib(monkeypatch):
    """apply() 必须同时写 "PNG" 与 CF_DIB(现代程序 + 只认位图的老程序)。"""
    png = _png(3, 2)
    written = {}

    class _FakeUser32:
        @staticmethod
        def EmptyClipboard():
            return 1

        @staticmethod
        def SetClipboardData(fmt, h):
            written[fmt] = h
            return 1

    monkeypatch.setattr(cb, "_open_clipboard", lambda: True)
    monkeypatch.setattr(cb, "_close_clipboard", lambda: True)
    monkeypatch.setattr(cb, "_alloc_global", lambda data: ("handle", data))
    monkeypatch.setattr(cb, "ownership_format", lambda: 0)
    monkeypatch.setattr(cb, "user32", _FakeUser32)
    monkeypatch.setattr(cb, "_png_format", lambda: 49161)

    ok = cb.apply({cb.FMT_PNG: png})
    assert ok is True
    assert 49161 in written                       # 注册的 PNG
    assert cb.CF_DIB in written                   # 兼容性 DIB
    assert written[cb.CF_DIB][1][:4] == b"\x28\x00\x00\x00"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
