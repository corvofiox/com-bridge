"""关键集成验证: hider 持有 SetCapture 时, RawInput 监听是否仍收到输入?

为什么必须验证: 新的光标隐藏机制让 hider 线程调用 SetCapture(接管**窗口
消息**)。若这个捕获同时挡掉了 RawInput(RIDEV_INPUTSINK) 的投递, 那么
穿越期间主电脑就**收不到物理鼠标位移** -> 无法转发、无法判定, 跨屏整体失效。

Windows 的模型是: SetCapture 只影响 WM_*MOUSE* 窗口消息的投递目标;
RawInput 走的是独立的设备级管线, 不受窗口捕获影响。

★ 判据说明(为什么不断言 mouse 计数增长):
  本测试是**自动化**的, 只能产生 SendInput 注入事件 —— 它们都带
  InputInjector.TAG, 会被监听器正确地归入 synthetic 并丢弃。因此 raw_mouse
  在自动化环境下必然为 0, 这**不是**缺陷。真正的判据是:
    (1) total 在捕获期间持续增长  => RawInput 投递未被捕获阻断(核心断言)
    (2) synthetic 增长而 mouse 不涨 => TAG 过滤仍然有效(没有被捕获破坏)
  物理鼠标带来的 raw_mouse 增长只能由用户在真机上目视确认。

用法: python -u tools/verify_capture_rawinput.py
"""
from __future__ import annotations

import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from com_bridge.cursor import CursorHider
from com_bridge.input import InputInjector, RawInputListener, virtual_desktop


def wiggle(n: int = 10) -> None:
    for _ in range(n):
        InputInjector.move_relative(4, 3)
        InputInjector.move_relative(-4, -3)


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    lst = RawInputListener(on_event=lambda ev: None)
    lst.start()
    if not lst.wait_ready(2.0):
        print("RawInputListener 启动失败")
        return 1
    time.sleep(0.3)

    hider = CursorHider(hide_on_motion=False, name="CapRawTest")
    if not hider.start():
        print("hider 启动失败")
        return 1
    time.sleep(0.2)

    # --- 1) 基线: 不持有捕获 ---
    wiggle(6)
    time.sleep(0.3)
    base = lst.diag()
    print(f"基线(无捕获): {base}")

    # --- 2) 让 hider 持有捕获, 再注入 ---
    hider.hide()
    time.sleep(0.4)
    print(f"  hider 线程计数器={hider._counter_after_hide} "
          f"captured={hider._captured} shown={hider.hidden}")
    wiggle(14)
    time.sleep(0.4)
    after = lst.diag()
    print(f"捕获期间注入: {after}")

    dm = after["mouse"] - base["mouse"]
    d_synth = after["synthetic"] - base["synthetic"]
    d_total = after["total"] - base["total"]
    print(f"  增量: total=+{d_total} mouse=+{dm} synthetic=+{d_synth}")
    print(f"  注: 自动化只能产生注入事件(带 TAG 被过滤), 故 mouse 不会增长; "
          f"物理位移需真机目视")

    results.append(("捕获期间 RawInput 投递未被阻断(总包数增长)",
                    d_total > 0, f"+{d_total}"))
    results.append(("捕获期间 TAG 过滤仍有效(synthetic 增长且未混入 mouse)",
                    d_synth > 0 and dm == 0, f"+{d_synth} / mouse+{dm}"))
    results.append(("hider 捕获已生效(本线程计数器为负)",
                    hider._counter_after_hide < 0,
                    f"{hider._counter_after_hide}"))

    # --- 3) 释放捕获后仍正常 ---
    hider.show()
    time.sleep(0.3)
    b2 = lst.diag()
    wiggle(8)
    time.sleep(0.3)
    a2 = lst.diag()
    dt2 = a2["total"] - b2["total"]
    results.append(("释放捕获后 RawInput 投递仍正常", dt2 > 0, f"+{dt2}"))

    hider.stop()
    lst.stop()
    time.sleep(0.3)

    print("\n=== 捕获 vs RawInput 集成验收 ===")
    allok = True
    for name, ok, detail in results:
        allok = allok and ok
        print(f"  {'OK  ' if ok else 'FAIL'} {name} {detail}")
    print(f"\n[结论] {'PASS —— 捕获只影响窗口消息, RawInput 不受影响' if allok else 'FAIL'}")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
