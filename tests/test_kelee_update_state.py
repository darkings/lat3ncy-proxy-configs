#!/usr/bin/env python3
"""State-transition tests for the KeLee build orchestrator.

The build manifest (``.build-manifest.json``) is the authoritative update state
and the legacy ``.hashes.json`` mirrors it for the deploy tooling. The rules
under test:

* state/state-adjacent files are written **only after** a build passed
  conversion, bundle integrity checks and ``validate_stash.validate_bundle``;
* a failure before promotion leaves the previous manifest and state byte-for-byte
  intact, so the next run retries the same build;
* the manifest fingerprint is a pure function of the build inputs (no
  ``CONVERTER_VERSION``), and "no change" never rewrites the manifest;
* the deploy scripts keep consuming ``.hashes.json`` (fingerprint + per-LPX
  hashes) during the migration.

Offline and deterministic: uses the fixture pipeline in
``tests/fixtures/build/`` with the real asset resolver and validator.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import io
import json
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

import check_kelee_update as build
import fixture_pipeline
from kelee_diagnostics import Diagnostic

BASE = "https://stash.example.test"
FIXED_NOW = _dt.datetime(2026, 10, 25, 0, 0, 0, tzinfo=_dt.timezone.utc)
LATER_NOW = _dt.datetime(2026, 10, 26, 12, 0, 0, tzinfo=_dt.timezone.utc)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


class StateTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="kelee-state-test-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.out_dir = self.root / "bundle"

    def build(self, *, out_dir: Path | None = None, pipeline=None, now=None, **overrides):
        kwargs: dict = {
            "public_base": BASE,
            "source_dir": FIXTURE_DIR,
            "fetch": False,
            "pipeline": pipeline if pipeline is not None else fixture_pipeline.FixturePipeline(),
            "now": lambda: now or FIXED_NOW,
        }
        kwargs.update(overrides)
        return build.build_bundle(out_dir or self.out_dir, **kwargs)

    def snapshot_state(self) -> dict[str, str]:
        return {
            ".build-manifest.json": sha256_file(self.out_dir / ".build-manifest.json"),
            ".hashes.json": sha256_file(self.out_dir / ".hashes.json"),
        }

    def failing_pipeline(self) -> fixture_pipeline.FixturePipeline:
        return fixture_pipeline.FixturePipeline(
            validator_failures=(
                Diagnostic("E_STATE_INJECTED", "injected failure", "bundle", True, "error", "validator"),
            )
        )


class FingerprintTests(StateTestBase):
    def _inputs(self, *, resources=None, public_base=BASE) -> dict:
        sources = {
            "A.lpx": {"sha256": "a" * 64},
            "B.lpx": {"sha256": "b" * 64},
        }
        resource_map = resources if resources is not None else {
            "https://example.test/a.js": {
                "sha256": "c" * 64,
                "relpath": "scripts/cccccccccccc-a.js",
                "kind": "script",
                "legacy_alias": "scripts/a.js",
            }
        }
        return build.build_fingerprint_inputs(
            public_base=public_base,
            mirror_base=public_base,
            schema=1,
            targets_digest="t" * 64,
            sources=sources,
            resources=resource_map,
            pipeline_inputs={"convert_kelee_lpx.py": "1", "validate_stash.py": "2"},
            upstream_digest="u" * 64,
        )

    def test_fingerprint_is_deterministic_and_order_independent(self) -> None:
        first = build.compute_fingerprint(self._inputs())
        second = build.compute_fingerprint(self._inputs())
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)

        reversed_inputs = dict(reversed(list(self._inputs().items())))
        self.assertEqual(build.compute_fingerprint(reversed_inputs), first)

    def test_every_fingerprinted_dimension_changes_the_digest(self) -> None:
        baseline = build.compute_fingerprint(self._inputs())
        mutations = {
            "public base": self._inputs(public_base="https://other.example.test"),
            "resource content": self._inputs(resources={
                "https://example.test/a.js": {
                    "sha256": "d" * 64,
                    "relpath": "scripts/dddddddddddd-a.js",
                    "kind": "script",
                    "legacy_alias": "scripts/a.js",
                }
            }),
            "converter source": build.build_fingerprint_inputs(
                public_base=BASE,
                mirror_base=BASE,
                schema=1,
                targets_digest="t" * 64,
                sources={"A.lpx": {"sha256": "a" * 64}},
                resources={},
                pipeline_inputs={"convert_kelee_lpx.py": "changed", "validate_stash.py": "2"},
                upstream_digest="u" * 64,
            ),
        }
        for label, payload in mutations.items():
            with self.subTest(label):
                self.assertNotEqual(build.compute_fingerprint(payload), baseline)

    def test_schema_version_is_part_of_the_fingerprint(self) -> None:
        first = build.compute_fingerprint(self._inputs())
        payload = self._inputs()
        payload["schema"] = payload["schema"] + 1
        self.assertNotEqual(build.compute_fingerprint(payload), first)

    def test_no_manual_converter_version_in_manifest_or_state(self) -> None:
        self.build()
        manifest_text = (self.out_dir / ".build-manifest.json").read_text(encoding="utf-8")
        state = read_json(self.out_dir / ".hashes.json")
        self.assertNotIn("CONVERTER_VERSION", manifest_text)
        self.assertNotIn("_converter_version", state)
        targets = read_json(self.out_dir / "targets.json")
        self.assertEqual(
            set(state) - {"_build_fingerprint", "_manifest_schema", "_generated_at"},
            {item["filename"] for item in targets},
        )


class StateTransitionTests(StateTestBase):
    def test_state_is_written_only_after_success(self) -> None:
        result = self.build()
        self.assertEqual(result.status, "built")
        state = read_json(self.out_dir / ".hashes.json")
        manifest = read_json(self.out_dir / ".build-manifest.json")
        self.assertEqual(state["_build_fingerprint"], manifest["fingerprint"])
        self.assertEqual(state["_build_fingerprint"], result.fingerprint)
        self.assertEqual(state["_manifest_schema"], manifest["schema"])

    def test_failed_validation_does_not_advance_state(self) -> None:
        self.build()
        state_before = self.snapshot_state()

        with self.assertRaises(build.BuildError):
            self.build(pipeline=self.failing_pipeline(), force=True)
        self.assertEqual(self.snapshot_state(), state_before)

        state = read_json(self.out_dir / ".hashes.json")
        manifest = read_json(self.out_dir / ".build-manifest.json")
        self.assertEqual(state["_build_fingerprint"], manifest["fingerprint"])

    def test_changed_inputs_fail_then_retry_advances_state(self) -> None:
        first = self.build()
        state_before = self.snapshot_state()

        snapshots = fixture_pipeline.load_fixture_snapshots()
        snapshots["https://kelee.one/Resource/JavaScript/Demo/clean.js"] = (
            b"// Fixture hosted script v2\nmodule.exports = { marker: 'clean-v2' };\n"
        )
        with self.assertRaises(build.BuildError):
            self.build(resource_snapshots=snapshots, pipeline=self.failing_pipeline())
        # Failure kept the previous build state: a retry must not be skipped as
        # "no change" even though the output dir still matches the old manifest.
        self.assertEqual(self.snapshot_state(), state_before)

        retried = self.build(resource_snapshots=snapshots)
        self.assertEqual(retried.status, "built")
        self.assertNotEqual(retried.fingerprint, first.fingerprint)
        state = read_json(self.out_dir / ".hashes.json")
        self.assertEqual(state["_build_fingerprint"], retried.fingerprint)
        self.assertEqual(read_json(self.out_dir / ".build-manifest.json")["fingerprint"], retried.fingerprint)

    def test_no_change_does_not_touch_state_files(self) -> None:
        self.build()
        state_before = self.snapshot_state()
        first_manifest = read_json(self.out_dir / ".build-manifest.json")
        result = self.build()
        self.assertEqual(result.status, "no-change")
        self.assertEqual(self.snapshot_state(), state_before)
        self.assertEqual(read_json(self.out_dir / ".build-manifest.json"), first_manifest)

    def test_force_rebuild_keeps_fingerprint_but_refreshes_manifest(self) -> None:
        first = self.build()
        result = self.build(force=True, now=LATER_NOW)
        self.assertEqual(result.status, "built")
        self.assertEqual(result.fingerprint, first.fingerprint)
        manifest = read_json(self.out_dir / ".build-manifest.json")
        self.assertEqual(manifest["fingerprint"], first.fingerprint)
        self.assertIn("2026-10-26T12:00:00Z", manifest["generated_at"])
        self.assertEqual(read_json(self.out_dir / ".hashes.json")["_build_fingerprint"], first.fingerprint)

    def test_corrupt_output_triggers_repair_and_state_update(self) -> None:
        first = self.build()
        state_before = read_json(self.out_dir / ".hashes.json")
        page = self.out_dir / "index.html"
        page.write_text("corrupted", encoding="utf-8")

        result = self.build()
        self.assertEqual(result.status, "built")
        self.assertTrue(any("index.html" in reason for reason in result.reasons), result.reasons)
        self.assertEqual(page.read_text(encoding="utf-8")[:15], "<!doctype html>")
        state = read_json(self.out_dir / ".hashes.json")
        self.assertEqual(state["_build_fingerprint"], first.fingerprint)
        self.assertEqual(
            {key: value for key, value in state.items() if key.startswith("_")},
            {key: value for key, value in state_before.items() if key.startswith("_")},
        )

    def test_missing_state_is_recreated_without_rebuild(self) -> None:
        self.build()
        (self.out_dir / ".hashes.json").unlink()
        result = self.build()
        self.assertEqual(result.status, "no-change")
        self.assertTrue((self.out_dir / ".hashes.json").is_file())
        self.assertEqual(read_json(self.out_dir / ".hashes.json")["_build_fingerprint"], result.fingerprint)

    def test_corrupt_manifest_is_replaced_after_rebuild(self) -> None:
        self.build()
        (self.out_dir / ".build-manifest.json").write_text("[]", encoding="utf-8")
        result = self.build()
        self.assertEqual(result.status, "built")
        self.assertEqual(result.reasons, ["no previous build manifest"])
        self.assertIsInstance(read_json(self.out_dir / ".build-manifest.json"), dict)

    def test_state_survives_crash_between_promote_and_manifest(self) -> None:
        """A crash after promoting outputs but before state keeps the old manifest.

        The next run detects the fingerprint mismatch and rebuilds; it must never
        mistake the half-finished run for a valid up-to-date bundle.
        """

        self.build()
        manifest_before = read_json(self.out_dir / ".build-manifest.json")
        # Simulate an interrupted promotion: outputs were replaced with bytes
        # from a new build, but the old manifest/state are still on disk.
        page = self.out_dir / "index.html"
        page.write_text(page.read_text(encoding="utf-8") + "<!-- dirty -->", encoding="utf-8")
        result = self.build()
        self.assertEqual(result.status, "built")
        self.assertEqual(read_json(self.out_dir / ".build-manifest.json")["fingerprint"], manifest_before["fingerprint"])
        self.assertNotIn("dirty", page.read_text(encoding="utf-8"))


class DeployCompatibilityTests(StateTestBase):
    def test_hashes_json_keeps_legacy_contract(self) -> None:
        result = self.build()
        state = read_json(self.out_dir / ".hashes.json")
        manifest = read_json(self.out_dir / ".build-manifest.json")
        self.assertIsInstance(state["_build_fingerprint"], str)
        self.assertEqual(len(state["_build_fingerprint"]), 64)
        self.assertIsInstance(state["_manifest_schema"], int)
        for filename, source in manifest["sources"].items():
            self.assertEqual(state[filename], source["sha256"])
            self.assertEqual(len(state[filename]), 64)
        self.assertEqual(state["_build_fingerprint"], result.fingerprint)

    def test_manifest_covers_every_published_file(self) -> None:
        self.build()
        manifest = read_json(self.out_dir / ".build-manifest.json")
        on_disk = {
            path.relative_to(self.out_dir).as_posix()
            for path in self.out_dir.rglob("*")
            if path.is_file()
        }
        expected = set(manifest["outputs"]) | {".build-manifest.json", ".hashes.json"}
        self.assertEqual(on_disk, expected)
        for relpath, meta in manifest["outputs"].items():
            self.assertEqual(sha256_file(self.out_dir / relpath), meta["sha256"], relpath)

    def test_resources_all_point_at_real_assets(self) -> None:
        self.build()
        manifest = read_json(self.out_dir / ".build-manifest.json")
        for url, record in manifest["resources"].items():
            if record["relpath"]:
                path = self.out_dir / record["relpath"]
                self.assertTrue(path.is_file(), url)
                self.assertEqual(sha256_file(path), record["sha256"], url)
            if record["legacy_alias"]:
                self.assertTrue((self.out_dir / record["legacy_alias"]).is_file(), url)


class ExitCodeContractTests(StateTestBase):
    def test_exit_codes_cover_built_nochange_failure(self) -> None:
        built = self.build()
        self.assertEqual((built.status, built.exit_code), ("built", 2))
        unchanged = self.build()
        self.assertEqual((unchanged.status, unchanged.exit_code), ("no-change", 0))

        with self.assertRaises(build.BuildError):
            self.build(pipeline=self.failing_pipeline(), force=True)

    def test_dry_run_never_writes_state(self) -> None:
        result = self.build(dry_run=True)
        self.assertEqual((result.status, result.exit_code), ("dry-run", 2))
        self.assertFalse(self.out_dir.exists())

        self.build()
        before = self.snapshot_state()
        result = self.build(dry_run=True)
        self.assertEqual((result.status, result.exit_code), ("no-change", 0))
        self.assertEqual(self.snapshot_state(), before)


if __name__ == "__main__":
    unittest.main()
