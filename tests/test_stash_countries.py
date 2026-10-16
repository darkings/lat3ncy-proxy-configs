"""Offline checks for private, on-demand Stash country group generation."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("generate_stash_countries", ROOT / "scripts/generate_stash_countries.py")
generator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generator)
TEMPLATE = yaml.safe_load((ROOT / "stash-ios.yaml").read_text(encoding="utf-8"))


class CountryGroupsTests(unittest.TestCase):
    def test_mixed_regions_and_unclassified(self):
        nodes = ["🇩🇪 DE-1", "JP Tokyo A", "🇺🇸 US-2", "🌐 未知线路", "🇭🇰 HK (02)"]
        result, unclassified = generator.build_profile(TEMPLATE, nodes)
        groups = {g["name"]: g for g in result["proxy-groups"]}
        self.assertEqual(unclassified, 1)
        self.assertEqual({"Hong Kong", "Japan", "United States", "Country DE - Germany"} & groups.keys(),
                         {"Hong Kong", "Japan", "United States", "Country DE - Germany"})
        self.assertFalse({"Taiwan", "Singapore", "Country KR - South Korea"} & groups.keys())
        for group, node in [("Country DE - Germany", nodes[0]), ("Japan", nodes[1]),
                            ("United States", nodes[2]), ("Hong Kong", nodes[4])]:
            self.assertEqual(groups[group]["use"], ["SubStore"])
            self.assertTrue(re.fullmatch(groups[group]["filter"], node))
            self.assertFalse(re.fullmatch(groups[group]["filter"], nodes[3]))
        for name in generator.SELECT_GROUPS:
            choices = groups[name]["proxies"]
            self.assertNotIn("Taiwan", choices)
            self.assertIn("Country DE - Germany", choices)
            self.assertIn("Japan", choices)
        self.assertEqual(result["rules"], TEMPLATE["rules"])
        self.assertEqual(result["dns"], TEMPLATE["dns"])
        self.assertEqual(result["proxy-providers"], TEMPLATE["proxy-providers"])
        self.assertEqual(result["proxies"], TEMPLATE["proxies"])
        self.assertEqual(TEMPLATE["proxy-groups"][0]["proxies"][2], "Hong Kong")

    def test_empty_country_list_does_not_create_direct_groups(self):
        result, count = generator.build_profile(TEMPLATE, ["Unmarked node"])
        self.assertEqual(count, 1)
        self.assertEqual({g["name"] for g in result["proxy-groups"]} & set(generator.OLD_REGIONS), set())
        self.assertEqual(next(g for g in result["proxy-groups"] if g["name"] == "Proxy")["proxies"],
                         ["Auto", "DIRECT"])

    def test_regex_filters_match_only_exact_node_and_ignore_status_nodes(self):
        result, count = generator.build_profile(TEMPLATE, ["🇰🇷 KR [1]+", "🇰🇷 剩余流量 3G", "🇩🇪 DE 01"])
        groups = {g["name"]: g for g in result["proxy-groups"]}
        korean = groups["Country KR - South Korea"]["filter"]
        self.assertEqual(count, 0)
        self.assertTrue(re.fullmatch(korean, "🇰🇷 KR [1]+"))
        self.assertFalse(re.fullmatch(korean, "🇰🇷 KR 111"))
        self.assertFalse(re.fullmatch(korean, "🇰🇷 剩余流量 3G"))
        self.assertFalse(re.fullmatch(korean, "🇩🇪 DE 01"))

    def test_regeneration_removes_absent_country_and_adds_new_one(self):
        first, _ = generator.build_profile(TEMPLATE, ["🇭🇰 HK 01", "🇩🇪 DE 01"])
        second, _ = generator.build_profile(TEMPLATE, ["🇩🇪 DE 01", "🇫🇷 FR 01"])
        first_names = {g["name"] for g in first["proxy-groups"]}
        second_names = {g["name"] for g in second["proxy-groups"]}
        self.assertIn("Hong Kong", first_names)
        self.assertNotIn("Hong Kong", second_names)
        self.assertIn("Country FR - France", second_names)
        for group in second["proxy-groups"]:
            if group["name"] in generator.SELECT_GROUPS:
                self.assertNotIn("Hong Kong", group["proxies"])
                self.assertIn("Country FR - France", group["proxies"])

    def test_ambiguous_country_is_not_guessed(self):
        self.assertIsNone(generator.country_of("🇯🇵 🇺🇸 dual exit"))
        self.assertIsNone(generator.country_of("JP to US relay"))
        self.assertIsNone(generator.country_of("Supernode"))
        self.assertEqual(generator.country_of("🇵🇱 Warsaw"), "PL")

    def test_no_secrets_copied_from_export_and_collection_mode(self):
        secret = "VERY_PRIVATE_PASSWORD_123"
        private_export = yaml.safe_dump({"proxies": [
            {"name": "🇩🇪 Berlin", "type": "trojan", "password": secret, "server": "private.example"}
        ]})
        with patch.object(Path, "read_text", return_value=private_export):
            result, _ = generator.build_profile(TEMPLATE, generator.load_names(Path("private.yaml")), collection=True)
        text = yaml.safe_dump(result, allow_unicode=True)
        self.assertNotIn(secret, text)
        self.assertNotIn("private.example", text)
        self.assertEqual(result["proxies"], TEMPLATE["proxies"])
        self.assertEqual(result["subscribe-url"], "http://sub.store/download/collection/ios?target=Stash")
        self.assertEqual(result["proxy-providers"]["SubStore"]["url"], result["subscribe-url"])
        self.assertEqual(result["proxy-providers"]["SubStore"]["path"],
                         TEMPLATE["proxy-providers"]["SubStore"]["path"])

    def test_invalid_exports_and_name_collisions_fail_closed(self):
        for invalid in ({"proxies": []}, {"proxies": [{"type": "ss"}]},
                        {"proxies": [{"name": "🇩🇪 1\nsecret"}]},
                        {"proxies": [{"name": "🇩🇪 1"}, {"name": "🇩🇪 1"}]}):
            with patch.object(Path, "read_text", return_value=yaml.safe_dump(invalid)):
                with self.assertRaises(ValueError):
                    generator.load_names(Path("private.yaml"))
        with self.assertRaises(ValueError):
            generator.build_profile(TEMPLATE, ["Auto"])
        with self.assertRaises(ValueError):
            generator.build_profile(TEMPLATE, ["剩余流量 3G"])


if __name__ == "__main__":
    unittest.main()
