# -*- coding: utf-8 -*-
"""注入坐标归一化必须与 Windows 的反变换**严格互逆**。

## 现场(2026-10-23 00:33, 副机真机日志)

用户从第一版起就报"副机边缘粘滞": 光标在靠主机那一侧的边缘附近, 往主机去
顺畅, 往副机里回头拉很粘滞, "用力拉才行"。连着改了几轮判定逻辑都没打中,
直到把逐事件日志打开才拍到:

    [agent][edge] inj dx=1 dy=0 (14,771) -> (14,771)
    [agent][edge] inj dx=1 dy=0 (14,771) -> (14,771)
    ...(连续几十条, 整整 1.5 秒)

即: **每一帧都请求 +1px, 光标却一步没动**。这不是判定问题, 是注入坐标的
量化问题。

## 根因

反推 Windows 对 `MOUSEEVENTF_ABSOLUTE|MOUSEEVENTF_VIRTUALDESK` 的反变换
(在 1920 宽的屏上逐个定点值实测, 与 floor 模型逐个吻合):

    px = floor(val * extent / 65536)

正向原来用的是 `round(pos * 65535 / (extent - 1))`(照搬 Deskflow)。它在
1920 宽的屏上只错 7 个像素, 但在副机 **4480x1600** 的桌面上错 **39 个** ——
相邻像素被压到同一个定点值:

    pos=13 -> val=190 -> floor(190*4480/65536) = 12   (目标 13, 落回 12)
    pos=15 -> val=219 -> floor(219*4480/65536) = 14   (目标 15, 落回 14)

`move_relative` 读到 14、目标 15、落回 14 …… 于是**永远原地不动**; 只有攒够
较大位移(用户"用力拉一下")才能跳到下一格。而进入副机的落点正好在 x=12~24,
133 个里最先踩到的就是 13/15 这两个坏点 —— 所以症状集中在边缘。

## 修法

正向改成**向上取整**, 精确逆:

    val = ceil((pos - origin) * 65536 / extent)

这样映射是**幂等**的(同一目标反复注入落在同一像素), 也不会像"除以 w 且
截断"那样每事件漂 1px。
"""
from __future__ import annotations

import pytest

from com_bridge.input import InputInjector

# Windows 的反变换(实测反推): 定点值 -> 像素
def _win_px(val: int, extent: int) -> int:
    return (val * extent) // 65536


# 覆盖: 单屏 1920x1080、副机 4480x1600、4K/2K/8K/常见笔记本宽
SIZES = (1920, 1080, 4480, 1600, 3840, 2560, 7680, 1366, 1280, 5120)


class TestAbsoluteRoundTrip:

    @pytest.mark.parametrize("extent", SIZES)
    def test_every_pixel_survives_roundtrip(self, extent):
        """每一个像素坐标注入后都必须落回原位(否则光标会"粘"在那里)。"""
        bad = [(p, InputInjector.to_absolute(p, 0, extent))
               for p in range(extent)
               if _win_px(InputInjector.to_absolute(p, 0, extent), extent) != p]
        assert not bad, f"{extent} 宽: {len(bad)} 个像素落不回原处, 例: {bad[:5]}"

    def test_offset_origin_also_exact(self):
        """负坐标/多屏偏移(left/top 非 0)同样必须精确。"""
        for origin, extent in ((-1920, 2560), (1920, 4480), (0, 4480)):
            for pos in (origin, origin + 1, origin + extent // 2,
                        origin + extent - 1):
                val = InputInjector.to_absolute(pos, origin, extent)
                assert 0 <= val <= 65535
                assert _win_px(val, extent) == pos - origin

    def test_clamped_to_wire_range(self):
        for pos in (-100, 0, 1919, 5000):
            val = InputInjector.to_absolute(pos, 0, 1920)
            assert 0 <= val <= 65535

    def test_degenerate_extent(self):
        assert InputInjector.to_absolute(5, 0, 1) == 0
        assert InputInjector.to_absolute(5, 0, 0) == 0

    def test_is_idempotent(self):
        """同一目标反复换算必须得到同一个定点值(不会逐事件漂移)。"""
        for extent in (1920, 4480):
            for pos in (0, 1, 12, 13, 15, 100, extent // 2, extent - 1):
                first = InputInjector.to_absolute(pos, 0, extent)
                for _ in range(20):
                    assert InputInjector.to_absolute(pos, 0, extent) == first


class TestOldFormulaWasLossy:
    """对照组: 旧公式在宽桌面上会丢像素 —— 这就是"粘滞"的成因。

    保留这条是为了防止有人"觉得照搬 Deskflow 更稳"而改回旧公式。
    """

    @staticmethod
    def _old(pos: int, extent: int) -> int:
        return max(0, min(65535, int(65535.0 * pos / max(1, extent - 1) + 0.5)))

    def test_no_loss_on_1920(self):
        bad = [p for p in range(1920)
               if _win_px(self._old(p, 1920), 1920) != p]
        assert len(bad) <= 10, f"1920 宽上本来就只有少量坏点, 实测 {len(bad)}"

    def test_lossy_on_4480(self):
        bad = [p for p in range(4480)
               if _win_px(self._old(p, 4480), 4480) != p]
        assert len(bad) >= 30, "旧公式在 4480 宽上必然丢一批像素"
        # 进入落点(12~24)里正好有坏点, 所以症状集中在边缘
        assert any(12 <= p <= 24 for p in bad), \
            f"边缘附近必须有坏点, 实际坏点示例: {bad[:12]}"
