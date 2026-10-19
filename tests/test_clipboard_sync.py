# -*- coding: utf-8 -*-
"""★★★ 回归: 剪贴板跨屏同步(参考 Deskflow 的 Clipboard + ClipboardChunk)。

## Deskflow 的设计(已读源码确认)
* `IClipboard::Format` 只有三种**规范化格式**, 各平台自己做转换:
    - Text   : UTF-8, 换行 LF
    - HTML   : UTF-8 片段
    - Bitmap : BMP 文件(24/32bpp, BI_RGB)
  这样网络上只需协商三种, 不用管各系统的私有剪贴板格式。
* 序列号检测变化(不轮询内容)。
* **分块传输**(ClipboardChunk 的 start/data/end + seq + offset)。
* 防回环: 自己写进去的内容不能再发出去。

## 我们的实现要点(踩过的坑)
1. **包必须 < MTU**: 每块 1000 字节 + 19 字节头 = 1019 < 1500。
   音频那边已因 3840 字节的包被 IP 分片吃过大亏。
2. **重组必须处理 UDP 乱序**。这里踩了三个坑:
   a) 用累加计数判断"收齐" -> 重复投递时多算 -> 乱序随机失败;
      改用**位图**记录已落位字节。
   b) END 先到时, START 到达会把 saw_end 清 False -> 永远不交付;
      saw_end 只能由"新 seq"重置。
   c) START 到达时清空已收数据 -> 先前收到的块白收;
      START 不应清空任何数据(轮次已由 seq 区分)。
   修完这三个, 200 次随机乱序 + 完全倒序全部成功。
3. **写本机剪贴板前必须登记自写标记**, 否则形成无限回环。
"""
import ast
import inspect
import os
import random
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import com_bridge.agent as agent_mod  # noqa: E402
import com_bridge.clipboard as cb  # noqa: E402
import com_bridge.clipboard_sync as cs  # noqa: E402
import com_bridge.server as srv_mod  # noqa: E402


# --- 1. 三种规范格式 --------------------------------------------------

def test_only_three_canonical_formats():
    """★ 只同步 Text / HTML / Bitmap(与 Deskflow 一致), 不做私有格式转发。"""
    assert cb.FMT_TEXT == "text"
    assert cb.FMT_HTML == "html"
    assert cb.FMT_BITMAP == "bitmap"
    # 采样只认这三种
    src = inspect.getsource(cb.sample)
    for f in ("FMT_TEXT", "FMT_HTML", "FMT_BITMAP"):
        assert f in src, f"sample 必须处理 {f}"


def test_text_normalized_to_lf():
    """★ 文本必须统一成 LF 换行(Deskflow 的规范), 否则跨机后多出 \\r。"""
    src = inspect.getsource(cb.sample)
    assert 'replace("\\r\\n", "\\n")' in src, "读取时要 CRLF -> LF"
    src2 = inspect.getsource(cb.apply)
    assert 'replace("\\n", "\\r\\n")' in src2, "写入 Windows 时要 LF -> CRLF"


def test_bitmap_converted_to_bmp_file():
    """★ 图片用 BMP 文件格式(Deskflow 的 Bitmap 规范), 而非 HBITMAP 句柄。"""
    src = inspect.getsource(cb._dib_to_bmp)
    assert "BITMAPFILEHEADER" in src or b"BM" in src.encode() or '"BM"' in src
    assert "BI_RGB" in src or "bi_compression != 0" in src, \
        "只接受 BI_RGB(与 Deskflow 的规范一致)"


# --- 2. 分块协议 -------------------------------------------------------

def test_chunk_fits_in_mtu():
    """★★★ 每块(含头)必须 < MTU 1500。

    音频曾因 3840 字节的包被 IP 分片、任一片丢失整包作废而爆音。
    剪贴板图片动辄几 MB, 不分块必然丢。
    """
    max_size = cs.CHUNK_SIZE + cs.HDR.size
    assert max_size < 1500, f"块大小 {max_size} 超过 MTU"


def test_chunk_header_has_offset_and_seq():
    """★ 必须带 offset 与 chunk_seq —— 否则乱序无法正确落位。"""
    fields = [f for f in cs.HDR.format.split() if f not in ("2s",)]
    assert cs.HDR.size == 19
    # 打包/解包往返
    c = cs.pack_chunk(cs.KIND_DATA, 7, 1234, 100, 2, b"hello")
    info = cs.unpack_chunk(c)
    assert info is not None
    kind, seq, total, off, cseq, pl = info
    assert (kind, seq, total, off, pl) == (cs.KIND_DATA, 7, 1234, 100, b"hello")


def test_reassembly_handles_reorder():
    """★★★ 重组必须处理 UDP 乱序(踩了三个坑才修好)。"""
    blob = cs._encode({"text": b"x" * 2500})
    chunks = cs.split_payload(9, blob)

    def reasm(order):
        a = cs._Assembler()
        res = None
        for c in order:
            k, s, t, o, _cs, pl = cs.unpack_chunk(c)
            r = a.feed(k, s, t, o, pl)
            if r:
                res = r
        return res

    assert reasm(chunks) == blob, "顺序重组失败"
    assert reasm(list(reversed(chunks))) == blob, "倒序重组失败"
    ok = 0
    for _ in range(30):
        sh = chunks[:]
        random.shuffle(sh)
        if reasm(sh) == blob:
            ok += 1
    assert ok == 30, f"乱序重组只成功 {ok}/30"


def test_reassembly_rejects_incomplete():
    """★ 丢块时必须拒绝交付(不能拼出半份内容)。"""
    blob = cs._encode({"text": b"y" * 2500})
    chunks = cs.split_payload(10, blob)
    a = cs._Assembler()
    res = None
    for c in chunks[:-1]:         # 故意丢掉 END
        k, s, t, o, _cs, pl = cs.unpack_chunk(c)
        r = a.feed(k, s, t, o, pl)
        if r:
            res = r
    assert res is None, "缺块时不该交付"


def test_reassembly_bounds_check():
    """★ 必须防越界/超大(恶意或损坏的包不能让程序崩)。"""
    a = cs._Assembler()
    a.feed(cs.KIND_START, 1, 100, 0, b"")
    # offset 超出 total
    r = a.feed(cs.KIND_DATA, 1, 100, 50, b"z" * 200)
    assert r is None
    # 超大 total
    big = cs.pack_chunk(cs.KIND_START, 2, cs.MAX_TOTAL + 1, 0, 0, b"")
    info = cs.unpack_chunk(big)
    assert info is None, "超大 total 应被拒绝"


# --- 3. 防回环 --------------------------------------------------------

def test_self_write_guard():
    """★★★ 自己写进剪贴板的内容不能被当成"用户复制"发回去。

    否则两台机器会无限互发同一份内容(Deskflow 用 setClipboard/
    grabClipboard 解决, 我们用一个时间窗)。
    """
    assert hasattr(cs.ClipboardSync, "note_self_write")
    src = inspect.getsource(cs.ClipboardSync._poll_once)
    assert "_self_write_until" in src, "必须检查自写时间窗"
    assert "skipped_self" in src, "应统计被跳过的自写"


def test_dedup_same_content():
    """★ 相同内容只发一次(用户可能重复复制同一份)。"""
    src = inspect.getsource(cs.ClipboardSync._poll_once)
    assert "_last_hash" in src
    assert "skipped_same" in src


# --- 4. 两端接线 -------------------------------------------------------

def test_server_wires_clipboard():
    """server 必须在 peer 地址确定后启用剪贴板同步。

    ★ 位置: `_on_client_connect`(peer 地址确定处), 而不是 `_start_input`
      —— 因为发送端需要知道 peer 的地址才能建 socket。
    """
    assert hasattr(srv_mod.Server, "_ensure_clipboard")
    assert hasattr(srv_mod.Server, "_on_local_copy")
    assert hasattr(srv_mod.Server, "_on_clipboard_recv")
    # 在整个 server 模块里找调用点(它在 _on_client_connect 里)
    src = inspect.getsource(srv_mod)
    assert "_ensure_clipboard(" in src, "必须实际调用 _ensure_clipboard"
    # 且必须在拿到 target 之后
    i_target = src.find("target = self._pick_input_target")
    i_clip = src.find("self._ensure_clipboard(")
    assert i_target >= 0 and i_clip > i_target, \
        "必须在 peer 地址确定后才启用剪贴板"


def test_agent_wires_clipboard():
    """agent 必须同样启用(双向)。"""
    assert hasattr(agent_mod.Agent, "_ensure_clipboard")
    assert hasattr(agent_mod.Agent, "_on_local_copy")
    assert hasattr(agent_mod.Agent, "_on_clipboard_recv")
    src = inspect.getsource(agent_mod.Agent.start)
    assert "_ensure_clipboard" in src


def test_both_sides_register_self_write():
    """★ 两端收到内容后都必须先登记自写标记再写入(否则回环)。"""
    for mod, cls in ((srv_mod, srv_mod.Server), (agent_mod, agent_mod.Agent)):
        src = inspect.getsource(cls._on_clipboard_recv)
        i_guard = src.find("note_self_write")
        i_apply = src.find("cb.apply")
        assert i_guard >= 0, f"{cls.__name__} 必须登记自写"
        assert i_apply > i_guard, \
            f"{cls.__name__} 必须先登记自写再写剪贴板"


# --- 5. 真实剪贴板往返 -------------------------------------------------

def test_text_roundtrip_real_clipboard():
    """真实的写入->读回往返(中文 + 多行)。"""
    text = "剪贴板往返测试\nline2\nline3"
    assert cb.apply({cb.FMT_TEXT: text.encode("utf-8")})
    got = cb.sample()
    assert got is not None
    assert got.get(cb.FMT_TEXT) == text.encode("utf-8")
