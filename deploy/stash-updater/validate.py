#!/usr/bin/env python3
"""Validate the generated KeLee Stash bundle before the server publishes it."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: validate.py <bundle-dir> <public-host>", file=sys.stderr)
        return 2

    root = Path(sys.argv[1]).resolve()
    public_host = sys.argv[2]
    targets_path = root / "targets.json"
    index_path = root / "index.html"
    if not targets_path.is_file() or not index_path.is_file():
        raise RuntimeError("缺少 targets.json 或 index.html")

    targets = json.loads(targets_path.read_text(encoding="utf-8"))
    missing: list[str] = []
    for item in targets:
        filename = str(item["filename"]).replace(".lpx", ".stoverride")
        path = root / filename
        if not path.is_file():
            missing.append(filename)
            continue
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise RuntimeError(f"YAML 根节点不是对象：{filename}")

    if missing:
        raise RuntimeError("缺少转换文件：" + ", ".join(missing))

    html = index_path.read_text(encoding="utf-8")
    expected_prefix = f"https://{public_host}/"
    if expected_prefix not in html:
        raise RuntimeError("index.html 未使用自托管安装地址")
    if "raw.githubusercontent.com/darkings/lat3ncy-proxy-configs" in html:
        raise RuntimeError("index.html 仍包含仓库 Raw 安装地址")

    print(f"validated {len(targets)} targets in {root}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
