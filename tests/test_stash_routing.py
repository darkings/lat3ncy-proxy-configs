"""Check local domain routing with unavailable remote rule sets.

This is a policy regression check, not a replacement for the Stash runtime.
Only the domain and domain-scoped UDP rules used by this configuration are
evaluated; DNS, GeoIP and IP routing remain runtime checks.
"""

from pathlib import Path
import re
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
CONFIG = yaml.safe_load((ROOT / "stash-ios.yaml").read_text(encoding="utf-8"))


def domain_matches(kind, value, host):
    if kind == "DOMAIN":
        return host == value
    return host == value or host.endswith("." + value)


def local_route(host, network="TCP", port=443, ad_domains=()):
    for rule in CONFIG["rules"]:
        parts = rule.split(",")
        kind = parts[0]
        if kind in {"DOMAIN", "DOMAIN-SUFFIX"}:
            if domain_matches(kind, parts[1], host):
                return parts[2]
        elif kind == "AND":
            match = re.fullmatch(
                r"AND,\(\((DOMAIN(?:-SUFFIX)?),([^,]+)\),"
                r"\(NETWORK,UDP\),\(DST-PORT,443\)\),REJECT", rule
            )
            if match is None:
                raise AssertionError(f"Unexpected logical rule: {rule}")
            if network == "UDP" and port == 443 and domain_matches(
                match[1], match[2], host
            ):
                return "REJECT"
        elif kind == "RULE-SET":
            # All remote resources are missing except an optional ad fixture.
            if parts[1] == "Cats-Team-AdRules" and any(
                domain_matches("DOMAIN-SUFFIX", domain, host)
                for domain in ad_domains
            ):
                return parts[2]
        elif kind == "MATCH":
            return parts[1]
        elif kind not in {"IP-CIDR", "IP-CIDR6", "GEOIP"}:
            raise AssertionError(f"Unexpected rule type: {rule}")
    raise AssertionError("Missing final policy")


class StashRoutingTests(unittest.TestCase):
    def test_core_services_without_any_remote_resources(self):
        hosts = (
            "minorshort.weixin.qq.com", "long.weixin.qq.com",
            "dns.weixin.qq.com.cn", "aedns.weixin.qq.com", "wx.qpic.cn",
            "www.douyin.com", "aweme.snssdk.com", "api.zijieapi.com",
            "video.douyinvod.com", "p228-fmip.icloud.com.cn",
            "gateway.icloud.com", "api.apple-cloudkit.com", "www.baidu.com",
            "unlisted-domestic.example.cn",
        )
        for host in hosts:
            for network in ("TCP", "UDP"):
                with self.subTest(host=host, network=network):
                    self.assertEqual(local_route(host, network), "DIRECT")

    def test_resource_downloads_and_foreign_fallback(self):
        self.assertEqual(local_route("raw.githubusercontent.com"), "GitHub")
        self.assertEqual(local_route("release-assets.githubusercontent.com"), "GitHub")
        self.assertEqual(local_route("github.com"), "GitHub")
        self.assertEqual(local_route("www.youtube.com"), "Proxy")
        self.assertEqual(local_route("www.tiktok.com"), "Proxy")
        self.assertEqual(local_route("unknown.example.org"), "Proxy")
        self.assertEqual(local_route("host.example.ts.net"), "Tailscale")

    def test_ads_remain_blocked_but_wechat_dns_is_allowed(self):
        ads = ("ad.weixin.qq.com", "dns.weixin.qq.com", "aedns.weixin.qq.com")
        self.assertEqual(local_route("ad.weixin.qq.com", ad_domains=ads), "REJECT")
        self.assertEqual(local_route("dns.weixin.qq.com", ad_domains=ads), "DIRECT")
        self.assertEqual(local_route("aedns.weixin.qq.com", ad_domains=ads), "DIRECT")
        self.assertEqual(local_route("adbs.ximalaya.com", "UDP"), "REJECT")

    def test_collection_override_preserves_provider_settings(self):
        override = yaml.safe_load((ROOT / "stash/overrides/substore-ios-collection.stoverride").read_text(encoding="utf-8"))
        url = "http://sub.store/download/collection/ios?target=Stash"
        self.assertEqual(override["subscribe-url"], url)
        provider = override["proxy-providers"]["SubStore"]
        self.assertEqual(provider, {"url": url})
        merged = CONFIG["proxy-providers"]["SubStore"] | provider
        self.assertEqual(merged["path"], "./providers/substore-ios.yaml")
        self.assertEqual(merged["health-check"], CONFIG["proxy-providers"]["SubStore"]["health-check"])


if __name__ == "__main__":
    unittest.main()
