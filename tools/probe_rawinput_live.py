"""验证 RawInputListener 能否收到**真实**鼠标输入。

若连真实鼠标事件都收不到, 那么整个跨屏功能不可能工作 —— 这才是
"鼠标移动到边缘无反应"的第一位原因, 与边缘判定无关。
运行后请**移动鼠标 / 按键**, 程序统计 3 秒。
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

import com_bridge.input as I


def main() -> int:
    events: list[tuple] = []

    def on_ev(ev) -> None:
        events.append((ev.type, ev.flags, ev.code, ev.a, ev.b))

    lis = I.RawInputListener(on_event=on_ev)
    lis.start()
    if not lis.wait_ready():
        print("!! 监听器启动失败(wait_ready 超时)")
        return 1

    print("监听器已就绪。请**移动鼠标**(3 秒)...")
    t0 = time.time()
    counts: list[dict] = []
    while time.time() - t0 < 3.0:
        counts.append(lis.diag())
        time.sleep(0.5)

    final = lis.diag()
    lis.stop()

    print(f"\n原始输入统计: {final}")
    print(f"回调解出的事件数: {len(events)}")
    if events[:5]:
        print(f"前 5 个事件 (type, flags, code, a, b):")
        for e in events[:5]:
            print("   ", e)

    print()
    if final["total"] == 0:
        print("[结论] FAIL —— 监听器**收不到任何原始输入**。")
        print("       跨屏功能不可能工作。可能原因:")
        print("       * RIDEV_INPUTSINK 需要窗口在前台会话; 后台/服务会话收不到")
        print("       * 进程运行在无交互桌面的会话里")
        print("       * 消息循环未实际泵消息(窗口创建失败但被静默忽略)")
        return 1
    if final["mouse"] == 0:
        print(f"[结论] 部分可用: 收到 {final['total']} 个原始输入, "
              f"但鼠标 0 个 —— 只收到键盘?")
        return 1
    print(f"[结论] PASS —— 收到 {final['mouse']} 个鼠标原始输入, "
          f"丢弃合成 {final['synthetic']} 个, 解出 {len(events)} 个事件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
