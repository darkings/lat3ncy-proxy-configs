#!/usr/bin/env python3
"""Deploy-side validation for the generated KeLee Stash bundle.

This is the last cheap gate before a bundle is handed to the shared publish
transaction (publish-release.sh). The build orchestrator already ran the full
Stash schema validation; this script re-checks the properties the deployment
depends on:

* required files exist (targets.json, index.html, each target .stoverride,
  kelee-scripts.stoverride, scripts/),
* the bundle was built for the deployment's public base (unified address), and
  no stale production hostname leaked in,
* hashed content-addressed scripts referenced by overrides/index/provider
  bundle exist,
* optional .build-manifest.json is consistent (public_base, outputs sha256),
* no unexpanded Loon placeholders remain.

Usage: validate.py <bundle-dir> <public-host>
Exit codes: 0 ok, 1 validation failure, 2 usage error.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is a deployment prerequisite
    yaml = None

HASHED_ASSET_RE = re.compile(r"^scripts/[0-9a-f]{12}-[A-Za-z0-9._-]+\.js$")
ASSET_REF_RE = re.compile(r"scripts/[A-Za-z0-9][A-Za-z0-9._-]*\.js")
PLACEHOLDER_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_.]*\}")
PUBLIC_BASE_RE = re.compile(r"^https?://[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")
RE_ABSOLUTE_URL = re.compile(r"https://([A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)/")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fail(message: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"validation failed: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_targets(root: Path) -> list[str]:
    path = root / "targets.json"
    if not path.is_file():
        fail(f"缺少 targets.json: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - surfaced as validation failure
        fail(f"targets.json 解析失败: {exc}")
    if not isinstance(data, list) or not data:
        fail("targets.json 必须是非空数组")
    names: list[str] = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            fail("targets.json 条目缺少 filename")
        filename = item["filename"].strip()
        if not filename:
            fail("targets.json 条目 filename 为空")
        if filename.endswith(".lpx"):
            filename = filename[:-4] + ".stoverride"
        elif not filename.endswith(".stoverride"):
            filename += ".stoverride"
        if "/" in filename or "\\" in filename or filename.startswith("."):
            fail(f"targets.json 中不允许的路径: {filename}")
        names.append(filename)
    return names


def resolve_public_base(public_host: str) -> tuple[str, str]:
    env_base = os.environ.get("KELEE_PUBLIC_BASE", "").strip()
    if env_base:
        if not PUBLIC_BASE_RE.match(env_base):
            fail(f"KELEE_PUBLIC_BASE 不是合法的 https?://域名: {env_base}")
        host = env_base.split("://", 1)[1]
        if host != public_host:
            fail(f"KELEE_PUBLIC_BASE 主机 {host} 与参数 public-host {public_host} 不一致")
        return env_base, host
    return f"https://{public_host}", public_host


def _stale_self_hosted_hosts(text: str, public_host: str) -> set[str]:
    """Return `stash.*` hostnames in absolute URLs that are not the deployed host.

    Deploy files must not carry a hardcoded production hostname; this generic
    pattern still catches a bundle that was built for an older self-hosted
    address (the actual hostname is never embedded in deploy tooling).
    """
    hosts: set[str] = set()
    for match in RE_ABSOLUTE_URL.finditer(text):
        host = match.group(1)
        if host != public_host and host.startswith("stash."):
            hosts.add(host)
    return hosts


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: validate.py <bundle-dir> <public-host>", file=sys.stderr)
        return 2

    root = Path(sys.argv[1]).resolve()
    public_host = sys.argv[2].strip()
    if not public_host:
        fail("public-host 不能为空")
    public_base, public_host = resolve_public_base(public_host)
    expected_prefix = public_base + "/"

    if not root.is_dir():
        fail(f"发布目录不存在: {root}")
    index_path = root / "index.html"
    if not index_path.is_file() or index_path.stat().st_size == 0:
        fail(f"缺少有效的 index.html: {index_path}")
    provider_path = root / "kelee-scripts.stoverride"
    if not provider_path.is_file() or provider_path.stat().st_size == 0:
        fail(f"缺少 kelee-scripts.stoverride: {provider_path}")
    scripts_dir = root / "scripts"
    if not scripts_dir.is_dir() or not any(scripts_dir.iterdir()):
        fail(f"scripts/ 目录缺失或为空: {scripts_dir}")

    targets = load_targets(root)
    missing = [name for name in targets if not (root / name).is_file()]
    if missing:
        fail("缺少转换文件: " + ", ".join(missing))

    for name in targets:
        text = (root / name).read_text(encoding="utf-8", errors="replace")
        if PLACEHOLDER_RE.search(text):
            fail(f"{name} 仍包含未展开的 Loon 占位符 ${{...}}")
        if yaml is not None:
            try:
                parsed = yaml.safe_load(text)
            except Exception as exc:  # noqa: BLE001
                fail(f"{name} YAML 解析失败: {exc}")
            if not isinstance(parsed, dict):
                fail(f"{name} YAML 根节点不是对象")

    provider_text = provider_path.read_text(encoding="utf-8", errors="replace")
    if "script-providers:" not in provider_text:
        fail("kelee-scripts.stoverride 缺少 script-providers 段")
    if expected_prefix not in provider_text:
        fail(f"kelee-scripts.stoverride 未使用部署地址 {public_base}")
    stale = _stale_self_hosted_hosts(provider_text, public_host)
    if stale:
        fail("kelee-scripts.stoverride 仍包含旧站点域名: " + ", ".join(sorted(stale)))

    html = index_path.read_text(encoding="utf-8", errors="replace")
    if expected_prefix not in html:
        fail(f"index.html 未使用部署安装地址 {expected_prefix}")
    stale = _stale_self_hosted_hosts(html, public_host)
    if stale:
        fail("index.html 仍包含旧站点域名: " + ", ".join(sorted(stale)))
    if "raw.githubusercontent.com/darkings/lat3ncy-proxy-configs" in html:
        # GitHub raw install URLs remain valid for other profiles, but the
        # self-hosted KeLee install entry must not fall back to them.
        if expected_prefix not in html:
            fail("index.html 仍包含仓库 Raw 安装地址")

    # Hashed assets referenced anywhere in the bundle must exist.
    asset_refs: set[str] = set()
    for path in list(root.glob("*.stoverride")) + [index_path, provider_path]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        asset_refs.update(ASSET_REF_RE.findall(text))
    missing_assets = sorted(ref for ref in asset_refs if not (root / ref).is_file())
    if missing_assets:
        fail("引用了不存在的脚本资源: " + ", ".join(missing_assets))
    hashed_refs = sorted(ref for ref in asset_refs if HASHED_ASSET_RE.match(ref))
    if not hashed_refs:
        fail("发布包没有引用任何内容寻址脚本（scripts/<sha12>-<slug>.js），构建器可能未启用自托管资产")

    # Optional build manifest must agree with the deployed base and byte hashes.
    manifest_path = root / ".build-manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            fail(f".build-manifest.json 解析失败: {exc}")
        if not isinstance(manifest, dict):
            fail(".build-manifest.json 根节点不是对象")
        manifest_base = manifest.get("public_base")
        if manifest_base and manifest_base != public_base:
            fail(f"构建清单 public_base={manifest_base} 与部署地址 {public_base} 不一致")
        outputs = manifest.get("outputs") or {}
        if not isinstance(outputs, dict) or not outputs:
            fail(".build-manifest.json 缺少 outputs")
        for rel, meta in outputs.items():
            if not isinstance(rel, str) or rel.startswith("/") or ".." in rel.split("/"):
                fail(f"构建清单包含不安全路径: {rel!r}")
            expected_sha = (meta or {}).get("sha256") if isinstance(meta, dict) else None
            if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
                fail(f"构建清单 outputs 缺少合法 sha256: {rel}")
            target = root / rel
            if not target.is_file():
                fail(f"构建清单列出的文件不存在: {rel}")
            actual_sha = sha256_file(target)
            if actual_sha.lower() != expected_sha.lower():
                fail(f"构建清单哈希不一致: {rel} (manifest={expected_sha} actual={actual_sha})")

    print(
        f"validated {len(targets)} targets, {len(hashed_refs)} hashed assets, "
        f"public_base={public_base} in {root}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
