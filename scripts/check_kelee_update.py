#!/usr/bin/env python3
"""
Daily check for selected KeLee plugins.
Fetches each .lpx with Loon UA, compares SHA256 / date with stored hashes,
re-converts to .stoverride if changed, updates .hashes.json and regenerates index.html.
Generated overrides and mirrored scripts are deployment artifacts; they are intentionally
kept in the ignored output directory and are not committed to the public repository.
"""
from __future__ import annotations
import argparse, json, hashlib, re, time, sys
from pathlib import Path
import urllib.request, urllib.error

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
LOON_UA = "Loon/764 CFNetwork/1498.700.1 Darwin/23.6.0 iPhone/17.6.1"
FETCH_HEADERS = {"User-Agent": LOON_UA, "Accept": "*/*"}

# Import converter
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from convert_kelee_lpx import convert_lpx_to_stash, dump_stash_yaml, fetch_text

TARGETS_JSON = REPO_ROOT / "stash/overrides/kelee/targets.json"
HASHES_JSON = REPO_ROOT / "stash/overrides/kelee/.hashes.json"
OUT_DIR = REPO_ROOT / "stash/overrides/kelee"
PROVIDER_BUNDLE = OUT_DIR / "kelee-scripts.stoverride"
MAIN_CONFIG = REPO_ROOT / "stash-ios.yaml"
CONVERTER_VERSION = 9

def fetch_lpx(url: str) -> str:
    """Fetch an upstream .lpx, retrying transient TLS/EOF failures.

    kelee.one sits behind Cloudflare; a single dropped TLS session used to abort
    the whole publish run (observed as SSL UNEXPECTED_EOF_WHILE_READING).
    """
    last_error = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=FETCH_HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
        except Exception as exc:  # noqa: BLE001 - retry any transport error
            last_error = exc
            time.sleep(1 + attempt)
    raise last_error

def lpx_hash_and_date(text: str):
    # hash of normalized text
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()
    m = re.search(r"^#!date=(.+)$", text, re.M)
    date = m.group(1).strip() if m else ""
    return h, date


def split_script_providers(converted_text: str):
    """Keep providers in the main config/bundle, not in app overrides."""
    lines = converted_text.splitlines()
    yaml_start = 0
    while yaml_start < len(lines) and (not lines[yaml_start].strip() or lines[yaml_start].lstrip().startswith("#")):
        yaml_start += 1
    header = "\n".join(lines[:yaml_start]).rstrip()
    data = yaml.safe_load("\n".join(lines[yaml_start:]))
    if not isinstance(data, dict):
        raise ValueError("converted override root is not a mapping")
    providers = data.pop("script-providers", {}) or {}
    body = dump_stash_yaml(data)
    return ((header + "\n" if header else "") + body), providers


def read_existing_providers():
    if not PROVIDER_BUNDLE.exists():
        return {}
    try:
        data = yaml.safe_load(PROVIDER_BUNDLE.read_text(encoding="utf-8")) or {}
        return data.get("script-providers") or {}
    except Exception:
        return {}


def order_providers(providers: dict, previous: dict):
    ordered = {name: providers[name] for name in previous if name in providers}
    ordered.update({name: value for name, value in providers.items() if name not in ordered})
    return ordered


def write_provider_bundle(providers: dict):
    data = {
        "name": "kelee-scripts",
        "desc": f"KeLee Scripts Hub ({len(providers)} providers)",
        "icon": "https://raw.githubusercontent.com/KOP-XIAO/QuantumultX/master/Icons/Kelee.png",
        "script-providers": providers,
    }
    PROVIDER_BUNDLE.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=4096, default_flow_style=False),
        encoding="utf-8",
    )


def sync_main_config_providers(providers: dict, old_provider_names: set):
    """Refresh URLs of providers the main config already declares.

    Converted KeLee providers belong to kelee-scripts.stoverride; adding them
    here would duplicate the bundle and could inject unexpanded Loon
    placeholders into the public config.
    """
    if not MAIN_CONFIG.exists():
        return False
    text = MAIN_CONFIG.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    current = data.get("script-providers") or {}
    if not current:
        # Providers live entirely in the kelee-scripts bundle now; nothing to sync.
        return False
    merged = {}
    for name, value in current.items():
        replacement = providers.get(name)
        # Keep main-config-only providers untouched; refresh known names in place.
        merged[name] = replacement if (name in old_provider_names and replacement) else value
    replacement_block = yaml.safe_dump(
        {"script-providers": merged},
        sort_keys=False,
        allow_unicode=True,
        width=4096,
        default_flow_style=False,
    ).rstrip() + "\n\n"
    pattern = re.compile(r"(?ms)^script-providers:\s*\n.*?(?=^\S)")
    if not pattern.search(text):
        return False
    updated = pattern.sub(replacement_block, text, count=1)
    if updated != text:
        MAIN_CONFIG.write_text(updated, encoding="utf-8")
        return True
    return False


SUBSTORE_ASSETS = {
    "sub-store-0.min.js": "https://cdn.jsdelivr.net/gh/sub-store-org/Sub-Store@release/sub-store-0.min.js",
    "sub-store-1.min.js": "https://cdn.jsdelivr.net/gh/sub-store-org/Sub-Store@release/sub-store-1.min.js",
}


def mirror_substore_assets():
    """Publish the Sub-Store scripts on our own host.

    Stash downloads script-providers through its own rules, so pointing them at
    GitHub leaves a bootstrap deadlock: without nodes there is no proxy, and
    without the Sub-Store script there are no nodes. Serving them from the same
    host as the overrides keeps first-run downloads working.
    """
    scripts_dir = OUT_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    mirrored = []
    for name, url in SUBSTORE_ASSETS.items():
        try:
            body = fetch_text(url)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! failed to mirror {name}: {exc}", flush=True)
            continue
        if not body.strip():
            print(f"  ! empty body for {name}", flush=True)
            continue
        (scripts_dir / name).write_text(body, encoding="utf-8")
        mirrored.append(name)
    if mirrored:
        print(f"  mirrored Sub-Store scripts: {', '.join(mirrored)}", flush=True)
    return mirrored


def validate_runtime_assets():
    filename = "9410-b8806e870a26db7d.js"
    asset_path = OUT_DIR / "scripts" / filename
    pdd_script = OUT_DIR / "scripts" / "pinduoduo-remove-ads.js"
    expected_url = f"https://stash.ponyo.fun/scripts/{filename}"
    if not asset_path.is_file() or asset_path.stat().st_size == 0:
        raise RuntimeError(f"missing mirrored runtime asset: {asset_path}")
    if expected_url not in pdd_script.read_text(encoding="utf-8"):
        raise RuntimeError("Pinduoduo script still references the UA-restricted runtime asset")

def main():
    parser = argparse.ArgumentParser(description="Check and rebuild selected KeLee overrides")
    parser.add_argument("--force", action="store_true", help="rebuild all targets even when LPX hashes match")
    args = parser.parse_args()
    targets = json.loads(TARGETS_JSON.read_text(encoding="utf-8"))
    hashes = {}
    if HASHES_JSON.exists():
        try:
            hashes = json.loads(HASHES_JSON.read_text(encoding="utf-8"))
        except:
            hashes = {}
    changed = []
    failed = []
    fetched = {}
    new_hashes = {"_converter_version": CONVERTER_VERSION}
    force_rebuild = args.force or hashes.get("_converter_version") != CONVERTER_VERSION
    for item in targets:
        fname = item["filename"]
        name = item.get("name", fname)
        url = f"https://kelee.one/Tool/Loon/Lpx/{fname}"
        stoverride_path = OUT_DIR / fname.replace(".lpx", ".stoverride")
        print(f"checking {name} {fname} ...", flush=True)
        try:
            text = fetch_lpx(url)
            h, date = lpx_hash_and_date(text)
            prev = hashes.get(fname)
            fetched[fname] = (text, url, h, date)
            new_hashes[fname] = h
            item["_fetched_hash"] = h
            if date:
                item["_fetched_date"] = date
            # compare hash or if file missing
            if prev != h or not stoverride_path.exists():
                print(f"  -> update needed (prev {prev[:8] if prev else 'none'} -> {h[:8]}, date {date})")
                changed.append(fname)
            else:
                print(f"  -> no change ({h[:8]})")
        except Exception as e:
            print(f"  -> failed {url}: {e}", file=sys.stderr)
            failed.append(fname)
        time.sleep(0.4)
    if failed:
        print(f"update check failed for {len(failed)} target(s): {', '.join(failed)}", file=sys.stderr)
        # 所有抓取成功后才开始写文件，避免发布半套更新。
        sys.exit(1)

    if force_rebuild or changed:
        reason = "--force" if args.force else (f"converter v{CONVERTER_VERSION}" if force_rebuild else "upstream update")
        print(f"rebuilding all {len(targets)} targets ({reason})")
        old_providers = read_existing_providers()
        all_providers = {}
        for item in targets:
            fname = item["filename"]
            text, url, _, _ = fetched[fname]
            converted = convert_lpx_to_stash(text, url, fetch_script_fallback=True)
            stoverride_text, providers = split_script_providers(converted)
            overlap = set(all_providers).intersection(providers)
            for provider_name in overlap:
                if all_providers[provider_name] != providers[provider_name]:
                    raise RuntimeError(f"provider conflict: {provider_name}")
            all_providers.update(providers)
            (OUT_DIR / fname.replace(".lpx", ".stoverride")).write_text(stoverride_text, encoding="utf-8")
        all_providers = order_providers(all_providers, old_providers)
        write_provider_bundle(all_providers)
        main_changed = sync_main_config_providers(all_providers, set(old_providers))
        mirror_substore_assets()
        validate_runtime_assets()
        HASHES_JSON.write_text(json.dumps(new_hashes, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"updated {len(changed)} upstream target(s); providers={len(all_providers)}; main-config={main_changed}")
        # regenerate html
        try:
            import generate_kelee_html
            generate_kelee_html.main()
            print("regenerated index.html")
        except Exception as e:
            print(f"failed to regenerate html: {e}", file=sys.stderr)
        # 离线 Stash 校验（先分析差异后编写的 validate_stash.py），失败则阻断发布
        try:
            import validate_stash
            try:
                validate_stash.main()
            except SystemExit as se:
                if se.code != 0:
                    print(f"Stash 校验失败，阻断发布 (exit {se.code})", file=sys.stderr)
                    sys.exit(1)
        except Exception as e:
            print(f"Stash 校验异常: {e}", file=sys.stderr)
            sys.exit(1)
        # also update list.json timestamp?
        # Exit with code 2 to signal workflow to commit
        sys.exit(2)
    else:
        # even if no change, ensure html exists (maybe first run)
        html_path = OUT_DIR / "index.html"
        if not html_path.exists():
            try:
                import generate_kelee_html
                generate_kelee_html.main()
                print("generated missing index.html")
            except Exception as e:
                print(f"html gen failed: {e}")
        print("no updates")
        sys.exit(0)

if __name__ == "__main__":
    main()
