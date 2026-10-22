# Com Bridge v0.4.3

v0.4.2 的一个小补丁，只改一处读取逻辑。

## 修：配置文件带 BOM 时被当成"坏配置"

`config.json` 如果被**记事本**另存、或用 PowerShell 的 `Set-Content -Encoding UTF8`
写过，文件开头会带 UTF-8 BOM。Python 的 `json.load` 遇到 BOM 会直接抛
`ValueError`，而我们的读取逻辑对任何异常都"退回默认值"——于是：

- 用户明明勾了"上次运行时退出 -> 下次自动连接"，重启后**不自动连**；
- 角色、方位、主电脑 IP 全部回到默认值，界面上看起来像"设置丢了"。

现在读取统一用 `utf-8-sig`（能容 BOM，也能读普通 UTF-8），并新增 3 项回归测试
（带 BOM 能读 / 坏 JSON 仍退回默认 / 我们自己写出去的配置不带 BOM）。

## 顺带

- 部署脚本里用 PowerShell 改配置时改用 `.NET File.WriteAllText(UTF8Encoding(false))`，
  不再产生 BOM。

## 升级

与 v0.4.2 相同：两台都要换。功能上与 v0.4.2 完全一致（剪贴板文字/图片/文件）。
