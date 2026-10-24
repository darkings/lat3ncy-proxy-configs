#!/usr/bin/env python3
"""Offline tests for the KeLee build orchestrator (scripts/check_kelee_update.py).

Covers item 9 (content-addressed script publishing incl. nested runtime assets
and legacy aliases), item 10 (fingerprint based update detection for LPX,
dependency, converter and configuration changes plus missing/corrupt outputs and
failure retry) and the build/page side of item 12 (single public base, no
hard-coded host, dynamic page data).

Everything runs against `tests/fixtures/build/` with the real
`kelee_assets.ResourceResolver` and the real `validate_stash.validate_bundle`:
no network, no upstream scripts and no writes outside temporary directories.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "build"
for path in (str(SCRIPTS_DIR), str(FIXTURE_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import yaml

import check_kelee_update as build
import fixture_pipeline
import generate_kelee_html as page
from kelee_diagnostics import Diagnostic

BASE = "https://stash.example.test"
CUSTOM_BASE = "https://kelee-mirror.example.test"
FIXED_NOW = _dt.datetime(2026, 10, 25, 0, 0, 0, tzinfo=_dt.timezone.utc)
HASHED_SCRIPTS_RE = r"^https://stash\.example\.test/scripts/[0-9a-f]{12}-[A-Za-z0-9._-]+\.js$"
DEFAULT_HOST = "stash.invalid"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tree_hashes(root: Path) -> dict[str, str]:
    root = Path(root)
    if not root.exists():
        return {}
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = _sha256_bytes(path.read_bytes())
    return result


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_manifest(out_dir: Path) -> dict:
    return read_json(Path(out_dir) / ".build-manifest.json")


def read_state(out_dir: Path) -> dict:
    return read_json(Path(out_dir) / ".hashes.json")


def fixture_snapshots(**overrides) -> dict[str, bytes]:
    snapshots = fixture_pipeline.load_fixture_snapshots()
    snapshots.update({url: (value.encode("utf-8") if isinstance(value, str) else value) for url, value in overrides.items()})
    return snapshots


def provider_urls(out_dir: Path) -> dict[str, str]:
    data = yaml.safe_load((Path(out_dir) / "kelee-scripts.stoverride").read_text(encoding="utf-8"))
    return {name: info["url"] for name, info in data["script-providers"].items()}


class BuildTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="kelee-build-test-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.out_dir = self.root / "bundle"

    # -- build helpers ---------------------------------------------------
    def run_build(self, *, out_dir: Path | None = None, pipeline=None, **overrides):
        kwargs: dict = {
            "public_base": BASE,
            "source_dir": FIXTURE_DIR,
            "fetch": False,
            "pipeline": pipeline if pipeline is not None else fixture_pipeline.FixturePipeline(),
            "now": lambda: FIXED_NOW,
        }
        kwargs.update(overrides)
        return build.build_bundle(out_dir or self.out_dir, **kwargs)

    def run_cli(self, argv: list[str], *, pipeline=None) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = build.main(argv, pipeline=pipeline if pipeline is not None else fixture_pipeline.FixturePipeline())
        return code, stdout.getvalue(), stderr.getvalue()

    def all_text_outputs(self, out_dir: Path) -> dict[str, str]:
        """Text artefacts a client consumes; manifests/state are diagnostics only."""

        skip = {".build-manifest.json", ".hashes.json"}
        result: dict[str, str] = {}
        for path in sorted(Path(out_dir).rglob("*")):
            if not path.is_file() or path.suffix == ".js":
                continue
            relpath = path.relative_to(out_dir).as_posix()
            if relpath in skip:
                continue
            result[relpath] = path.read_text(encoding="utf-8", errors="replace")
        return result


# ---------------------------------------------------------------------------
# Item 9: content-addressed assets, legacy aliases, provider bundle
# ---------------------------------------------------------------------------
class ContentAddressedPublishingTests(BuildTestBase):
    def test_build_produces_complete_content_addressed_bundle(self) -> None:
        result = self.run_build()

        self.assertEqual(result.status, "built")
        self.assertEqual(result.exit_code, 2)
        self.assertTrue(result.changed)
        self.assertEqual(result.reasons, ["no previous build manifest"])

        # every expected artifact exists
        for name in ("targets.json", "kelee-scripts.stoverride", "index.html",
                     "DemoAds.stoverride", "DemoScript.stoverride", "DemoNested.stoverride"):
            self.assertTrue((self.out_dir / name).is_file(), name)
        self.assertTrue((self.out_dir / ".build-manifest.json").is_file())
        self.assertTrue((self.out_dir / ".hashes.json").is_file())

        # provider URLs are content-addressed on our own base
        urls = provider_urls(self.out_dir)
        self.assertEqual(sorted(urls), ["clean", "parentscript"])
        for name, url in urls.items():
            self.assertRegex(url, HASHED_SCRIPTS_RE, name)

        # hashed files + legacy aliases hold identical bytes and the hash prefix is real
        scripts = sorted(p for p in (self.out_dir / "scripts").iterdir() if p.is_file())
        self.assertTrue(scripts)
        for path in scripts:
            data = path.read_bytes()
            if "-" in path.name and len(path.name.split("-", 1)[0]) == 12:
                prefix = path.name.split("-", 1)[0]
                self.assertTrue(_sha256_bytes(data).startswith(prefix), path.name)
        for name, url in urls.items():
            hashed = self.out_dir / url[len(BASE) + 1:]
            logical = hashed.name.split("-", 1)[1]
            legacy = self.out_dir / "scripts" / logical
            self.assertTrue(hashed.is_file(), hashed)
            self.assertTrue(legacy.is_file(), legacy)
            self.assertEqual(hashed.read_bytes(), legacy.read_bytes())

        # nested runtime asset was mirrored as well
        manifest = read_manifest(self.out_dir)
        chunk_url = "https://kelee.one/Resource/JavaScript/Demo/chunk-abc123def456.js"
        chunk = manifest["resources"][chunk_url]
        self.assertEqual(chunk["kind"], "runtime")
        self.assertIsNotNone(chunk["relpath"])
        self.assertTrue((self.out_dir / chunk["relpath"]).is_file())
        self.assertEqual(chunk["legacy_alias"], "scripts/chunk-abc123def456.js")
        parent = manifest["resources"]["https://kelee.one/Resource/JavaScript/Demo/ParentScript.js"]
        parent_text = (self.out_dir / parent["relpath"]).read_text(encoding="utf-8")
        self.assertIn(f"{BASE}/{chunk['relpath']}", parent_text)
        self.assertNotIn(chunk_url, parent_text)

        # inline jq dependency tracked with its content hash, never published
        jq = manifest["resources"]["https://kelee.one/Resource/JQLang/Demo/clean.jq"]
        self.assertEqual(jq["kind"], "jq")
        self.assertIsNone(jq["relpath"])
        self.assertEqual(len(jq["sha256"]), 64)

        # no upstream kelee.one resource URL leaks into generated overrides
        for name, text in self.all_text_outputs(self.out_dir).items():
            self.assertNotIn("kelee.one/Resource", text, name)

    def test_state_file_replaces_manual_converter_version(self) -> None:
        self.run_build()
        state = read_state(self.out_dir)
        self.assertNotIn("_converter_version", state)
        manifest = read_manifest(self.out_dir)
        self.assertEqual(state["_build_fingerprint"], manifest["fingerprint"])
        self.assertEqual(len(state["_build_fingerprint"]), 64)
        for filename, meta in manifest["sources"].items():
            self.assertEqual(state[filename], meta["sha256"])

    def test_build_is_idempotent_and_atomic(self) -> None:
        first = self.run_build()
        before = tree_hashes(self.out_dir)
        second = self.run_build()

        self.assertEqual(second.status, "no-change")
        self.assertEqual(second.exit_code, 0)
        self.assertEqual(second.fingerprint, first.fingerprint)
        self.assertEqual(tree_hashes(self.out_dir), before)

    def test_force_rebuilds_identical_inputs(self) -> None:
        self.run_build()
        result = self.run_build(force=True)
        self.assertEqual(result.status, "built")
        self.assertEqual(result.exit_code, 2)
        self.assertIn("--force rebuild requested", result.reasons)


# ---------------------------------------------------------------------------
# Item 10: dependency-only update detection
# ---------------------------------------------------------------------------
class DependencyUpdateDetectionTests(BuildTestBase):
    def test_script_dependency_change_rebuilds_with_new_hashed_name(self) -> None:
        first = self.run_build()
        old_urls = provider_urls(self.out_dir)
        snapshots = fixture_snapshots(**{
            "https://kelee.one/Resource/JavaScript/Demo/clean.js":
                "// Fixture hosted script v2\nmodule.exports = { marker: 'clean-v2' };\n",
        })
        result = self.run_build(resource_snapshots=snapshots)

        self.assertEqual(result.status, "built")
        self.assertEqual(result.exit_code, 2)
        self.assertNotEqual(result.fingerprint, first.fingerprint)
        self.assertTrue(any("dependency changed" in reason for reason in result.reasons), result.reasons)

        manifest = read_manifest(self.out_dir)
        # LPX sources are untouched; only the mirrored resource changed.
        self.assertEqual(manifest["sources"], first.manifest["sources"])
        new_urls = provider_urls(self.out_dir)
        self.assertNotEqual(old_urls["clean"], new_urls["clean"])
        self.assertRegex(new_urls["clean"], HASHED_SCRIPTS_RE)
        new_rel = new_urls["clean"][len(BASE) + 1:]
        self.assertTrue((self.out_dir / new_rel).is_file())
        self.assertIn("clean-v2", (self.out_dir / new_rel).read_text(encoding="utf-8"))
        legacy = self.out_dir / "scripts" / new_rel.split("-", 1)[1]
        self.assertEqual(legacy.read_bytes(), (self.out_dir / new_rel).read_bytes())

    def test_jq_dependency_change_rebuilds_override_body(self) -> None:
        first = self.run_build()
        snapshots = fixture_snapshots(**{
            "https://kelee.one/Resource/JQLang/Demo/clean.jq":
                ".data |= map(select(.ad == null)) | del(.banner, .promoted)\n",
        })
        result = self.run_build(resource_snapshots=snapshots)
        self.assertEqual(result.status, "built")
        self.assertNotEqual(result.fingerprint, first.fingerprint)

        manifest = read_manifest(self.out_dir)
        jq = manifest["resources"]["https://kelee.one/Resource/JQLang/Demo/clean.jq"]
        self.assertIn("promoted", (self.out_dir / "DemoAds.stoverride").read_text(encoding="utf-8"))

    def test_nested_runtime_change_rewrites_and_rehashes_parent(self) -> None:
        self.run_build()
        first = read_manifest(self.out_dir)
        parent_url = "https://kelee.one/Resource/JavaScript/Demo/ParentScript.js"
        chunk_url = "https://kelee.one/Resource/JavaScript/Demo/chunk-abc123def456.js"
        first_parent = first["resources"][parent_url]
        first_chunk = first["resources"][chunk_url]

        snapshots = fixture_snapshots(**{
            chunk_url: "// Fixture nested runtime chunk v2\nexport const CHUNK_MARKER = 'chunk-v2';\n",
        })
        result = self.run_build(resource_snapshots=snapshots)
        self.assertEqual(result.status, "built")
        self.assertNotEqual(result.fingerprint, first["fingerprint"])

        second = read_manifest(self.out_dir)
        second_parent = second["resources"][parent_url]
        second_chunk = second["resources"][chunk_url]
        self.assertNotEqual(first_chunk["relpath"], second_chunk["relpath"])
        # The parent's upstream bytes are identical but its published content is
        # not: the substituted child URL changed, so the parent hash must change.
        self.assertNotEqual(first_parent["relpath"], second_parent["relpath"])
        parent_text = (self.out_dir / second_parent["relpath"]).read_text(encoding="utf-8")
        self.assertIn(second_chunk["relpath"], parent_text)
        self.assertNotIn(first_chunk["relpath"], parent_text)
        self.assertTrue((self.out_dir / second_chunk["legacy_alias"]).is_file())

    def test_converter_source_change_rebuilds(self) -> None:
        self.run_build()
        pipeline = fixture_pipeline.FixturePipeline()
        pipeline.code_revision = "2"
        result = self.run_build(pipeline=pipeline)
        self.assertEqual(result.status, "built")
        self.assertTrue(
            any("converter/validator/source changed" in reason for reason in result.reasons),
            result.reasons,
        )

    def test_converter_output_change_is_fingerprinted(self) -> None:
        self.run_build()
        result = self.run_build(pipeline=fixture_pipeline.FixturePipeline(mutation_marker="changed"))
        self.assertEqual(result.status, "built")


# ---------------------------------------------------------------------------
# Item 10: missing / corrupted outputs and failure retry
# ---------------------------------------------------------------------------
class RebuildAndRetryTests(BuildTestBase):
    def test_missing_page_is_rebuilt(self) -> None:
        self.run_build()
        (self.out_dir / "index.html").unlink()
        result = self.run_build()
        self.assertEqual(result.status, "built")
        self.assertTrue(any("index.html" in reason for reason in result.reasons), result.reasons)
        self.assertTrue((self.out_dir / "index.html").is_file())

    def test_corrupted_override_and_asset_are_rebuilt(self) -> None:
        self.run_build()
        manifest = read_manifest(self.out_dir)
        override = self.out_dir / "DemoScript.stoverride"
        override.write_text("broken: [\n", encoding="utf-8")
        hashed = next(iter(sorted(p for p in (self.out_dir / "scripts").iterdir() if p.name.startswith(tuple("0123456789abcdef")))))
        hashed.write_bytes(b"corrupted")

        result = self.run_build()
        self.assertEqual(result.status, "built")
        self.assertEqual(len(result.reasons), 2, result.reasons)
        new_manifest = read_manifest(self.out_dir)
        self.assertEqual(
            _sha256_bytes(override.read_bytes()), new_manifest["outputs"]["DemoScript.stoverride"]["sha256"]
        )
        self.assertEqual(_sha256_bytes(hashed.read_bytes()), new_manifest["outputs"][hashed.relative_to(self.out_dir).as_posix()]["sha256"])

    def test_corrupted_manifest_triggers_rebuild(self) -> None:
        self.run_build()
        (self.out_dir / ".build-manifest.json").write_text("{not json", encoding="utf-8")
        result = self.run_build()
        self.assertEqual(result.status, "built")
        self.assertEqual(result.reasons, ["no previous build manifest"])

    def test_missing_state_is_recreated_on_no_change(self) -> None:
        self.run_build()
        state = self.out_dir / ".hashes.json"
        state.unlink()
        result = self.run_build()
        self.assertEqual(result.status, "no-change")
        self.assertTrue(state.is_file())
        self.assertEqual(read_state(self.out_dir)["_build_fingerprint"], result.fingerprint)

    def test_conversion_failure_writes_nothing(self) -> None:
        pipeline = fixture_pipeline.FixturePipeline(fail_targets=("DemoAds.lpx",))
        with self.assertRaises(build.BuildError) as raised:
            self.run_build(pipeline=pipeline)
        self.assertTrue(any(d.code == "E_FIXTURE_INJECTED" for d in raised.exception.diagnostics))
        self.assertFalse(self.out_dir.exists())

    def test_missing_dependency_snapshot_fails_without_partial_bundle(self) -> None:
        snapshots = fixture_pipeline.load_fixture_snapshots()
        snapshots.pop("https://kelee.one/Resource/JavaScript/Demo/chunk-abc123def456.js")
        with self.assertRaises(build.BuildError) as raised:
            self.run_build(resource_snapshots=snapshots)
        self.assertTrue(any("E_SNAPSHOT_MISSING" in d.code or "E_SNAPSHOT_MISSING" in d.message
                            for d in raised.exception.diagnostics), raised.exception.diagnostics)
        self.assertFalse(self.out_dir.exists())

    def test_validation_failure_keeps_previous_state_and_retry_works(self) -> None:
        first = self.run_build()
        manifest_before = (self.out_dir / ".build-manifest.json").read_bytes()
        state_before = (self.out_dir / ".hashes.json").read_bytes()
        tree_before = tree_hashes(self.out_dir)

        failing = fixture_pipeline.FixturePipeline(
            validator_failures=(
                Diagnostic("E_INJECTED_VALIDATION", "injected validation failure", "bundle", True, "error", "validator"),
            )
        )
        with self.assertRaises(build.BuildError) as raised:
            self.run_build(pipeline=failing, force=True)
        self.assertTrue(any(d.code == "E_INJECTED_VALIDATION" for d in raised.exception.diagnostics))
        self.assertEqual((self.out_dir / ".build-manifest.json").read_bytes(), manifest_before)
        self.assertEqual((self.out_dir / ".hashes.json").read_bytes(), state_before)
        self.assertEqual(tree_hashes(self.out_dir), tree_before)

        retry = self.run_build(force=True)
        self.assertEqual(retry.status, "built")
        self.assertEqual(retry.fingerprint, first.fingerprint)

    def test_missing_provider_fails_integrity_check(self) -> None:
        pipeline = fixture_pipeline.FixturePipeline(drop_provider_for=("DemoScript.lpx",))
        with self.assertRaises(build.BuildError) as raised:
            self.run_build(pipeline=pipeline)
        codes = {d.code for d in raised.exception.diagnostics}
        self.assertIn("E_SCRIPT_PROVIDER_MISSING", codes)
        self.assertFalse(self.out_dir.exists())


# ---------------------------------------------------------------------------
# Item 12 (build side): one public base, no stale default host
# ---------------------------------------------------------------------------
class PublicBaseTests(BuildTestBase):
    def test_custom_base_is_used_everywhere(self) -> None:
        result = self.run_build(public_base=CUSTOM_BASE)
        self.assertEqual(result.status, "built")
        custom_re = r"^https://kelee-mirror\.example\.test/scripts/[0-9a-f]{12}-[A-Za-z0-9._-]+\.js$"
        for name, url in provider_urls(self.out_dir).items():
            self.assertRegex(url, custom_re, name)
        html = (self.out_dir / "index.html").read_text(encoding="utf-8")
        self.assertIn(CUSTOM_BASE, html)
        self.assertIn("stash://install-override", html)
        for name, text in self.all_text_outputs(self.out_dir).items():
            self.assertNotIn(DEFAULT_HOST, text, name)

    def test_base_change_triggers_rebuild_and_leaves_no_stale_host(self) -> None:
        first = self.run_build()
        self.assertIn(BASE, (self.out_dir / "index.html").read_text(encoding="utf-8"))
        result = self.run_build(public_base=CUSTOM_BASE)
        self.assertEqual(result.status, "built")
        self.assertTrue(any("public_base changed" in reason for reason in result.reasons), result.reasons)
        self.assertNotEqual(result.fingerprint, first.fingerprint)
        for name, text in self.all_text_outputs(self.out_dir).items():
            self.assertNotIn(BASE, text, name)
            self.assertNotIn(DEFAULT_HOST, text, name)

    def test_publish_build_requires_explicit_base(self) -> None:
        with mock.patch.dict(os.environ, {"KELEE_PUBLIC_BASE": "", "STASH_PUBLIC_HOST": "", "KELEE_MIRROR_BASE": ""}):
            with self.assertRaises(build.BuildError):
                build.build_bundle(
                    self.out_dir,
                    source_dir=FIXTURE_DIR,
                    fetch=False,
                    pipeline=fixture_pipeline.FixturePipeline(),
                )
        self.assertFalse(self.out_dir.exists())


# ---------------------------------------------------------------------------
# Page rendering (item 12 build/page side, item 9 consistency)
# ---------------------------------------------------------------------------
class PageGenerationTests(BuildTestBase):
    def test_page_is_manifest_driven(self) -> None:
        self.run_build()
        html = (self.out_dir / "index.html").read_text(encoding="utf-8")
        manifest = read_manifest(self.out_dir)
        self.assertIn(manifest["fingerprint"][:12], html)
        self.assertIn(manifest["generated_at"], html)
        self.assertIn("provider 2 个", html)
        self.assertIn("共 3 个目标", html)
        # One dependency target + the pinned provider bundle card.
        self.assertIn("依赖 2 个（含 Provider 合集）", html)
        self.assertIn('data-filter="dep">依赖 2</span>', html)
        self.assertNotIn(DEFAULT_HOST, html)
        # install links point at stable override names, not hashed assets
        self.assertIn(f"{BASE}/DemoScript.stoverride", html)
        self.assertIn(f"{BASE}/kelee-scripts.stoverride", html)

        # Counts come from the bundle, not from the template: change the target
        # list so two targets are dependencies and prove every badge follows.
        targets = fixture_pipeline.load_fixture_targets()
        for target in targets:
            target["dependency"] = target["filename"] in {"DemoAds.lpx", "DemoScript.lpx"}
        self.run_build(targets=targets, force=True)
        html = (self.out_dir / "index.html").read_text(encoding="utf-8")
        self.assertIn("依赖 3 个（含 Provider 合集）", html)
        self.assertIn('data-filter="dep">依赖 3</span>', html)
        self.assertIn("provider 2 个", html)

    def test_dependency_counts_are_consistent(self) -> None:
        self.run_build()
        html = (self.out_dir / "index.html").read_text(encoding="utf-8")
        header = re.search(r"依赖 (\d+) 个（含 Provider 合集）", html)
        pill = re.search(r'data-filter="dep">依赖 (\d+)</span>', html)
        self.assertIsNotNone(header, "header lost its dependency count")
        self.assertIsNotNone(pill, "dependency pill missing")
        self.assertEqual(header.group(1), pill.group(1))
        # 1 dependency target + 1 provider bundle card
        self.assertEqual(int(header.group(1)), 2)
        self.assertEqual(page.validate_page(html, public_base=BASE, entry_count=3), [])
        tampered = html.replace("依赖 2 个（含 Provider 合集）", "依赖 1 个（含 Provider 合集）")
        self.assertTrue(any("disagree" in problem for problem in page.validate_page(tampered, public_base=BASE)))

    def test_validate_page_rejects_stale_host_and_missing_counts(self) -> None:
        self.run_build()
        html = (self.out_dir / "index.html").read_text(encoding="utf-8")
        self.assertEqual(page.validate_page(html, public_base=BASE), [])

        stale = html.replace(BASE, f"https://{DEFAULT_HOST}")
        problems = page.validate_page(stale, public_base=BASE)
        self.assertTrue(any(DEFAULT_HOST in problem for problem in problems), problems)

        no_provider_count = html.replace("provider 2 个", "provider ? 个")
        self.assertTrue(page.validate_page(no_provider_count, public_base=BASE, provider_count=2))

        wrong_all_count = html.replace('data-filter="all">全部 4</span>', 'data-filter="all">全部 9</span>')
        self.assertTrue(page.validate_page(wrong_all_count, public_base=BASE, entry_count=3))

    def test_standalone_page_generator_works_from_manifest(self) -> None:
        self.run_build()
        page_path = self.out_dir / "index.html"
        page_path.unlink()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = page.main(["--out-dir", str(self.out_dir), "--public-base", BASE])
        self.assertEqual(code, 0, stdout.getvalue())
        self.assertTrue(page_path.is_file())
        self.assertIn(BASE, page_path.read_text(encoding="utf-8"))

    def test_standalone_page_generator_requires_base(self) -> None:
        self.run_build()
        with mock.patch.dict(os.environ, {"KELEE_PUBLIC_BASE": "", "STASH_PUBLIC_HOST": "", "KELEE_MIRROR_BASE": ""}):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = page.main(["--out-dir", str(self.out_dir)])
        self.assertEqual(code, 1)
        self.assertIn("KELEE_PUBLIC_BASE", stderr.getvalue())


# ---------------------------------------------------------------------------
# CLI contract
# ---------------------------------------------------------------------------
class CliContractTests(BuildTestBase):
    def _argv(self, *extra: str) -> list[str]:
        return [
            "--out-dir", str(self.out_dir),
            "--public-base", BASE,
            "--snapshot-dir", str(FIXTURE_DIR),
            "--quiet",
            *extra,
        ]

    def test_exit_codes_built_then_unchanged(self) -> None:
        code, _, _ = self.run_cli(self._argv())
        self.assertEqual(code, 2)
        code, _, _ = self.run_cli(self._argv())
        self.assertEqual(code, 0)
        code, _, _ = self.run_cli(self._argv("--force"))
        self.assertEqual(code, 2)

    def test_failure_exit_code_and_json_report(self) -> None:
        report = self.root / "report.json"
        failing = fixture_pipeline.FixturePipeline(
            validator_failures=(Diagnostic("E_INJECTED_VALIDATION", "boom", "bundle", True, "error", "validator"),)
        )
        code, _, stderr = self.run_cli(self._argv("--json-report", str(report)), pipeline=failing)
        self.assertEqual(code, 1)
        self.assertIn("boom", stderr)
        self.assertEqual(read_json(report)["status"], "failure")

    def test_json_report_is_metadata_only(self) -> None:
        report = self.root / "report.json"
        code, _, _ = self.run_cli(self._argv("--json-report", str(report)))
        self.assertEqual(code, 2)
        data = read_json(report)
        self.assertEqual(data["status"], "built")
        self.assertEqual(data["exit_code"], 2)
        for meta in data["sources"].values():
            self.assertNotIn("text", meta, "LPX bodies must not leak into the build report")
            self.assertEqual(len(meta["sha256"]), 64)
        self.assertLess(len(json.dumps(data)), 100_000)

    def test_dry_run_writes_nothing(self) -> None:
        code, stdout, _ = self.run_cli(self._argv("--dry-run"))
        self.assertEqual(code, 2)
        self.assertIn("dry run", stdout.lower())
        self.assertFalse(self.out_dir.exists())

    def test_dry_run_after_build_reports_no_change(self) -> None:
        self.run_cli(self._argv())
        code, stdout, _ = self.run_cli(self._argv("--dry-run"))
        self.assertEqual(code, 0)
        self.assertIn("up to date", stdout)

    def test_missing_public_base_fails_clearly(self) -> None:
        argv = [
            "--out-dir", str(self.out_dir),
            "--snapshot-dir", str(FIXTURE_DIR),
            "--quiet",
        ]
        with mock.patch.dict(os.environ, {"KELEE_PUBLIC_BASE": "", "STASH_PUBLIC_HOST": "", "KELEE_MIRROR_BASE": ""}):
            code, _, stderr = self.run_cli(argv)
        self.assertEqual(code, 1)
        self.assertIn("public base", stderr)

    def test_audit_snapshot_absent_is_reported(self) -> None:
        with mock.patch.object(build, "AUDIT_SNAPSHOT_DIR", self.root / "missing-audit"):
            code, _, stderr = self.run_cli(["--out-dir", str(self.out_dir), "--audit-snapshot", "--quiet"])
        self.assertEqual(code, 1)
        self.assertIn("audit snapshot", stderr)


# ---------------------------------------------------------------------------
# Integration with the real converter/resolver/validator (no network)
# ---------------------------------------------------------------------------
def _real_converter_available() -> bool:
    try:
        import convert_kelee_lpx

        return hasattr(convert_kelee_lpx, "convert_lpx")
    except Exception:  # noqa: BLE001 - converter still being published
        return False


@unittest.skipUnless(_real_converter_available(), "shared convert_lpx(...) API not published yet")
class RealPipelineIntegrationTests(BuildTestBase):
    def test_real_pipeline_builds_and_validates_fixture_bundle(self) -> None:
        result = self.run_build()
        self.assertEqual(result.status, "built")
        manifest = read_manifest(self.out_dir)
        self.assertEqual(manifest["public_base"], BASE)
        self.assertTrue(manifest["providers"])
        urls = provider_urls(self.out_dir)
        for name, url in urls.items():
            self.assertTrue(url.startswith(BASE + "/scripts/"), name)
        # The real converter emits at least one mirrored kelee.one script from
        # the fixtures (clean.js + ParentScript.js), tracked as published assets.
        published = [rec for rec in manifest["resources"].values() if rec["relpath"]]
        self.assertGreaterEqual(len(published), 2)
        for rec in published:
            self.assertTrue((self.out_dir / rec["relpath"]).is_file(), rec["relpath"])

    def test_real_pipeline_no_change_on_second_build(self) -> None:
        first = self.run_build()
        second = self.run_build()
        self.assertEqual(second.status, "no-change")
        self.assertEqual(second.fingerprint, first.fingerprint)


if __name__ == "__main__":
    unittest.main()
