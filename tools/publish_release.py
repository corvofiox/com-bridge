# -*- coding: utf-8 -*-
"""发布 GitHub Release：从命令行读 tag / 资产 / 说明文件，上传资产。

用法:
    python tools/publish_release.py <tag> <asset_path> [notes_md]

例:
    python tools/publish_release.py v0.3.0 dist/com-bridge-gui.exe tools/RELEASE_v0.3.0.md
"""
import json
import os
import subprocess
import sys
import urllib.request

REPO = "corvofiox/com-bridge"


def gh_token() -> str:
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n",
        capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line[len("password="):]
    raise RuntimeError("credential helper 未返回 token")


def api(url: str, token: str, data: bytes | None = None,
        method: str | None = None,
        ctype: str = "application/json; charset=utf-8"):
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    if data is not None:
        req.add_header("Content-Type", ctype)
    with urllib.request.urlopen(req) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    tag = sys.argv[1]
    asset = sys.argv[2]
    notes_path = sys.argv[3] if len(sys.argv) > 3 else None

    body = ""
    if notes_path and os.path.exists(notes_path):
        with open(notes_path, encoding="utf-8") as f:
            body = f.read()

    title = tag
    if tag == "v0.3.0":
        title = "v0.3.0 — 音频链路整修（四个独立爆音源）"

    token = gh_token()
    payload = json.dumps({
        "tag_name": tag,
        "name": title,
        "body": body,
        "draft": False,
    }, ensure_ascii=False).encode("utf-8")
    status, rel = api(
        f"https://api.github.com/repos/{REPO}/releases", token, payload)
    print("release:", status, rel["id"], rel["html_url"])

    with open(asset, "rb") as f:
        blob = f.read()
    up = rel["upload_url"].split("{")[0]
    up += "?name=" + os.path.basename(asset)
    status2, a = api(up, token, blob, ctype="application/octet-stream")
    print("asset:", status2, a["browser_download_url"],
          f"{a['size'] / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
