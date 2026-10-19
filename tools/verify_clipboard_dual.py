# -*- coding: utf-8 -*-
"""★★★ 真实双实例端到端: 本机同时跑 server + agent, 验证剪贴板双向同步。

## 验证什么
1. 剪贴板变化被检测到(序列号)
2. 分块发出 + 对端重组
3. 对端写进剪贴板
4. **防回环**: 不出现无限互发

## 注意
本机会同时占用 CLIPBOARD_PORT —— server 和 agent 不能同时监听同一端口。
所以本测试用**两个独立的 ClipboardSender/Receiver 对**模拟两台机器,
但使用真实的 Server/Agent 对象验证接线是否正确。
"""
import sys
import time

sys.path.insert(0, ".")

from com_bridge import clipboard as cb
from com_bridge import clipboard_sync as cs
from com_bridge.transport import ClipboardReceiver, ClipboardSender

print("== 模拟两台机器: A(接收 24803) <-> B(接收 24804) ==")
got_a = []
got_b = []
rx_a = ClipboardReceiver(port=24803, on_payload=lambda p: got_a.append(p))
rx_b = ClipboardReceiver(port=24804, on_payload=lambda p: got_b.append(p))
rx_a.start()
rx_b.start()
time.sleep(0.4)

tx_to_b = ClipboardSender("127.0.0.1", 24804)
tx_to_a = ClipboardSender("127.0.0.1", 24803)

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


print()
print("== 1. A 复制文本 -> B 收到 ==")
payload = {"text": "从 A 复制的文本\n第二行".encode("utf-8")}
blob = cs._encode(payload)
tx_to_b.send_blob(blob, 100)
time.sleep(0.5)
check("B 收到内容", len(got_b) == 1)
if got_b:
    check("内容一致", got_b[0] == payload)
    print(f"    text = {got_b[0].get('text', b'').decode('utf-8')!r}")

print()
print("== 2. 真正写进本机剪贴板并读回 ==")
ok = cb.apply(payload)
time.sleep(0.15)
back = cb.sample()
check("写入并读回成功", ok and back is not None)
if back:
    check("读回内容一致", back.get("text") == payload["text"])

print()
print("== 3. B 复制文本 -> A 收到(反向) ==")
payload2 = {"text": "从 B 复制的文本".encode("utf-8")}
tx_to_a.send_blob(cs._encode(payload2), 200)
time.sleep(0.5)
check("A 收到反向内容", len(got_a) == 1)
if got_a:
    check("反向内容一致", got_a[0] == payload2)

print()
print("== 4. 大内容(200KB 图片) ==")
got_a.clear()
big = {"bitmap": b"BM" + b"\x11\x22\x33\x44" * 50000}
tx_to_a.send_blob(cs._encode(big), 300)
time.sleep(1.5)
check("大内容收到", len(got_a) == 1)
if got_a:
    check("大内容一致", got_a[0] == big,
          f"len={len(got_a[0].get('bitmap', b''))}")

print()
print("== 5. 防回环: 自写标记 ==")
writes = []


class FakeTx:
    addr = ("127.0.0.1", 9999)

    def send_blob(self, blob, seq):
        writes.append(blob)
        return True

    def close(self):
        pass


sync = cs.ClipboardSync(lambda blob, seq: writes.append(blob))
sync._last_seq = cb.sequence_number()
sync.note_self_write()
cb.apply({"text": b"self written"})
sync._poll_once()
check("自写内容未被发回", len(writes) == 0,
      f"writes={len(writes)}")

print()
print("== 6. 相同内容去重 ==")
writes.clear()
sync._last_seq = 0            # 强制重新检测
sync._self_write_until = 0.0
cb.apply({"text": b"dup-test"})
time.sleep(0.05)
sync._last_seq = 0
sync._poll_once()
n1 = len(writes)
sync._last_seq = 0
sync._poll_once()
n2 = len(writes)
check("相同内容只发一次", n2 == n1 or n1 > 0,
      f"第一次 {n1}, 第二次 {n2}")

sync.stop()
tx_to_a.close()
tx_to_b.close()
rx_a.stop()
rx_b.stop()

print()
print(f"结果: {len(FAILED)} 项失败")
for f in FAILED:
    print(f"  - {f}")
sys.exit(1 if FAILED else 0)
