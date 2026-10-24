#!/usr/bin/env python3
"""Loon ``.lpx`` → Stash ``.stoverride`` converter (v3).

Entry points
------------
``convert_lpx(text, lpx_url, *, resolver, public_base, mirror_scripts=True)``
    Full conversion returning :class:`ConversionResult` with the Stash mapping,
    rendered YAML, per-input diagnostics/counts and mirrored resources.

``convert_lpx_to_stash(text, lpx_url="", fetch_script_fallback=True, ...)``
    Backwards-compatible helper returning only the YAML text; raises
    :class:`ConversionError` when the input contains blocking problems (the old
    behaviour embedded such lines as comments and returned "success").

The converter itself contains no network code: upstream reads go through
:class:`kelee_assets.ResourceResolver`, which supports live fetching as well as
offline snapshot replay for deterministic tests and the audit evidence.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import kelee_config as cfg  # noqa: E402
import kelee_semantics as semantics  # noqa: E402
from kelee_assets import (  # noqa: E402
    LOON_UA,
    ResourceResolver,
    load_audit_snapshot_map,
    load_snapshot_map,
)
from kelee_diagnostics import Diagnostic, DiagnosticCollection  # noqa: E402
from kelee_semantics import ConversionResult, convert_lpx as _convert_lpx  # noqa: E402

# Legacy network helper kept for callers that imported it from the old module.
FETCH_HEADERS = {"User-Agent": LOON_UA, "Accept": "*/*"}


def fetch_text(url: str, headers: dict | None = None, timeout: int = 20) -> str:
    """Fetch a text resource with the Loon UA (legacy helper)."""

    request = urllib.request.Request(url, headers=headers or FETCH_HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


class ConversionError(RuntimeError):
    """Raised when blocking diagnostics must stop publication."""

    def __init__(self, message: str, diagnostics: DiagnosticCollection):
        self.diagnostics = diagnostics
        super().__init__(message)


class StashBodyRewrite(str):
    """A body-rewrite scalar that must remain quoted in emitted YAML."""


class StashYamlDumper(yaml.SafeDumper):
    pass


StashYamlDumper.add_representer(
    StashBodyRewrite,
    lambda dumper, value: dumper.represent_scalar("tag:yaml.org,2002:str", value, style="'"),
)


def dump_stash_yaml(data: dict) -> str:
    """Dump a Stash override, keeping each body-rewrite rule one quoted scalar."""

    http = data.get("http")
    if isinstance(http, dict) and isinstance(http.get("body-rewrite"), list):
        http["body-rewrite"] = [StashBodyRewrite(str(rule)) for rule in http["body-rewrite"]]
    return yaml.dump(
        data,
        Dumper=StashYamlDumper,
        sort_keys=False,
        allow_unicode=True,
        width=1_000_000,
        default_flow_style=False,
    )


def _render_header(header: dict, lpx_url: str, diagnostics: DiagnosticCollection) -> str:
    lines = [f"# {header.get('name', 'Unnamed')}"]
    desc = (header.get("desc") or "").replace("\\n", " ").replace("\n", " ").strip()
    if desc:
        lines.append(f"# {desc}")
    for key, label in (
        ("author", "author"),
        ("homepage", "homepage"),
        ("icon", "icon"),
        ("date", "date"),
    ):
        value = (header.get(key) or "").strip()
        if value:
            lines.append(f"# {label}: {value}")
    if lpx_url:
        lines.append(f"# lpx: {lpx_url}")
    lines.append("# converted: Loon .lpx -> Stash .stoverride (scripts/convert_kelee_lpx.py v3)")
    if diagnostics:
        blocking = diagnostics.blocking_errors
        warnings = diagnostics.warnings
        if blocking:
            lines.append(f"# BLOCKING diagnostics: {len(blocking)} (bundle must not be published)")
            for diagnostic in blocking[:10]:
                lines.append(f"#   {diagnostic}")
        if warnings:
            lines.append(f"# warnings: {len(warnings)} (see build manifest for the full list)")
    return "\n".join(lines)


def convert_lpx(
    text: str,
    lpx_url: str = "",
    *,
    resolver: Optional[ResourceResolver] = None,
    public_base: Optional[str] = None,
    mirror_scripts: bool = True,
) -> ConversionResult:
    """Convert one Loon plugin and return the full structured result."""

    if resolver is None:
        resolved = cfg.resolve_public_base(public_base)
        resolver = ResourceResolver(resolved.base, mirror_base=resolved.mirror_base, fetch=False)
        mirror_scripts = False
    base = (public_base or resolver.public_base).rstrip("/")
    result = _convert_lpx(
        text,
        lpx_url,
        resolver=resolver,
        public_base=base,
        mirror_scripts=mirror_scripts,
    )
    header, _ = semantics.parse_lpx(text)
    result.yaml = _render_header(header, lpx_url, result.diagnostics) + "\n" + dump_stash_yaml(result.stash)
    return result


def convert_lpx_to_stash(
    text: str,
    lpx_url: str = "",
    fetch_script_fallback: bool = True,
    *,
    resolver: Optional[ResourceResolver] = None,
    public_base: Optional[str] = None,
    allow_blocking: bool = False,
) -> str:
    """Legacy helper: return YAML text, raising on blocking diagnostics."""

    if resolver is None:
        base = cfg.resolve_public_base(public_base).base
        resolver = ResourceResolver(base, fetch=fetch_script_fallback)
    result = convert_lpx(
        text,
        lpx_url,
        resolver=resolver,
        public_base=public_base or resolver.public_base,
        mirror_scripts=fetch_script_fallback,
    )
    if result.blocking and not allow_blocking:
        first = "; ".join(str(d) for d in result.diagnostics.blocking_errors[:3])
        raise ConversionError(f"conversion has blocking diagnostics: {first}", result.diagnostics)
    return result.yaml


def default_resolver(
    *,
    public_base: Optional[str] = None,
    fetch: bool = True,
    snapshot_dir: Optional[Path] = None,
    audit_snapshot: bool = False,
    mirror_base: Optional[str] = None,
) -> ResourceResolver:
    resolved = cfg.resolve_public_base(public_base)
    base = resolved.base
    snapshots = {}
    if snapshot_dir is not None:
        snapshots = load_snapshot_map(Path(snapshot_dir))
    if audit_snapshot:
        snapshots.update(load_audit_snapshot_map(REPO_ROOT))
    return ResourceResolver(
        base,
        mirror_base=mirror_base or resolved.mirror_base,
        fetch=fetch and not audit_snapshot and not snapshot_dir,
        snapshots=snapshots,
    )


def convert_file(
    lpx_path: Path,
    out_path: Path,
    lpx_url: str = "",
    *,
    resolver: Optional[ResourceResolver] = None,
    public_base: Optional[str] = None,
    write_assets: bool = True,
) -> ConversionResult:
    text = Path(lpx_path).read_text(encoding="utf-8")
    result = convert_lpx(
        text,
        lpx_url or Path(lpx_path).name,
        resolver=resolver,
        public_base=public_base,
    )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(result.yaml, encoding="utf-8")
    if write_assets and resolver is not None:
        assets_root = out_path.parent
        resolver.write_assets(assets_root)
    print(f"converted {lpx_path} -> {out_path} ({len(result.yaml.encode('utf-8'))} bytes)")
    return result


def convert_audit_snapshot(
    audit_dir: Path,
    out_dir: Path,
    *,
    public_base: Optional[str] = None,
    only: Optional[list[str]] = None,
) -> list[dict]:
    """Offline replay of the local audit evidence (never used by publish paths)."""

    audit_dir = Path(audit_dir)
    snapshots = load_audit_snapshot_map(REPO_ROOT)
    resolved = cfg.resolve_public_base(public_base)
    resolver = ResourceResolver(
        resolved.base,
        mirror_base=resolved.mirror_base,
        fetch=False,
        snapshots=snapshots,
    )
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summaries: list[dict] = []
    for lpx in sorted(audit_dir.glob("*.lpx")):
        if only and lpx.name not in only:
            continue
        text = lpx.read_text(encoding="utf-8")
        url = f"https://kelee.one/Tool/Loon/Lpx/{lpx.name}"
        result = convert_lpx(text, url, resolver=resolver, public_base=resolved.base)
        out_path = out_dir / lpx.name.replace(".lpx", ".stoverride")
        out_path.write_text(result.yaml, encoding="utf-8")
        summaries.append({"file": lpx.name, **result.summary(result.yaml)})
    resolver.write_assets(out_dir)
    return summaries


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Convert Loon .lpx plugins to Stash overrides")
    parser.add_argument("--lpx-file", type=Path, help="Local .lpx file to convert")
    parser.add_argument("--lpx-url", help="Remote .lpx URL to fetch and convert")
    parser.add_argument("--out", type=Path, help="Output .stoverride path (single file)")
    parser.add_argument("--public-base", help="Public base URL used for mirrored script URLs")
    parser.add_argument("--snapshot-dir", type=Path, help="Offline URL→bytes snapshot directory")
    parser.add_argument("--audit-snapshot", action="store_true", help="Replay .local/upstream-audit offline")
    parser.add_argument("--audit-out-dir", type=Path, default=REPO_ROOT / ".local/audit-converted")
    parser.add_argument("--only", nargs="*", help="With --audit-snapshot: only these .lpx files")
    parser.add_argument("--no-mirror", action="store_true", help="Do not mirror/fetch scripts (legacy mode)")
    parser.add_argument("--json-report", type=Path, help="Write per-target conversion report JSON")
    args = parser.parse_args(argv)

    if args.audit_snapshot:
        summaries = convert_audit_snapshot(
            REPO_ROOT / ".local/upstream-audit",
            args.audit_out_dir,
            public_base=args.public_base,
            only=args.only,
        )
        if args.json_report:
            args.json_report.parent.mkdir(parents=True, exist_ok=True)
            args.json_report.write_text(
                json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        blocking = [item for item in summaries if any(d["blocking"] for d in item["diagnostics"])]
        print(f"converted {len(summaries)} audit plugins -> {args.audit_out_dir}; "
              f"blocking targets: {len(blocking)}")
        for item in summaries:
            blocking_count = sum(1 for d in item["diagnostics"] if d["blocking"])
            print(
                f"  {item['file']}: rules={item['counts'].get('rules', 0)} "
                f"url={item['counts'].get('url_rewrite', 0)} mock={item['counts'].get('mock', 0)} "
                f"script={item['counts'].get('script', 0)} args={item['counts'].get('argument_entries', 0)} "
                f"body={item['counts'].get('body_rewrite', 0)} blocking={blocking_count}"
            )
        return 0 if not blocking else 2

    if args.lpx_url and args.out:
        resolver = default_resolver(
            public_base=args.public_base,
            fetch=not args.no_mirror and args.snapshot_dir is None,
            snapshot_dir=args.snapshot_dir,
        )
        text = resolver.fetch_text(args.lpx_url)
        result = convert_lpx(text, args.lpx_url, resolver=resolver, public_base=args.public_base)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(result.yaml, encoding="utf-8")
        resolver.write_assets(args.out.parent)
        print(f"converted {args.lpx_url} -> {args.out}")
    elif args.lpx_file and args.out:
        resolver = default_resolver(
            public_base=args.public_base,
            fetch=not args.no_mirror and args.snapshot_dir is None,
            snapshot_dir=args.snapshot_dir,
        )
        convert_file(
            args.lpx_file,
            args.out,
            str(args.lpx_file),
            resolver=resolver,
            public_base=args.public_base,
        )
    else:
        parser.print_help()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
