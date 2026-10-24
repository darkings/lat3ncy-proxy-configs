#!/usr/bin/env python3
"""Regression tests for ``scripts/validate_stash.py`` (current Stash schemas).

Run::

    python tests/test_validate_stash.py

The validator must accept the current documented Stash override schema
(``http.mock`` with text/base64, ``http.script`` with require-body / argument,
``url-rewrite`` reject/target forms, one quoted scalar per body-rewrite, ...) and
reject the *old* invented fields (``body``/``data-type``/``statusCode`` in mock,
``enable``/``tag``/``requires-body``/``binary-body-mode`` in script, ...).
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
PUBLIC_BASE = "https://custom.example"

sys.path.insert(0, str(SCRIPTS_DIR))

import yaml  # noqa: E402

from convert_kelee_lpx import dump_stash_yaml  # noqa: E402
from validate_stash import (  # noqa: E402
    ValidationError,
    main as validate_main,
    validate_bundle,
    validate_bundle_or_raise,
)

SCRIPT_CONTENT = b'console.log("validator fixture script");\n'
SCRIPT_SHA12 = hashlib.sha256(SCRIPT_CONTENT).hexdigest()[:12]
SCRIPT_RELPATH = f"scripts/{SCRIPT_SHA12}-fixture.js"
SCRIPT_URL = f"{PUBLIC_BASE}/{SCRIPT_RELPATH}"

VALID_PROVIDERS = {
    "fixture-script": {"url": SCRIPT_URL, "interval": 86400},
    "external-script": {"url": "https://github.example.test/external.js", "interval": 3600},
}


def valid_override() -> dict:
    """A minimal bundle target using only the current Stash schema."""

    return {
        "name": "Validator Fixture",
        "desc": "minimal valid override",
        "icon": "https://example.test/icon.png",
        "author": "conversion-verifier",
        "rules": [
            "DOMAIN-SUFFIX,example.com,Proxy",
            "DOMAIN,ad.example.com,REJECT",
            "DOMAIN,drop.example.com,REJECT-DROP",
            "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
            "AND,((DOMAIN,api.example.com),(NETWORK,UDP),(DST-PORT,443)),REJECT",
        ],
        "http": {
            "mitm": ["api.example.com"],
            "mock": [
                {
                    "match": "(?i)^https://api\\.example\\.com/json\\?",
                    "status-code": 200,
                    "headers": {"Content-Type": "application/json"},
                    "text": '{"ok":true}',
                },
                {
                    "match": "(?i)^https://api\\.example\\.com/pixel\\.png$",
                    "status-code": 200,
                    "headers": {"Content-Type": "image/png"},
                    "base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGMAAQAABQABDQottAAAAABJRU5ErkJggg==",
                },
            ],
            "url-rewrite": [
                "(?i)^https://api\\.example\\.com/ad - reject",
                "(?i)^https://api\\.example\\.com/go https://target.example.test/landing 307",
            ],
            "header-rewrite": [
                "(?i)^https://api\\.example\\.com/req/ response-add x-test 1",
            ],
            "body-rewrite": [
                "(?i)^https://api\\.example\\.com/feed response-replace-regex <ad>[^<]*</ad> <ad></ad>",
                "(?i)^https://api\\.example\\.com/data response-jq del(.data.ad)",
            ],
            "script": [
                {
                    "match": "(?i)^https://api\\.example\\.com/bootstrap\\?",
                    "name": "fixture-script",
                    "type": "response",
                    "require-body": True,
                    "binary-mode": True,
                    "timeout": 5,
                    "argument": '{"tab":false}',
                },
                {
                    "match": "(?i)^https://api\\.example\\.com/other\\?",
                    "name": "external-script",
                    "type": "request",
                },
            ],
        },
    }


def provider_bundle(providers: dict | None = None) -> dict:
    return {"name": "kelee-scripts", "icon": "https://example.test/icon.png",
            "script-providers": providers if providers is not None else copy.deepcopy(VALID_PROVIDERS)}


def write_bundle(
    root: Path,
    *,
    override: dict | str | None = None,
    targets: list | None = None,
    providers: dict | None | bool = None,
    scripts: dict[str, bytes] | None = None,
    write_scripts: bool = True,
) -> Path:
    """Write a generated-style bundle.

    ``providers=None`` writes the merged ``kelee-scripts.stoverride`` provider
    bundle (the build-orchestrator layout); pass a dict to override it, or
    ``False`` to omit it entirely.
    """

    root.mkdir(parents=True, exist_ok=True)
    targets = targets if targets is not None else [{"filename": "Demo.lpx", "plugin": "Demo"}]
    (root / "targets.json").write_text(json.dumps(targets), encoding="utf-8")
    if override is None:
        override = valid_override()
    if isinstance(override, dict):
        (root / "Demo.stoverride").write_text(dump_stash_yaml(override), encoding="utf-8")
    else:
        (root / "Demo.stoverride").write_text(override, encoding="utf-8")
    if providers is not False:
        bundle = provider_bundle() if providers is None else {
            "name": "kelee-scripts",
            "script-providers": providers,
        }
        (root / "kelee-scripts.stoverride").write_text(
            yaml.safe_dump(bundle, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
    if write_scripts:
        for relpath, content in (
            scripts if scripts is not None else {SCRIPT_RELPATH: SCRIPT_CONTENT}
        ).items():
            path = root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    return root


def blocking(diagnostics) -> list:
    return [d for d in diagnostics if d.blocking]


def codes(diagnostics) -> list[str]:
    return [d.code for d in diagnostics]


class ValidatorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="kelee-validator-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def bundle(self, **kwargs) -> Path:
        return write_bundle(self.tmp / "bundle", **kwargs)

    def validate(self, root: Path, **kwargs):
        kwargs.setdefault("public_base", PUBLIC_BASE)
        return validate_bundle(root, **kwargs)

    def assert_rejected(self, root: Path, expected_code: str) -> None:
        diagnostics = self.validate(root)
        found = codes(blocking(diagnostics))
        self.assertIn(
            expected_code,
            found,
            f"expected blocking {expected_code}; got {[(d.code, d.message) for d in diagnostics]}",
        )

    def invoke_cli(self, root: Path) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = validate_main(
                [
                    "--dir",
                    str(root),
                    "--public-base",
                    PUBLIC_BASE,
                    "--targets",
                    str(root / "targets.json"),
                    "--main-config",
                    str(self.tmp / "missing-main.yaml"),
                    "--json",
                ]
            )
        return code, output.getvalue()


class ValidBundleTests(ValidatorTestCase):
    def test_accepts_current_schema_with_custom_public_base(self) -> None:
        root = self.bundle()
        diagnostics = self.validate(root, expected_targets=["Demo.lpx"])
        self.assertEqual(
            blocking(diagnostics),
            [],
            f"valid bundle must not be blocked: {[(d.code, d.message) for d in diagnostics]}",
        )
        self.assertNotIn("E_ASSET_DANGLING", codes(diagnostics))

    def test_valid_bundle_has_no_legacy_format_diagnostics(self) -> None:
        diagnostics = self.validate(self.bundle())
        for forbidden in (
            "E_MOCK_UNKNOWN_FIELD",
            "E_SCRIPT_UNKNOWN_FIELD",
            "E_BODY_YAML_UNQUOTED",
            "E_LEGACY_MARKER",
            "E_HTTP_UNKNOWN_SECTION",
            "E_PROVIDER_KELEE_URL",
        ):
            self.assertNotIn(forbidden, codes(diagnostics))

    def test_validate_bundle_or_raise_accepts_valid_bundle(self) -> None:
        diagnostics = validate_bundle_or_raise(self.bundle(), public_base=PUBLIC_BASE)
        self.assertEqual(blocking(diagnostics), [])

    def test_scripts_resolve_against_provider_bundle(self) -> None:
        diagnostics = self.validate(self.bundle())
        self.assertNotIn("E_SCRIPT_PROVIDER_MISSING", codes(diagnostics))
        self.assertNotIn("E_ASSET_MISSING", codes(diagnostics))

    def test_per_override_script_providers_are_registered(self) -> None:
        """Stash overrides may carry their own script-providers section."""

        override = valid_override()
        override["script-providers"] = copy.deepcopy(VALID_PROVIDERS)
        root = self.bundle(override=override, providers=False)
        diagnostics = self.validate(root)
        self.assertEqual(
            blocking(diagnostics),
            [],
            "an override that declares its own script-providers must validate: "
            f"{[(d.code, d.message) for d in diagnostics]}",
        )

    def test_cli_returns_zero_for_valid_bundle(self) -> None:
        root = self.bundle()
        code, output = self.invoke_cli(root)
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output) if output.strip().startswith("[") else [], [])


class InvalidMockTests(ValidatorTestCase):
    def mutate_mock(self, changes: dict) -> Path:
        override = valid_override()
        mock = copy.deepcopy(override["http"]["mock"][0])
        mock.update(changes)
        override["http"]["mock"] = [mock]
        return self.bundle(override=override)

    def test_rejects_wrong_status_code_type(self) -> None:
        self.assert_rejected(self.mutate_mock({"status-code": "200"}), "E_MOCK_STATUS_TYPE")

    def test_rejects_wrong_status_code_range(self) -> None:
        self.assert_rejected(self.mutate_mock({"status-code": 999}), "E_MOCK_STATUS_RANGE")

    def test_rejects_legacy_body_field(self) -> None:
        self.assert_rejected(self.mutate_mock({"body": "{}"}), "E_MOCK_UNKNOWN_FIELD")

    def test_rejects_legacy_data_type_field(self) -> None:
        self.assert_rejected(self.mutate_mock({"data-type": "json"}), "E_MOCK_UNKNOWN_FIELD")

    def test_rejects_legacy_status_code_field(self) -> None:
        self.assert_rejected(self.mutate_mock({"statusCode": 200}), "E_MOCK_UNKNOWN_FIELD")

    def test_rejects_text_and_base64_together(self) -> None:
        self.assert_rejected(
            self.mutate_mock({"base64": "AAAA", "text": "AAAA"}),
            "E_MOCK_TEXT_AND_BASE64",
        )

    def test_rejects_invalid_base64(self) -> None:
        self.assert_rejected(self.mutate_mock({"base64": "not base64!!"}), "E_MOCK_BASE64_INVALID")

    def test_rejects_mock_without_body(self) -> None:
        override = valid_override()
        override["http"]["mock"][0].pop("text")
        self.assert_rejected(self.bundle(override=override), "E_MOCK_BODY_MISSING")

    def test_rejects_non_mapping_mock(self) -> None:
        override = valid_override()
        override["http"]["mock"] = ["text"]
        self.assert_rejected(self.bundle(override=override), "E_MOCK_NOT_MAPPING")

    def test_rejects_mock_headers_wrong_type(self) -> None:
        self.assert_rejected(self.mutate_mock({"headers": ["Content-Type"]}), "E_MOCK_HEADERS_TYPE")


class InvalidScriptTests(ValidatorTestCase):
    def mutate_script(self, changes: dict) -> Path:
        override = valid_override()
        script = copy.deepcopy(override["http"]["script"][0])
        script.update(changes)
        override["http"]["script"] = [script]
        return self.bundle(override=override)

    def test_rejects_legacy_requires_body_hyphen(self) -> None:
        self.assert_rejected(self.mutate_script({"requires-body": True}), "E_SCRIPT_UNKNOWN_FIELD")

    def test_rejects_legacy_requires_body_underscore(self) -> None:
        self.assert_rejected(self.mutate_script({"requires_body": True}), "E_SCRIPT_UNKNOWN_FIELD")

    def test_rejects_legacy_binary_body_mode(self) -> None:
        self.assert_rejected(
            self.mutate_script({"binary-body-mode": True}), "E_SCRIPT_UNKNOWN_FIELD"
        )

    def test_rejects_legacy_enable_field(self) -> None:
        self.assert_rejected(self.mutate_script({"enable": True}), "E_SCRIPT_UNKNOWN_FIELD")

    def test_rejects_legacy_tag_field(self) -> None:
        self.assert_rejected(self.mutate_script({"tag": "Test"}), "E_SCRIPT_UNKNOWN_FIELD")

    def test_rejects_unknown_provider_reference(self) -> None:
        self.assert_rejected(
            self.mutate_script({"name": "missing-provider"}), "E_SCRIPT_PROVIDER_MISSING"
        )

    def test_rejects_non_string_argument(self) -> None:
        self.assert_rejected(
            self.mutate_script({"argument": {"tab": False}}), "E_SCRIPT_ARGUMENT_TYPE"
        )

    def test_rejects_invalid_script_type(self) -> None:
        self.assert_rejected(self.mutate_script({"type": "response-body"}), "E_SCRIPT_TYPE")

    def test_rejects_script_without_match(self) -> None:
        override = valid_override()
        override["http"]["script"][0].pop("match")
        self.assert_rejected(self.bundle(override=override), "E_SCRIPT_MATCH")


class InvalidRewriteTests(ValidatorTestCase):
    def test_rejects_mock_inside_url_rewrite(self) -> None:
        override = valid_override()
        override["http"]["url-rewrite"] = ["(?i)^https://api\\.example\\.com/mocked - mock"]
        self.assert_rejected(self.bundle(override=override), "E_URL_MOCK")

    def test_rejects_unquoted_body_rewrite_scalar(self) -> None:
        root = self.bundle()
        text = (root / "Demo.stoverride").read_text(encoding="utf-8")
        quoted = (
            "  - '(?i)^https://api\\.example\\.com/feed response-replace-regex "
            "<ad>[^<]*</ad> <ad></ad>'"
        )
        self.assertIn(quoted, text)
        mutated = text.replace(quoted, quoted.replace("  - '", "  - ").removesuffix("'"))
        (root / "Demo.stoverride").write_text(mutated, encoding="utf-8")
        self.assert_rejected(root, "E_BODY_YAML_UNQUOTED")

    def test_rejects_capture_substitution_in_redirect_target(self) -> None:
        override = valid_override()
        override["http"]["url-rewrite"] = [
            "(?i)^https://api\\.example\\.com/go https://target.example.test/${1} 307",
        ]
        self.assert_rejected(self.bundle(override=override), "E_URL_UNDOCUMENTED_CAPTURE")

    def test_rejects_non_absolute_redirect_target(self) -> None:
        override = valid_override()
        override["http"]["url-rewrite"] = ["(?i)^https://api\\.example\\.com/go /landing 307"]
        self.assert_rejected(self.bundle(override=override), "E_URL_REDIRECT_TARGET")

    def test_rejects_legacy_header_action(self) -> None:
        override = valid_override()
        override["http"]["header-rewrite"] = [
            "(?i)^https://api\\.example\\.com/req header-add x-test 1",
        ]
        self.assert_rejected(self.bundle(override=override), "E_HEADER_ACTION")

    def test_rejects_quoted_jq_program(self) -> None:
        override = valid_override()
        override["http"]["body-rewrite"] = [
            '(?i)^https://api\\.example\\.com/data response-jq "del(.data.ad)"',
        ]
        self.assert_rejected(self.bundle(override=override), "E_BODY_JQ_QUOTED")

    def test_rejects_unbalanced_jq_program(self) -> None:
        override = valid_override()
        override["http"]["body-rewrite"] = [
            "(?i)^https://api\\.example\\.com/data response-jq del(.data.ad",
        ]
        self.assert_rejected(self.bundle(override=override), "E_BODY_JQ_UNBALANCED")


class CrossReferenceTests(ValidatorTestCase):
    def test_rejects_unknown_rule_policy(self) -> None:
        override = valid_override()
        override["rules"] = ["DOMAIN,ad.example.com,NOT-A-POLICY"]
        self.assert_rejected(self.bundle(override=override), "E_RULE_POLICY_MISSING")

    def test_rejects_unknown_rule_type(self) -> None:
        override = valid_override()
        override["rules"] = ["FOOBAR,ad.example.com,REJECT"]
        self.assert_rejected(self.bundle(override=override), "E_RULE_TYPE")

    def test_rejects_dangling_asset(self) -> None:
        providers = copy.deepcopy(VALID_PROVIDERS)
        providers["fixture-script"]["url"] = f"{PUBLIC_BASE}/scripts/0123456789ab-missing.js"
        self.assert_rejected(self.bundle(providers=providers), "E_ASSET_DANGLING")

    def test_rejects_corrupt_asset_against_manifest(self) -> None:
        root = self.bundle()
        diagnostics = self.validate(
            root,
            assets={SCRIPT_RELPATH: "0" * 64},
        )
        self.assertIn("E_ASSET_CORRUPT", codes(blocking(diagnostics)))

    def test_rejects_provider_bundle_kelee_origin(self) -> None:
        providers = copy.deepcopy(VALID_PROVIDERS)
        providers["fixture-script"]["url"] = (
            "https://kelee.one/Resource/JavaScript/Spotify/Spotify_remove_ads.js"
        )
        self.assert_rejected(self.bundle(providers=providers), "E_PROVIDER_KELEE_URL")

    def test_rejects_unknown_top_level_field(self) -> None:
        override = valid_override()
        override["enabled"] = True
        self.assert_rejected(self.bundle(override=override), "E_TOP_UNKNOWN_FIELD")

    def test_rejects_unknown_http_section(self) -> None:
        override = valid_override()
        override["http"]["data-rewrite"] = []
        self.assert_rejected(self.bundle(override=override), "E_HTTP_UNKNOWN_SECTION")

    def test_rejects_leftover_loon_placeholder(self) -> None:
        override = valid_override()
        override["http"]["header-rewrite"] = [
            "(?i)^https://api\\.example\\.com/req/ response-add x-token ${argument}",
        ]
        self.assert_rejected(self.bundle(override=override), "E_LEGACY_MARKER")

    def test_rejects_leftover_placeholder_in_rule(self) -> None:
        override = valid_override()
        override["rules"] = ["DOMAIN,${host},REJECT"]
        self.assert_rejected(self.bundle(override=override), "E_RULE_PLACEHOLDER")


class BundleCompletenessTests(ValidatorTestCase):
    def test_rejects_empty_bundle_directory(self) -> None:
        root = self.tmp / "empty"
        root.mkdir()
        self.assert_rejected(root, "E_BUNDLE_EMPTY")

    def test_rejects_empty_override(self) -> None:
        self.assert_rejected(self.bundle(override="{}\n"), "E_EMPTY_OVERRIDE")

    def test_rejects_missing_target_output(self) -> None:
        self.assert_rejected(
            self.bundle(targets=[{"filename": "Missing.lpx"}]), "E_TARGET_OUTPUT_MISSING"
        )

    def test_rejects_incomplete_targets_against_expected(self) -> None:
        diagnostics = self.validate(self.bundle(), expected_targets=["Demo.lpx", "Other.lpx"])
        self.assertIn("E_TARGETS_INCOMPLETE", codes(blocking(diagnostics)))

    def test_cli_returns_two_for_invalid_bundle(self) -> None:
        override = valid_override()
        override["http"]["mock"][0]["status-code"] = "200"
        root = self.bundle(override=override)
        code, output = self.invoke_cli(root)
        self.assertEqual(code, 2)
        reported = json.loads(output)
        self.assertIn("E_MOCK_STATUS_TYPE", [item["code"] for item in reported])

    def test_validate_bundle_or_raise_raises_on_blocking(self) -> None:
        override = valid_override()
        override["http"]["mock"][0].pop("text")
        root = self.bundle(override=override)
        with self.assertRaises(ValidationError):
            validate_bundle_or_raise(root, public_base=PUBLIC_BASE)


if __name__ == "__main__":
    unittest.main()
