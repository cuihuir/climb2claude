#!/usr/bin/env python3
"""Turn a local subscription or share-link list into a Clash/Mihomo profile.

No arguments in a terminal starts the prompt: paste one share link at a
time, then name the file. Files, --url, and -o stay one-shot.

Conversion stays on this machine. A --url download only fetches that
subscription; the body is not sent to a conversion site.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlsplit

PROXY_SCHEMES = {
    "vless",
    "vmess",
    "trojan",
    "ss",
    "ssr",
    "hysteria",
    "hysteria2",
    "hy2",
    "tuic",
    "anytls",
}

URI_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s]+)")
PLAIN_NAME = re.compile(r"[A-Za-z0-9._/+-]+")
RESERVED_WORDS = {"true", "false", "null", "yes", "no", "on", "off"}


class Skip(Exception):
    pass


def b64decode_text(value: str) -> str | None:
    compact = "".join(value.split())
    if not compact:
        return None
    padded = compact + "=" * (-len(compact) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            raw = decoder(padded, validate=False)
        except Exception:
            continue
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None


def parse_query(query: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for part in query.split("&"):
        if not part:
            continue
        key, _, raw = part.partition("=")
        found[unquote(key)] = unquote(raw)
    return found


def split_host_port(hostport: str) -> tuple[str, int]:
    hostport = hostport.strip()
    if hostport.startswith("["):
        host, _, port = hostport[1:].partition("]:")
        if not host or not port:
            raise Skip("地址里没有端口")
        return host, int(port)
    host, sep, port = hostport.rpartition(":")
    if not sep or not host:
        raise Skip("地址里没有端口")
    return host, int(port)


def split_uri(uri: str) -> tuple[str, str, dict[str, str], str]:
    parts = urlsplit(uri)
    scheme = parts.scheme.lower()
    name = unquote(parts.fragment) if parts.fragment else ""
    query = parse_query(parts.query)
    body = parts.netloc + parts.path
    return scheme, body, query, name


def flag_true(value: str | None) -> bool:
    return (value or "").lower() in {"1", "true", "yes"}


def first_name(name: str, fallback: str, used: set[str]) -> str:
    base = name.strip() or fallback
    candidate = base
    number = 2
    while candidate in used:
        candidate = f"{base}-{number}"
        number += 1
    used.add(candidate)
    return candidate


def alpn_list(value: str | None) -> list[str] | None:
    if not value:
        return None
    items = [item.strip() for item in value.split(",") if item.strip()]
    return items or None


def reality_opts(query: dict[str, str]) -> dict:
    public_key = query.get("pbk") or query.get("publicKey") or ""
    if not public_key:
        raise Skip("REALITY 缺少 pbk")
    opts: dict = {"public-key": public_key}
    short_id = query.get("sid") or query.get("shortId") or ""
    if short_id:
        opts["short-id"] = short_id
    return opts


def transport_fields(query: dict[str, str], host_header: str) -> dict:
    network = (query.get("type") or query.get("net") or "tcp").lower()
    header_host = query.get("host") or host_header
    path = query.get("path") or "/"
    extra: dict = {}
    if network in {"tcp", "raw", "none"}:
        return {"network": "tcp"}
    if network == "ws":
        extra["network"] = "ws"
        extra["ws-opts"] = {"path": path, "headers": {"Host": header_host}}
        return extra
    if network == "grpc":
        service = query.get("serviceName") or query.get("path") or ""
        extra["network"] = "grpc"
        if service:
            extra["grpc-opts"] = {"grpc-service-name": service}
        return extra
    if network in {"h2", "http"}:
        extra["network"] = "h2"
        extra["h2-opts"] = {"host": [header_host], "path": path}
        return extra
    if network == "httpupgrade":
        extra["network"] = "httpupgrade"
        extra["http-upgrade-opts"] = {"host": header_host, "path": path}
        return extra
    raise Skip(f"不支持的传输 {network}")


def tls_fields(query: dict[str, str], server: str) -> dict:
    security = (query.get("security") or "").lower()
    fields: dict = {}
    if security in {"tls", "reality"} or flag_true(query.get("tls")):
        fields["tls"] = True
    sni = query.get("sni") or query.get("peer") or ""
    if security == "reality":
        fields["tls"] = True
        fields["servername"] = sni or server
        fields["client-fingerprint"] = query.get("fp") or "chrome"
        fields["reality-opts"] = reality_opts(query)
    elif fields.get("tls"):
        if sni:
            fields["servername"] = sni
        fingerprint = query.get("fp")
        if fingerprint:
            fields["client-fingerprint"] = fingerprint
        if flag_true(query.get("allowInsecure") or query.get("insecure") or query.get("allow_insecure")):
            fields["skip-cert-verify"] = True
        alpn = alpn_list(query.get("alpn"))
        if alpn:
            fields["alpn"] = alpn
    return fields


def parse_vless(uri: str, used: set[str]) -> dict:
    _, body, query, name = split_uri(uri)
    user, sep, hostport = body.rpartition("@")
    if not sep or not user:
        raise Skip("VLESS 缺少 uuid")
    server, port = split_host_port(hostport)
    node = {
        "name": first_name(name, f"{server}:{port}", used),
        "type": "vless",
        "server": server,
        "port": port,
        "uuid": unquote(user),
        "udp": True,
    }
    node.update(transport_fields(query, server))
    node.update(tls_fields(query, server))
    flow = query.get("flow") or ""
    if flow:
        node["flow"] = flow
    return node


def parse_vmess(uri: str, used: set[str]) -> dict:
    payload = uri.split("://", 1)[1]
    payload = payload.split("#", 1)[0]
    decoded = b64decode_text(payload)
    if not decoded:
        raise Skip("VMess 不是 Base64 JSON")
    try:
        info = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise Skip("VMess JSON 无法解析") from exc
    server = str(info.get("add") or "")
    port = int(info.get("port"))
    if not server:
        raise Skip("VMess 缺少地址")
    network = str(info.get("net") or "tcp").lower()
    header_type = str(info.get("type") or "none").lower()
    query = {
        "type": "http" if network == "tcp" and header_type == "http" else network,
        "host": str(info.get("host") or ""),
        "path": str(info.get("path") or ""),
        "sni": str(info.get("sni") or ""),
        "fp": str(info.get("fp") or ""),
        "alpn": str(info.get("alpn") or ""),
        "tls": "1" if str(info.get("tls") or "").lower() == "tls" else "",
        "allowInsecure": str(info.get("allowInsecure") or info.get("skip-cert-verify") or ""),
        "serviceName": str(info.get("path") or "") if network == "grpc" else "",
    }
    node = {
        "name": first_name(str(info.get("ps") or ""), f"{server}:{port}", used),
        "type": "vmess",
        "server": server,
        "port": port,
        "uuid": str(info.get("id") or ""),
        "alterId": int(info.get("aid") or 0),
        "cipher": str(info.get("scy") or "auto"),
        "udp": True,
    }
    if not node["uuid"]:
        raise Skip("VMess 缺少 uuid")
    node.update(transport_fields(query, server))
    node.update(tls_fields(query, server))
    return node


def parse_trojan(uri: str, used: set[str]) -> dict:
    _, body, query, name = split_uri(uri)
    password, sep, hostport = body.rpartition("@")
    if not sep:
        raise Skip("Trojan 缺少密码")
    server, port = split_host_port(hostport)
    node = {
        "name": first_name(name, f"{server}:{port}", used),
        "type": "trojan",
        "server": server,
        "port": port,
        "password": unquote(password),
        "udp": True,
    }
    node.update(transport_fields(query, server))
    security = (query.get("security") or "tls").lower()
    if security != "none":
        node["tls"] = True
        sni = query.get("sni") or query.get("peer") or query.get("host") or server
        node["sni"] = sni
        if flag_true(query.get("allowInsecure") or query.get("insecure")):
            node["skip-cert-verify"] = True
    return node


def parse_ss(uri: str, used: set[str]) -> dict:
    _, body, query, name = split_uri(uri)
    if query.get("plugin"):
        raise Skip("Shadowsocks 带插件，这份粮单不写插件")
    if "@" in body:
        userinfo, hostport = body.rsplit("@", 1)
        decoded = b64decode_text(unquote(userinfo)) or unquote(userinfo)
        method, sep, password = decoded.partition(":")
        if not sep:
            raise Skip("Shadowsocks 用户信息无法解析")
        server, port = split_host_port(hostport)
    else:
        decoded = b64decode_text(unquote(body))
        if not decoded or "@" not in decoded:
            raise Skip("Shadowsocks 不是 SIP002")
        userinfo, hostport = decoded.rsplit("@", 1)
        method, sep, password = userinfo.partition(":")
        if not sep:
            raise Skip("Shadowsocks 用户信息无法解析")
        server, port = split_host_port(hostport)
    return {
        "name": first_name(name, f"{server}:{port}", used),
        "type": "ss",
        "server": server,
        "port": port,
        "cipher": method,
        "password": password,
        "udp": True,
    }


def parse_hysteria2(uri: str, used: set[str]) -> dict:
    _, body, query, name = split_uri(uri)
    password, sep, hostport = body.rpartition("@")
    if not sep:
        raise Skip("Hysteria2 缺少密码")
    server, port = split_host_port(hostport)
    node = {
        "name": first_name(name, f"{server}:{port}", used),
        "type": "hysteria2",
        "server": server,
        "port": port,
        "password": unquote(password),
        "udp": True,
    }
    sni = query.get("sni") or ""
    if sni:
        node["sni"] = sni
    alpn = alpn_list(query.get("alpn")) or ["h3"]
    node["alpn"] = alpn
    if flag_true(query.get("insecure") or query.get("allowInsecure")):
        node["skip-cert-verify"] = True
    obfs = query.get("obfs") or ""
    obfs_password = query.get("obfs-password") or query.get("obfsPassword") or ""
    if obfs:
        node["obfs"] = obfs
    if obfs_password:
        node["obfs-password"] = obfs_password
    return node


def parse_tuic(uri: str, used: set[str]) -> dict:
    _, body, query, name = split_uri(uri)
    userinfo, sep, hostport = body.rpartition("@")
    if not sep or ":" not in unquote(userinfo):
        raise Skip("TUIC 需要 uuid:password")
    uuid, _, password = unquote(userinfo).partition(":")
    server, port = split_host_port(hostport)
    node = {
        "name": first_name(name, f"{server}:{port}", used),
        "type": "tuic",
        "server": server,
        "port": port,
        "uuid": uuid,
        "password": password,
        "congestion-controller": query.get("congestion_control") or "bbr",
        "udp-relay-mode": query.get("udp_relay_mode") or "native",
        "alpn": alpn_list(query.get("alpn")) or ["h3"],
        "udp": True,
    }
    sni = query.get("sni") or ""
    if sni:
        node["sni"] = sni
    if flag_true(query.get("allow_insecure") or query.get("insecure") or query.get("allowInsecure")):
        node["skip-cert-verify"] = True
    if flag_true(query.get("reduce_rtt")):
        node["reduce-rtt"] = True
    return node


def parse_anytls(uri: str, used: set[str]) -> dict:
    _, body, query, name = split_uri(uri)
    password, sep, hostport = body.rpartition("@")
    if not sep:
        raise Skip("AnyTLS 缺少密码")
    server, port = split_host_port(hostport)
    node = {
        "name": first_name(name, f"{server}:{port}", used),
        "type": "anytls",
        "server": server,
        "port": port,
        "password": unquote(password),
        "udp": True,
    }
    sni = query.get("sni") or server
    node["sni"] = sni
    if flag_true(query.get("insecure") or query.get("allowInsecure")):
        node["skip-cert-verify"] = True
    return node


PARSERS = {
    "vless": parse_vless,
    "vmess": parse_vmess,
    "trojan": parse_trojan,
    "ss": parse_ss,
    "hysteria2": parse_hysteria2,
    "hy2": parse_hysteria2,
    "tuic": parse_tuic,
    "anytls": parse_anytls,
}


def unwrap_subscription(text: str) -> str:
    body = text.lstrip("\ufeff").strip()
    current = body
    for _ in range(2):
        if "://" in current:
            return current
        decoded = b64decode_text(current)
        if not decoded or decoded == current:
            return current
        current = decoded.strip()
    return current


def iter_uris(text: str) -> list[str]:
    found: list[str] = []
    for match in URI_RE.finditer(unwrap_subscription(text)):
        uri = match.group(1).strip()
        scheme = uri.split(":", 1)[0].lower()
        if scheme in PROXY_SCHEMES:
            found.append(uri)
    return found


def parse_subscription(text: str, used: set[str] | None = None) -> tuple[list[dict], list[str]]:
    nodes: list[dict] = []
    skipped: list[str] = []
    used = set() if used is None else used
    uris = iter_uris(text)
    if not uris and text.strip():
        skipped.append("没有找到分享串")
    for uri in uris:
        scheme = uri.split(":", 1)[0].lower()
        parser = PARSERS.get(scheme)
        if parser is None:
            skipped.append(f"不支持的协议 {scheme}")
            continue
        try:
            nodes.append(parser(uri, used))
        except Skip as exc:
            skipped.append(str(exc))
        except (ValueError, KeyError) as exc:
            skipped.append(f"{scheme} 解析失败: {exc}")
    return nodes, skipped


def yaml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if not isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if value == "":
        return '""'
    if PLAIN_NAME.fullmatch(value) and value.lower() not in RESERVED_WORDS and value[0].isalpha():
        return value
    return json.dumps(value, ensure_ascii=False)


def dump_value(key: str, value: object, indent: int, lines: list[str], bullet: bool) -> None:
    pad = " " * indent
    head = f"{pad}- {key}:" if bullet else f"{pad}{key}:"
    if isinstance(value, dict):
        items = [(item_key, item_value) for item_key, item_value in value.items() if item_value is not None]
        lines.append(head)
        for item_key, item_value in items:
            dump_value(item_key, item_value, indent + 2, lines, False)
        return
    if isinstance(value, list):
        lines.append(head)
        if not value:
            return
        if isinstance(value[0], dict):
            for item in value:
                pairs = [(item_key, item_value) for item_key, item_value in item.items() if item_value is not None]
                first_key, first_value = pairs[0]
                dump_value(first_key, first_value, indent + 2, lines, True)
                for item_key, item_value in pairs[1:]:
                    dump_value(item_key, item_value, indent + 4, lines, False)
            return
        for item in value:
            lines.append(f"{pad}  - {yaml_scalar(item)}")
        return
    lines.append(f"{head} {yaml_scalar(value)}")


PRIVATE_DIRECT = [
    "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve",
    "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
    "IP-CIDR,172.16.0.0/12,DIRECT,no-resolve",
    "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve",
    "IP-CIDR,100.64.0.0/10,DIRECT,no-resolve",
    "IP-CIDR6,::1/128,DIRECT,no-resolve",
    "IP-CIDR6,fc00::/7,DIRECT,no-resolve",
]

# Loyalsoldier/clash-rules release tracks the latest China domain and IP lists.
CN_RULE_FILES = (
    ("cn-domain", "domain", "direct.txt"),
    ("cn-ip", "ipcidr", "cncidr.txt"),
)
CN_RULE_MIRRORS = (
    "https://cdn.jsdelivr.net/gh/Loyalsoldier/clash-rules@release/{name}",
    "https://fastly.jsdelivr.net/gh/Loyalsoldier/clash-rules@release/{name}",
    "https://raw.githubusercontent.com/Loyalsoldier/clash-rules/release/{name}",
)


def render_profile(nodes: list[dict], providers: list[dict] | None = None) -> str:
    rules = []
    if providers:
        rules.extend(PRIVATE_DIRECT)
        for provider in providers:
            rule = f"RULE-SET,{provider['name']},DIRECT"
            if provider["behavior"] == "ipcidr":
                rule += ",no-resolve"
            rules.append(rule)
    rules.append("MATCH,Proxy")
    profile = {
        "mode": "rule",
        "ipv6": False,
        "proxies": nodes,
        "proxy-groups": [
            {
                "name": "Proxy",
                "type": "select",
                "proxies": [node["name"] for node in nodes] + ["DIRECT"],
            }
        ],
        "rules": rules,
    }
    if providers:
        profile["rule-providers"] = {
            provider["name"]: {
                "type": "inline",
                "behavior": provider["behavior"],
                "payload": provider["payload"],
            }
            for provider in providers
        }
    lines: list[str] = []
    for key, value in profile.items():
        dump_value(key, value, 0, lines, False)
    return "\n".join(lines) + "\n"


def read_url(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "clash-verge/v2.0.0"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def load_text(paths: list[str], urls: list[str]) -> str:
    chunks: list[str] = []
    for url in urls:
        try:
            chunks.append(read_url(url))
        except Exception as exc:
            raise SystemExit(f"订阅下载失败: {url}: {exc}") from exc
    if paths:
        for path in paths:
            if path == "-":
                chunks.append(sys.stdin.read())
            else:
                with open(path, encoding="utf-8") as handle:
                    chunks.append(handle.read())
    elif not urls:
        chunks.append(sys.stdin.read())
    return "\n".join(chunks)


def convert(text: str, providers: list[dict] | None = None) -> tuple[str, int, list[str]]:
    stripped = text.lstrip("\ufeff").strip()
    if re.search(r"(?m)^proxies:\s*$", stripped) and "://" not in unwrap_subscription(stripped):
        return stripped if stripped.endswith("\n") else stripped + "\n", -1, []
    nodes, skipped = parse_subscription(text)
    if not nodes:
        return "", 0, skipped
    return render_profile(nodes, providers), len(nodes), skipped


def fetch_ruleset(filename: str, read_fn) -> str:
    errors: list[str] = []
    for template in CN_RULE_MIRRORS:
        url = template.format(name=filename)
        try:
            body = read_fn(url)
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            continue
        if "payload:" not in body:
            errors.append(f"{url}: 不是规则库")
            continue
        return body if body.endswith("\n") else body + "\n"
    raise RuntimeError("；".join(errors))


def download_cn_bundles(read_fn, print_fn) -> list[tuple[str, str, str]]:
    bundles: list[tuple[str, str, str]] = []
    for name, behavior, filename in CN_RULE_FILES:
        print_fn(f"正在获取最新规则 {filename} ...")
        bundles.append((name, behavior, fetch_ruleset(filename, read_fn)))
    return bundles


def payload_items(body: str) -> list[str]:
    items: list[str] = []
    started = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped == "payload:":
            started = True
            continue
        if not started or not stripped.startswith("- "):
            continue
        value = stripped[2:].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if value:
            items.append(value)
    if not items:
        raise RuntimeError("规则库是空的")
    return items


def inline_cn_providers(bundles: list[tuple[str, str, str]]) -> list[dict]:
    return [
        {"name": name, "behavior": behavior, "payload": payload_items(body)}
        for name, behavior, body in bundles
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把订阅或分享串转成本机的 Clash / Mihomo 粮单。转换不经过第三方网站。",
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        help="订阅文件。一行一条分享串，或订阅接口返回的 Base64。不给文件且不是终端时，从标准输入读。",
    )
    parser.add_argument("-o", "--output", help="写出的 yaml。一步式不写这个参数时打到标准输出。")
    parser.add_argument(
        "--url",
        action="append",
        default=[],
        help="先下载这份订阅再在本机转换。可以重复。只访问给出的地址。",
    )
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="一条一条询问分享串，再询问要生成的文件名。不带参数、并且标准输入是终端时也会进入这个模式。",
    )
    parser.add_argument(
        "--cn",
        action="store_true",
        help="转换时在线获取最新的中国大陆域名和 IP 库，写进同一个粮单。这些地址直连，其余走代理。",
    )
    return parser


def node_summary(node: dict) -> str:
    return f"{node['name']}（{node['type']} {node['server']}:{node['port']}）"


def output_name(raw: str, default: str) -> str:
    name = raw.strip() or default
    if not name.lower().endswith((".yaml", ".yml")):
        name += ".yaml"
    return name


def confirm_overwrite(path: str, input_fn) -> bool:
    if not Path(path).exists():
        return True
    answer = input_fn(f"{path} 已存在，覆盖吗？[y/N] ")
    return answer.strip().lower() in {"y", "yes"}


def wants_cn_rules(answer: str) -> bool:
    return answer.strip().lower() in {"y", "yes"}


def interactive_session(input_fn, print_fn, default_output: str = "clash.yaml", read_fn=read_url) -> int:
    print_fn("一条一条贴分享串。直接回车结束，再写要生成的文件名。")
    nodes: list[dict] = []
    used: set[str] = set()
    skipped: list[str] = []
    while True:
        try:
            line = input_fn(f"第 {len(nodes) + 1} 条分享串（回车结束）：")
        except EOFError:
            print_fn("")
            break
        if not line.strip():
            break
        parsed, reasons = parse_subscription(line, used)
        if parsed:
            nodes.extend(parsed)
            for node in parsed:
                print_fn(f"已收下：{node_summary(node)}")
        for reason in reasons:
            skipped.append(reason)
            print_fn(f"这条没收下：{reason}")
        if not parsed and not reasons:
            print_fn("这条没收下：没有找到分享串")
    if not nodes:
        print_fn("没有收下任何分享串。")
        return 1
    while True:
        try:
            chosen = input_fn(f"生成的文件名 [{default_output}]：")
        except EOFError:
            print_fn("")
            print_fn("没有文件名，未生成。")
            return 1
        path = output_name(chosen, default_output)
        if confirm_overwrite(path, input_fn):
            break
    try:
        use_cn = wants_cn_rules(
            input_fn("在线获取最新的中国大陆域名和 IP 库，这些地址直连，其余走代理？[y/N] ")
        )
    except EOFError:
        print_fn("")
        print_fn("没有选择规则，未生成。")
        return 1
    providers = None
    if use_cn:
        try:
            bundles = download_cn_bundles(read_fn, print_fn)
        except Exception as exc:
            print_fn(f"大陆规则下载失败：{exc}")
            return 1
        providers = inline_cn_providers(bundles)
    Path(path).write_text(render_profile(nodes, providers), encoding="utf-8")
    print_fn(f"已生成 {path}（{len(nodes)} converted, {len(skipped)} skipped）")
    return 0


def write_result(rendered: str, count: int, skipped: list[str], output: str | None) -> int:
    if count == 0:
        for reason in skipped:
            print(f"skip: {reason}", file=sys.stderr)
        print("0 converted, 0 skipped" if not skipped else f"0 converted, {len(skipped)} skipped", file=sys.stderr)
        return 1
    if count < 0:
        print("输入已经是 Clash 配置，按原样写出。", file=sys.stderr)
    else:
        print(f"{count} converted, {len(skipped)} skipped", file=sys.stderr)
        for reason in skipped:
            print(f"skip: {reason}", file=sys.stderr)
    if output:
        with open(output, "w", encoding="utf-8") as handle:
            handle.write(rendered)
    else:
        sys.stdout.write(rendered)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    interactive = args.interactive or (not args.inputs and not args.url and sys.stdin.isatty())
    if interactive and (args.inputs or args.url):
        print("交互式不要同时带文件或 --url。", file=sys.stderr)
        return 2
    if interactive:
        try:
            return interactive_session(input, print, args.output or "clash.yaml")
        except KeyboardInterrupt:
            print("", file=sys.stderr)
            return 130
    text = load_text(args.inputs, args.url)
    if not args.cn:
        rendered, count, skipped = convert(text)
        return write_result(rendered, count, skipped, args.output)
    preview, count, skipped = convert(text)
    if count <= 0:
        if count < 0:
            print("输入已经是 Clash 配置，大陆规则没有写进去。", file=sys.stderr)
        return write_result(preview, count, skipped, args.output)
    try:
        bundles = download_cn_bundles(read_url, lambda message: print(message, file=sys.stderr))
        providers = inline_cn_providers(bundles)
    except Exception as exc:
        print(f"大陆规则下载失败: {exc}", file=sys.stderr)
        return 1
    rendered, count, skipped = convert(text, providers)
    return write_result(rendered, count, skipped, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
