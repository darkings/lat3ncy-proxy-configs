#!/usr/bin/env python3
"""Audit configured remote resources according to tests/remote-resources.json."""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


URL_PATTERN = re.compile(r"https?://[^\s,\)\]`]+")


def probe(url: str) -> tuple[str, int | None, str | None]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "lat3ncy-proxy-configs-ci",
            "Range": "bytes=0-0",
            "Accept": "*/*",
        },
    )
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                response.read(1)
                return url, response.status, None
        except urllib.error.HTTPError as exc:
            if attempt == 0 and (exc.code == 429 or exc.code >= 500):
                time.sleep(1)
                continue
            return url, exc.code, str(exc.reason)
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            return url, None, str(exc)
    return url, None, "unknown error"


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=repo_root / "tests" / "remote-resources.json")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    required_hosts = set(manifest["required_hosts"])
    warning_hosts = set(manifest["warning_hosts"])
    known_hosts = required_hosts | warning_hosts | set(manifest["ignored_hosts"])
    local_first_prefixes = tuple(manifest["local_first_prefixes"])

    urls: set[str] = set()
    for relative_path in manifest["scan_files"]:
        content = (repo_root / relative_path).read_text(encoding="utf-8")
        urls.update(match.rstrip("'\".;") for match in URL_PATTERN.findall(content))

    unknown_hosts = sorted({urlsplit(url).hostname for url in urls} - known_hosts)
    if unknown_hosts:
        print("FAIL: unclassified remote hosts: " + ", ".join(unknown_hosts))
        return 1

    candidates = sorted(
        url
        for url in urls
        if urlsplit(url).hostname in required_hosts | warning_hosts
        and not url.startswith(local_first_prefixes)
    )
    failures: list[tuple[str, int | None, str | None]] = []
    warnings: list[tuple[str, int | None, str | None]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        for url, status, error in executor.map(probe, candidates):
            host = urlsplit(url).hostname
            if status is not None and 200 <= status < 400:
                continue
            item = (url, status, error)
            if host in warning_hosts:
                warnings.append(item)
            else:
                failures.append(item)

    if warnings:
        statuses = collections.Counter(str(status or "network") for _, status, _ in warnings)
        hosts = sorted({urlsplit(url).hostname for url, _, _ in warnings})
        summary = ", ".join(f"{key}={value}" for key, value in sorted(statuses.items()))
        print(f"WARN: {len(warnings)} optional resources unavailable ({summary}; hosts={','.join(hosts)})")
        if args.verbose:
            for url, status, error in warnings:
                print(f"WARN: optional resource {status or 'network'} {url} ({error})")
    for url, status, error in failures:
        print(f"FAIL: required resource {status or 'network'} {url} ({error})")

    if failures:
        return 1
    print(f"PASS: remote resource audit ({len(candidates) - len(warnings)} reachable, {len(warnings)} warnings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
