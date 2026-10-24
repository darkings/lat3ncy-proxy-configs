#!/usr/bin/env python3
"""Resource acquisition, content-addressed publishing and fingerprints.

The converter no longer overwrites the same ``/scripts/<name>.js`` URL with new
bytes (which browsers/Stash may keep forever under a one-year immutable cache).
Every mirrored JavaScript file, remote jq program and dynamically loaded runtime
chunk is published under a content-derived path::

    scripts/<sha256[:12]>-<slug>.js

and additionally under its historical path ``scripts/<slug>.js`` as a
revalidation-only compatibility alias so overrides installed on a device
before the migration keep resolving their old URLs. Publishing policy lives in
``deploy/stash-caddyfile``: hashed paths are immutable, legacy aliases are
revalidated.

All upstream reads go through :class:`ResourceResolver`, which supports:

* live HTTP(S) fetching with the Loon User-Agent (upstream Cloudflare gate),
* an offline ``snapshots`` mapping (URL → bytes/path) for deterministic tests
  and for replaying ``.local/upstream-audit`` without network access,
* nested dependency mirroring: upstream scripts that reference other hosted
  ``.js`` resources (e.g. Pinduoduo's dynamically loaded webpack chunk) get
  those resources mirrored too, and the parent script is rewritten to point at
  the content-addressed child URL before the parent itself is hashed.

The resolver is intentionally independent from YAML generation so the
converter, the build orchestrator, the validator and tests can share it.
"""
from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from kelee_config import (
    MIRRORED_RUNTIME_ASSETS,
    hashed_asset_relpath,
    legacy_asset_relpath,
    sha256_hex,
)

LOON_UA = "Loon/764 CFNetwork/1498.700.1 Darwin/23.6.0 iPhone/17.6.1"
DEFAULT_HEADERS = {"User-Agent": LOON_UA, "Accept": "*/*"}

# Absolute http(s) URL that ends in .js (possible query string tolerated).
NESTED_JS_URL_RE = re.compile(r"https?://[^\s\"'`<>)]+?\.js(?:\?[^\s\"'`<>)]*)?")


class ResourceError(RuntimeError):
    """A required upstream resource could not be obtained."""

    def __init__(self, code: str, detail: str, *, url: str = "", cause: object = None):
        self.code = code
        self.detail = detail
        self.url = url
        self.cause = cause
        super().__init__(f"{code}: {detail}" + (f" ({url})" if url else ""))


@dataclass
class PublishedAsset:
    """One file that must be written under the published site root."""

    relpath: str
    content: bytes
    sha256: str
    kind: str  # script | runtime | legacy-static
    source_url: str
    logical_name: str
    legacy_relpath: str = ""
    url: str = ""
    mirror_url: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)

    def manifest_entry(self) -> dict:
        return {
            "relpath": self.relpath,
            "sha256": self.sha256,
            "size": len(self.content),
            "kind": self.kind,
            "source_url": self.source_url,
            "legacy_relpath": self.legacy_relpath,
        }


class ResourceResolver:
    """Fetch upstream resources once and publish them content-addressed."""

    def __init__(
        self,
        public_base: str,
        *,
        mirror_base: str = "",
        fetch: bool = True,
        snapshots: Optional[Mapping[str, bytes | str | Path]] = None,
        timeout: int = 20,
        retries: int = 3,
        headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.public_base = public_base.rstrip("/")
        self.mirror_base = (mirror_base or self.public_base).rstrip("/")
        self.fetch_enabled = bool(fetch)
        self.snapshots = dict(snapshots or {})
        self.timeout = timeout
        self.retries = retries
        self.headers = dict(headers or DEFAULT_HEADERS)
        self._content_cache: dict[str, bytes] = {}
        self._resource_hashes: dict[str, str] = {}
        self._assets_by_relpath: dict[str, PublishedAsset] = {}
        self._url_to_asset: dict[str, PublishedAsset] = {}
        self._nested_seen: set[str] = set()

    # -- upstream reads ---------------------------------------------------
    def _snapshot_bytes(self, url: str) -> bytes:
        value = self.snapshots.get(url)
        if value is None:
            raise ResourceError(
                "E_SNAPSHOT_MISSING",
                "offline mode requested but no snapshot for this URL",
                url=url,
            )
        if isinstance(value, bytes):
            return value
        path = Path(value)
        return path.read_bytes()

    def fetch_bytes(self, url: str) -> bytes:
        """Return the raw bytes of an upstream resource (cached)."""

        if url in self._content_cache:
            return self._content_cache[url]
        if url in self.snapshots or not self.fetch_enabled:
            data = self._snapshot_bytes(url)
        else:
            data = self._fetch_http(url)
        self._content_cache[url] = data
        self._resource_hashes[url] = sha256_hex(data)
        return data

    def fetch_text(self, url: str, *, errors: str = "replace") -> str:
        return self.fetch_bytes(url).decode("utf-8", errors=errors)

    def _fetch_http(self, url: str) -> bytes:
        last_error: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                request = urllib.request.Request(url, headers=self.headers)
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code in (429, 500, 502, 503, 504) and attempt + 1 < self.retries:
                    time.sleep(1 + attempt)
                    continue
                raise ResourceError(
                    "E_FETCH_HTTP", f"HTTP {exc.code}", url=url, cause=exc
                ) from exc
            except Exception as exc:  # noqa: BLE001 - transport errors are retryable
                last_error = exc
                if attempt + 1 < self.retries:
                    time.sleep(1 + attempt)
                    continue
                raise ResourceError(
                    "E_FETCH_FAILED", f"{type(exc).__name__}: {exc}", url=url, cause=exc
                ) from exc
        raise ResourceError(  # pragma: no cover - defensive
            "E_FETCH_FAILED", f"{last_error}", url=url, cause=last_error
        )

    # -- content-addressed publishing ------------------------------------
    def _register(self, asset: PublishedAsset, *, legacy_alias: bool) -> PublishedAsset:
        existing = self._assets_by_relpath.get(asset.relpath)
        if existing is not None:
            if existing.sha256 != asset.sha256:
                raise ResourceError(
                    "E_ASSET_HASH_COLLISION",
                    f"path {asset.relpath} would hold two different payloads",
                    url=asset.source_url,
                )
            return existing
        if legacy_alias and not asset.legacy_relpath:
            asset.legacy_relpath = legacy_asset_relpath(asset.logical_name)
        self._assets_by_relpath[asset.relpath] = asset
        if legacy_alias and asset.legacy_relpath:
            legacy = PublishedAsset(
                relpath=asset.legacy_relpath,
                content=asset.content,
                sha256=asset.sha256,
                kind=asset.kind,
                source_url=asset.source_url,
                logical_name=asset.logical_name,
                url=f"{self.public_base}/{asset.legacy_relpath}",
                mirror_url=f"{self.mirror_base}/{asset.legacy_relpath}",
            )
            self._assets_by_relpath.setdefault(legacy.relpath, legacy)
            asset.aliases = (legacy.relpath,)
            self._content_cache.setdefault(asset.source_url, asset.content)
        self._url_to_asset[asset.source_url] = asset
        return asset

    def publish_bytes(
        self,
        content: bytes,
        *,
        logical_name: str,
        source_url: str = "",
        kind: str = "script",
        legacy_alias: bool = True,
        mirror_nested: bool = False,
        self_url: str = "",
        _depth: int = 0,
    ) -> PublishedAsset:
        """Publish ``content`` under a content-addressed path and return it."""

        if mirror_nested:
            content = self.mirror_nested_assets(content, self_url=self_url, _depth=_depth)
        relpath = hashed_asset_relpath(content, logical_name)
        asset = PublishedAsset(
            relpath=relpath,
            content=content,
            sha256=sha256_hex(content),
            kind=kind,
            source_url=source_url,
            logical_name=logical_name,
            url=f"{self.public_base}/{relpath}",
            mirror_url=f"{self.mirror_base}/{relpath}",
        )
        return self._register(asset, legacy_alias=legacy_alias)

    def publish_script(self, source_url: str, content: bytes | None = None) -> PublishedAsset:
        """Publish an upstream script, mirroring its nested hosted dependencies."""

        raw = self.fetch_bytes(source_url) if content is None else (
            content if isinstance(content, bytes) else content.encode("utf-8")
        )
        return self.publish_bytes(
            raw,
            logical_name=source_url.rsplit("/", 1)[-1] or "script.js",
            source_url=source_url,
            kind="script",
            legacy_alias=True,
            mirror_nested=True,
            self_url=source_url,
        )

    def mirror_nested_assets(self, content: bytes, *, self_url: str = "", _depth: int = 0) -> bytes:
        """Rewrite hosted nested JS references (PDD runtime chunk etc.)."""

        if _depth > 4:  # defensive recursion guard
            raise ResourceError(
                "E_ASSET_NESTING_TOO_DEEP", "nested mirrored assets exceed depth 4"
            )
        text = content.decode("utf-8", "surrogateescape")
        for match in sorted(set(NESTED_JS_URL_RE.findall(text)), key=len, reverse=True):
            if self_url and match == self_url:
                continue
            if match in self._nested_seen:
                # Already published/replaced in this resolver session.
                asset = self._url_to_asset.get(match)
                if asset is not None:
                    text = text.replace(match, asset.url)
                continue
            if match not in MIRRORED_RUNTIME_ASSETS and "kelee.one" not in match:
                continue
            logical = MIRRORED_RUNTIME_ASSETS.get(match) or match.rsplit("/", 1)[-1]
            child_content = self.fetch_bytes(match)
            child_content = self.mirror_nested_assets(
                child_content, self_url=match, _depth=_depth + 1
            )
            child = self.publish_bytes(
                child_content,
                logical_name=logical,
                source_url=match,
                kind="runtime",
                legacy_alias=True,
            )
            self._nested_seen.add(match)
            text = text.replace(match, child.url)
        return text.encode("utf-8", "surrogateescape")

    # -- state/accessors --------------------------------------------------
    def resolve_script_url(self, source_url: str) -> tuple[PublishedAsset, str]:
        """Fetch+publish a script and return (asset, rewritten content text)."""

        asset = self.publish_script(source_url)
        return asset, asset.content.decode("utf-8", "surrogateescape")

    def published_assets(self) -> dict[str, bytes]:
        return {rel: asset.content for rel, asset in self._assets_by_relpath.items()}

    def asset_manifest(self) -> list[dict]:
        entries = []
        for relpath in sorted(self._assets_by_relpath):
            asset = self._assets_by_relpath[relpath]
            entry = asset.manifest_entry()
            entry["legacy_relpath"] = asset.legacy_relpath
            entries.append(entry)
        return entries

    def resource_manifest(self) -> dict[str, dict]:
        return {
            url: {"sha256": digest}
            for url, digest in sorted(self._resource_hashes.items())
        }

    def upstream_digest(self) -> str:
        """Digest of every upstream resource actually consumed."""

        payload = "\n".join(
            f"{url}\t{digest}" for url, digest in sorted(self._resource_hashes.items())
        )
        return sha256_hex(payload)

    def write_assets(self, root: Path) -> list[str]:
        written = []
        for relpath, content in sorted(self.published_assets().items()):
            path = Path(root) / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.read_bytes() == content:
                continue
            path.write_bytes(content)
            written.append(relpath)
        return written


def _normalize_snapshot_text(data: bytes) -> bytes:
    """Audit snapshots were written through Windows text mode (CRLF).

    Upstream serves LF; normalizing keeps replay byte-compatible with a live
    fetch so asset hashes/URLs are stable between snapshot and live builds.
    """

    return data.replace(b"\r\n", b"\n")


def load_snapshot_map(
    snapshot_dir: Path,
    *,
    mapping_file: Optional[Path] = None,
) -> dict[str, bytes]:
    """Load a URL→bytes replay map from a local audit snapshot directory.

    ``script-manifest.json`` (URL→file) is authoritative when present. Remote
    jq programs and runtime chunks have no basename in the audit layout, so
    they are mapped by an explicit ``resources.json`` (URL→relative file) next
    to the snapshots or in ``mapping_file``. Text snapshots are CRLF→LF
    normalized to match live HTTP bodies. Missing files are omitted so the
    converter emits an explicit ``E_SNAPSHOT_MISSING`` diagnostic instead of
    silently dropping rules.
    """

    import json

    root = Path(snapshot_dir)
    mapping: dict[str, bytes] = {}

    def _resolve(rel: str) -> Optional[Path]:
        path = Path(rel)
        candidates = []
        if path.is_absolute():
            candidates.append(path)
        else:
            candidates.extend([Path.cwd() / rel, root / rel])
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    manifest = root / "script-manifest.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - malformed snapshot manifest is not fatal
            data = {}
        for url, entry in data.items():
            rel = entry.get("file") if isinstance(entry, dict) else entry
            if not rel:
                continue
            path = _resolve(str(rel))
            if path is not None:
                mapping[url] = _normalize_snapshot_text(path.read_bytes())

    extra_candidates = []
    if mapping_file is not None:
        extra_candidates.append(Path(mapping_file))
    extra_candidates.append(root / "resources.json")
    for extra in extra_candidates:
        if not extra.is_file():
            continue
        try:
            data = json.loads(extra.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for url, rel in data.items():
            if url in mapping:
                continue
            if isinstance(rel, dict):
                rel = rel.get("file") or rel.get("relpath")
            if not rel:
                continue
            path = _resolve(str(rel))
            if path is not None:
                mapping[url] = _normalize_snapshot_text(path.read_bytes())
    return mapping


def load_audit_snapshot_map(repo_root: Path) -> dict[str, bytes]:
    """Best-effort replay map for ``.local/upstream-audit`` (local-only).

    Returns an empty dict when the audit evidence is not present so CI/tests
    never depend on it.
    """

    repo_root = Path(repo_root)
    audit = repo_root / ".local" / "upstream-audit"
    if not audit.is_dir():
        return {}
    mapping_file = repo_root / ".local" / "team" / "audit-resources.json"
    return load_snapshot_map(audit, mapping_file=mapping_file if mapping_file.is_file() else None)
