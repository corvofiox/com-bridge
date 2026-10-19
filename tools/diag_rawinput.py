"""诊断: RawInputListener 是否收得到物理鼠标事件, 合成事件是否被过滤。

用法: python tools/diag_rawinput.py [秒数]
手动移动鼠标若干秒, 观察 raw_mouse 是否增长。
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

from com_bridge.input import (  # noqa: E402
    InputInjector,
    RawInputListener,
    get_cursor_pos,
)


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
    events: list = []
    lst = RawInputListener(on_event=events.append)
    lst.start()
    if not lst.wait_ready(3):
        print("!! 监听器启动失败")
        return 1
    print(f"监听器就绪。请手动移动鼠标 {seconds:.0f} 秒 ...")

    t0 = time.monotonic()
    last = lst.diag()
    while time.monotonic() - t0 < seconds:
        time.sleep(0.5)
        cur = lst.diag()
        if cur != last:
            print(f"  t+{time.monotonic() - t0:4.1f}s  {cur}  光标={get_cursor_pos()}")
            last = cur

    print()
    print("最终统计:", lst.diag())
    print("收到事件对象:", len(events))
    print()

    # 再用 SendInput 注入, 验证合成事件被过滤
    before = lst.diag()
    for _ in range(20):
        InputInjector.move_relative(5, 0)
        time.sleep(0.01)
    time.sleep(0.3)
    after = lst.diag()
    print("注入 20 次相对移动后:", after)
    filtered = (after["synthetic"] > before["synthetic"]
                and after["mouse"] == before["mouse"])
    print("合成事件被过滤:", filtered)

    d = lst.diag()
    if d["mouse"] == 0 and d["synthetic"] == 0 and d["total"] == 0:
        print()
        print("!! 一个 WM_INPUT 都没收到。可能原因:")
        print("   - 进程无桌面会话(远程/服务环境)")
        print("   - 未以足够权限运行(需与前台窗口同权限或更高)")
        print("   - 原始输入注册失败(检查上面的 OSError)")
    lst.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
