# climb2claude

公开笔记：怎样把 [Claude Code](https://docs.anthropic.com/en/docs/claude-code) 装到自己的环境里，并把它用成日常工具。

仓库说明：hard to climb to claude。

## 记录范围

- 安装和运行环境
- 登录与网络
- 日常用法
- 已经踩过、并且能复现的问题

笔记放在 [`docs/`](docs/README.md)。每篇只写验证过的步骤、预期结果和回退方法。

## 当前状态

第一步是网络检测，见 [docs/01-check-network.md](docs/01-check-network.md)。出口使用外网 VPS 上的 sing-box 节点，VLESS 链接转成 Clash YAML 后导入。2026-10-20 的检测还没有用这台新出口复测。

## 许可

[MIT](LICENSE)
