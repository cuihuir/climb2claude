# Claude Code 通过 SSH 使用远程主机

日期：2026-11-16

小爬在家写文件，把需要橙色那只回答的问题送到远处去。

这里的“端口镜像”指 **SSH 本地端口转发**：Claude Code 在本机读写文件、执行工具，模型请求及所需上下文通过 SSH 发给远端 Magpie，再由它调用 Claude。远端已经部署好服务时，按本文接入即可。

```mermaid
flowchart LR
    A[本机 Claude Code] --> B[本机 127.0.0.1:13425]
    B -->|SSH 加密隧道| C[远端 Magpie 127.0.0.1:3425]
    C --> D[Claude 服务]
```

订阅凭据留在远端；请求中的代码和上下文会经过远端，应使用自己信任的主机与管理员。两端服务都绑定回环地址，访问入口是 SSH。

## 1. 准备

- 本机安装 Claude Code 和 OpenSSH，确认 `claude --version`、`ssh -V` 能运行。
- 远端管理员已部署 Magpie、完成 Claude 登录，并确认 `http://127.0.0.1:3425/v1/models` 能返回模型目录。本文从已有远端服务开始。
- 向管理员取得 SSH 主机地址、登录用户名、SSH 端口及服务器主机指纹。
- 准备本机专用 SSH 密钥，其公钥授权给上述远端登录用户。已有密钥及授权可跳过对应步骤。

| 客户端 | 自动启动工具 | 依赖 |
| --- | --- | --- |
| Linux | [magpie_tunnel.py](../tools/claude-ssh/magpie_tunnel.py)，也可用 Bash 入口 | Python 3.11+、OpenSSH |
| Windows | [start_claude_magpie.ps1](../tools/claude-ssh/start_claude_magpie.ps1) | PowerShell 5.1 / 7、OpenSSH；无需 Python |
| macOS | 第 4 节 OpenSSH 手工命令 | OpenSSH；Python 自动启动器依赖 Linux `/proc`，不适用 |

从仓库根目录执行下文命令。也可以只复制 Python 文件，或只复制 PS1 文件到客户端，再将 `connection.json` 放在它旁边；这种情况下相应调整命令路径。文件清单与配置优先级见 [工具说明](../tools/claude-ssh/README.md)。

## 2. 在新客户端生成密钥

Windows PowerShell：

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.ssh" | Out-Null
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\claude_tunnel_client1" -C claude-tunnel-client1
```

Linux：

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
ssh-keygen -t ed25519 -f ~/.ssh/claude_tunnel_client1 -C claude-tunnel-client1
chmod 600 "$HOME/.ssh/claude_tunnel_client1"
```

已有同名文件时不要覆盖，换一个客户端名称，并同步修改后面的私钥路径。macOS 可用相同的密钥生成命令。

生成的两个文件用途不同：

| 文件 | 放在哪里 |
| --- | --- |
| `claude_tunnel_client1` | 私钥，留在本机 `.ssh` 目录；Windows 权限仅授予本人及可信系统账号 |
| `claude_tunnel_client1.pub` | 公钥，可以交给管理员授权 |

如果设置了密钥口令，启动自动脚本前先在已运行的 SSH 代理中执行 `ssh-add 私钥路径`。脚本使用 `BatchMode`，不会循环询问口令或账号密码。

## 3. 让管理员授权公钥

将 `.pub` 文件交给已能登录的设备。需要上传时，在该设备的公钥文件所在目录执行；将 `SSH_PORT`、`SSH_USER`、`SSH_HOST` 替换为实际连接信息：

```bash
scp -P SSH_PORT ./claude_tunnel_client1.pub "SSH_USER@SSH_HOST:~/claude_tunnel_client1.pub"
```

管理员登录远端，**以新客户端将要登录的用户**执行：

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
printf '\n' >> ~/.ssh/authorized_keys
printf 'restrict,port-forwarding,permitopen="127.0.0.1:3425",command="/bin/false" %s\n' \
  "$(cat ~/claude_tunnel_client1.pub)" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

每个公钥完整占一行，`>>` 保留已有授权，通常无需重启 SSH。远端服务端口不是 `3425` 时，相应修改 `permitopen`。

这条授权允许转发到 Magpie，禁止该密钥启动交互会话或执行任意远端命令。因此后面用 `ssh -N` 核验，直接用它登录 shell 被拒绝是预期行为。若独立隧道账号还需禁用 `-R` 远程转发，管理员可在 sshd 中为该账号设置 `AllowTcpForwarding local`；上述公钥选项本身不禁止所有远程转发。

每个客户端用独立密钥，撤销时只移除对应授权行。私钥不上传到远端，也不放进仓库。

## 4. 在新客户端核对指纹，试通隧道

替换私钥路径及三个 SSH 占位符，在新客户端执行。Windows、Linux 和 macOS 的 OpenSSH 都支持这个核心命令：

```bash
ssh -i "PRIVATE_KEY_PATH" -o IdentitiesOnly=yes -o ExitOnForwardFailure=yes -N -L 127.0.0.1:13425:127.0.0.1:3425 -p SSH_PORT SSH_USER@SSH_HOST
```

`PRIVATE_KEY_PATH` 是本机**无 `.pub` 后缀**的私钥完整路径，例如 Linux 的 `/home/你的用户名/.ssh/claude_tunnel_client1`，或 Windows 的 `C:\Users\你的用户名\.ssh\claude_tunnel_client1`。

首次连接时，将显示的服务器指纹与管理员提供的指纹核对，一致后接受，记录到本机 `known_hosts`。遇到指纹变化先向管理员核实，不要关闭主机校验。自动脚本使用 `StrictHostKeyChecking=yes`，不会自行接受新主机。

连接成功时通常没有输出，窗口会一直保持运行。另开终端验证本机入口：

```bash
curl --noproxy '*' -fsS http://127.0.0.1:13425/v1/models
```

Windows PowerShell 5.1 用 `curl.exe` 执行上述参数，避免 `curl` 别名被当作 `Invoke-WebRequest`。期望返回带模型 ID 的 JSON。完成后在隧道窗口按 `Ctrl+C`，释放端口，再启动下一节的脚本。

`-i` 指定私钥，`-N` 只建隧道，`-L` 把本机 `13425` 转给远端 `127.0.0.1:3425`。第二个 `127.0.0.1` 指远端自身。两端端口可以不同；SSH 服务端口则由 `-p` 指定。

## 5. 配置连接，启动自动隧道

### 填写本机连接文件

首次创建时执行下面的复制命令；已有 `connection.json` 时直接编辑，跳过复制。

Linux：

```bash
cp tools/claude-ssh/connection.example.json tools/claude-ssh/connection.json
chmod 600 tools/claude-ssh/connection.json
```

Windows PowerShell：

```powershell
Copy-Item .\tools\claude-ssh\connection.example.json .\tools\claude-ssh\connection.json
```

打开 `connection.json`，替换示例主机和用户名，按管理员提供的信息填写 SSH 端口：

```json
{
  "host": "ssh.example.invalid",
  "user": "claude-tunnel",
  "ssh_port": 22,
  "local_port": 13425,
  "remote_port": 3425
}
```

`host`、`user`、`ssh_port` 是 SSH 连接信息；`local_port` 是本机 Claude 使用的端口，`remote_port` 是远端 Magpie 的端口。示例地址不可连接，脚本没有预设个人主机。真实文件已加入 `.gitignore`，只留在本机，文件中不需要密码或订阅令牌。

### Linux

首次配置并启动：

```bash
python3 tools/claude-ssh/magpie_tunnel.py --identity-file "$HOME/.ssh/claude_tunnel_client1" --configure-claude
```

以后每次使用只启动隧道：

```bash
python3 tools/claude-ssh/magpie_tunnel.py --identity-file "$HOME/.ssh/claude_tunnel_client1"
```

也可用 `bash tools/claude-ssh/start_claude_magpie.sh`，后面带相同参数。Linux 首次配置还会添加 Claude 底部额度状态栏，保留已有自定义状态栏。

### Windows

首次配置并启动：

```powershell
.\tools\claude-ssh\start_claude_magpie.ps1 -IdentityFile "$env:USERPROFILE\.ssh\claude_tunnel_client1" -ConfigureClaude
```

以后每次使用：

```powershell
.\tools\claude-ssh\start_claude_magpie.ps1 -IdentityFile "$env:USERPROFILE\.ssh\claude_tunnel_client1"
```

执行策略阻止脚本时，审阅文件后可仅为这次进程使用：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\claude-ssh\start_claude_magpie.ps1 -IdentityFile "$env:USERPROFILE\.ssh\claude_tunnel_client1" -ConfigureClaude
```

这不修改系统执行策略；组织策略仍可能限制执行。

### 首次配置做了什么

看到 `SSH tunnel ready.` 后保持窗口打开；首次还要等到 Claude 配置成功。配置脚本读取 `/v1/models`，更新当前用户的 `~/.claude/settings.json`（Windows 为 `%USERPROFILE%\.claude\settings.json`）：

- 网关设为 `http://127.0.0.1:13425`，认证占位值为 `magpie`。
- 默认选择 Opus，并为 Opus / Sonnet / Haiku / Fable 配置最新可识别版本、模型映射及菜单。
- 保留其他设置。Linux 写字段回滚记录，Windows 在 settings 旁保存完整备份。

当前自动配置要求目录包含四个家族的标准版本 ID，例如 `claude/claude-opus-<版本号>`；缺少任一家族时会报错并保留原 settings。配置失败时隧道仍运行，处理错误后停止并带配置参数重试。普通启动不修改 Claude 配置。

仅手工转发（包括 macOS）时，可先备份 settings，再合并以下字段到现有 JSON；将 `model` 替换为 `/v1/models` 实际返回的模型 ID，保留原文件其他字段：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:13425",
    "ANTHROPIC_AUTH_TOKEN": "magpie"
  },
  "model": "替换为远端目录中的模型ID"
}
```

`magpie` 是此部署允许的回环网关占位值；实际访问依靠 SSH 授权。若管理员配置了额外网关认证，应按其部署要求填写。

## 6. 使用 Claude、查看状态、结束

另开终端，进入要工作的**本机项目目录**，运行：

```bash
claude
```

用 `/status` 核对接入状态，`/model` 检查模型菜单，发一条简短请求确认能收到回复。模型目录可访问，只说明接口可达；实际回复成功才完成使用验收。项目级设置或显式命令参数可能覆盖全局配置。

自动隧道窗口显示单行上下行速率和累计流量，断线后每 5 秒重连，不重放失败请求。累计值为本次启动转发的应用字节，不含 SSH 协议开销。保持窗口打开，同一端口只开一个隧道。

Linux 还显示远端 5H / Weekly 已用比例及重置倒计时；首次连接和重连时读取，之后每 3 分钟读取一次。不发送模型保活请求。接口不可用时显示未知或 `[stale]`，不据此推断额度归零；数据是 Magpie 最近采集的快照。Windows 入口提供流量显示，未集成这套额度状态栏。

Linux 在已有隧道上查看额度快照：

```bash
python3 tools/claude-ssh/magpie_tunnel.py status
```

只需流量时，Linux 启动命令可加 `--no-usage`。结束时在隧道窗口按 `Ctrl+C`，并退出 Claude；脚本清理自己创建的 SSH 进程和端口，不创建系统服务或自启动项。

## 7. 常见问题

| 现象 | 检查与处理 |
| --- | --- |
| 公钥已授权，仍提示账号密码 | 核对 `-i` 是否指向本机私钥；自定义文件名不会被 SSH 自动选中。确认公钥放在实际登录用户的 `authorized_keys` 中 |
| 自动脚本认证失败 | 先用第 4 节手工命令验证。加密私钥先由 `ssh-add` 加载到代理；脚本不交互询问密码 |
| 主机指纹未知或变化 | 与管理员核对，再用手工命令建立可信 `known_hosts`；不要关闭校验 |
| `administratively prohibited` | 管理员核对 sshd 转发权限，以及公钥 `permitopen` 是否与远端 `127.0.0.1:3425` 一致 |
| 本机端口被占用 | 先关闭旧隧道；需要保留旧隧道时换 `local_port`，并重新运行首次配置。仅有 `TIME_WAIT` 不代表有残留监听进程 |
| `Connection refused` | 确认隧道运行，再让管理员核对远端 Magpie 的监听端口；本机先用第 4 节 `curl` 检查 |
| shell 已 export 新地址，Claude 仍用旧网关 | 检查用户及项目 settings；旧配置可能覆盖环境变量。执行首次配置后重新打开 Claude |
| 新模型没有出现在菜单 | 先核对远端 `/v1/models` 含目标模型，再带 `--configure-claude` / `-ConfigureClaude` 重新启动，最后重开 Claude；重启本身不刷新菜单 |
| 首次配置提示缺少模型家族 | 请管理员核对目录与命名，或按第 5 节手工配置已有模型。无需为此改 SSH 隧道 |
| Claude 设置再次变回旧网关 | 检查本机 Magpie 或其他工具是否也在管理 Claude settings，确定由哪个入口维护 |

## 8. 回滚与撤销授权

先停止隧道并退出 Claude。Linux 恢复本工具改过、且此后未被手工修改的字段：

```bash
python3 tools/claude-ssh/magpie_tunnel.py restore
```

记录在有效绝对路径的 `$XDG_STATE_HOME/claude-magpie/settings-changes.json`，否则在 `~/.local/state/claude-magpie/settings-changes.json`。使用原启动环境及同一用户回滚；记录可能包含旧设置，不要分享。后续编辑的字段会保留并报告冲突。

Windows 在 settings 旁查找相应的 `settings.json.before-magpie-*.bak`，核对时间和内容后恢复。备份可能含私有配置；配置后有其他修改时，应只恢复相关字段，避免整文件覆盖。Linux 的 `restore` 不读取这种 Windows 备份。手工配置则从自己事先保存的备份恢复对应字段。

只撤回 Linux 底部状态栏时，可删除由本工具添加的 `statusLine` 字段。删除或移动脚本前先回滚，因为状态栏保存了脚本绝对路径。

撤销某个客户端时，管理员只移除远端 `authorized_keys` 中该客户端对应的一行；如需立即中断访问，还要结束该客户端已建立的 SSH 连接。其他密钥和远端 Magpie 保持原样。

## 验证范围与来源

本文整合自 Fedora 运维知识库的 SSH/Magpie 快速开始、完整配置和已用启动工具，保留了重连、单实例、流量统计、模型目录同步及回滚能力，移除了个人主机默认值和原仓库依赖。

2026-11-16，集成版本通过 61 项离线回归，覆盖模拟 SSH 重连、端口冲突、进程清理、流量统计、额度、Claude 配置保留及回滚，并核对了 28 处相对链接。测试使用临时文件和回环端口，不连接真实远端或修改用户的 Claude 配置。

Linux 原方案已验证隧道转发、模型回复、断线重连及停止释放端口。Windows 脚本已有 PowerShell 7/Linux 与模拟 SSH 回归，**尚不等同于 Windows 5.1 或 Windows 实机验收**。此处接入仍应在自己的客户端完成第 4、6 节的验证。
