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

## 处理

按这个顺序改 Clash Verge。只改界面里的开关，让它自己重写运行配置。

1. 关闭 Clash 的 IPv6。先不动系统的 IPv6 开关。
2. 打开 Tun（虚拟网卡）。系统代理可以保持打开。Tun 关闭时，53 端口和 UDP 不进 Clash，检测页的 DNS、WebRTC 会继续指向国内。
3. 打开 Clash 的 DNS 覆写。对外解析使用走代理的 DoH，用下面两组即可：

```yaml
ipv6: false
dns:
  enable: true
  ipv6: false
  enhanced-mode: fake-ip
  respect-rules: true
  nameserver:
    - https://1.1.1.1/dns-query
    - https://8.8.8.8/dns-query
  proxy-server-nameserver:
    - 223.5.5.5
```

`proxy-server-nameserver` 只负责解析代理节点自己的域名。检测页上看到的解析器来自 `nameserver`。

4. 保存后重载配置，完全关闭再打开做过检测的浏览器，然后重新打开检测页。旧页面会留下上一次的 WebRTC 结果。

复测时如果 DNS 服务商从中国电信变成阿里云，说明系统的 DNS over TLS 仍在直接访问 AliDNS。Tun 的 `dns-hijack: any:53` 接不住 853 端口。这时把当前网络连接的 DNS over TLS 暂时关掉，让查询进入 Tun，再次复测。

国内 IPv4 卡片和 WebRTC 卡片是同一类直连泄露。Tun 生效后，这两项应不再指向国内运营商。

关掉 IPv6 后，Claude 出口和 Cloudflare 出口应变成同一条 IPv4。若仍是两个地址，先记下来，下一步再收域名分流。

## 复测

刷新 [检测页](https://ip.net.coffee/claude/)，逐项对照上面的合格线。六项都通过后，这一步才算完成。

## 回滚

在 Clash Verge 里把 IPv6 打开、Tun 关掉，并关闭这次加上的 DNS 覆写。系统代理恢复到修改前的状态。

如果为了复测关闭过当前连接的 DNS over TLS，改回原来的加密 DNS 设置。
