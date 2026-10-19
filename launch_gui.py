"""GUI(窗口化)打包入口, 与 launch.py 同理: 放项目根, 让 com_bridge 作为
普通包被 PyInstaller 分析到。
"""

from __future__ import annotations

import sys


def main() -> int:
    from com_bridge.cli import main as cli_main

    try:
        rc = cli_main(["gui"])
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 2
    return int(rc)


if __name__ == "__main__":
    raise SystemExit(main())
