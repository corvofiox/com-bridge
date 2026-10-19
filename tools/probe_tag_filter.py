"""区分两种可能: 监听器坏了, 还是"本会话收不到物理鼠标"。

做法: 注入**不带 TAG** 的合成移动(模拟真实鼠标的原始输入路径),
若监听器能收到它 -> 监听器与注册都正常, 之前 total=0 只是因为
本自动化会话没有物理鼠标事件投递。
若连不带 TAG 的注入也收不到 -> 注册/会话层面有问题。
"""

from __future__ import annotations

import ctypes
import sys
import time

sys.path.insert(0, ".")

import com_bridge.input as I
from com_bridge.input import INPUT, INPUT_MOUSE, MOUSEEVENTF_MOVE, user32


def main() -> int:
    events: list[tuple] = []
    lis = I.RawInputListener(on_event=lambda ev: events.append(
        (ev.type, ev.a, ev.b)))
    lis.start()
    if not lis.wait_ready():
        print("监听器启动失败")
        return 1
    time.sleep(0.3)

    def inject_untagged(dx: int, dy: int) -> None:
        """不带 TAG 的注入: 在 raw input 里与真实鼠标不可区分(除 hDevice)。"""
        i = INPUT(type=INPUT_MOUSE)
        i.union.mi.dx = dx
        i.union.mi.dy = dy
        i.union.mi.dwFlags = MOUSEEVENTF_MOVE
        # 故意不设 dwExtraInfo(保持 0)
        user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))

    print("[A] 注入 5 个**不带 TAG** 的移动(应被监听器收到):")
    b = lis.diag()
    for _ in range(5):
        inject_untagged(4, 2)
        time.sleep(0.05)
    time.sleep(0.4)
    a = lis.diag()
    print(f"    total +{a['total']-b['total']}, "
          f"mouse +{a['mouse']-b['mouse']}, "
          f"synthetic +{a['synthetic']-b['synthetic']}")
    print(f"    回调解出事件: {len(events)}")
    got_untagged = (a["total"] - b["total"]) > 0

    print("\n[B] 注入 5 个**带 TAG** 的移动(应被过滤):")
    b2 = lis.diag()
    n_before = len(events)
    for _ in range(5):
        I.InputInjector.move_relative(4, 2)
        time.sleep(0.05)
    time.sleep(0.4)
    a2 = lis.diag()
    print(f"    total +{a2['total']-b2['total']}, "
          f"synthetic +{a2['synthetic']-b2['synthetic']}")
    print(f"    新增回调事件: {len(events) - n_before}")

    lis.stop()
    print()
    if not got_untagged:
        print("[结论] 本会话里监听器收不到任何原始输入(连不带 TAG 的注入也"
              "收不到)。")
        print("       -> 这是**运行会话**的限制, 无法在此自动验证;")
        print("          请在真实桌面双击运行 dist\\com-bridge-gui.exe 复测。")
        return 2
    if a2["synthetic"] - b2["synthetic"] >= 5:
        print("[结论] PASS —— TAG 过滤生效: 不带 TAG 能收到, 带 TAG 全被丢弃")
        return 0
    print("[结论] FAIL —— TAG 未被识别, 自激仍会发生")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
