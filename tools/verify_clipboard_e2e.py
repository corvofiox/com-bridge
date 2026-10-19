# -*- coding: utf-8 -*-
"""端到端验证: 剪贴板 UDP 传输(发送 -> 接收 -> 重组)。"""
import sys
import time

sys.path.insert(0, ".")

from com_bridge import clipboard_sync as cs
from com_bridge.transport import ClipboardReceiver, ClipboardSender

got = []
recv = ClipboardReceiver(port=24803, on_payload=lambda p: got.append(p))
recv.start()
time.sleep(0.3)
print(f"  接收端就绪: port={recv.port}")

sender = ClipboardSender("127.0.0.1", 24803)

# 1. 小文本
payload = {"text": "跨屏剪贴板测试".encode("utf-8"), "html": b"<i>x</i>"}
blob = cs._encode(payload)
print(f"\n  [小文本] {len(blob)} 字节")
ok = sender.send_blob(blob, 1)
time.sleep(0.4)
print(f"  发送={ok} 块数={sender.sent_chunks} 收到={len(got)}")
if got:
    print(f"  一致: {got[0] == payload}")
    print(f"  text: {got[0].get('text', b'').decode('utf-8')!r}")

# 2. 大图片(模拟 500KB, 500 块)
got.clear()
big = {"bitmap": b"BM" + bytes(range(256)) * 2000}     # ~512KB
blob2 = cs._encode(big)
print(f"\n  [大图片] {len(blob2)} 字节 = {len(cs.split_payload(2, blob2))} 块")
ok2 = sender.send_blob(blob2, 2)
time.sleep(1.2)
print(f"  发送={ok2} 收到={len(got)}")
if got:
    print(f"  一致: {got[0] == big}  (len={len(got[0].get('bitmap', b''))})")

print(f"\n  stats: received={recv.received} assembled={recv.assembled} "
      f"bad={recv.bad}")

# 3. 无分片验证
max_chunk = max(len(c) for c in cs.split_payload(3, blob2))
print(f"\n  [MTU] 最大块 {max_chunk} 字节 (必须 < 1500): "
      f"{'OK' if max_chunk < 1500 else 'FAIL'}")

sender.close()
recv.stop()
