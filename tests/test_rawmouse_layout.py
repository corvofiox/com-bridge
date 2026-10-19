"""RAWMOUSE / RAWKEYBOARD 结构体布局回归 —— 用**真实捕获字节**锁死。

★★ 这是用户症状"进入副机后鼠标点击完全没作用, 但鼠标能移动"的根因。

Windows 的 RAWMOUSE 定义(winuser.h):
    typedef struct tagRAWMOUSE {
        USHORT usFlags;
        union {
            ULONG ulButtons;                 // ★ union 有 ULONG -> 4 字节对齐
            struct { USHORT usButtonFlags; USHORT usButtonData; };
        };
        ULONG  ulRawButtons;
        LONG   lLastX;
        LONG   lLastY;
        ULONG  ulExtraInformation;
    } RAWMOUSE;

union 的对齐是 **4**, 所以 `usFlags`(2 字节) 之后有 **2 字节填充**:
    usFlags            @0
    (padding)          @2   <- 编译器插入
    usButtonFlags      @4
    usButtonData       @6
    ulRawButtons       @8
    lLastX             @12
    lLastY             @16
    ulExtraInformation @20
    sizeof = 24

ctypes 会**按自然对齐**排: 2 字节 usFlags 后直接接 2 字节 usButtonFlags(@2),
于是按钮标志读到的是 usButtonData 的高半部分 —— 真正的标志位全被读成 0,
`_emit_mouse` 里所有 `if bf & RI_MOUSE_*` 永不成立 => **点击与滚轮一次都没
转发过**。位移字段恰好在 @12(两种布局一致), 所以移动始终正常 —— 症状正是
"能移动、点不了"。

下面的字节向量来自**实测捕获**(tools/diag_hook_types.py 打印的 raw bytes),
所以这个回归测试不依赖任何活动桌面或外部程序。
"""

from __future__ import annotations

import ctypes

import pytest

from com_bridge.input import (
    RAWKEYBOARD,
    RAWMOUSE,
    RI_MOUSE_BUTTON_4_DOWN,
    RI_MOUSE_BUTTON_5_DOWN,
    RI_MOUSE_LEFT_BUTTON_DOWN,
    RI_MOUSE_LEFT_BUTTON_UP,
    RI_MOUSE_MIDDLE_BUTTON_DOWN,
    RI_MOUSE_RIGHT_BUTTON_DOWN,
    RI_MOUSE_WHEEL,
)

# 实测捕获的 RAWMOUSE 原始字节(24 字节)
CAPTURED = {
    "move_11_7": bytes.fromhex("0000000000000000000000000b0000000700000000000000"),
    "left_down": bytes.fromhex("000000000100000000000000000000000000000000000000"),
    "left_up": bytes.fromhex("000000000200000000000000000000000000000000000000"),
    "wheel_up": bytes.fromhex("000000000004780000000000000000000000000000000000"),
}


class TestRawMouseLayout:
    def test_offsets_match_windows(self):
        """字段偏移必须与 winuser.h 一致(尤其按钮在 @4 而非 @2)。"""
        expected = {
            "usFlags": 0,
            "usButtonFlags": 4,
            "usButtonData": 6,
            "ulRawButtons": 8,
            "lLastX": 12,
            "lLastY": 16,
            "ulExtraInformation": 20,
        }
        for name, off in expected.items():
            field = getattr(RAWMOUSE, name)
            assert field.offset == off, (
                f"{name} 偏移应为 {off}, 实际 {field.offset} —— "
                f"布局错了会让按钮标志永远读成 0(点击无法转发)"
            )
        assert ctypes.sizeof(RAWMOUSE) == 24

    def test_size_matches_windows(self):
        assert ctypes.sizeof(RAWMOUSE) == 24, "RAWMOUSE 必须是 24 字节"

    def test_button_field_is_not_at_2(self):
        """★ 直接锁死那个错误: 按钮标志**不能**落在 offset 2。

        这是本 bug 的本质 —— 若有人"清理"掉那个填充字段, 测试立刻失败。
        """
        assert RAWMOUSE.usButtonFlags.offset == 4, (
            "usButtonFlags 必须在 offset 4(union 的 4 字节对齐). "
            "若为 2, 点击标志会读到 usButtonData 的高半部分 -> 永远是 0"
        )


class TestCapturedBytesDecode:
    """用实测字节验证解析结果 —— 这是"点击能被识别"的直接证明。"""

    def test_move(self):
        rm = RAWMOUSE.from_buffer_copy(CAPTURED["move_11_7"])
        assert rm.lLastX == 11 and rm.lLastY == 7
        assert rm.usButtonFlags == 0

    def test_left_down(self):
        rm = RAWMOUSE.from_buffer_copy(CAPTURED["left_down"])
        assert rm.usButtonFlags & RI_MOUSE_LEFT_BUTTON_DOWN, (
            "左键按下必须被识别 —— 否则点击无法转发到副机(用户症状)"
        )
        assert rm.lLastX == 0 and rm.lLastY == 0

    def test_left_up(self):
        rm = RAWMOUSE.from_buffer_copy(CAPTURED["left_up"])
        assert rm.usButtonFlags & RI_MOUSE_LEFT_BUTTON_UP

    def test_wheel(self):
        rm = RAWMOUSE.from_buffer_copy(CAPTURED["wheel_up"])
        assert rm.usButtonFlags & RI_MOUSE_WHEEL, "滚轮标志必须被识别"
        assert ctypes.c_short(rm.usButtonData).value == 120, (
            "滚轮增量必须正确解出(120 = 向上)"
        )

    @pytest.mark.parametrize("flag", [
        RI_MOUSE_LEFT_BUTTON_DOWN, RI_MOUSE_LEFT_BUTTON_UP,
        RI_MOUSE_RIGHT_BUTTON_DOWN, RI_MOUSE_MIDDLE_BUTTON_DOWN,
        RI_MOUSE_BUTTON_4_DOWN, RI_MOUSE_BUTTON_5_DOWN,
        RI_MOUSE_WHEEL,
    ])
    def test_every_flag_roundtrip(self, flag):
        """构造每种按钮标志的字节, 确认都能被解出(覆盖全部按钮类型)。"""
        raw = bytearray(24)
        raw[4:6] = flag.to_bytes(2, "little")      # 按钮标志在 @4
        rm = RAWMOUSE.from_buffer_copy(bytes(raw))
        assert rm.usButtonFlags == flag, f"标志 0x{flag:04x} 未被正确解析"

    def test_button_data_sign(self):
        """滚轮负增量(向下滚)必须解出负数, 不能被当成大正数。"""
        raw = bytearray(24)
        raw[4:6] = RI_MOUSE_WHEEL.to_bytes(2, "little")
        raw[6:8] = (-120 & 0xFFFF).to_bytes(2, "little")
        rm = RAWMOUSE.from_buffer_copy(bytes(raw))
        assert ctypes.c_short(rm.usButtonData).value == -120


class TestRawKeyboardLayout:
    """键盘结构体也一并锁死(避免同类布局错误)。"""

    def test_keyboard_fields(self):
        assert RAWKEYBOARD.MakeCode.offset == 0
        assert RAWKEYBOARD.Flags.offset == 2
        assert RAWKEYBOARD.VKey.offset == 6
        assert RAWKEYBOARD.Message.offset == 8
        assert RAWKEYBOARD.ExtraInformation.offset == 12
        assert ctypes.sizeof(RAWKEYBOARD) == 16
