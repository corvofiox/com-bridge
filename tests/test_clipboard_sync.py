# -*- coding: utf-8 -*-
"""剪贴板内容编解码 + 变化检测的回归测试(2026-10-23 改为 Deskflow 规范)。

## 与旧实现的区别

* 传输从 UDP 改成 **TCP**(见 clip_transfer.py): 图片是**裸 DIB**(4K 截图约
  33MB), 文件可到 GB 级, UDP 分块必然丢包;
* 序列化改成 Deskflow `IClipboard::marshall` 的结构:
  `u32 格式数` + 每个格式 `u32 formatID + u32 长度 + 数据`;
* **格式 ID 与 Deskflow 的 `IClipboard::Format` 同值**: Text=0, HTML=1,
  Bitmap=2; 我们额外加 Files=3(桌面剪贴板复制文件是 CF_HDROP);
* 防回环改用 Deskflow 的**所有权标记**(注册 `"Com Bridge Ownership"`),
  时间窗只作兜底。
"""
from __future__ import annotations

import inspect
import os
import struct

import pytest

from com_bridge import clipboard as cb


# --- 序列化 ---------------------------------------------------------------

def test_format_ids_match_deskflow():
    """Text/HTML/Bitmap 的 id 必须与 Deskflow 的 enum 同值。"""
    assert cb.FMT_IDS[cb.FMT_TEXT] == 0
    assert cb.FMT_IDS[cb.FMT_HTML] == 1
    assert cb.FMT_IDS[cb.FMT_BITMAP] == 2
    assert cb.FMT_IDS[cb.FMT_FILES] == 3


def test_marshall_layout_matches_deskflow():
    """布局: u32 数量 | (u32 id + u32 len + data)*。"""
    blob = cb.marshall({cb.FMT_TEXT: b"hi"})
    (n,) = struct.unpack_from("<I", blob, 0)
    assert n == 1
    fid, size = struct.unpack_from("<II", blob, 4)
    assert fid == 0 and size == 2
    assert blob[12:] == b"hi"


def test_marshall_unmarshall_roundtrip():
    payload = {cb.FMT_TEXT: "中文".encode(), cb.FMT_HTML: b"<b>x</b>",
               cb.FMT_BITMAP: b"\x28\x00\x00\x00" + b"\x00" * 36}
    out = cb.unmarshall(cb.marshall(payload), max_total=1 << 20)
    assert out == payload


def test_unmarshall_skips_unknown_format():
    """对端多一种格式时不能整份作废(Deskflow 注释里的同一取舍)。"""
    blob = struct.pack("<I", 2)
    blob += struct.pack("<II", 0, 2) + b"hi"
    blob += struct.pack("<II", 9, 2) + b"zz"      # 未知 id
    assert cb.unmarshall(blob) == {cb.FMT_TEXT: b"hi"}


def test_unmarshall_rejects_truncated():
    blob = cb.marshall({cb.FMT_TEXT: b"hello"})
    assert cb.unmarshall(blob[:-2]) == {}


def test_unmarshall_rejects_absurd_count():
    blob = struct.pack("<I", 9999)
    assert cb.unmarshall(blob) == {}


def test_marshall_empty():
    assert cb.marshall({}) == b""
    assert cb.marshall({cb.FMT_TEXT: b""}) == b""


# --- 文件清单 -------------------------------------------------------------

def test_file_manifest_roundtrip():
    entries = [{"name": "照片/a.jpg", "size": 10, "src": "C:/x/a.jpg"},
               {"name": "b.txt", "size": 3, "src": "C:/x/b.txt"}]
    doc = cb.unpack_file_manifest(cb.pack_file_manifest(entries))
    assert doc["total"] == 13
    assert [f["name"] for f in doc["files"]] == ["照片/a.jpg", "b.txt"]
    assert "src" not in doc["files"][0], "本机绝对路径不能上线"


def test_file_manifest_rejects_garbage():
    assert cb.unpack_file_manifest(b"not json")["files"] == []
    assert cb.unpack_file_manifest(b"{}")["files"] == []


def test_path_list_codec():
    paths = [r"C:\a\b.txt", r"D:\中文\c.png"]
    assert cb.unpack_paths(cb.pack_paths(paths)) == paths


# --- DIB ------------------------------------------------------------------

def test_image_size_reads_header():
    dib = struct.pack("<IiiHHIIiiII", 40, 1920, -1080, 1, 32, 0,
                      1920 * 1080 * 4, 0, 0, 0, 0)
    assert cb.image_size(dib) == (1920, 1080, 32)


def test_bitmap_wire_format_is_dib_not_bmp_file():
    """Deskflow 的 Bitmap 规范是**裸 DIB**(不带 14 字节 BMP 文件头)。"""
    src = inspect.getsource(cb)
    assert "CF_DIB" in src
    assert "_dib_to_bmp" not in src, "不应再把 DIB 包成 BMP 文件"


# --- 变化检测 -------------------------------------------------------------

def _make_sync(on_copy=None, on_files=None):
    from com_bridge.clipboard_sync import ClipboardSync
    sent: list = []
    files: list = []
    s = ClipboardSync(on_copy or sent.append,
                      on_files=on_files or
                      (lambda blob, entries: files.append((blob, entries))))
    s._sent = sent
    s._files = files
    return s


def test_ownership_marker_is_registered():
    fmt = cb.ownership_format()
    assert fmt > 0, "所有权标记格式必须能注册(Deskflow 的防回环手段)"


def test_self_write_guard_by_owner_marker(monkeypatch):
    from com_bridge import clipboard as cbmod
    s = _make_sync()
    monkeypatch.setattr(cbmod, "sequence_number", lambda: 12345)
    monkeypatch.setattr(cbmod, "is_owned_by_us", lambda: True)
    monkeypatch.setattr(cbmod, "sample", lambda: {cb.FMT_TEXT: b"x"})
    s._last_seq = 1
    s._poll_once()
    assert s._sent == [], "自己写的剪贴板不能再发回去"
    assert s.stats["skipped_self"] == 1


def test_dedup_same_content(monkeypatch):
    from com_bridge import clipboard as cbmod
    s = _make_sync()
    seq = {"n": 0}

    def bump():
        seq["n"] += 1
        return seq["n"]

    monkeypatch.setattr(cbmod, "sequence_number", bump)
    monkeypatch.setattr(cbmod, "is_owned_by_us", lambda: False)
    monkeypatch.setattr(cbmod, "sample_files", lambda: [])
    monkeypatch.setattr(cbmod, "sample", lambda: {cb.FMT_TEXT: b"same"})
    s._poll_once()
    s._poll_once()
    assert len(s._sent) == 1, "同样的内容只发一次"
    assert s.stats["skipped_same"] == 1


def test_files_take_priority_over_text(monkeypatch, tmp_path):
    from com_bridge import clipboard as cbmod
    f = tmp_path / "a.txt"
    f.write_bytes(b"hello")
    s = _make_sync()
    seq = {"n": 0}

    def bump():
        seq["n"] += 1
        return seq["n"]

    monkeypatch.setattr(cbmod, "sequence_number", bump)
    monkeypatch.setattr(cbmod, "is_owned_by_us", lambda: False)
    monkeypatch.setattr(cbmod, "sample_files", lambda: [str(f)])
    monkeypatch.setattr(cbmod, "sample", lambda: {cb.FMT_TEXT: b"path text"})
    s._poll_once()
    assert s._sent == [], "复制文件时不该再发路径文本"
    assert len(s._files) == 1
    blob, entries = s._files[0]
    assert entries[0]["name"] == "a.txt" and entries[0]["size"] == 5
    doc = cb.unpack_file_manifest(cb.unmarshall(blob)[cb.FMT_FILES])
    assert doc["files"][0]["name"] == "a.txt"


def test_files_dedup_uses_mtime(monkeypatch, tmp_path):
    from com_bridge import clipboard as cbmod
    f = tmp_path / "a.txt"
    f.write_bytes(b"hello")
    s = _make_sync()
    seq = {"n": 0}

    def bump():
        seq["n"] += 1
        return seq["n"]

    monkeypatch.setattr(cbmod, "sequence_number", bump)
    monkeypatch.setattr(cbmod, "is_owned_by_us", lambda: False)
    monkeypatch.setattr(cbmod, "sample_files", lambda: [str(f)])
    s._poll_once()
    s._poll_once()
    assert len(s._files) == 1, "同名同大小同 mtime 的文件不重复传"
    st = f.stat()
    os.utime(f, (st.st_atime, st.st_mtime + 10))
    s._poll_once()
    assert len(s._files) == 2, "内容变了(mtime 变)必须重传"


def test_oversized_payload_skipped(monkeypatch):
    from com_bridge import clipboard as cbmod
    s = _make_sync()
    seq = {"n": 0}

    def bump():
        seq["n"] += 1
        return seq["n"]

    monkeypatch.setattr(cbmod, "sequence_number", bump)
    monkeypatch.setattr(cbmod, "is_owned_by_us", lambda: False)
    monkeypatch.setattr(cbmod, "sample_files", lambda: [])
    monkeypatch.setattr(cbmod, "sample",
                        lambda: {cb.FMT_BITMAP: b"\x00" * (cb.MAX_PAYLOAD + 1)})
    s._poll_once()
    assert s._sent == []
    assert s.stats["skipped_big"] == 1


# --- 接线 -----------------------------------------------------------------

def test_server_wires_new_clipboard():
    from com_bridge.server import Server
    src = inspect.getsource(Server._ensure_clipboard)
    assert "ClipInbox" in src and "ClipOutbox" in src
    assert "ClipboardSync(" in src and "on_files=" in src


def test_agent_wires_new_clipboard():
    from com_bridge.agent import Agent
    src = inspect.getsource(Agent._ensure_clipboard)
    assert "ClipInbox" in src and "ClipOutbox" in src
    assert "_on_local_files" in inspect.getsource(Agent)
    assert "def _on_local_files" in inspect.getsource(Agent)


def test_old_udp_transport_is_gone():
    import com_bridge.transport as tr
    assert not hasattr(tr, "ClipboardSender")
    assert not hasattr(tr, "ClipboardReceiver")


@pytest.mark.skipif(os.environ.get("CI") == "1", reason="需要真实剪贴板")
def test_text_roundtrip_real_clipboard():
    """真剪贴板: 写文本再读回来(同时验证所有权标记)。"""
    ok = cb.apply({cb.FMT_TEXT: "跨机剪贴板 test".encode("utf-8")})
    if not ok:
        pytest.skip("剪贴板被别的程序占用, 跳过")
    got = cb.sample()
    assert got and got.get(cb.FMT_TEXT) == "跨机剪贴板 test".encode("utf-8")
    assert cb.is_owned_by_us(), "写进去的内容必须带所有权标记"


# --- DIB 规范化(2026-10-23 图片传不过去的根因) -------------------------

def _mk_dib(w: int, h: int, bits: int = 32, compression: int = 0,
            masks: bytes = b"") -> bytes:
    pixels = b"\x40\x30\x20\xff" * (w * h)
    head = struct.pack("<IiiHHIIiiII", 40, w, -h, 1, bits, compression,
                       len(pixels), 0, 0, 0, 0)
    return head + masks + pixels


def test_dib_bit_rgb_passes_through():
    dib = _mk_dib(8, 4)
    assert cb.normalise_dib(dib) == dib


def test_dib_bitfields_is_normalised_not_dropped():
    """★ 真机的截图就是 BI_BITFIELDS(40 字节头 + 12 字节掩码 + BGRA 像素)。

    旧实现只接受 BI_RGB, 于是这种图被整份丢掉 —— 用户看到的就是
    "图片不能跨机粘贴"。Deskflow 的 normaliseMalformedMacDib 是**转换**: 
    把 biCompression 改回 BI_RGB 并丢掉掩码块。
    """
    masks = struct.pack("<III", 0x00FF0000, 0x0000FF00, 0x000000FF)
    src = _mk_dib(8, 4, compression=3, masks=masks)
    out = cb.normalise_dib(src)
    assert out, "BI_BITFIELDS 必须能被接受"
    assert len(out) == 40 + 8 * 4 * 4, "掩码块必须被丢掉"
    assert struct.unpack_from("<I", out, 16)[0] == 0, "必须改写为 BI_RGB"
    assert cb.image_size(out) == (8, 4, 32)
    # 像素原样保留
    assert out[40:] == src[52:]


def test_dib_jpeg_or_png_compression_rejected():
    """BI_JPEG(=4)/BI_PNG(=5) 的内容不是像素, 不能当位图发。"""
    for comp in (4, 5):
        assert cb.normalise_dib(_mk_dib(4, 4, compression=comp)) == b""


def test_dib_garbage_rejected():
    assert cb.normalise_dib(b"") == b""
    assert cb.normalise_dib(b"1234") == b""
    assert cb.normalise_dib(b"\x00" * 40) == b""


def test_describe_mentions_image_size():
    dib = _mk_dib(16, 8)
    text = cb.describe({cb.FMT_BITMAP: dib})
    assert "图片" in text and "16x8" in text
    assert "文字" in cb.describe({cb.FMT_TEXT: b"hi"})
