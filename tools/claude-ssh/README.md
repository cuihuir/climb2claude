# Claude SSH 启动工具

来自 Fedora 运维知识库已使用的 Magpie SSH 工具，随 [完整教程](../../docs/claude-ssh.md) 收录。此版本清除了个人连接默认值和原仓库配置路径；真实连接信息只保存在已被 Git 忽略的 `connection.json`。

| 文件 | 用途 |
| --- | --- |
| `magpie_tunnel.py` | Linux 独立入口：隧道、流量、额度、Claude 配置及回滚；Python 3.11+、OpenSSH |
| `start_claude_magpie.sh` | Linux Bash 入口，调用同目录 Python 文件 |
| `start_claude_magpie.ps1` | Windows 独立入口：隧道、流量及 Claude 配置；PowerShell 5.1 / 7、OpenSSH，无需 Python |
| `connection.example.json` | 连接字段示例，复制为 `connection.json` 后填入管理员提供的信息 |

先完成教程中的公钥授权和服务器指纹核对，再启动。在本目录运行：

```bash
# Linux：首次配置 Claude
python3 ./magpie_tunnel.py --identity-file "$HOME/.ssh/claude_tunnel_client1" --configure-claude
# 日常去掉 --configure-claude；Ctrl+C 停止
```

```powershell
# Windows：首次配置 Claude
.\start_claude_magpie.ps1 -IdentityFile "$env:USERPROFILE\.ssh\claude_tunnel_client1" -ConfigureClaude
# 日常去掉 -ConfigureClaude；Ctrl+C 停止
```

Linux 配置优先级：`--config` → `CLAUDE_MAGPIE_CONFIG` → 同目录 `connection.json` → 用户状态目录的 `connection.json`。状态和字段回滚记录写入有效绝对路径的 `$XDG_STATE_HOME/claude-magpie/`，否则写入 `~/.local/state/claude-magpie/`。

Windows 配置优先级：显式连接参数 → `-ConfigPath` 或同目录 `connection.json` → 默认端口。主机和用户名没有预设，缺失时交互询问。配置 Claude 前会在 settings 旁生成完整备份。

两个版本都只监听 IPv4 回环，使用 SSH 密钥及已核对的 `known_hosts`，每 5 秒重连，只管理自己创建的进程。首次配置会修改当前用户的 Claude Code settings；普通启动不修改。完整用法、限制和恢复方法见教程。

Linux 实现依赖 `/proc` 校验 SSH 端口归属，**不支持 macOS**。macOS 可按教程使用 OpenSSH 手工转发。Windows 脚本原有回归使用 Linux 上的 PowerShell 7 和模拟 SSH，仍需 Windows 实机验收。
