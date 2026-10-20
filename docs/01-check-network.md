# 第一步：检测网络环境

日期：2026-10-20

打开 [Claude AI IP 风险检测](https://ip.net.coffee/claude/)，把标成风险的项目处理掉，再进入安装。检测在浏览器里完成，包含 DNS 和 WebRTC，命令行访问同一个网址看不到这些结果。

不要把国内出口地址、DNS 出口地址写进仓库。

## 合格线

| 项目 | 通过 |
| --- | --- |
| 页顶提示 | 没有「当前为 IPv6 环境」 |
| Claude AI 出口 | IPv4，并且和 Cloudflare 出口属于同一次代理出口 |
| 信任评分 | 「纯净」，Claude 支持地区为「正常」 |
| DNS 泄露 | 不是「可能泄露」，没有「中国DNS」 |
| WebRTC UDP | 不是「可能泄露」，归属地不是 China，出口没有「异常」 |
| 可用性 | `claude.ai`、`anthropic.com` 为「正常」，服务状态正常 |

信任分 75 以上且标成纯净，就可以留下当前节点。机房 IP 不如住宅 IP，但分数已经是纯净时，这一步不换节点。

## 这一次的结果

检测页同时给出了三张出口卡片：国内运营商 IPv4、一条美国 Cloudflare IPv4、一条美国 Claude IPv6。Claude 出口被标成机房 IP。

已经通过：

- 信任分 82，纯净，支持地区正常
- 未检出 VPN、代理、Tor 和滥用记录
- `claude.ai` 与 `anthropic.com` 正常，约 186ms，服务全部正常

尚未通过：

- 页顶提示当前是 IPv6，Claude 出口是 IPv6
- 国内 IPv4 出口仍然可见，并和两条海外出口同时出现
- DNS 为「可能泄露」，徽章是「中国DNS」，服务商为中国电信
- WebRTC UDP 为「可能泄露」，出口标成异常，归属地是 China

## 原因

本机是 Clash Verge Rev，内核是 mihomo。核对运行配置时：

- 系统代理开着，模式是 `rule`
- Tun 关着
- Clash 的 `ipv6` 为 `true`，系统也没有关闭 IPv6
- Clash DNS 开着，但系统解析器没有使用它

系统代理只接管浏览器的 HTTP 和 HTTPS。所以 Claude 和 Cloudflare 的检测走到了美国，DNS 查询和 WebRTC 的 UDP 仍从国内网络出去。检测页看到的 DNS 服务商是中国电信；系统当前的加密 DNS 是 AliDNS。这次泄露的查询没有走那条加密 DNS。

Clash 允许 IPv6 后，Claude 出口走了代理节点的 IPv6，Cloudflare 出口仍是另一条 IPv4。页顶因此出现 IPv6 警告。

## 解决方法

使用一台外网 VPS 的地址作为出口。在 VPS 上用 [233boy/sing-box](https://github.com/233boy/sing-box) 的[一键脚本](https://233boy.com/sing-box/sing-box-script/)建好节点，取出 VLESS 链接，再经 [VLESS to Clash](https://shipengtao.com/en/tools/vless-to-clash/) 转成 YAML，导入本机的 Clash Verge Rev。

链接、UUID 和下载下来的 YAML 只留在本机，不提交到仓库。

### 1. 准备 VPS

准备一台外网 VPS。脚本支持 Ubuntu、Debian、CentOS，作者推荐 Ubuntu 22。装好后用 root 登录。

云厂商的安全组要放行节点端口。安装结束前端口还未知，装完再按脚本给出的端口补规则。

### 2. 在 VPS 上安装 sing-box

下面这条命令在 VPS 上执行，不在本机执行。脚本来自上面的 GitHub 仓库，安装时会自动创建一条 VLESS-REALITY：

```bash
bash <(wget -qO- -o- https://github.com/233boy/sing-box/raw/main/install.sh)
```

装完后在 VPS 上用 `sb` 打开管理面板。查看链接：

```bash
sb url
```

默认只有一条配置时，这条命令会打印 VLESS 链接。复制整行，形如 `vless://...`。

端口不通时，用 `sb ip` 看 VPS 地址，再打开 [tcp.ping.pe](https://tcp.ping.pe/)，填写 `IP:端口`。显示 successful 之后再继续。脚本和配置在 VPS 的 `/etc/sing-box`。

### 3. 转成 Clash YAML

在本机浏览器打开 [VLESS to Clash](https://shipengtao.com/en/tools/vless-to-clash/)。转换在浏览器本地完成，链接不会上传。

1. 把 VLESS 链接贴进输入框，一行一条。
2. 输出格式选 **Clash / Mihomo YAML**。本机内核是 mihomo；原版 Clash 和 Clash Premium 不支持 VLESS。
3. 路由选 **Proxy all traffic**，让检测流量走这台 VPS。
4. 点 Convert，再点 Download，保存 YAML 文件。

这个文件是一次性的本地配置，不会自动更新。VPS 上的 UUID、端口或密钥变了之后，要重新 `sb url`，再转换一次。

### 4. 导入 Clash Verge Rev

在 Clash Verge Rev 里：配置 → 新建 → 本地配置，选中刚下载的 YAML。启用这份配置，并选中其中的 VPS 节点。

完全关掉做过检测的浏览器再打开。旧页面会留下上一次的 WebRTC 结果。

## 复测

刷新 [检测页](https://ip.net.coffee/claude/)，逐项对照上面的合格线。Claude 出口应变成这台 VPS 的 IPv4。六项都通过后，这一步才算完成。

若 DNS 或 WebRTC 仍指向国内，打开 Tun，让 UDP 和 53 端口也进入这份配置，然后重新检测。

## 回滚

在 Clash Verge Rev 里停用这份本地配置，切回原来的配置。

VPS 上卸载脚本：

```bash
sb uninstall
```

`sb del` 会直接删除节点且不再确认。只想停用时，在客户端切走即可。
