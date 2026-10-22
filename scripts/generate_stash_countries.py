#!/usr/bin/env python3
"""Build a local Stash profile with only nonempty subscription country groups.

The input is a *local* Sub-Store Stash YAML export. No node credentials are
copied into the result: the profile still obtains proxies via SubStore. Node
names are used in exact-match group filters, so the output remains private.
"""

from __future__ import annotations

import argparse
import copy
import re
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "stash-ios.yaml"
OUTPUT = ROOT / ".local" / "stash-ios-countries.yaml"
OLD_REGIONS = ("Hong Kong", "Taiwan", "Japan", "Singapore", "United States")
SELECT_GROUPS = ("Proxy", "Spotify", "Telegram", "OpenAI", "GitHub", "Microsoft", "Google", "YouTube", "TikTok")
# Subscription-status placeholders ("剩余流量 3G", "套餐到期 …") must never be
# treated as usable nodes. The template no longer carries this filter (Proxy now
# lists only groups), so the generator keeps the single source of truth here.
STATUS_NODE_FILTER = (
    r"(?i)^(?!Tailscale(?:-Node)?$)(?!.*(?:群|邀请|返利|官网|客服|订阅|剩余|流量|套餐|到期|过期"
    r"|有效期|已用|重置|机场|通知|频道|教程|更新|作者|\b(?:USE|USED|TOTAL|EXPIRE|EXPIRED|TRAFFIC"
    r"|REMAINING|RESET|BANDWIDTH)\d*\b|\d{4}-\d{2}-\d{2}|\dG)).+$"
)

# A country flag always works (including countries not in this table). These
# aliases also cover common subscriptions without flags; unknown nodes remain
# in Auto and Proxy rather than being assigned to a guessed country.
COUNTRIES = {
    "HK": ("Hong Kong", ("香港", "Hong Kong", "Hong-Kong", "HK", "HKG")),
    "TW": ("Taiwan", ("台湾", "台灣", "臺灣", "Taiwan", "Taipei", "台北", "TW", "TWN")),
    "JP": ("Japan", ("日本", "东京", "東京", "大阪", "Japan", "Tokyo", "Osaka", "JP", "JPN")),
    "SG": ("Singapore", ("新加坡", "狮城", "獅城", "Singapore", "SG", "SGP")),
    "US": ("United States", ("美国", "美國", "United States", "USA", "US", "Los Angeles", "Seattle")),
    "KR": ("South Korea", ("韩国", "韓國", "首尔", "首爾", "Korea", "Seoul", "KR", "KOR")),
    "DE": ("Germany", ("德国", "德國", "Germany", "Frankfurt", "DE", "DEU")),
    "GB": ("United Kingdom", ("英国", "英國", "United Kingdom", "Britain", "London", "UK", "GB", "GBR")),
    "FR": ("France", ("法国", "法國", "France", "Paris", "FR", "FRA")),
    "CA": ("Canada", ("加拿大", "Canada", "Toronto", "CA", "CAN")),
    "AU": ("Australia", ("澳大利亚", "澳大利亞", "澳洲", "Australia", "Sydney", "AU", "AUS")),
    "NL": ("Netherlands", ("荷兰", "荷蘭", "Netherlands", "Amsterdam", "NL", "NLD")),
    "RU": ("Russia", ("俄罗斯", "俄羅斯", "Russia", "Moscow", "RU", "RUS")),
    "IN": ("India", ("印度", "India", "Mumbai", "IN", "IND")),
    "TH": ("Thailand", ("泰国", "泰國", "Thailand", "Bangkok", "TH", "THA")),
    "VN": ("Vietnam", ("越南", "Vietnam", "Hanoi", "VN", "VNM")),
    "MY": ("Malaysia", ("马来西亚", "馬來西亞", "Malaysia", "Kuala Lumpur", "MY", "MYS")),
    "ID": ("Indonesia", ("印尼", "印度尼西亚", "印度尼西亞", "Indonesia", "Jakarta", "ID", "IDN")),
    "PH": ("Philippines", ("菲律宾", "菲律賓", "Philippines", "Manila", "PH", "PHL")),
    "TR": ("Turkey", ("土耳其", "Turkey", "Istanbul", "TR", "TUR")),
    "CH": ("Switzerland", ("瑞士", "Switzerland", "Zurich", "CH", "CHE")),
    "SE": ("Sweden", ("瑞典", "Sweden", "Stockholm", "SE", "SWE")),
    "NO": ("Norway", ("挪威", "Norway", "Oslo", "NO", "NOR")),
    "FI": ("Finland", ("芬兰", "芬蘭", "Finland", "Helsinki", "FI", "FIN")),
    "IT": ("Italy", ("意大利", "Italy", "Milan", "IT", "ITA")),
    "ES": ("Spain", ("西班牙", "Spain", "Madrid", "ES", "ESP")),
    "BR": ("Brazil", ("巴西", "Brazil", "Sao Paulo", "BR", "BRA")),
    "AE": ("United Arab Emirates", ("阿联酋", "阿聯酋", "UAE", "Dubai", "AE", "ARE")),
    "NZ": ("New Zealand", ("新西兰", "紐西蘭", "New Zealand", "Auckland", "NZ", "NZL")),
}
FLAGS = re.compile(r"[\U0001f1e6-\U0001f1ff]{2}")
REGEX_META = re.compile(r"([\\.^$|?*+()\[\]{}])")


def escape_filter(name: str) -> str:
    # RE2 need not understand Python-specific escapes such as backslash-space.
    return REGEX_META.sub(lambda match: "\\" + match.group(1), name)


def country_of(name: str) -> str | None:
    flags = {"".join(chr(ord(letter) - 0x1F1E6 + ord("A")) for letter in flag)
             for flag in FLAGS.findall(name)}
    if len(flags) > 1:
        return None
    if flags:
        return next(iter(flags))
    matches = set()
    for code, (_, aliases) in COUNTRIES.items():
        for alias in aliases:
            pattern = re.escape(alias)
            if alias[0].isascii() and alias[0].isalpha():
                pattern = r"(?<![A-Za-z])" + pattern + r"(?![A-Za-z])"
            if re.search(pattern, name, re.IGNORECASE):
                matches.add(code)
                break
    return next(iter(matches)) if len(matches) == 1 else None


def load_names(source: Path) -> list[str]:
    data = yaml.safe_load(source.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        nodes = data.get("proxies")
    else:
        nodes = data
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("Input must contain a nonempty 'proxies' list (Stash YAML export)")
    names = []
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("name"), str):
            raise ValueError("Every proxy must be an object with a string name")
        name = node["name"]
        if not name.strip() or any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise ValueError("Proxy names must be nonempty and contain no control characters")
        names.append(name)
    if len(names) != len(set(names)):
        raise ValueError("Duplicate proxy names: rename/deduplicate them in Sub-Store first")
    return names


def build_profile(template: dict, names: list[str], collection: bool = False) -> tuple[dict, int]:
    if not names or len(names) != len(set(names)):
        raise ValueError("A nonempty set of unique proxy names is required")
    if any(not isinstance(n, str) or not n.strip() or any(ord(c) < 32 or ord(c) == 127 for c in n) for n in names):
        raise ValueError("Proxy names must be nonempty strings without control characters")
    profile = copy.deepcopy(template)
    groups = profile["proxy-groups"]
    original = {group["name"]: group for group in groups}
    if not all(group in original for group in (*SELECT_GROUPS, "Auto", *OLD_REGIONS)):
        raise ValueError("Template group structure has changed; update the generator first")
    if any(original[name].get("use") != ["SubStore"] for name in OLD_REGIONS):
        raise ValueError("Template country groups no longer use SubStore")
    grouped: dict[str, list[str]] = defaultdict(list)
    # Country groups must not re-introduce subscription-status placeholders, so
    # filter the export with the same rule the template used to carry on Proxy.
    eligible = [n for n in names if re.fullmatch(STATUS_NODE_FILTER, n)]
    if not eligible:
        raise ValueError("All exported nodes look like subscription-status placeholders; refusing to emit Direct-only groups")
    for name in eligible:
        code = country_of(name)
        if code:
            grouped[code].append(name)
    country_groups = []
    for code in sorted(grouped):
        label = COUNTRIES.get(code, (f"Country {code}", ()))[0]
        # Preserve the original five names so their UI selections survive.
        display = label if label in OLD_REGIONS else f"Country {code} - {label}"
        country_groups.append({
            "name": display,
            "type": "url-test",
            "use": ["SubStore"],
            # Exact names avoid accidentally including other countries after a
            # subscription renames or adds nodes. Refresh the whole profile
            # when nodes change; refreshing the provider alone cannot add groups.
            "filter": "^(?:" + "|".join(escape_filter(n) for n in grouped[code]) + ")$",
            "interval": 1800,
            "lazy": True,
        })
    new_names = [group["name"] for group in country_groups]
    if len(set(new_names)) != len(new_names) or set(names) & (set(original) | set(new_names)):
        raise ValueError("A proxy name conflicts with a strategy group name; rename it in Sub-Store")
    for group_name in SELECT_GROUPS:
        group = original[group_name]
        proxy_list = group["proxies"]
        if not all(old in proxy_list for old in OLD_REGIONS):
            raise ValueError(f"Template selection group {group_name} has changed")
        group["proxies"] = [p for p in proxy_list if p not in OLD_REGIONS] + new_names
    # Auto aggregates country groups, never individual subscription nodes.
    auto = original["Auto"]
    if "use" in auto or not set(OLD_REGIONS) <= set(auto.get("proxies") or []):
        raise ValueError("Template Auto group no longer aggregates country groups")
    auto["proxies"] = new_names
    profile["proxy-groups"] = [g for g in groups if g["name"] not in OLD_REGIONS] + country_groups
    if collection:
        url = "http://sub.store/download/collection/ios?target=Stash"
        profile["subscribe-url"] = url
        profile["proxy-providers"]["SubStore"]["url"] = url
    return profile, len(eligible) - sum(map(len, grouped.values()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", required=True, type=Path, help="Private local Stash YAML export of ios")
    parser.add_argument("--collection", action="store_true", help="Use the local ios collection for the provider")
    args = parser.parse_args()
    if args.nodes.resolve() in {TEMPLATE.resolve(), OUTPUT.resolve()}:
        parser.error("Input must not be the template or generated output")
    template = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    try:
        profile, unclassified = build_profile(template, load_names(args.nodes), args.collection)
    except (OSError, yaml.YAMLError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    # Only this ignored local path is written. Do not publish the input or this
    # generated profile: exact-match filters can contain private node labels.
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(yaml.safe_dump(profile, allow_unicode=True, sort_keys=False, width=1000), encoding="utf-8")
    dynamic = [g for g in profile["proxy-groups"] if g["name"].startswith("Country ") or g["name"] in OLD_REGIONS]
    print(f"Saved {OUTPUT} ({len(dynamic)} nonempty country groups; {unclassified} unclassified proxies in Auto/Proxy)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
