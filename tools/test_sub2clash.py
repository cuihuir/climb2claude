#!/usr/bin/env python3
import base64
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import sub2clash


VLESS = (
    "vless://11111111-1111-1111-1111-111111111111@1.2.3.4:12170"
    "?encryption=none&security=reality&flow=xtls-rprx-vision&type=tcp"
    "&sni=www.microsoft.com&pbk=_vd7ABC&fp=chrome#reality-node"
)
HY2 = "hysteria2://p%40ss@1.2.3.4:50903?alpn=h3&insecure=1#hy2-node"
TROJAN = "trojan://secret@example.com:443?sni=example.com&type=ws&path=%2Fdoor#trojan-node"
SS = "ss://" + base64.b64encode(b"2022-blake3-aes-128-gcm:cGFzcw==").decode() + "@9.9.9.9:8388#ss-node"
VMESS_INFO = (
    '{"v":"2","ps":"vmess-node","add":"8.8.8.8","port":"443","id":"22222222-2222-2222-2222-222222222222",'
    '"aid":"0","scy":"auto","net":"ws","type":"none","host":"cdn.example","path":"/ws","tls":"tls","sni":"cdn.example"}'
)
VMESS = "vmess://" + base64.b64encode(VMESS_INFO.encode()).decode()


class ConvertTests(unittest.TestCase):
    def test_reality_and_hysteria2_profile(self):
        rendered, count, skipped = sub2clash.convert(VLESS + "\n" + HY2 + "\n")
        self.assertEqual(count, 2)
        self.assertEqual(skipped, [])
        self.assertIn("mode: rule\n", rendered)
        self.assertIn("ipv6: false\n", rendered)
        self.assertIn("    port: 12170\n", rendered)
        self.assertIn('public-key: "_vd7ABC"\n', rendered)
        self.assertIn("flow: xtls-rprx-vision\n", rendered)
        self.assertIn("servername: www.microsoft.com\n", rendered)
        self.assertIn("    port: 50903\n", rendered)
        self.assertIn('password: "p@ss"\n', rendered)
        self.assertIn("skip-cert-verify: true\n", rendered)
        self.assertIn('  - "MATCH,Proxy"\n', rendered)
        self.assertIn("      - reality-node\n", rendered)
        self.assertIn("      - DIRECT\n", rendered)
        self.assertNotIn("short-id:", rendered)

    def test_base64_subscription_and_banner(self):
        body = base64.b64encode(f"{VLESS}\n{VMESS}\n".encode()).decode()
        wrapped = "\n".join(body[index : index + 64] for index in range(0, len(body), 64))
        pasted = "------------- banner -------------\n" + VLESS + "\n关注 https://example.test\n"
        rendered, count, skipped = sub2clash.convert(wrapped)
        self.assertEqual((count, skipped), (2, []))
        self.assertIn("type: vmess\n", rendered)
        self.assertIn("network: ws\n", rendered)
        self.assertIn('path: "/ws"\n', rendered)
        banner_rendered, banner_count, banner_skipped = sub2clash.convert(pasted)
        self.assertEqual(banner_count, 1)
        self.assertEqual(banner_skipped, [])
        self.assertIn("name: reality-node\n", banner_rendered)

    def test_trojan_ss_and_skip(self):
        text = "\n".join([TROJAN, SS, "ssr://not-supported", "plain text"])
        rendered, count, skipped = sub2clash.convert(text)
        self.assertEqual(count, 2)
        self.assertEqual(skipped, ["不支持的协议 ssr"])
        self.assertIn("type: trojan\n", rendered)
        self.assertIn("network: ws\n", rendered)
        self.assertIn('path: "/door"\n', rendered)
        self.assertIn('cipher: "2022-blake3-aes-128-gcm"\n', rendered)
        self.assertIn("    port: 8388\n", rendered)

    def test_duplicate_names_and_empty(self):
        rendered, count, skipped = sub2clash.convert(VLESS + "\n" + VLESS + "\n")
        self.assertEqual(count, 2)
        self.assertEqual(skipped, [])
        self.assertIn("name: reality-node\n", rendered)
        self.assertIn("name: reality-node-2\n", rendered)
        empty, empty_count, empty_skipped = sub2clash.convert("   \n")
        self.assertEqual(empty, "")
        self.assertEqual(empty_count, 0)
        self.assertEqual(empty_skipped, [])

    def test_interactive_prompts_and_writes(self):
        answers = iter([VLESS, "不是分享串", HY2, "", "out", "n"])
        prompts: list[str] = []
        messages: list[str] = []

        def fake_input(prompt: str) -> str:
            prompts.append(prompt)
            return next(answers)

        with tempfile.TemporaryDirectory() as directory:
            previous = pathlib.Path.cwd()
            os_chdir = pathlib.Path(directory)
            try:
                import os

                os.chdir(os_chdir)
                code = sub2clash.interactive_session(fake_input, messages.append)
            finally:
                os.chdir(previous)
            written = pathlib.Path(directory, "out.yaml").read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertEqual(prompts[0], "第 1 条分享串（回车结束）：")
        self.assertEqual(prompts[1], "第 2 条分享串（回车结束）：")
        self.assertEqual(prompts[2], "第 2 条分享串（回车结束）：")
        self.assertEqual(prompts[3], "第 3 条分享串（回车结束）：")
        self.assertEqual(prompts[4], "生成的文件名 [clash.yaml]：")
        self.assertIn("中国大陆域名和 IP 库", prompts[5])
        self.assertTrue(any(line.startswith("已收下：reality-node") for line in messages))
        self.assertTrue(any("这条没收下" in line for line in messages))
        self.assertTrue(any(line.startswith("已生成 out.yaml") for line in messages))
        self.assertIn("name: hy2-node\n", written)
        self.assertIn('  - "MATCH,Proxy"\n', written)

    def test_cn_rules_download_and_render(self):
        bodies = {
            "direct.txt": "payload:\n  - '+.cn'\n",
            "cncidr.txt": "payload:\n  - '1.0.1.0/24'\n",
        }
        seen: list[str] = []

        def fake_read(url: str) -> str:
            seen.append(url)
            if "cdn.jsdelivr.net" in url and url.endswith("direct.txt"):
                raise OSError("mirror down")
            for name, body in bodies.items():
                if url.endswith(name) and "fastly.jsdelivr.net" in url:
                    return body
            raise OSError("not this mirror")

        messages: list[str] = []
        bundles = sub2clash.download_cn_bundles(fake_read, messages.append)
        self.assertEqual([item[0] for item in bundles], ["cn-domain", "cn-ip"])
        self.assertTrue(any("direct.txt" in url and "cdn.jsdelivr.net" in url for url in seen))
        self.assertTrue(any(url.startswith("https://fastly.jsdelivr.net/") for url in seen))
        providers = sub2clash.inline_cn_providers(bundles)
        rendered = sub2clash.render_profile(
            [{"name": "reality-node", "type": "vless", "server": "1.2.3.4", "port": 1}],
            providers,
        )
        self.assertIn("type: inline\n", rendered)
        self.assertIn('"+.cn"\n', rendered)
        self.assertIn('"1.0.1.0/24"\n', rendered)
        self.assertIn("RULE-SET,cn-domain,DIRECT", rendered)
        self.assertIn("RULE-SET,cn-ip,DIRECT,no-resolve", rendered)
        self.assertIn("IP-CIDR,192.168.0.0/16,DIRECT,no-resolve", rendered)
        self.assertNotIn("cn-domain.yaml", rendered)
        self.assertIn("MATCH,Proxy", rendered.split("rules:", 1)[1])
        self.assertTrue(any("正在获取最新规则 direct.txt" in line for line in messages))

    def test_interactive_refuses_empty(self):
        answers = iter(["",])

        def fake_input(prompt: str) -> str:
            return next(answers)

        messages: list[str] = []
        code = sub2clash.interactive_session(fake_input, messages.append)
        self.assertEqual(code, 1)
        self.assertIn("没有收下任何分享串。", messages)

    def test_existing_clash_passthrough(self):
        original = "mode: rule\nproxies:\n  - name: keep\n    type: ss\n    port: 1\n"
        rendered, count, skipped = sub2clash.convert(original)
        self.assertEqual(count, -1)
        self.assertEqual(skipped, [])
        self.assertEqual(rendered, original if original.endswith("\n") else original + "\n")


if __name__ == "__main__":
    unittest.main()
