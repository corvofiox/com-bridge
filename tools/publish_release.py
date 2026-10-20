# -*- coding: utf-8 -*-
"""创建 GitHub Release v0.2.0 并上传 exe(避免 PowerShell JSON 转义坑)。"""
import json
import subprocess
import sys
import urllib.request

REPO = "corvofiox/com-bridge"
TAG = "v0.2.0"

BODY = """## 键盘（根因修复，物理按键实测证实）

**修复：跨屏期间键盘完全失灵 / "自愈 3 次未生效 → 暂停键盘转发"**

根因：光标隐藏器（hider）持前台 + 注册了键盘 RawInput（RIDEV_INPUTSINK）时，Windows 会让本进程的低级键盘钩子**收不到任何按键**——物理按键也收不到（实测 kbd_calls +0 而 RawInput +36）。Deskflow 从未踩坑，因为它全程不注册 RawInput。

- `RawInputListener` 新增 `register_keyboard` 开关；server/agent 均传 `False`，并显式 `RIDEV_REMOVE` 注销键盘注册
- 移除已被证伪的"RawInput 键盘泄漏检测"及其自愈循环（它会在错误前提下把正常的键盘转发停掉）
- 穿越态（hider 持前台）不再因探针误报而丢弃键盘事件

## 音频（欠载假信号修复 + 全路径诊断）

- 修复"欠载"计数假信号：`pop_aligned` 在渲染缓冲满（budget=0）时不再误计欠载
- WASAPI 采集缓冲显式 100ms（原为引擎默认最小值，Python 调度抖动会丢音频）
- 静音补包改 catch-up 语义：按真实经过时间补足（旧节奏供给仅为消费速率的 1/12）
- 时间戳单调性保护：静音→有声切换时防止 ts 倒退
- 双端每 5 秒诊断快照，覆盖所有可能产生波形断口的路径：丢包/静音填充/重同步/carry 丢帧/削波/时钟 offset 跳变

## 已知问题

- 副机传入音频偶有周期性噼啪声：已排除丢包、欠载、静音填充、削波、时钟跳变（真机实测全部为零）；怀疑剩余方向为音频数据层，下版本继续定位

## 升级方式

两台机器都用本版本 exe 替换旧版，重启程序即可。
"""


def gh_token() -> str:
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n",
        capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line[len("password="):]
    raise RuntimeError("no token in credential helper output")


def api(url: str, token: str, data: bytes | None = None, method: str | None = None,
        ctype: str = "application/json; charset=utf-8"):
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    if data is not None:
        req.add_header("Content-Type", ctype)
    with urllib.request.urlopen(req) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def main() -> int:
    token = gh_token()
    payload = json.dumps({
        "tag_name": TAG,
        "name": "v0.2.0 — 键盘冻结根因修复 + 音频诊断体系",
        "body": BODY,
        "draft": False,
    }, ensure_ascii=False).encode("utf-8")
    status, rel = api(
        f"https://api.github.com/repos/{REPO}/releases", token, payload)
    print("release created:", status, rel["id"], rel["html_url"])

    # 上传 exe 资产
    exe = r"D:\我的文档\工作区\Vibe Coding\Com Bridge\dist\com-bridge-gui.exe"
    with open(exe, "rb") as f:
        blob = f.read()
    up = rel["upload_url"].split("{")[0]
    up += "?name=com-bridge-gui.exe"
    status2, asset = api(up, token, blob,
                         ctype="application/octet-stream")
    print("asset uploaded:", status2, asset["browser_download_url"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
