#!/usr/bin/env python3
"""Deterministic fixture pipeline for build-orchestrator tests.

This is **not** the production converter. It implements the same small contract
the build orchestrator expects (``Pipeline`` in ``scripts/check_kelee_update.py``)
so the orchestration logic — content-addressed publishing, nested runtime asset
mirroring, manifest/fingerprint/state handling, validation gating and page
rendering — can be tested offline and deterministically. It uses the *real*
``kelee_assets.ResourceResolver`` and the *real* ``validate_stash.validate_bundle``
so those integrations are exercised too; only the Loon→Stash translation step is
a deliberately tiny stand-in.

Fixture LPX subset used by the fixtures in this directory:

* ``#!name=`` / ``#!desc=`` / ``#!author=`` / ``#!icon=`` / ``#!homepage=`` /
  ``#!date=`` header lines;
* ``[Rule]`` — copied verbatim (``DOMAIN,ads.example.test,REJECT``);
* ``[MitM]`` — ``hostname=a, b``;
* ``[Script]`` — ``http-response REGEX script-path=URL, requires-body=true``;
* ``[Rewrite]`` — ``<phase> if ${url} ~= /regex/flags then
  response.json.jq_file("URL")``, ``... then response.json.jq("expr")`` or
  ``... then reject_dict(200)``.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import check_kelee_update as build
from kelee_diagnostics import Diagnostic, DiagnosticCollection

FIXTURE_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = build.SCRIPTS_DIR

HEADER_RE = re.compile(r"^#!\s*([^=]+)=(.*)$")
SECTION_RE = re.compile(r"^\[(.+)\]$")
SCRIPT_RE = re.compile(r"^(http-(response|request))\s+(\S+)\s+(.+)$")
EXPR_RE = re.compile(
    r"^(response|request)\s+if\s+\$\{url\}\s*~=\s*/(.*?)/([a-z]*)\s+then\s+(.+)$"
)
JQ_FILE_RE = re.compile(r'^(?:response|request)\.json\.jq_file\("(.+)"\)$')
JQ_INLINE_RE = re.compile(r'^(?:response|request)\.json\.jq\("(.*)"\)$', re.S)
REJECT_RE = re.compile(r"^reject(?:_dict|_array|_img)?\((\d+)\)$")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stash_regex(pattern: str, flags: str) -> str:
    unsupported = set(flags) - {"i", "m", "s"}
    if unsupported:
        raise ValueError(f"unsupported Loon regex flags: {sorted(unsupported)}")
    escaped = pattern.replace(r"\/", "/")
    prefix = f"(?{''.join(sorted(set(flags)))})" if flags else ""
    return prefix + escaped


def provider_slug(url: str) -> str:
    stem = url.rsplit("/", 1)[-1].rsplit("?", 1)[0]
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    slug = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").lower()
    return slug or "script"


def load_fixture_targets() -> list[dict]:
    return build.load_targets(FIXTURE_DIR / "targets.json")


def load_fixture_snapshots() -> dict[str, bytes]:
    """URL→bytes replay map for this fixture bundle (real kelee_assets loader)."""

    from kelee_assets import load_snapshot_map

    return load_snapshot_map(FIXTURE_DIR)


class FixturePipeline(build.DefaultPipeline):
    """Mini converter + real resolver/validator, with failure injection hooks."""

    code_revision = "1"

    def __init__(
        self,
        *,
        fail_targets: tuple[str, ...] = (),
        drop_provider_for: tuple[str, ...] = (),
        validator_failures: tuple[Diagnostic, ...] = (),
        mutation_marker: str = "",
    ) -> None:
        super().__init__()
        self.fail_targets = set(fail_targets)
        self.drop_provider_for = set(drop_provider_for)
        self.validator_failures = list(validator_failures)
        self.mutation_marker = mutation_marker

    # -- fingerprint -----------------------------------------------------
    @property
    def fingerprint_inputs(self) -> dict[str, str]:
        inputs = {
            "fixture_pipeline.py": f"fixture-pipeline-{self.code_revision}-{self.mutation_marker}"
        }
        for name in ("kelee_assets.py", "kelee_config.py", "validate_stash.py"):
            path = SCRIPTS_DIR / name
            inputs[name] = _sha256_file(path) if path.is_file() else "absent"
        return inputs

    # -- conversion ------------------------------------------------------
    def convert(self, lpx_text: str, lpx_url: str, *, resolver, public_base: str):
        filename = lpx_url.rsplit("/", 1)[-1] or "fixture.lpx"
        header: dict[str, str] = {}
        sections: dict[str, list[str]] = {}
        current: str | None = None
        for raw_line in lpx_text.splitlines():
            line = raw_line.rstrip()
            header_match = HEADER_RE.match(line.strip())
            if header_match and current is None:
                header[header_match.group(1).strip()] = header_match.group(2).strip()
                continue
            section_match = SECTION_RE.match(line.strip())
            if section_match:
                current = section_match.group(1).strip()
                sections.setdefault(current, [])
                continue
            if current is not None:
                sections[current].append(line)

        diagnostics: list[Diagnostic] = []
        providers: dict[str, dict] = {}
        scripts: list[dict] = []
        body_rewrite: list[str] = []
        url_rewrite: list[str] = []
        upstream_resources: dict[str, dict] = {}
        counts = {
            "rules": 0,
            "url_rewrite": 0,
            "body_rewrite": 0,
            "mock": 0,
            "script": 0,
            "providers": 0,
            "mitm": 0,
            "header_rewrite": 0,
            "arg_entries": 0,
            "unsupported": 0,
        }

        rules = [line.strip() for line in sections.get("Rule", []) if line.strip() and not line.strip().startswith("#")]
        counts["rules"] = len(rules)

        mitm_hosts: list[str] = []
        for line in sections.get("MitM", []):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            payload = stripped.split("=", 1)[1] if "=" in stripped else stripped
            mitm_hosts.extend(host.strip() for host in payload.split(",") if host.strip())
        counts["mitm"] = len(mitm_hosts)

        for line in sections.get("Script", []):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = SCRIPT_RE.match(stripped)
            if not match:
                diagnostics.append(
                    Diagnostic("E_FIXTURE_SCRIPT", f"unsupported script line: {stripped[:120]}", filename)
                )
                counts["unsupported"] += 1
                continue
            phase, _, script_regex, rest = match.groups()
            options: dict[str, str] = {}
            for token in rest.split(","):
                token = token.strip()
                if "=" in token:
                    key, _, value = token.partition("=")
                    options[key.strip()] = value.strip()
            script_url = options.get("script-path", "")
            if not script_url:
                diagnostics.append(Diagnostic("E_FIXTURE_SCRIPT", "script line has no script-path", filename))
                continue
            asset = resolver.publish_script(script_url)
            name = provider_slug(script_url)
            providers[name] = {"url": asset.url, "interval": 86400}
            entry = {
                "name": name,
                "type": "response" if phase == "http-response" else "request",
                "match": stash_regex(script_regex, options.get("flags", "")),
            }
            if options.get("requires-body", "").lower() == "true":
                entry["require-body"] = True
            cur_counts = counts
            cur_counts["script"] += 1
            scripts.append(entry)

        for line in sections.get("Rewrite", []):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = EXPR_RE.match(stripped)
            if not match:
                diagnostics.append(
                    Diagnostic("E_FIXTURE_REWRITE", f"unsupported rewrite line: {stripped[:120]}", filename)
                )
                counts["unsupported"] += 1
                continue
            phase, pattern, flags, action = match.groups()
            regex = stash_regex(pattern, flags)
            jq_file = JQ_FILE_RE.match(action)
            if jq_file:
                jq_url = jq_file.group(1)
                raw = resolver.fetch_bytes(jq_url)
                upstream_resources[jq_url] = {
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "bytes": len(raw),
                    "kind": "jq",
                }
                expression = raw.decode("utf-8").strip()
                line_action = "request-jq" if phase == "request" else "response-jq"
                body_rewrite.append(f"{regex} {line_action} {expression}")
                counts["body_rewrite"] += 1
                continue
            jq_inline = JQ_INLINE_RE.match(action)
            if jq_inline:
                expression = jq_inline.group(1).replace(r"\"", '"')
                line_action = "request-jq" if phase == "request" else "response-jq"
                body_rewrite.append(f"{regex} {line_action} {expression}")
                counts["body_rewrite"] += 1
                continue
            reject = REJECT_RE.match(action)
            if reject:
                status = int(reject.group(1))
                if status == 200 and "dict" in action:
                    mapped = "reject-dict"
                elif status == 200:
                    mapped = "reject-200"
                elif status == 404:
                    mapped = "reject"
                else:
                    mapped = "reject"
                url_rewrite.append(f"{regex} - {mapped}")
                counts["url_rewrite"] += 1
                continue
            diagnostics.append(
                Diagnostic("E_FIXTURE_REWRITE", f"unsupported rewrite action: {action[:120]}", filename)
            )
            counts["unsupported"] += 1

        stash: dict = {
            "name": header.get("name", filename[:-4]),
        }
        if header.get("desc"):
            stash["desc"] = header["desc"].replace("\\n", " ")
        if header.get("icon"):
            stash["icon"] = header["icon"]
        http: dict = {}
        if scripts:
            http["script"] = scripts
        if body_rewrite:
            http["body-rewrite"] = body_rewrite
        if url_rewrite:
            http["url-rewrite"] = url_rewrite
        if mitm_hosts:
            http["mitm"] = mitm_hosts
        if http:
            stash["http"] = http
        if rules:
            stash["rules"] = rules
        if providers:
            stash["script-providers"] = providers

        filename = lpx_url.rsplit("/", 1)[-1]
        if filename in self.fail_targets:
            diagnostics.append(
                Diagnostic(
                    "E_FIXTURE_INJECTED",
                    "injected conversion failure for retry tests",
                    filename,
                    True,
                    "error",
                    "converter",
                )
            )
        if filename in self.drop_provider_for:
            stash.pop("script-providers", None)

        if self.mutation_marker:
            # Deterministic hook to prove converter-output-only changes are
            # fingerprinted even when every upstream source is identical.
            stash["desc"] = f"{stash.get('desc', '')} [{self.mutation_marker}]".strip()

        counts["providers"] = len(stash.get("script-providers") or {})
        return build.ConversionOutcome(
            filename=filename,
            name=stash.get("name", filename),
            stash=stash,
            diagnostics=diagnostics,
            counts=counts,
            source_sha256=hashlib.sha256(lpx_text.encode("utf-8")).hexdigest(),
            source_date=header.get("date", ""),
            source_url=lpx_url,
            upstream_resources=upstream_resources,
            meta=header,
        )

    # -- validation ------------------------------------------------------
    def validate(self, bundle_dir, *, expected_targets, public_base, assets):
        if self.validator_failures:
            return DiagnosticCollection(list(self.validator_failures))
        return super().validate(
            bundle_dir,
            expected_targets=expected_targets,
            public_base=public_base,
            assets=assets,
        )
