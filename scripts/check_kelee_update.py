#!/usr/bin/env python3
"""KeLee → Stash build orchestrator: fetch, convert, publish, validate, record.

This module is the only supported way to (re)build the publishable bundle. It
replaces the previous "check hashes, else re-convert and hope" script with a
transactional build:

1. resolve the *single* public base (``kelee_config.resolve_public_base``) and
   load the target list;
2. obtain every upstream ``.lpx`` (live fetch or offline snapshot);
3. convert every target with the shared converter and a
   :class:`kelee_assets.ResourceResolver` (content-addressed scripts, remote jq
   programs and nested runtime chunks; offline replay supported);
4. write a complete staging bundle (overrides, provider bundle, page, assets);
5. prove bundle integrity (every referenced asset exists, provider bundle
   covers every ``http.script`` reference, no stale host / upstream kelee.one
   URLs, page invariants) and run ``validate_stash.validate_bundle``;
6. only then promote the staging bundle into the output directory, write
   ``.build-manifest.json`` and the legacy ``.hashes.json`` state.

Fingerprint / update detection (item 10)
----------------------------------------
There is **no hand-maintained CONVERTER_VERSION**. The build fingerprint is a
SHA-256 over the canonical JSON of:

* manifest schema version and the public/mirror base;
* the target list (canonical form);
* every source ``.lpx`` SHA-256 (+ upstream ``#!date``);
* the resolver's ``upstream_digest()`` *and* the published resource map
  (content-addressed relpath + SHA-256 for every mirrored script / runtime
  chunk / inlined jq program);
* the SHA-256 of every pipeline source file (converter, shared config, asset
  resolver, expression parser, diagnostics, validator).

Consequences: a dependency-only change (mirrored JS/jq/runtime chunk changed
while every ``.lpx`` stayed identical) rebuilds the bundle; a converter /
validator / config change rebuilds the bundle; a deleted or corrupted generated
file triggers a rebuild even when the fingerprint matches; identical inputs and
intact outputs report "no change" (exit 0) and touch nothing.

Failure semantics: every failure before promotion leaves the output directory
exactly as it was (staging happens in a temporary directory), so the previous
manifest/state stays valid and the next run retries the same build. State is
never advanced on a failed conversion/validation.

CLI
---
``python scripts/check_kelee_update.py [--out-dir DIR] [--public-base URL]
[--mirror-base URL] [--targets PATH] [--snapshot-dir DIR] [--audit-snapshot]
[--force] [--dry-run] [--json-report FILE] [--no-legacy-static]``

Exit codes (kept stable for the deploy scripts / CI):

* ``0`` — success, nothing changed (bundle already matches the inputs);
* ``2`` — success, bundle was (re)built and is publishable;
* ``1`` — failure (conversion, resource, integrity, validation or IO error).
"""
from __future__ import annotations

import argparse
import copy
import datetime as _dt
import hashlib
import inspect
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

try:  # PyYAML is a hard dependency of the converter and validator as well
    import yaml
except ImportError as exc:  # pragma: no cover - environment error
    raise SystemExit(f"PyYAML is required: {exc}") from exc

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from kelee_diagnostics import Diagnostic  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BUILD_MANIFEST_SCHEMA = 1
MANIFEST_NAME = ".build-manifest.json"
STATE_NAME = ".hashes.json"
TARGETS_NAME = "targets.json"
PROVIDER_BUNDLE_NAME = "kelee-scripts.stoverride"
PAGE_NAME = "index.html"
SCRIPTS_DIRNAME = "scripts"

DEFAULT_OUT_DIR = REPO_ROOT / "stash" / "overrides" / "kelee"
DEFAULT_TARGETS_PATH = DEFAULT_OUT_DIR / TARGETS_NAME
AUDIT_SNAPSHOT_DIR = REPO_ROOT / ".local" / "upstream-audit"

# Upstream distribution channel for the Loon plugins themselves (not a deploy
# host: it is only ever read from).
UPSTREAM_LPX_BASE = "https://kelee.one/Tool/Loon/Lpx"
LOON_UA = "Loon/764 CFNetwork/1498.700.1 Darwin/23.6.0 iPhone/17.6.1"

# Pipeline source files whose bytes contribute to the build fingerprint. Every
# file that can change generated output MUST be listed here; there is no manual
# version counter to forget to bump.
PIPELINE_SOURCE_FILES = (
    "convert_kelee_lpx.py",
    "kelee_config.py",
    "kelee_assets.py",
    "kelee_semantics.py",
    "kelee_enhancements.py",
    "kelee_diagnostics.py",
    "loon_expr.py",
    # The build orchestrator itself and the page generator directly shape the
    # published bundle: editing either must invalidate the fingerprint.
    "check_kelee_update.py",
    "generate_kelee_html.py",
)
VALIDATOR_SOURCE_FILES = ("validate_stash.py",)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CHANGED = 2

PROVIDER_BUNDLE_ICON = "https://raw.githubusercontent.com/KOP-XIAO/QuantumUltX/master/Icons/Kelee.png"

_HASHED_RELPATH_RE = re.compile(r"^scripts/[0-9a-f]{8,}-[^/]+$")
_OVERLAY_ASSET_URL_RE = re.compile(r"/scripts/[0-9a-f]{8,}-[^/]+$")


# ---------------------------------------------------------------------------
# Result / error types
# ---------------------------------------------------------------------------
@dataclass
class AssetRecord:
    """One mirrored resource (script, runtime chunk or legacy static asset)."""

    url: str
    sha256: str
    relpath: str
    kind: str = "script"
    legacy_alias: str | None = None
    size: int | None = None


@dataclass
class ConversionOutcome:
    """Normalised converter output for one target."""

    filename: str
    name: str
    stash: dict
    diagnostics: list[Diagnostic] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    source_sha256: str = ""
    source_date: str = ""
    source_url: str = ""
    yaml_text: str | None = None
    upstream_resources: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    providers: dict = field(default_factory=dict)
    patches: list = field(default_factory=list)
    enhancements: list = field(default_factory=list)

    @property
    def output_name(self) -> str:
        return _override_name(self.filename)


@dataclass
class BuildResult:
    """Outcome of :func:`build_bundle`."""

    status: str  # no-change | built | dry-run
    changed: bool
    exit_code: int
    fingerprint: str
    previous_fingerprint: str | None = None
    out_dir: str = ""
    reasons: list[str] = field(default_factory=list)
    manifest: dict | None = None
    sources: dict = field(default_factory=dict)
    resources: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)
    target_results: list = field(default_factory=list)
    totals: dict = field(default_factory=dict)
    warnings: list[Diagnostic] = field(default_factory=list)
    errors: list[Diagnostic] = field(default_factory=list)

    def as_report(self) -> dict:
        return {
            "status": self.status,
            "changed": self.changed,
            "exit_code": self.exit_code,
            "fingerprint": self.fingerprint,
            "previous_fingerprint": self.previous_fingerprint,
            "out_dir": self.out_dir,
            "reasons": list(self.reasons),
            "targets": int(self.totals.get("targets") or 0),
            "providers": int(self.totals.get("providers") or 0),
            "assets": int(self.totals.get("assets") or 0),
            "outputs": len(self.outputs),
            "sources": self.sources,
            "warnings": [_diag_dict(d) for d in self.warnings],
            "errors": [_diag_dict(d) for d in self.errors],
        }


class BuildError(RuntimeError):
    """A build failed; nothing was promoted into the output directory."""

    def __init__(self, message: str, diagnostics: Iterable[Diagnostic] = ()) -> None:
        self.diagnostics = list(diagnostics) or [
            Diagnostic("E_BUILD_FAILED", message, source="build")
        ]
        detail = message
        if self.diagnostics:
            first = self.diagnostics[0]
            detail = f"{message}: {first.code}: {first.message}" if message else str(first)
        super().__init__(detail)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    if tmp.exists():
        tmp.unlink()
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _write_json_atomic(path: Path, value: Any) -> None:
    _write_bytes_atomic(
        path, (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode("utf-8")
    )


def _diag_dict(diagnostic: Diagnostic) -> dict:
    if hasattr(diagnostic, "as_dict"):
        return dict(diagnostic.as_dict())
    return {
        "code": getattr(diagnostic, "code", "E_UNKNOWN"),
        "message": getattr(diagnostic, "message", str(diagnostic)),
        "path": getattr(diagnostic, "path", ""),
        "blocking": bool(getattr(diagnostic, "blocking", True)),
        "severity": getattr(diagnostic, "severity", "error"),
        "source": getattr(diagnostic, "source", "build"),
    }


def _to_diagnostic(item: Any, *, source: str = "build") -> Diagnostic:
    if isinstance(item, Diagnostic):
        return item
    if isinstance(item, Mapping):
        return Diagnostic(
            str(item.get("code", "E_UNKNOWN")),
            str(item.get("message", "")),
            str(item.get("path", "")),
            bool(item.get("blocking", True)),
            str(item.get("severity", "error")),
            str(item.get("source", source)),
        )
    if isinstance(item, str):
        return Diagnostic("E_DIAGNOSTIC", item, source=source)
    return Diagnostic(
        str(getattr(item, "code", "E_UNKNOWN")),
        str(getattr(item, "message", item)),
        str(getattr(item, "path", "")),
        bool(getattr(item, "blocking", True)),
        str(getattr(item, "severity", "error")),
        str(getattr(item, "source", source)),
    )


def _normalize_diagnostics(raw: Any, *, source: str = "build") -> list[Diagnostic]:
    if raw is None:
        return []
    if isinstance(raw, (str, bytes)):
        return [_to_diagnostic(raw, source=source)]
    if isinstance(raw, Mapping):
        return [_to_diagnostic(raw, source=source)]
    try:
        items = list(raw)
    except TypeError:
        return [_to_diagnostic(raw, source=source)]
    return [_to_diagnostic(item, source=source) for item in items]


def _blocking(diagnostics: Iterable[Diagnostic]) -> list[Diagnostic]:
    return [d for d in diagnostics if getattr(d, "blocking", True)]


def _call_with_supported_kwargs(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call ``fn`` passing only the keyword arguments its signature accepts."""

    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):  # builtins / C functions
        return fn(*args, **kwargs)
    parameters = signature.parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return fn(*args, **kwargs)
    accepted = {key: value for key, value in kwargs.items() if key in parameters}
    return fn(*args, **accepted)


def _override_name(filename: str) -> str:
    return filename[:-4] + ".stoverride" if filename.lower().endswith(".lpx") else filename + ".stoverride"


def _parse_lpx_header(text: str) -> dict:
    header: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("#!"):
            break
        body = stripped[2:]
        key, _, value = body.partition("=")
        header[key.strip()] = value.strip()
    return header


def _lpx_date(text: str) -> str:
    return _parse_lpx_header(text).get("date", "")


# ---------------------------------------------------------------------------
# Pipeline (converter / resolver / validator adapters)
# ---------------------------------------------------------------------------
class Pipeline:
    """Adapter around the shared converter and validator modules.

    Kept as a class so tests can inject a deterministic offline pipeline; the
    production implementation is :class:`DefaultPipeline`.
    """

    @property
    def fingerprint_inputs(self) -> Mapping[str, str]:  # pragma: no cover - interface
        raise NotImplementedError

    def make_resolver(
        self,
        *,
        public_base: str,
        mirror_base: str,
        fetch: bool,
        resource_snapshots: Mapping[str, Any],
    ) -> Any:  # pragma: no cover - interface
        raise NotImplementedError

    def convert(
        self,
        lpx_text: str,
        lpx_url: str,
        *,
        resolver: Any,
        public_base: str,
    ) -> Any:  # pragma: no cover - interface
        raise NotImplementedError

    def validate(
        self,
        bundle_dir: Path,
        *,
        expected_targets: Sequence[str],
        public_base: str,
        assets: Mapping[str, str],
    ) -> Any:  # pragma: no cover - interface
        raise NotImplementedError

    def post_process_resolver(self, resolver: Any) -> None:
        """Hook for pipeline-specific extra assets (e.g. legacy static files)."""


class DefaultPipeline(Pipeline):
    """Production pipeline using ``convert_kelee_lpx`` / ``validate_stash``."""

    def __init__(self, *, scripts_dir: Path = SCRIPTS_DIR, repo_root: Path = REPO_ROOT) -> None:
        self.scripts_dir = Path(scripts_dir)
        self.repo_root = Path(repo_root)

    @property
    def fingerprint_inputs(self) -> Mapping[str, str]:
        inputs: dict[str, str] = {}
        for name in PIPELINE_SOURCE_FILES + VALIDATOR_SOURCE_FILES:
            path = self.scripts_dir / name
            inputs[name] = _sha256_file(path) if path.is_file() else "absent"
        return inputs

    def make_resolver(
        self,
        *,
        public_base: str,
        mirror_base: str,
        fetch: bool,
        resource_snapshots: Mapping[str, Any],
    ) -> Any:
        try:
            import kelee_assets
        except ImportError as exc:  # pragma: no cover - bring-up guard
            raise BuildError(f"scripts/kelee_assets.py is required: {exc}") from exc
        resolver_cls = getattr(kelee_assets, "ResourceResolver", None)
        if resolver_cls is None:  # pragma: no cover - contract guard
            raise BuildError("kelee_assets.ResourceResolver is missing")
        kwargs = {
            "mirror_base": mirror_base,
            "fetch": fetch,
            "snapshots": dict(resource_snapshots or {}),
        }
        executor = resolver_cls(public_base, **kwargs)
        return executor

    def convert(
        self,
        lpx_text: str,
        lpx_url: str,
        *,
        resolver: Any,
        public_base: str,
    ) -> Any:
        try:
            import convert_kelee_lpx
        except ImportError as exc:  # pragma: no cover - bring-up guard
            raise BuildError(f"scripts/convert_kelee_lpx.py is required: {exc}") from exc
        converter = getattr(convert_kelee_lpx, "convert_lpx", None)
        if converter is None:  # pragma: no cover - bring-up guard
            raise BuildError(
                "scripts/convert_kelee_lpx.py does not expose convert_lpx(...) yet; "
                "cannot build a content-addressed bundle"
            )
        return _call_with_supported_kwargs(
            converter,
            lpx_text,
            lpx_url,
            resolver=resolver,
            public_base=public_base,
        )

    def validate(
        self,
        bundle_dir: Path,
        *,
        expected_targets: Sequence[str],
        public_base: str,
        assets: Mapping[str, str],
    ) -> Any:
        try:
            import validate_stash
        except ImportError as exc:  # pragma: no cover - bring-up guard
            raise BuildError(f"scripts/validate_stash.py is required: {exc}") from exc
        validator = getattr(validate_stash, "validate_bundle", None)
        if validator is None:  # pragma: no cover - bring-up guard
            raise BuildError(
                "scripts/validate_stash.py does not expose validate_bundle(...); refusing to publish"
            )
        known_policies = None
        loader = getattr(validate_stash, "load_main_config_policies", None)
        main_config = Path(self.repo_root) / "stash-ios.yaml"
        if callable(loader) and main_config.is_file():
            try:
                known_policies = loader(main_config)
            except Exception:  # noqa: BLE001 - optional enrichment only
                known_policies = None
        return _call_with_supported_kwargs(
            validator,
            Path(bundle_dir),
            expected_targets=list(expected_targets),
            public_base=public_base,
            known_policies=known_policies,
            assets=dict(assets),
        )


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------
def compute_fingerprint(inputs: Mapping[str, Any]) -> str:
    """Deterministic SHA-256 over the canonical build inputs."""

    return _sha256_text(_canonical_json(dict(inputs)))


def build_fingerprint_inputs(
    *,
    public_base: str,
    mirror_base: str,
    schema: int,
    targets_digest: str,
    sources: Mapping[str, Any],
    resources: Mapping[str, Any],
    pipeline_inputs: Mapping[str, str],
    upstream_digest: str,
    upstream_base: str = UPSTREAM_LPX_BASE,
) -> dict:
    """Assemble the canonical fingerprint payload (also stored in the manifest)."""

    return {
        "schema": int(schema),
        "public_base": public_base,
        "mirror_base": mirror_base,
        "upstream_base": upstream_base,
        "targets": targets_digest,
        "sources": {
            str(name): {"sha256": str(meta.get("sha256", ""))}
            for name, meta in sorted(sources.items())
        },
        "resources": {
            str(url): {
                "sha256": str(meta.get("sha256", "")),
                "relpath": meta.get("relpath") or "",
                "kind": str(meta.get("kind", "")),
                "legacy_alias": meta.get("legacy_alias") or "",
            }
            for url, meta in sorted(resources.items())
        },
        "upstream_digest": upstream_digest,
        "pipeline": {str(key): str(value) for key, value in sorted(pipeline_inputs.items())},
    }


def _canonical_targets(targets: Sequence[Mapping[str, Any]]) -> list[dict]:
    normalized: list[dict] = []
    for target in targets:
        item = {str(key): target[key] for key in sorted(target)}
        normalized.append(item)
    return normalized


def _targets_digest(targets: Sequence[Mapping[str, Any]]) -> str:
    return _sha256_text(_canonical_json(_canonical_targets(targets)))


def _diff_fingerprint_inputs(previous: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    for key in ("schema", "public_base", "mirror_base", "upstream_base", "targets", "upstream_digest"):
        if previous.get(key) != current.get(key):
            reasons.append(f"{key} changed")
    previous_pipeline = previous.get("pipeline") or {}
    current_pipeline = current.get("pipeline") or {}
    for name in sorted(set(previous_pipeline) | set(current_pipeline)):
        if previous_pipeline.get(name) != current_pipeline.get(name):
            reasons.append(f"converter/validator/source changed: {name}")
    previous_sources = previous.get("sources") or {}
    current_sources = current.get("sources") or {}
    for name in sorted(set(previous_sources) | set(current_sources)):
        before = (previous_sources.get(name) or {}).get("sha256")
        after = (current_sources.get(name) or {}).get("sha256")
        if before != after:
            reasons.append(f"upstream LPX changed: {name}")
    previous_resources = previous.get("resources") or {}
    current_resources = current.get("resources") or {}
    for url in sorted(set(previous_resources) | set(current_resources)):
        before = previous_resources.get(url)
        after = current_resources.get(url)
        if before != after:
            kind = "dependency" if before and after else ("added dependency" if after else "removed dependency")
            reasons.append(f"{kind} changed: {url}")
    return reasons


# ---------------------------------------------------------------------------
# Input loading
# ---------------------------------------------------------------------------
def load_targets(path: Path | str) -> list[dict]:
    path = Path(path)
    if not path.is_file():
        raise BuildError(f"targets file does not exist: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError(f"cannot parse targets file {path}: {exc}") from exc
    if not isinstance(data, list) or not data:
        raise BuildError(f"targets file {path} must be a non-empty JSON list")
    targets: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(data):
        if not isinstance(item, Mapping) or not item.get("filename"):
            raise BuildError(f"targets entry #{index} has no filename: {item!r}")
        filename = str(item["filename"])
        if not filename.lower().endswith(".lpx"):
            raise BuildError(f"target {filename!r} is not a .lpx file")
        if filename in seen:
            raise BuildError(f"duplicate target: {filename}")
        seen.add(filename)
        entry = dict(item)
        entry["filename"] = filename
        entry.setdefault("name", filename[:-4])
        targets.append(entry)
    return targets


def _coerce_source_text(value: Any, *, name: str) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, Path):
        return value.read_text(encoding="utf-8")
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping) and "text" in value:
        return str(value["text"])
    raise BuildError(f"unsupported source payload for {name}: {type(value).__name__}")


def _fetch_upstream_text(url: str, *, fetcher: Callable[[str], str] | None = None) -> str:
    if fetcher is not None:
        return fetcher(url)
    try:
        import convert_kelee_lpx
    except ImportError:  # pragma: no cover - fallback for very early bring-up
        convert_kelee_lpx = None  # type: ignore
    fetch = getattr(convert_kelee_lpx, "fetch_text", None) if convert_kelee_lpx else None
    if callable(fetch):
        return fetch(url)
    import time
    import urllib.request

    request = urllib.request.Request(
        url, headers={"User-Agent": LOON_UA, "Accept": "*/*"}
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except Exception as exc:  # noqa: BLE001 - retry any transport error
            last_error = exc
            time.sleep(1 + attempt)
    raise BuildError(f"failed to fetch {url}: {last_error}")


def load_sources(
    targets: Sequence[Mapping[str, Any]],
    *,
    snapshot_map: Mapping[str, Any] | None = None,
    source_dir: Path | str | None = None,
    fetch: bool = True,
    fetcher: Callable[[str], str] | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, dict]:
    """Return ``{filename: {text, sha256, date, url}}`` for every target."""

    snapshot_map = dict(snapshot_map or {})
    source_dir = Path(source_dir) if source_dir else None
    loaded: dict[str, dict] = {}
    for target in targets:
        filename = str(target["filename"])
        url = f"{UPSTREAM_LPX_BASE}/{filename}"
        text: str | None = None
        if filename in snapshot_map:
            text = _coerce_source_text(snapshot_map[filename], name=filename)
        elif source_dir is not None:
            path = source_dir / filename
            if not path.is_file():
                raise BuildError(
                    f"offline snapshot is missing the LPX file {filename}: {path}",
                )
            text = path.read_text(encoding="utf-8")
        elif fetch:
            if log:
                log(f"  fetching {url}")
            try:
                text = _fetch_upstream_text(url, fetcher=fetcher)
            except BuildError as exc:
                raise BuildError(
                    f"failed to fetch upstream LPX {filename}",
                    [
                        Diagnostic(
                            "E_LPX_FETCH_FAILED",
                            str(exc),
                            path=filename,
                            blocking=True,
                            severity="error",
                            source="build",
                        )
                    ],
                ) from exc
        else:
            raise BuildError(
                f"no source for {filename}: offline mode needs a snapshot (--snapshot-dir / snapshot_map)"
            )
        loaded[filename] = {
            "text": text,
            "sha256": _sha256_text(text),
            "date": _lpx_date(text),
            "url": url,
        }
    return loaded


def load_resource_snapshots(
    *,
    source_dir: Path | str | None,
    resource_snapshots: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if resource_snapshots is not None:
        return dict(resource_snapshots)
    if source_dir is None:
        return {}
    try:
        from kelee_assets import load_snapshot_map
    except ImportError:  # pragma: no cover - bring-up guard
        return {}
    return load_snapshot_map(Path(source_dir))


# ---------------------------------------------------------------------------
# Conversion normalisation
# ---------------------------------------------------------------------------
def _coerce_conversion_result(
    raw: Any,
    *,
    target: Mapping[str, Any],
    source: Mapping[str, Any],
) -> ConversionOutcome:
    filename = str(target["filename"])
    if raw is None:
        raise BuildError(f"converter returned no result for {filename}")
    if isinstance(raw, Mapping):
        stash = raw.get("stash")
        yaml_text = raw.get("yaml")
        diagnostics = raw.get("diagnostics")
        counts = raw.get("counts")
        upstream = raw.get("upstream_resources")
        source_sha = raw.get("source_sha256")
        name = raw.get("name")
        providers = raw.get("providers")
        patches = raw.get("patches")
        enhancements = raw.get("enhancements")
    else:
        stash = getattr(raw, "stash", None)
        yaml_text = getattr(raw, "yaml", None)
        diagnostics = getattr(raw, "diagnostics", None)
        counts = getattr(raw, "counts", None)
        upstream = getattr(raw, "upstream_resources", None)
        source_sha = getattr(raw, "source_sha256", None)
        name = getattr(raw, "name", None)
        providers = getattr(raw, "providers", None)
        patches = getattr(raw, "patches", None)
        enhancements = getattr(raw, "enhancements", None)

    if not isinstance(stash, dict) or not stash:
        raise BuildError(f"converter produced an empty/invalid Stash mapping for {filename}")
    meta = _parse_lpx_header(str(source.get("text", "")))
    display_name = str(name or stash.get("name") or target.get("name") or filename[:-4])
    diagnostics_list: list[Diagnostic] = []
    for diagnostic in _normalize_diagnostics(diagnostics, source="converter"):
        path = getattr(diagnostic, "path", "")
        if not path:
            diagnostic = Diagnostic(
                diagnostic.code,
                diagnostic.message,
                filename,
                getattr(diagnostic, "blocking", True),
                getattr(diagnostic, "severity", "error"),
                getattr(diagnostic, "source", "converter"),
            )
        diagnostics_list.append(diagnostic)
    upstream_resources = dict(upstream or {}) if isinstance(upstream, Mapping) else {}
    return ConversionOutcome(
        filename=filename,
        name=display_name,
        stash=stash,
        diagnostics=diagnostics_list,
        counts=dict(counts or {}),
        source_sha256=str(source_sha or source.get("sha256") or ""),
        source_date=str(source.get("date") or meta.get("date", "")),
        source_url=str(source.get("url") or ""),
        yaml_text=yaml_text if isinstance(yaml_text, str) else None,
        upstream_resources=upstream_resources,
        meta=meta,
        providers=dict(providers or {}) if isinstance(providers, Mapping) else {},
        patches=[dict(item) if isinstance(item, Mapping) else item for item in (patches or [])],
        enhancements=[dict(item) if isinstance(item, Mapping) else item for item in (enhancements or [])],
    )


# ---------------------------------------------------------------------------
# YAML dumping for generated overrides
# ---------------------------------------------------------------------------
class _QuotedScalar(str):
    """Fallback marker for YAML strings that must stay single-quoted."""


class _FallbackDumper(yaml.SafeDumper):
    pass


def _represent_quoted(dumper: yaml.SafeDumper, data: _QuotedScalar) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="'")


_FallbackDumper.add_representer(_QuotedScalar, _represent_quoted)


def _dump_stash_yaml(data: dict) -> str:
    """Dump override YAML with the converter's Stash-aware dumper when possible."""

    try:
        from convert_kelee_lpx import dump_stash_yaml as dumper

        return dumper(data)
    except (ImportError, AttributeError):
        pass
    http = data.get("http")
    prepared = dict(data)
    if isinstance(http, Mapping) and isinstance(http.get("body-rewrite"), list):
        prep_http = dict(http)
        prep_http["body-rewrite"] = [_QuotedScalar(str(item)) for item in http["body-rewrite"]]
        prepared["http"] = prep_http
    return yaml.dump(
        prepared,
        Dumper=_FallbackDumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=4096,
    )


def _header_comment(meta: Mapping[str, Any], *, name: str, url: str, fingerprint: str) -> str:
    lines = [f"# {name}"]
    desc = str(meta.get("desc", "")).replace("\n", " ").replace("\\n", " ").strip()
    if desc:
        lines.append(f"# {desc}")
    for key in ("author", "homepage", "icon", "date", "openUrl"):
        value = meta.get(key)
        if value:
            lines.append(f"# {key}: {value}")
    if url:
        lines.append(f"# lpx: {url}")
    lines.append("# converted: Loon .lpx → Stash .stoverride (auto, content-addressed bundle)")
    if fingerprint:
        lines.append(f"# build-fingerprint: {fingerprint[:16]}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Bundle writing
# ---------------------------------------------------------------------------
def _extract_providers(outcome: ConversionOutcome) -> dict:
    stash = copy.copy(outcome.stash)
    providers = stash.pop("script-providers", None)
    outcome.stash = stash
    if providers is None:
        providers = outcome.providers or {}
    if not isinstance(providers, Mapping):
        raise BuildError(f"{outcome.filename}: script-providers must be a mapping")
    outcome.providers = {str(name): dict(info) if isinstance(info, Mapping) else info for name, info in providers.items()}
    return outcome.providers


def _resource_url_map(asset_records: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for record in asset_records:
        source_url = str(record.get("source_url") or "")
        relpath = str(record.get("relpath") or "")
        if source_url and relpath and not mapping.get(source_url):
            mapping[source_url] = relpath
    return mapping


def _rewrite_provider_urls(providers: Mapping[str, Any], *, public_base: str, assets: Mapping[str, Any]) -> tuple[dict, list[Diagnostic]]:
    """Point every provider at the content-addressed published asset when known."""

    warnings: list[Diagnostic] = []
    rewritten: dict[str, Any] = {}
    url_map = _resource_url_map(assets["records"])
    for name, info in providers.items():
        value = dict(info) if isinstance(info, Mapping) else {}
        url = value.get("url")
        if isinstance(url, str):
            if url.startswith(public_base + "/"):
                pass  # already content-addressed by the converter
            elif url in url_map:
                value["url"] = f"{public_base}/{url_map[url]}"
            elif "kelee.one" in url:
                raise BuildError(f"provider {name!r} still points at kelee.one: {url}")
            else:
                warnings.append(
                    Diagnostic(
                        "W_PROVIDER_EXTERNAL",
                        f"provider {name!r} points at an external URL ({url}); not mirrored",
                        path=PROVIDER_BUNDLE_NAME,
                        blocking=False,
                        severity="warning",
                        source="build",
                    )
                )
        rewritten[str(name)] = value
    return rewritten, warnings


def _order_providers(providers: Mapping[str, Any], previous: Mapping[str, Any]) -> dict:
    ordered: dict[str, Any] = {}
    for name in previous:
        if name in providers:
            ordered[name] = providers[name]
    for name in providers:
        if name not in ordered:
            ordered[name] = providers[name]
    return ordered


def _read_previous_providers(out_dir: Path) -> dict:
    path = Path(out_dir) / PROVIDER_BUNDLE_NAME
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    providers = data.get("script-providers") if isinstance(data, Mapping) else None
    return dict(providers) if isinstance(providers, Mapping) else {}


def _provider_bundle_text(providers: Mapping[str, Any]) -> str:
    data = {
        "name": "kelee-scripts",
        "desc": f"KeLee Scripts Hub ({len(providers)} providers)",
        "icon": PROVIDER_BUNDLE_ICON,
        "script-providers": dict(providers),
    }
    return yaml.safe_dump(
        data,
        sort_keys=False,
        allow_unicode=True,
        width=4096,
        default_flow_style=False,
    )


def _safe_write(root: Path, relpath: str, data: bytes) -> Path:
    rel = str(relpath).replace("\\", "/")
    if rel.startswith("/") or ".." in Path(rel).parts:
        raise BuildError(f"refusing to write unsafe output path: {relpath!r}")
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@dataclass
class StageResult:
    """Everything written into the staging directory, with hashes."""

    outputs: dict[str, dict] = field(default_factory=dict)
    override_data: dict[str, dict] = field(default_factory=dict)
    providers: dict = field(default_factory=dict)
    warnings: list[Diagnostic] = field(default_factory=list)


def _write_bundle(
    staging: Path,
    *,
    outcomes: Sequence[ConversionOutcome],
    targets: Sequence[Mapping[str, Any]],
    public_base: str,
    fingerprint: str,
    assets: Mapping[str, Any],
    previous_providers: Mapping[str, Any],
) -> StageResult:
    result = StageResult()
    merged_providers: dict[str, Any] = {}
    for outcome in outcomes:
        providers = _extract_providers(outcome)
        for name, info in providers.items():
            if name in merged_providers and merged_providers[name] != info:
                raise BuildError(
                    f"provider conflict for {name!r}: different definitions across targets",
                )
            merged_providers[name] = info

    rewritten_providers, provider_warnings = _rewrite_provider_urls(
        merged_providers, public_base=public_base, assets=assets
    )
    result.warnings.extend(provider_warnings)
    result.providers = _order_providers(rewritten_providers, previous_providers)

    # 1. per-target overrides (providers stripped; written from the canonical dict)
    for outcome in outcomes:
        stash_copy = dict(outcome.stash)
        stash_copy.pop("script-providers", None)
        if not stash_copy.get("name"):
            stash_copy["name"] = outcome.name
        body = _dump_stash_yaml(stash_copy)
        header = _header_comment(
            outcome.meta, name=outcome.name, url=outcome.source_url, fingerprint=fingerprint
        )
        text = header + body
        name = outcome.output_name
        relpath = name
        _safe_write(staging, relpath, text.encode("utf-8"))
        result.outputs[relpath] = {
            "sha256": _sha256_text(text),
            "size": len(text.encode("utf-8")),
            "counts": _int_counts(outcome.counts),
        }
        result.override_data[relpath] = stash_copy

    # 2. provider bundle
    bundle_text = _provider_bundle_text(result.providers)
    _safe_write(staging, PROVIDER_BUNDLE_NAME, bundle_text.encode("utf-8"))
    result.outputs[PROVIDER_BUNDLE_NAME] = {
        "sha256": _sha256_text(bundle_text),
        "size": len(bundle_text.encode("utf-8")),
        "counts": {"providers": len(result.providers)},
    }

    # 3. content-addressed assets (+ legacy aliases)
    published: Mapping[str, bytes] = assets["published"]
    for relpath in sorted(published):
        data = bytes(published[relpath])
        _safe_write(staging, relpath, data)
        digest = _sha256_bytes(data)
        basename = Path(relpath).name
        if _HASHED_RELPATH_RE.match(relpath.replace("\\", "/")):
            prefix = basename.split("-", 1)[0]
            if not digest.startswith(prefix):
                raise BuildError(
                    f"content-addressed asset {relpath} does not match its SHA-256 prefix"
                )
        result.outputs[relpath] = {"sha256": digest, "size": len(data), "counts": {}}

    # 4. targets.json (copy of the authoritative target list)
    targets_text = _targets_json_text(targets)
    _safe_write(staging, TARGETS_NAME, targets_text.encode("utf-8"))
    result.outputs[TARGETS_NAME] = {
        "sha256": _sha256_text(targets_text),
        "size": len(targets_text.encode("utf-8")),
        "counts": {"targets": len(targets)},
    }
    return result


def _int_counts(counts: Mapping[str, Any]) -> dict:
    return {
        str(key): int(value)
        for key, value in counts.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def _targets_json_text(targets: Sequence[Mapping[str, Any]]) -> str:
    return json.dumps(list(targets), ensure_ascii=False, indent=2) + "\n"


def _aggregate_totals(outcomes: Sequence[ConversionOutcome], stage: StageResult, resources: Mapping[str, Any]) -> dict:
    totals: dict[str, Any] = {}
    for outcome in outcomes:
        for key, value in _int_counts(outcome.counts).items():
            totals[key] = int(totals.get(key, 0)) + value
    totals["targets"] = len(outcomes)
    totals["converted"] = len(outcomes)
    totals["providers"] = len(stage.providers)
    totals["assets"] = len([url for url, meta in resources.items() if meta.get("relpath")])
    totals["resources"] = len(resources)
    totals["outputs"] = len(stage.outputs)
    totals["asset_files"] = len([rel for rel in stage.outputs if rel.startswith(SCRIPTS_DIRNAME + "/")])
    return totals


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
def _previous_manifest(out_dir: Path) -> dict | None:
    path = Path(out_dir) / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("outputs"), dict):
        return None
    return data


def check_existing_outputs(out_dir: Path, manifest: Mapping[str, Any]) -> list[str]:
    """Return a list of missing/corrupted outputs described by ``manifest``."""

    problems: list[str] = []
    out_dir = Path(out_dir)
    outputs = manifest.get("outputs") or {}
    if not isinstance(outputs, Mapping) or not outputs:
        return ["manifest has no outputs"]
    for relpath, meta in sorted(outputs.items()):
        path = out_dir / str(relpath)
        if not path.is_file():
            problems.append(f"missing output: {relpath}")
            continue
        expected = str((meta or {}).get("sha256", ""))
        if expected and _sha256_file(path) != expected:
            problems.append(f"corrupted output (sha256 mismatch): {relpath}")
    page = out_dir / PAGE_NAME
    if not page.is_file():
        problems.append(f"missing output: {PAGE_NAME}")
    return problems


def _build_manifest(
    *,
    public_base: str,
    mirror_base: str,
    fingerprint: str,
    fingerprint_inputs: Mapping[str, Any],
    generated_at: str,
    sources: Mapping[str, Mapping[str, Any]],
    resources: Mapping[str, Mapping[str, Any]],
    outputs: Mapping[str, Mapping[str, Any]],
    outcomes: Sequence[ConversionOutcome],
    stage: StageResult,
    totals: Mapping[str, Any],
    patches: Sequence[Mapping[str, Any]],
    enhancements: Sequence[Mapping[str, Any]],
    warnings: Sequence[Diagnostic],
    upstream_digest: str,
) -> dict:
    pipeline_inputs = dict(fingerprint_inputs.get("pipeline") or {})
    converter_digest = _sha256_text(
        _canonical_json({key: value for key, value in pipeline_inputs.items() if key not in VALIDATOR_SOURCE_FILES})
    )
    target_results = []
    for outcome in outcomes:
        relpath = outcome.output_name
        output_meta = outputs.get(relpath) or {}
        stash = outcome.stash if isinstance(outcome.stash, Mapping) else {}
        meta = {
            key: str(value)
            for key, value in outcome.meta.items()
            if key in ("name", "desc", "author", "homepage", "icon", "date", "openUrl", "tag")
        }
        for key in ("name", "desc", "icon", "author", "homepage", "date"):
            if not meta.get(key) and stash.get(key):
                meta[key] = str(stash[key])
        target_results.append(
            {
                "filename": outcome.filename,
                "name": outcome.name,
                "status": "converted",
                "source_sha256": outcome.source_sha256,
                "source_date": outcome.source_date,
                "output": relpath,
                "output_sha256": str(output_meta.get("sha256", "")),
                "counts": _int_counts(outcome.counts),
                "meta": meta,
                "patches": [dict(item) if isinstance(item, Mapping) else item for item in outcome.patches],
                "enhancements": [
                    dict(item) if isinstance(item, Mapping) else item for item in outcome.enhancements
                ],
                "warnings": [_diag_dict(d) for d in outcome.diagnostics if not d.blocking],
            }
        )
    return {
        "schema": BUILD_MANIFEST_SCHEMA,
        "generator": "scripts/check_kelee_update.py",
        "public_base": public_base,
        "mirror_base": mirror_base,
        "fingerprint": fingerprint,
        "fingerprint_inputs": dict(fingerprint_inputs),
        "generated_at": generated_at,
        "upstream_digest": upstream_digest,
        "converter_digest": converter_digest,
        "validator_digest": str(pipeline_inputs.get("validate_stash.py", "")),
        "targets_digest": str(fingerprint_inputs.get("targets", "")),
        "sources": {
            str(name): {
                "sha256": str(meta.get("sha256", "")),
                "date": str(meta.get("date", "")),
                "url": str(meta.get("url", "")),
            }
            for name, meta in sources.items()
        },
        "resources": {
            str(url): {
                "sha256": str(meta.get("sha256", "")),
                "relpath": meta.get("relpath") or None,
                "kind": str(meta.get("kind", "script")),
                "legacy_alias": meta.get("legacy_alias") or None,
                "size": meta.get("size"),
            }
            for url, meta in resources.items()
        },
        "outputs": {
            str(relpath): {
                "sha256": str(meta.get("sha256", "")),
                "size": meta.get("size"),
                "counts": dict(meta.get("counts") or {}),
            }
            for relpath, meta in outputs.items()
        },
        "target_results": target_results,
        "providers": list(stage.providers),
        "patches": [dict(item) for item in patches],
        "enhancements": [dict(item) for item in enhancements],
        "totals": dict(totals),
        "errors": [],
        "warnings": [_diag_dict(d) for d in warnings],
    }


def write_state(out_dir: Path, *, manifest: Mapping[str, Any], sources: Mapping[str, Mapping[str, Any]]) -> Path:
    """Write the legacy ``.hashes.json`` state file (kept for deploy tooling)."""

    payload: dict[str, Any] = {
        "_build_fingerprint": manifest.get("fingerprint", ""),
        "_manifest_schema": manifest.get("schema", BUILD_MANIFEST_SCHEMA),
        "_generated_at": manifest.get("generated_at", ""),
    }
    for filename, meta in sources.items():
        payload[str(filename)] = str(meta.get("sha256", ""))
    path = Path(out_dir) / STATE_NAME
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    if path.is_file():
        try:
            if path.read_text(encoding="utf-8") == serialized:
                return path
        except OSError:
            pass
    _write_bytes_atomic(path, serialized.encode("utf-8"))
    return path


# ---------------------------------------------------------------------------
# Integrity checks (run against the staging bundle, before promotion)
# ---------------------------------------------------------------------------
def _text_outputs(outputs: Mapping[str, Mapping[str, Any]]) -> Iterable[str]:
    for relpath in outputs:
        if str(relpath).startswith(SCRIPTS_DIRNAME + "/"):
            continue
        yield str(relpath)


def verify_staged_bundle(
    staging: Path,
    *,
    stage: StageResult,
    targets: Sequence[Mapping[str, Any]],
    public_base: str,
    resources: Mapping[str, Mapping[str, Any]],
    legacy_hosts: Sequence[str],
    page_validator: Callable[[str, dict], list[str]],
    fingerprint: str,
) -> list[Diagnostic]:
    """Prove the staged bundle is complete and internally consistent."""

    problems: list[Diagnostic] = []
    staging = Path(staging)

    def add(code: str, message: str, path: str = "") -> None:
        problems.append(Diagnostic(code, message, path, True, "error", "build"))

    # 1. outputs exist and match their hashes
    for relpath, meta in sorted(stage.outputs.items()):
        path = staging / relpath
        if not path.is_file():
            add("E_OUTPUT_MISSING", f"expected output {relpath} was not written", relpath)
            continue
        digest = _sha256_file(path)
        if digest != meta.get("sha256"):
            add("E_OUTPUT_HASH", f"output {relpath} hash mismatch", relpath)

    # 2. targets.json completeness
    targets_path = staging / TARGETS_NAME
    if not targets_path.is_file():
        add("E_TARGETS_MISSING", "targets.json was not staged", TARGETS_NAME)
    else:
        try:
            written_targets = json.loads(targets_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            add("E_TARGETS_JSON", f"targets.json does not parse: {exc}", TARGETS_NAME)
            written_targets = []
        actual = {str(item.get("filename")) for item in written_targets if isinstance(item, Mapping)}
        expected = {str(item["filename"]) for item in targets}
        if actual != expected:
            add("E_TARGETS_MISMATCH", f"targets.json mismatch: {sorted(actual ^ expected)}", TARGETS_NAME)
        for target in targets:
            output = staging / _override_name(str(target["filename"]))
            if not output.is_file():
                add("E_TARGET_OUTPUT_MISSING", f"missing override {output.name}", output.name)

    # 3. provider bundle + script references
    bundle_path = staging / PROVIDER_BUNDLE_NAME
    providers: Mapping[str, Any] = {}
    if not bundle_path.is_file():
        add("E_PROVIDER_BUNDLE_MISSING", "provider bundle was not staged", PROVIDER_BUNDLE_NAME)
    else:
        try:
            bundle_data = yaml.safe_load(bundle_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            add("E_PROVIDER_BUNDLE_YAML", f"provider bundle does not parse: {exc}", PROVIDER_BUNDLE_NAME)
            bundle_data = {}
        providers = bundle_data.get("script-providers") if isinstance(bundle_data, Mapping) else None
        if not isinstance(providers, Mapping):
            add("E_PROVIDER_BUNDLE_TYPE", "provider bundle has no script-providers mapping", PROVIDER_BUNDLE_NAME)
            providers = {}

    published_scripts = {
        relpath[len(SCRIPTS_DIRNAME) + 1 :]: meta
        for relpath, meta in stage.outputs.items()
        if str(relpath).startswith(SCRIPTS_DIRNAME + "/")
    }
    for name, info in providers.items():
        url = info.get("url") if isinstance(info, Mapping) else None
        if not isinstance(url, str):
            add("E_PROVIDER_URL", f"provider {name!r} has no url", PROVIDER_BUNDLE_NAME)
            continue
        if not url.startswith(public_base + "/"):
            if "kelee.one" in url:
                add("E_PROVIDER_KELEE_URL", f"provider {name!r} points at kelee.one: {url}", PROVIDER_BUNDLE_NAME)
            continue
        relpath = url[len(public_base) + 1 :]
        if not relpath.startswith(SCRIPTS_DIRNAME + "/"):
            add("E_PROVIDER_PATH", f"provider {name!r} URL is outside /scripts/: {url}", PROVIDER_BUNDLE_NAME)
            continue
        if relpath not in stage.outputs:
            add("E_PROVIDER_ASSET_MISSING", f"provider {name!r} references unpublished asset {relpath}", relpath)
            continue
        filename = relpath.rsplit("/", 1)[-1]
        if _HASHED_RELPATH_RE.match(relpath):
            prefix = filename.split("-", 1)[0]
            if not str(stage.outputs[relpath].get("sha256", "")).startswith(prefix):
                add("E_PROVIDER_ASSET_HASH", f"provider {name!r} asset hash prefix mismatch: {relpath}", relpath)

    override_files = sorted(p for p in staging.glob("*.stoverride") if p.name != PROVIDER_BUNDLE_NAME)
    for path in override_files:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            add("E_OVERRIDE_YAML", f"{path.name} does not parse: {exc}", path.name)
            continue
        if not isinstance(data, Mapping):
            add("E_OVERRIDE_ROOT", f"{path.name} root is not a mapping", path.name)
            continue
        if "script-providers" in data:
            add("E_OVERRIDE_INLINE_PROVIDERS", f"{path.name} must not inline script-providers", path.name)
        http = data.get("http")
        if isinstance(http, Mapping):
            for entry in http.get("script") or []:
                script_name = entry.get("name") if isinstance(entry, Mapping) else None
                if isinstance(script_name, str) and script_name not in providers:
                    add(
                        "E_SCRIPT_PROVIDER_MISSING",
                        f"{path.name} references provider {script_name!r} missing from the bundle",
                        path.name,
                    )

    # 4. resource map consistency
    for url, meta in sorted(resources.items()):
        relpath = meta.get("relpath")
        if not relpath:
            if not meta.get("sha256"):
                add("E_RESOURCE_HASH", f"resource {url} has no sha256", "")
            continue
        if relpath not in stage.outputs:
            add("E_RESOURCE_MISSING", f"resource {url} published path {relpath} is missing", relpath)
        legacy = meta.get("legacy_alias")
        if legacy and legacy not in stage.outputs:
            add("E_RESOURCE_ALIAS_MISSING", f"legacy alias {legacy} for {url} is missing", legacy)

    # 5. page invariants
    page_path = staging / PAGE_NAME
    if page_path.is_file():
        page_problems = page_validator(page_path.read_text(encoding="utf-8"), {})
        for problem in page_problems:
            add("E_PAGE", problem, PAGE_NAME)
    else:
        add("E_PAGE_MISSING", "index.html was not staged", PAGE_NAME)

    # 6. no stale default host / unresolved upstream asset URLs in text outputs
    for relpath in _text_outputs(stage.outputs):
        path = staging / relpath
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for host in legacy_hosts:
            if host and host.lower() in text.lower():
                add("E_LEGACY_HOST", f"{relpath} still references stale host {host!r}", relpath)
        if "kelee.one/Resource" in text:
            add("E_UPSTREAM_RESOURCE_URL", f"{relpath} still references an upstream kelee.one resource", relpath)

    return problems


# ---------------------------------------------------------------------------
# Page rendering adapter
# ---------------------------------------------------------------------------
def _render_stage_page(
    *,
    staging: Path,
    targets: Sequence[Mapping[str, Any]],
    outcomes: Sequence[ConversionOutcome],
    outputs: Mapping[str, Mapping[str, Any]],
    public_base: str,
    mirror_base: str,
    providers: Mapping[str, Any],
    fingerprint: str,
    generated_at: str,
    totals: Mapping[str, Any],
    page_module: Any,
) -> str:
    """Build page entries from conversion outcomes and render the landing page."""

    target_results = []
    for outcome in outcomes:
        stash = outcome.stash if isinstance(outcome.stash, Mapping) else {}
        target_results.append(
            {
                "filename": outcome.filename,
                "name": outcome.name,
                "desc": outcome.meta.get("desc") or stash.get("desc", ""),
                "icon": outcome.meta.get("icon") or stash.get("icon", ""),
                "author": outcome.meta.get("author", ""),
                "source_date": outcome.source_date,
                "source_sha256": outcome.source_sha256,
            }
        )
    manifest_like = {"target_results": target_results}
    entries = page_module.build_entries(staging, targets, manifest_like)
    for entry in entries:
        relpath = entry.get("stoverride")
        output_meta = outputs.get(relpath) or {}
        entry["output_sha256"] = str(output_meta.get("sha256", entry.get("output_sha256", "")))
    provider_info = {"count": len(providers)}
    return page_module.render_page(
        entries=entries,
        provider_info=provider_info,
        public_base=public_base,
        mirror_base=mirror_base,
        fingerprint=fingerprint,
        generated_at=generated_at,
        totals=dict(totals),
        schema=BUILD_MANIFEST_SCHEMA,
    )


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------
def promote_staging(
    staging: Path,
    out_dir: Path,
    *,
    outputs: Mapping[str, Mapping[str, Any]],
    log: Callable[[str], None] | None = None,
) -> list[str]:
    """Copy staged outputs into ``out_dir``; identical files are left untouched.

    Old content-addressed assets and legacy aliases are intentionally not
    deleted: overrides installed on a device keep resolving them until the
    publish/retention tooling removes unreferenced files.
    """

    staging = Path(staging)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for relpath, meta in sorted(outputs.items()):
        source = staging / relpath
        target = out_dir / relpath
        expected = str(meta.get("sha256", ""))
        if target.is_file() and expected and _sha256_file(target) == expected:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + f".tmp{os.getpid()}")
        shutil.copyfile(source, tmp)
        os.replace(tmp, target)
        written.append(relpath)
    if log:
        log(f"  promoted {len(written)}/{len(outputs)} file(s) into {out_dir}")
    return written


# ---------------------------------------------------------------------------
# Main build entry point
# ---------------------------------------------------------------------------
def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _timestamp(moment: _dt.datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    return moment.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_bases(
    public_base: str | None,
    mirror_base: str | None,
    *,
    require_explicit: bool = True,
) -> tuple[str, str]:
    """Resolve the single public base + mirror base through ``kelee_config``."""

    try:
        import kelee_config
    except ImportError:  # pragma: no cover - extremely early bring-up
        kelee_config = None  # type: ignore
    if kelee_config is not None:
        try:
            resolved = kelee_config.resolve_public_base(public_base, require=require_explicit)
        except TypeError:  # older/looser signature
            resolved = kelee_config.resolve_public_base(public_base or "")
        except Exception as exc:
            raise BuildError(f"cannot resolve public base: {exc}") from exc
        base = getattr(resolved, "base", resolved)
        mirror = getattr(resolved, "mirror_base", "") or base
    else:  # pragma: no cover - fallback used only if kelee_config is absent
        base = (public_base or os.environ.get("KELEE_PUBLIC_BASE") or "").strip().rstrip("/")
        if not base:
            raise BuildError(
                "no public base configured: pass --public-base or set KELEE_PUBLIC_BASE"
            )
        mirror = (mirror_base or os.environ.get("KELEE_MIRROR_BASE") or base).strip().rstrip("/")
    if mirror_base:
        try:
            mirror = kelee_config.normalize_base(mirror_base, what="mirror base") if kelee_config else mirror_base.rstrip("/")
        except Exception as exc:
            raise BuildError(f"cannot resolve mirror base: {exc}") from exc
    return str(base).rstrip("/"), str(mirror).rstrip("/")


def _default_targets_path(
    out_dir: Path | None = None, *, source_dir: Path | str | None = None
) -> Path:
    candidates = []
    if source_dir is not None:
        candidates.append(Path(source_dir) / TARGETS_NAME)
    if out_dir is not None:
        candidates.append(Path(out_dir) / TARGETS_NAME)
    candidates.extend([DEFAULT_TARGETS_PATH, REPO_ROOT / "stash" / "overrides" / "kelee" / TARGETS_NAME])
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return DEFAULT_TARGETS_PATH


def build_bundle(
    out_dir: Path | str = DEFAULT_OUT_DIR,
    *,
    public_base: str | None = None,
    mirror_base: str | None = None,
    targets: Sequence[Mapping[str, Any]] | None = None,
    targets_path: Path | str | None = None,
    source_dir: Path | str | None = None,
    snapshot_map: Mapping[str, Any] | None = None,
    resource_snapshots: Mapping[str, Any] | None = None,
    fetch: bool | None = None,
    force: bool = False,
    dry_run: bool = False,
    publish_legacy_static: bool | None = None,
    pipeline: Pipeline | None = None,
    now: Callable[[], _dt.datetime] | None = None,
    log: Callable[[str], None] | None = None,
) -> BuildResult:
    """Build (or verify up-to-date-ness of) the publishable KeLee bundle.

    Parameters
    ----------
    out_dir:
        Output directory. Only modified after conversion, integrity checks and
        validation succeeded; on failure it is left untouched.
    public_base / mirror_base:
        Explicit URLs; otherwise resolved from ``KELEE_PUBLIC_BASE`` /
        ``KELEE_MIRROR_BASE`` / ``STASH_PUBLIC_HOST`` via ``kelee_config``.
        Publish builds require an explicit/env base (``require=True``).
    targets / targets_path:
        Target list (``[{filename, name, dependency?}]``) or a JSON file.
    source_dir:
        Directory containing the ``.lpx`` snapshots for offline builds. When
        given and ``resource_snapshots`` is omitted, mirrored resources are
        replayed from the same directory via ``kelee_assets.load_snapshot_map``.
    snapshot_map:
        Explicit ``{filename: text|bytes|Path}`` mapping for the ``.lpx``
        sources (overrides ``source_dir`` per file).
    resource_snapshots:
        Explicit ``{url: text|bytes|Path}`` mapping for the mirrored resources.
    fetch:
        Allow live HTTP reads. Defaults to true only when no offline sources
        were supplied.
    force:
        Rebuild even when the fingerprint matches and the outputs are intact.
    dry_run:
        Do everything except writing into ``out_dir``; report the plan.

    Returns
    -------
    BuildResult with ``status`` ``no-change`` / ``built`` / ``dry-run`` and the
    process exit code (0 / 2 / and 1 only via :class:`BuildError`).
    """

    emit = log if log is not None else (lambda message: None)
    pipeline = pipeline or DefaultPipeline()
    out_dir = Path(out_dir)
    now = now or _utc_now
    warnings: list[Diagnostic] = []

    # ---- inputs ---------------------------------------------------------
    if targets is None:
        targets_path_resolved = (
            Path(targets_path)
            if targets_path
            else _default_targets_path(out_dir, source_dir=source_dir)
        )
        targets = load_targets(targets_path_resolved)
        emit(f"targets: {len(targets)} from {targets_path_resolved}")
    else:
        targets = list(targets)
        if targets_path is None:
            targets_path_resolved = _default_targets_path(out_dir, source_dir=source_dir)
        else:
            targets_path_resolved = Path(targets_path)
        emit(f"targets: {len(targets)} (provided in memory)")
    if not targets:
        raise BuildError("target list is empty")

    base, mirror = _resolve_bases(public_base, mirror_base)
    emit(f"public base: {base}" + (f" (mirror: {mirror})" if mirror != base else ""))

    offline = snapshot_map is not None or source_dir is not None or resource_snapshots is not None
    use_fetch = (not offline) if fetch is None else bool(fetch)
    if publish_legacy_static is None:
        publish_legacy_static = use_fetch

    sources = load_sources(
        targets,
        snapshot_map=snapshot_map,
        source_dir=source_dir,
        fetch=use_fetch,
        log=emit,
    )
    resource_map = load_resource_snapshots(source_dir=source_dir, resource_snapshots=resource_snapshots)
    if offline:
        emit(f"offline sources: {len(resource_map)} mirrored resource snapshot(s)")
    # Metadata-only view of the sources: the LPX bodies stay in memory and must
    # never leak into the manifest / JSON report.
    source_meta = {
        str(name): {
            "sha256": str(meta["sha256"]),
            "date": str(meta.get("date", "")),
            "url": str(meta.get("url", "")),
        }
        for name, meta in sources.items()
    }

    # ---- conversion -----------------------------------------------------
    resolver = pipeline.make_resolver(
        public_base=base,
        mirror_base=mirror,
        fetch=use_fetch,
        resource_snapshots=resource_map,
    )
    outcomes: list[ConversionOutcome] = []
    conversion_errors: list[Diagnostic] = []
    for target in targets:
        filename = str(target["filename"])
        source = sources[filename]
        try:
            raw = pipeline.convert(
                str(source["text"]),
                str(source["url"]),
                resolver=resolver,
                public_base=base,
            )
        except BuildError:
            raise
        except Exception as exc:  # noqa: BLE001 - convert every target, then report
            conversion_errors.append(
                Diagnostic(
                    "E_CONVERSION_CRASH",
                    f"{type(exc).__name__}: {exc}",
                    filename,
                    True,
                    "error",
                    "converter",
                )
            )
            continue
        outcome = _coerce_conversion_result(raw, target=target, source=source)
        outcomes.append(outcome)
        conversion_errors.extend(_blocking(outcome.diagnostics))
        if not _blocking(outcome.diagnostics):
            emit(
                f"  converted {filename} "
                f"({', '.join(f'{k}={v}' for k, v in sorted(_int_counts(outcome.counts).items()))})"
            )
    if conversion_errors:
        raise BuildError("conversion failed", conversion_errors)

    # ---- legacy static assets (old installed overrides) -----------------
    if publish_legacy_static:
        warnings.extend(_publish_legacy_static(resolver, emit=emit))

    # ---- resources + fingerprint ---------------------------------------
    assets = _collect_assets(resolver, warnings=warnings)
    resources = _merge_resources(assets, outcomes)
    upstream_digest = ""
    try:
        digest_fn = getattr(resolver, "upstream_digest", None)
        if callable(digest_fn):
            upstream_digest = str(digest_fn())
    except Exception as exc:  # noqa: BLE001 - digest is diagnostic, not fatal
        warnings.append(
            Diagnostic("W_UPSTREAM_DIGEST", f"cannot compute upstream digest: {exc}", blocking=False, severity="warning", source="build")
        )
    pipeline_inputs = dict(pipeline.fingerprint_inputs)
    fingerprint_inputs = build_fingerprint_inputs(
        public_base=base,
        mirror_base=mirror,
        schema=BUILD_MANIFEST_SCHEMA,
        targets_digest=_targets_digest(targets),
        sources=source_meta,
        resources=resources,
        pipeline_inputs=pipeline_inputs,
        upstream_digest=upstream_digest,
    )
    fingerprint = compute_fingerprint(fingerprint_inputs)
    emit(f"fingerprint: {fingerprint[:16]} ({len(resources)} resource(s), {len(assets['published'])} asset file(s))")

    previous = _previous_manifest(out_dir)
    previous_fingerprint = str(previous.get("fingerprint", "")) if previous else None
    reasons: list[str] = []
    if previous is None:
        reasons.append("no previous build manifest")
    elif previous_fingerprint != fingerprint:
        reasons.extend(
            _diff_fingerprint_inputs(previous.get("fingerprint_inputs") or {}, fingerprint_inputs)
            or ["build fingerprint changed"]
        )
    corrupt_outputs = check_existing_outputs(out_dir, previous) if previous else []
    if corrupt_outputs:
        reasons.extend(corrupt_outputs)
    changed = bool(force or reasons)

    if not changed:
        # ensure the legacy state file exists/reflects the current manifest
        if previous is not None:
            write_state(out_dir, manifest=previous, sources=source_meta)
        emit(f"no change (fingerprint {fingerprint[:16]}); bundle already up to date")
        totals = dict((previous or {}).get("totals") or {})
        return BuildResult(
            status="no-change",
            changed=False,
            exit_code=EXIT_OK,
            fingerprint=fingerprint,
            previous_fingerprint=previous_fingerprint,
            out_dir=str(out_dir),
            reasons=[],
            manifest=previous,
            sources=source_meta,
            resources=resources,
            outputs=dict((previous or {}).get("outputs") or {}),
            target_results=list((previous or {}).get("target_results") or []),
            totals=totals,
            warnings=warnings,
        )

    if force and not reasons:
        reasons = ["--force rebuild requested"]
    emit("rebuild needed: " + "; ".join(reasons[:6]) + ("" if len(reasons) <= 6 else f" (+{len(reasons) - 6} more)"))

    # ---- staging build --------------------------------------------------
    import generate_kelee_html as page_module

    previous_providers = _read_previous_providers(out_dir)
    generated_at = _timestamp(now())

    with tempfile.TemporaryDirectory(prefix="kelee-build-") as tmp:
        staging = Path(tmp)
        stage = _write_bundle(
            staging,
            outcomes=outcomes,
            targets=targets,
            public_base=base,
            fingerprint=fingerprint,
            assets=assets,
            previous_providers=previous_providers,
        )
        stage.warnings.extend(warnings)
        totals = _aggregate_totals(outcomes, stage, resources)
        totals["outputs"] = int(totals.get("outputs", 0)) + 1  # + index.html
        page = _render_stage_page(
            staging=staging,
            targets=targets,
            outcomes=outcomes,
            outputs=stage.outputs,
            public_base=base,
            mirror_base=mirror,
            providers=stage.providers,
            fingerprint=fingerprint,
            generated_at=generated_at,
            totals=totals,
            page_module=page_module,
        )
        _safe_write(staging, PAGE_NAME, page.encode("utf-8"))
        stage.outputs[PAGE_NAME] = {
            "sha256": _sha256_text(page),
            "size": len(page.encode("utf-8")),
            "counts": {"entries": len(outcomes)},
        }

        # diagnostics: integration patches (converter decisions) vs warnings
        patches: list[dict] = []
        enhancements: list[dict] = []
        for outcome in outcomes:
            for item in outcome.patches:
                patch = dict(item) if isinstance(item, Mapping) else {"detail": str(item)}
                patch.setdefault("filename", outcome.filename)
                patches.append(patch)
            for item in outcome.enhancements:
                enhancement = dict(item) if isinstance(item, Mapping) else {"detail": str(item)}
                enhancement.setdefault("filename", outcome.filename)
                enhancements.append(enhancement)
            for diagnostic in outcome.diagnostics:
                code = str(getattr(diagnostic, "code", ""))
                if not getattr(diagnostic, "blocking", True) and (
                    "PATCH" in code.upper() or "CONFLICT" in code.upper()
                ) and not outcome.patches:
                    patches.append(
                        {
                            "filename": outcome.filename,
                            "kind": code,
                            "detail": getattr(diagnostic, "message", str(diagnostic)),
                        }
                    )

        integrity_problems = verify_staged_bundle(
            staging,
            stage=stage,
            targets=targets,
            public_base=base,
            resources=resources,
            legacy_hosts=_legacy_hosts(base),
            page_validator=_page_validator(page_module, public_base=base, providers=len(stage.providers), entries=len(outcomes), fingerprint=fingerprint),
            fingerprint=fingerprint,
        )
        if integrity_problems:
            raise BuildError("bundle integrity check failed", integrity_problems)

        asset_hashes = {
            relpath: str(meta.get("sha256", ""))
            for relpath, meta in stage.outputs.items()
            if str(relpath).startswith(SCRIPTS_DIRNAME + "/")
        }
        validation = pipeline.validate(
            staging,
            expected_targets=[str(target["filename"]) for target in targets],
            public_base=base,
            assets=asset_hashes,
        )
        validation_diagnostics = _normalize_diagnostics(validation, source="validator")
        blocking = _blocking(validation_diagnostics)
        if blocking:
            raise BuildError("bundle validation failed", blocking)
        warnings.extend(d for d in validation_diagnostics if not getattr(d, "blocking", True))
        warnings.extend(stage.warnings)

        manifest = _build_manifest(
            public_base=base,
            mirror_base=mirror,
            fingerprint=fingerprint,
            fingerprint_inputs=fingerprint_inputs,
            generated_at=generated_at,
            sources=source_meta,
            resources=resources,
            outputs=stage.outputs,
            outcomes=outcomes,
            stage=stage,
            totals=totals,
            patches=patches,
            enhancements=enhancements,
            warnings=warnings,
            upstream_digest=upstream_digest,
        )

        if dry_run:
            emit(
                f"dry run: would build {len(stage.outputs)} output(s), "
                f"{len(stage.providers)} provider(s), {len(resources)} resource(s)"
            )
            return BuildResult(
                status="dry-run",
                changed=True,
                exit_code=EXIT_CHANGED,
                fingerprint=fingerprint,
                previous_fingerprint=previous_fingerprint,
                out_dir=str(out_dir),
                reasons=reasons,
                manifest=manifest,
                sources=source_meta,
                resources=resources,
                outputs=stage.outputs,
                target_results=manifest["target_results"],
                totals=totals,
                warnings=warnings,
            )

        # ---- promote ----------------------------------------------------
        promote_staging(staging, out_dir, outputs=stage.outputs, log=emit)
        post_problems = check_existing_outputs(out_dir, manifest)
        if post_problems:
            raise BuildError("promoted bundle failed post-write verification", [
                Diagnostic("E_POST_PROMOTE", problem, blocking=True, severity="error", source="build")
                for problem in post_problems
            ])
        _write_json_atomic(out_dir / MANIFEST_NAME, manifest)
        write_state(out_dir, manifest=manifest, sources=source_meta)

    emit(
        f"built bundle: {len(stage.outputs)} output(s), {len(stage.providers)} provider(s), "
        f"{len(resources)} resource(s), fingerprint {fingerprint[:16]}"
    )
    return BuildResult(
        status="built",
        changed=True,
        exit_code=EXIT_CHANGED,
        fingerprint=fingerprint,
        previous_fingerprint=previous_fingerprint,
        out_dir=str(out_dir),
        reasons=reasons,
        manifest=manifest,
        sources=source_meta,
        resources=resources,
        outputs=stage.outputs,
        target_results=manifest["target_results"],
        totals=totals,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Asset collection / merging
# ---------------------------------------------------------------------------
def _publish_legacy_static(resolver: Any, *, emit: Callable[[str], None]) -> list[Diagnostic]:
    """Publish historical static scripts so pre-migration overrides keep working."""

    warnings: list[Diagnostic] = []
    try:
        import kelee_config

        mapping = dict(getattr(kelee_config, "LEGACY_STATIC_ASSETS", {}) or {})
    except ImportError:  # pragma: no cover
        mapping = {}
    if not mapping:
        return warnings
    fetch_bytes = getattr(resolver, "fetch_bytes", None)
    publish_bytes = getattr(resolver, "publish_bytes", None)
    if not callable(fetch_bytes) or not callable(publish_bytes):
        return warnings
    for logical_name, url in sorted(mapping.items()):
        try:
            content = fetch_bytes(url)
            _call_with_supported_kwargs(
                publish_bytes,
                content,
                logical_name=logical_name,
                source_url=url,
                kind="legacy-static",
                legacy_alias=True,
            )
            emit(f"  mirrored legacy static asset {logical_name}")
        except Exception as exc:  # noqa: BLE001 - optional retention asset
            warnings.append(
                Diagnostic(
                    "W_LEGACY_STATIC_UNAVAILABLE",
                    f"legacy static asset {logical_name} unavailable: {exc}",
                    logical_name,
                    False,
                    "warning",
                    "build",
                )
            )
    return warnings


def _collect_assets(resolver: Any, *, warnings: list[Diagnostic]) -> dict:
    published_fn = getattr(resolver, "published_assets", None)
    manifest_fn = getattr(resolver, "asset_manifest", None)
    if not callable(published_fn) or not callable(manifest_fn):
        raise BuildError(
            "resource resolver does not implement published_assets()/asset_manifest(); "
            "cannot publish content-addressed assets"
        )
    published = dict(published_fn() or {})
    records = [_to_asset_record(record) for record in (manifest_fn() or [])]
    return {"published": published, "records": records}


def _to_asset_record(record: Any) -> dict:
    if isinstance(record, Mapping):
        return dict(record)
    return {
        "relpath": getattr(record, "relpath", ""),
        "sha256": getattr(record, "sha256", ""),
        "size": getattr(record, "size", None),
        "kind": getattr(record, "kind", "script"),
        "source_url": getattr(record, "source_url", ""),
        "legacy_relpath": getattr(record, "legacy_relpath", ""),
    }


def _merge_resources(assets: Mapping[str, Any], outcomes: Sequence[ConversionOutcome]) -> dict[str, dict]:
    """Published assets first, then non-published (inlined jq) dependencies."""

    merged: dict[str, dict] = {}
    for record in assets.get("records", []):
        relpath = str(record.get("relpath") or "")
        legacy = str(record.get("legacy_relpath") or "")
        if not relpath:
            continue
        hashed = bool(_HASHED_RELPATH_RE.match(relpath.replace("\\", "/")))
        if not hashed and not legacy:
            # legacy alias row of an already-recorded primary asset
            continue
        source_url = str(record.get("source_url") or "")
        if not source_url or source_url in merged:
            continue
        merged[source_url] = {
            "sha256": str(record.get("sha256", "")),
            "relpath": relpath,
            "kind": str(record.get("kind", "script")),
            "legacy_alias": legacy or None,
            "size": record.get("size"),
        }
    for outcome in outcomes:
        for url, info in (outcome.upstream_resources or {}).items():
            url = str(url)
            if url in merged:
                continue
            sha = ""
            kind = "jq" if url.rsplit("?", 1)[0].endswith(".jq") else "resource"
            if isinstance(info, Mapping):
                sha = str(info.get("sha256", ""))
                kind = str(info.get("kind") or kind)
            if not sha:
                continue
            merged[url] = {
                "sha256": sha,
                "relpath": None,
                "kind": kind,
                "legacy_alias": None,
                "size": info.get("bytes") if isinstance(info, Mapping) else None,
            }
    return dict(sorted(merged.items()))


# ---------------------------------------------------------------------------
# Page validation adapter
# ---------------------------------------------------------------------------
def _page_validator(page_module: Any, *, public_base: str, providers: int, entries: int, fingerprint: str) -> Callable[[str, dict], list[str]]:
    def validate(page_text: str, _extra: Mapping[str, Any]) -> list[str]:
        issues = page_module.validate_page(
            page_text,
            public_base=public_base,
            fingerprint=fingerprint,
            provider_count=providers,
            entry_count=entries,
        )
        return list(issues)

    return validate


def _legacy_hosts(public_base: str) -> list[str]:
    try:
        import kelee_config

        default_base = str(getattr(kelee_config, "DEFAULT_PUBLIC_BASE", ""))
    except ImportError:  # pragma: no cover
        default_base = ""
    host = ""
    if default_base.startswith(("http://", "https://")):
        host = default_base.split("://", 1)[1].split("/", 1)[0]
    base_host = public_base.split("://", 1)[-1].split("/", 1)[0]
    if not host or host.lower() == base_host.lower():
        return []
    return [host]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _write_json_report(path: str, report: Mapping[str, Any]) -> None:
    if not path:
        return
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(destination, report)


def _print_diagnostics(diagnostics: Iterable[Diagnostic]) -> None:
    for diagnostic in diagnostics:
        location = f" [{diagnostic.path}]" if getattr(diagnostic, "path", "") else ""
        print(f"  ERROR {diagnostic.code}{location}: {diagnostic.message}", file=sys.stderr)


def main(argv: Sequence[str] | None = None, *, pipeline: "Pipeline | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build, verify and record the publishable KeLee Stash bundle",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "exit codes: 0 = up to date (or dry-run up to date), "
            "2 = bundle built/publishable, 1 = failure"
        ),
    )
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="output bundle directory")
    parser.add_argument("--public-base", default=None, help="public URL base (default: env KELEE_PUBLIC_BASE)")
    parser.add_argument("--mirror-base", default=None, help="optional mirror base (default: env KELEE_MIRROR_BASE)")
    parser.add_argument("--targets", default=None, help="targets.json path (default: <out-dir>/targets.json)")
    parser.add_argument("--snapshot-dir", default=None, help="offline directory with .lpx snapshots + resource mapping")
    parser.add_argument(
        "--audit-snapshot",
        action="store_true",
        help=f"replay the local audit snapshots in {AUDIT_SNAPSHOT_DIR} (offline, requires .local/)",
    )
    parser.add_argument("--force", action="store_true", help="rebuild even when the fingerprint matches")
    parser.add_argument("--dry-run", action="store_true", help="convert + validate in a temp dir, write nothing")
    parser.add_argument("--json-report", default=None, help="write a machine-readable build report")
    parser.add_argument("--no-legacy-static", action="store_true", help="do not mirror historical static assets")
    parser.add_argument("--quiet", action="store_true", help="suppress progress output")
    args = parser.parse_args(argv)

    emit = (lambda message: None) if args.quiet else (lambda message: print(message, flush=True))
    out_dir = Path(args.out_dir).expanduser().resolve()
    state = {"status": "failure", "exit_code": EXIT_FAILURE, "fingerprint": "", "errors": []}

    source_dir = None
    resource_snapshots = None
    snapshot_map = None
    if args.audit_snapshot:
        if not AUDIT_SNAPSHOT_DIR.is_dir():
            print(
                f"audit snapshot directory not found: {AUDIT_SNAPSHOT_DIR}",
                file=sys.stderr,
            )
            _write_json_report(args.json_report, state)
            return EXIT_FAILURE
        source_dir = AUDIT_SNAPSHOT_DIR
        try:
            from kelee_assets import load_audit_snapshot_map

            resource_snapshots = load_audit_snapshot_map(REPO_ROOT)
        except Exception as exc:  # noqa: BLE001
            print(f"cannot load audit snapshot map: {exc}", file=sys.stderr)
            _write_json_report(args.json_report, state)
            return EXIT_FAILURE
        emit(f"offline audit replay: {source_dir} ({len(resource_snapshots)} resources)")
    elif args.snapshot_dir:
        source_dir = Path(args.snapshot_dir).expanduser().resolve()
        if not source_dir.is_dir():
            print(f"snapshot directory not found: {source_dir}", file=sys.stderr)
            _write_json_report(args.json_report, state)
            return EXIT_FAILURE

    try:
        result = build_bundle(
            out_dir,
            public_base=args.public_base,
            mirror_base=args.mirror_base,
            targets_path=args.targets,
            source_dir=source_dir,
            snapshot_map=snapshot_map,
            resource_snapshots=resource_snapshots,
            fetch=False if (source_dir is not None) else None,
            force=args.force,
            dry_run=args.dry_run,
            publish_legacy_static=False if args.no_legacy_static else None,
            pipeline=pipeline,
            log=emit,
        )
    except BuildError as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        _print_diagnostics(exc.diagnostics)
        report = dict(state)
        report["errors"] = [_diag_dict(d) for d in exc.diagnostics]
        _write_json_report(args.json_report, report)
        return EXIT_FAILURE
    except Exception as exc:  # noqa: BLE001 - never crash with a traceback-only failure
        print(f"build failed unexpectedly: {type(exc).__name__}: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        report = dict(state)
        report["errors"] = [
            {"code": "E_BUILD_CRASH", "message": f"{type(exc).__name__}: {exc}", "blocking": True, "severity": "error"}
        ]
        _write_json_report(args.json_report, report)
        return EXIT_FAILURE

    report = result.as_report()
    report["status"] = result.status
    _write_json_report(args.json_report, report)
    if result.status == "no-change":
        print("no update: bundle is up to date (exit 0)", flush=True)
    elif result.status == "dry-run":
        if result.changed:
            print("dry run: changes would be published (exit 2)", flush=True)
        else:
            print("dry run: nothing to do (exit 0)", flush=True)
    else:
        print(
            f"bundle built: {result.totals.get('targets', 0)} target(s), "
            f"{result.totals.get('providers', 0)} provider(s), "
            f"{len(result.outputs)} output(s); publish it now (exit 2)",
            flush=True,
        )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
