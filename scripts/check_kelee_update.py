#!/usr/bin/env python3
"""
Daily check for KeLee 19 selected plugins.
Fetches each .lpx with Loon UA, compares SHA256 / date with stored hashes,
re-converts to .stoverride if changed, updates .hashes.json and regenerates index.html.
"""
from __future__ import annotations
import json, hashlib, re, time, sys
from pathlib import Path
import urllib.request, urllib.error

REPO_ROOT = Path(__file__).resolve().parents[1]
LOON_UA = "Loon/764 CFNetwork/1498.700.1 Darwin/23.6.0 iPhone/17.6.1"
FETCH_HEADERS = {"User-Agent": LOON_UA, "Accept": "*/*"}

# Import converter
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from convert_kelee_lpx import convert_lpx_to_stash, fetch_text

TARGETS_JSON = REPO_ROOT / "stash/overrides/kelee/targets.json"
HASHES_JSON = REPO_ROOT / "stash/overrides/kelee/.hashes.json"
OUT_DIR = REPO_ROOT / "stash/overrides/kelee"

def fetch_lpx(url: str) -> str:
    req = urllib.request.Request(url, headers=FETCH_HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")

def lpx_hash_and_date(text: str):
    # hash of normalized text
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()
    m = re.search(r"^#!date=(.+)$", text, re.M)
    date = m.group(1).strip() if m else ""
    return h, date

def main():
    targets = json.loads(TARGETS_JSON.read_text(encoding="utf-8"))
    hashes = {}
    if HASHES_JSON.exists():
        try:
            hashes = json.loads(HASHES_JSON.read_text(encoding="utf-8"))
        except:
            hashes = {}
    changed = []
    failed = []
    new_hashes = dict(hashes)
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
            # compare hash or if file missing
            if prev != h or not stoverride_path.exists():
                print(f"  -> update needed (prev {prev[:8] if prev else 'none'} -> {h[:8]}, date {date})")
                # convert
                stoverride_text = convert_lpx_to_stash(text, url, fetch_script_fallback=True)
                stoverride_path.write_text(stoverride_text, encoding="utf-8")
                new_hashes[fname] = h
                # also store date for html
                item["_fetched_date"] = date
                item["_fetched_hash"] = h
                changed.append(fname)
            else:
                print(f"  -> no change ({h[:8]})")
                # keep date for html generation
                item["_fetched_hash"] = h
                if date:
                    item["_fetched_date"] = date
        except Exception as e:
            print(f"  -> failed {url}: {e}", file=sys.stderr)
            failed.append(fname)
        time.sleep(0.4)
    if failed:
        print(f"update check failed for {len(failed)} target(s): {', '.join(failed)}", file=sys.stderr)
        # 不提交/发布不完整的更新；已写出的单个转换文件会在下次成功检查时重建。
        sys.exit(1)
    # save hashes
    if changed:
        HASHES_JSON.write_text(json.dumps(new_hashes, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"updated {len(changed)}: {', '.join(changed)}")
        # regenerate html
        try:
            import generate_kelee_html
            generate_kelee_html.main()
            print("regenerated index.html")
        except Exception as e:
            print(f"failed to regenerate html: {e}", file=sys.stderr)
        # also update list.json timestamp?
        # Exit with code 2 to signal workflow to commit
        sys.exit(2 if changed else 0)
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
