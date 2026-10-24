#!/usr/bin/env python3
"""Regression tests for the fixed Loon ``.lpx`` -> Stash converter (v3).

Run::

    python tests/test_kelee_conversion.py

Everything is offline and deterministic: upstream scripts/jq programs are served
from ``tests/fixtures/conversion`` through ``ResourceResolver(snapshots=...)``.
Generated capture-rewrite request scripts are executed with Node 24 through
``tests/fixtures/conversion/node_runner.cjs`` and their exact ``$done()`` output
is asserted; when Node is not on PATH those tests skip with an explicit marker.

Evidence-only tests that need ``.local/upstream-audit`` live in
``tests/test_audit_conversion.py`` / ``tests/test_spotify_argument_repro.py`` and
skip cleanly when the local audit snapshots are absent (fresh CI checkout).
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "conversion"
LPX_BASE_URL = "https://kelee.one/Tool/Loon/Lpx"
PUBLIC_BASE = "https://example.test"

sys.path.insert(0, str(SCRIPTS_DIR))

import yaml  # noqa: E402

import kelee_config as cfg  # noqa: E402
from convert_kelee_lpx import (  # noqa: E402
    ConversionError,
    convert_lpx,
    convert_lpx_to_stash,
    dump_stash_yaml,
)
from kelee_assets import ResourceResolver  # noqa: E402

NODE = shutil.which("node")
NODE_RUNNER = FIXTURES / "node_runner.js"
NODE_REQUIRED = unittest.skipUnless(
    NODE,
    "Node not available on PATH: generated request scripts could not be executed",
)

ABSENT = object()

PNG_BODY = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGMAAQAABQABDQottAAAAABJRU5ErkJggg=="
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def fixture_snapshots() -> dict[str, bytes]:
    """URL -> raw bytes replay map (see fixtures/conversion/snapshots.json)."""

    mapping = json.loads((FIXTURES / "snapshots.json").read_text(encoding="utf-8"))
    return {url: (FIXTURES / rel).read_bytes() for url, rel in mapping.items()}


def convert_with_resolver(
    name: str,
    *,
    url_name: str | None = None,
    snapshots: bool = True,
    public_base: str = PUBLIC_BASE,
    mirror_scripts: bool = True,
) -> tuple[object, ResourceResolver]:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    resolver = ResourceResolver(
        public_base,
        fetch=False,
        snapshots=fixture_snapshots() if snapshots else {},
    )
    result = convert_lpx(
        text,
        f"{LPX_BASE_URL}/{url_name or name}",
        resolver=resolver,
        public_base=public_base,
        mirror_scripts=mirror_scripts,
    )
    return result, resolver


def convert(name: str, **kwargs):
    result, _resolver = convert_with_resolver(name, **kwargs)
    return result


def blocking_codes(result) -> list[str]:
    return [d.code for d in result.diagnostics if d.blocking]


def all_codes(result) -> list[str]:
    return [d.code for d in result.diagnostics]


def non_blocking(result, code: str):
    return [d for d in result.diagnostics if d.code == code and not d.blocking]


def scratch_dir(testcase: unittest.TestCase) -> Path:
    path = Path(tempfile.mkdtemp(prefix="kelee-conv-test-"))
    testcase.addCleanup(shutil.rmtree, path, True)
    return path


def run_node_script(
    script_content: bytes | str,
    *,
    cwd: Path,
    request: dict | None = None,
    response: dict | None = None,
    response_body_b64: str | None = None,
    argument=ABSENT,
) -> dict:
    """Execute one generated/upstream script in the Node sandbox runner."""

    assert NODE is not None and NODE_RUNNER.is_file()
    script_path = cwd / "script.js"
    script_path.write_bytes(
        script_content.encode("utf-8") if isinstance(script_content, str) else script_content
    )
    spec: dict = {}
    if request is not None:
        spec["request"] = request
    if response is not None:
        spec["response"] = response
    if response_body_b64 is not None:
        spec["responseBodyBase64"] = response_body_b64
    if argument is not ABSENT:
        spec["argument"] = argument
    context_path = cwd / "context.json"
    context_path.write_text(json.dumps(spec), encoding="utf-8")
    result_path = cwd / "result.json"
    process = subprocess.run(
        [NODE, str(NODE_RUNNER), str(script_path), str(context_path), str(result_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if process.returncode != 0 or not result_path.is_file():
        raise AssertionError(
            f"node runner failed rc={process.returncode}\nstdout={process.stdout}\nstderr={process.stderr}"
        )
    return json.loads(result_path.read_text(encoding="utf-8"))


def publish_script_text(resolver: ResourceResolver, provider_url: str) -> bytes:
    """Look up the published (content-addressed) asset bytes behind a provider URL."""

    assert provider_url.startswith(PUBLIC_BASE + "/"), provider_url
    relpath = provider_url[len(PUBLIC_BASE) + 1 :]
    assets = resolver.published_assets()
    if relpath not in assets:
        raise AssertionError(f"published asset missing: {relpath} (known: {sorted(assets)})")
    return assets[relpath]


def compact(rule: str) -> str:
    return re.sub(r"\s+", "", rule)


def balanced_jq(expression: str) -> bool:
    """Independent (test-side) bracket balance check for jq / regex payloads."""

    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    in_string = False
    index = 0
    while index < len(expression):
        char = expression[index]
        if in_string:
            if char == "\\":
                index += 2
                continue
            if char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "([{":
            stack.append(char)
        elif char in ")]}":
            if not stack or stack.pop() != pairs[char]:
                return False
        index += 1
    return not stack and not in_string


def assert_valid_stash_fields(testcase: unittest.TestCase, item: dict, allowed: set[str]) -> None:
    extra = set(item) - allowed
    testcase.assertFalse(extra, f"unexpected Stash fields: {sorted(extra)} in {item!r}")


# ---------------------------------------------------------------------------
# 1. Mock conversion
# ---------------------------------------------------------------------------


class MockConversionTests(unittest.TestCase):
    """response.body.mock(...) -> native http.mock items (Stash schema only)."""

    MOCK_FIELDS = {"match", "status-code", "headers", "text", "base64"}

    def setUp(self) -> None:
        self.result = convert("mock_contract.lpx")
        self.mocks = self.result.stash.get("http", {}).get("mock", [])

    def mock_for(self, marker: str) -> dict:
        for entry in self.mocks:
            if marker in entry["match"]:
                return entry
        self.fail(f"no mock matching {marker!r} in {[m['match'] for m in self.mocks]}")

    def test_mock_entries_use_only_current_stash_schema(self) -> None:
        self.assertEqual(len(self.mocks), 5, self.mocks)
        for entry in self.mocks:
            assert_valid_stash_fields(self, entry, self.MOCK_FIELDS)
            self.assertIsInstance(entry["match"], str)
            self.assertTrue(entry["match"])
            self.assertIsInstance(entry["status-code"], int)
            self.assertNotIsInstance(entry["status-code"], bool)
            for forbidden in ("body", "data", "data-type", "statusCode", "mock-data-is-base64"):
                self.assertNotIn(forbidden, entry)
            self.assertTrue(
                ("text" in entry) != ("base64" in entry),
                f"mock must use exactly one of text/base64: {entry!r}",
            )

    def test_text_mock_preserves_escaped_json_body_and_defaults(self) -> None:
        entry = self.mock_for("/json")
        self.assertEqual(entry["status-code"], 200)  # Loon default
        self.assertEqual(
            entry["headers"],
            {"Content-Type": "application/json"},
            "Loon json mock content type must map to application/json",
        )
        self.assertEqual(
            entry["text"],
            r'{"ok":true,"items":[1,2,3],"nested":{"quote":"\""}}',
            "escaped Loon string body must be decoded exactly once",
        )
        self.assertNotIn("base64", entry)

    def test_text_mock_status_and_plain_content_type(self) -> None:
        entry = self.mock_for("/teapot$")
        self.assertEqual(entry["status-code"], 418)
        self.assertEqual(entry["headers"], {"Content-Type": "text/plain"})
        self.assertEqual(entry["text"], "short and stout")

    def test_png_mock_base64_decodes_to_original_bytes(self) -> None:
        entry = self.mock_for("/pixel\\.png$")
        self.assertIn("base64", entry)
        self.assertNotIn("text", entry)
        self.assertEqual(entry["headers"], {"Content-Type": "image/png"})
        self.assertEqual(base64.b64decode(entry["base64"], validate=True), PNG_BODY)

    def test_html_mock_and_content_type_mapping(self) -> None:
        entry = self.mock_for("other\\.example\\.com/payload$")
        self.assertEqual(entry["headers"], {"Content-Type": "text/html", "x-other": "1"})
        self.assertEqual(entry["text"], '<div class="ad">x</div>')

    def test_plain_mock_defaults_to_status_200(self) -> None:
        entry = self.mock_for("/plain$")
        self.assertEqual(entry["status-code"], 200)
        self.assertEqual(entry["headers"], {"Content-Type": "text/plain"})
        self.assertEqual(entry["text"], "ok")

    def test_mock_body_never_contains_the_argument_list(self) -> None:
        for entry in self.mocks:
            payload = entry.get("text", "") + entry.get("base64", "")
            for leaked in (
                "response.body.mock",
                "Content-Type",
                '"json"',
                '"text"',
                "isBase64",
            ):
                self.assertNotIn(leaked, payload, f"mock argument list leaked into body: {entry!r}")

    def test_sibling_header_add_merges_into_matching_mock_only(self) -> None:
        merged = {
            entry["match"]: entry["headers"]
            for entry in self.mocks
            if "x-other" in entry["headers"]
        }
        self.assertEqual(len(merged), 1, f"x-other must merge into exactly one mock: {merged}")
        self.assertIn("other\\.example\\.com/payload$", next(iter(merged)))
        self.assertEqual(
            self.result.stash["http"].get("header-rewrite"),
            ["(?i)^https://headers\\.example\\.com/x$ response-add x-only v"],
            "a header add without a matching mock stays an http.header-rewrite rule",
        )
        merges = [p for p in self.result.patches if p["kind"] == "mock-header-merge"]
        self.assertEqual(len(merges), 1, merges)

    def test_header_add_does_not_leak_to_other_paths_on_same_host(self) -> None:
        """Reported defect #1 regression: host-only matching over-merges."""

        result = convert("mock_header_merge.lpx")
        mocks = result.stash["http"]["mock"]
        by_match = {entry["match"]: entry for entry in mocks}
        grpc_a = by_match["(?i)^https://grpc\\.example\\.com/svc/A$"]
        grpc_b = by_match["(?i)^https://grpc\\.example\\.com/svc/B$"]
        for entry in (grpc_a, grpc_b):
            self.assertEqual(
                entry["headers"].get("grpc-status"),
                "0",
                "grpc-status must merge into the two gRPC mocks it matches",
            )
        json_mock = by_match["(?i)^https://api\\.example\\.com/json\\?"]
        self.assertNotIn(
            "x-leak",
            json_mock["headers"],
            "response.header.add for /other must not be merged into the /json mock on the same host",
        )
        self.assertIn(
            "(?i)^https://api\\.example\\.com/other$ response-add x-leak no",
            result.stash["http"].get("header-rewrite", []),
            "an unmergeable header add must be emitted as http.header-rewrite",
        )
        merges = [p for p in result.patches if p["kind"] == "mock-header-merge"]
        self.assertEqual(len(merges), 2, merges)

    def test_malformed_base64_blocks(self) -> None:
        result = convert("mock_invalid_base64.lpx")
        self.assertTrue(result.blocking)
        self.assertIn("E_MOCK_BASE64", all_codes(result))
        self.assertFalse(result.stash.get("http", {}).get("mock"))

    def test_unknown_content_type_blocks_and_reports_type(self) -> None:
        result = convert("mock_unknown_content_type.lpx")
        self.assertTrue(result.blocking)
        self.assertTrue(
            any("yaml" in d.message for d in result.diagnostics if d.blocking),
            [d.message for d in result.diagnostics],
        )

    def test_bad_status_and_missing_arguments_block(self) -> None:
        result = convert("mock_bad_status.lpx")
        self.assertTrue(result.blocking)
        messages = " | ".join(d.message for d in result.diagnostics if d.blocking)
        self.assertIn("999", messages)
        self.assertIn("42", messages)
        self.assertIn("mock", messages)
        self.assertFalse(result.stash.get("http", {}).get("mock"))


# ---------------------------------------------------------------------------
# 2. Script arguments / enable / options
# ---------------------------------------------------------------------------


class ScriptArgumentTests(unittest.TestCase):
    """Loon [Argument] defaults must be encoded with each script's real protocol."""

    def setUp(self) -> None:
        self.result = convert("script_arguments.lpx")
        self.entries = self.result.stash.get("http", {}).get("script", [])
        self.providers = self.result.stash.get("script-providers", {})

    def entry_for(self, marker: str) -> dict:
        for entry in self.entries:
            if marker in entry["match"]:
                return entry
        self.fail(f"no script entry matching {marker!r}: {[e['match'] for e in self.entries]}")

    def test_spotify_json_object_argument_and_options(self) -> None:
        entry = self.entry_for("spotify-bootstrap")
        self.assertEqual(entry["argument"], '{"tab":false,"useractivity":true}')
        self.assertIsInstance(entry["argument"], str)
        self.assertIs(entry["require-body"], True)
        self.assertIs(entry["binary-mode"], True)
        self.assertEqual(entry["timeout"], 5)
        self.assertEqual(entry["type"], "response")
        assert_valid_stash_fields(self, entry, cfg.SCRIPT_FIELDS)
        for forbidden in ("enable", "tag", "requires-body", "requires_body", "binary-body-mode"):
            self.assertNotIn(forbidden, entry)

    def test_bilibili_argument_keeps_declaration_order_and_types(self) -> None:
        entry = self.entry_for("bili-response")
        self.assertEqual(
            entry["argument"],
            '{"displayUpList":"show","purifyComment":true,"sponsorBlock":false,"logLevel":"off"}',
        )
        parsed = json.loads(entry["argument"])
        self.assertIsInstance(parsed["displayUpList"], str)
        self.assertIs(parsed["purifyComment"], True)
        self.assertIs(parsed["sponsorBlock"], False)
        self.assertEqual(parsed["logLevel"], "off")

    def test_weatherkit_argument_is_amp_joined_key_value(self) -> None:
        entry = self.entry_for("weatherkit\\.example\\.com/api/v2/weather")
        self.assertEqual(
            entry["argument"],
            "Weather.Provider=ColorfulClouds"
            "&NextHour.Provider=ColorfulClouds"
            "&AirQuality.Calculate.Algorithm=EU_EAQI"
            "&API.ColorfulClouds.Token="
            "&API.QWeather.Host=devapi.qweather.com"
            "&API.QWeather.Token="
            "&API.WAQI.Token="
            "&Storage=Argument"
            "&LogLevel=WARN",
            "WeatherKit reads &-joined key=value pairs with original dotted names",
        )
        # External (non-kelee.one) scripts keep their upstream provider URL.
        provider = self.providers[entry["name"]]
        self.assertEqual(
            provider["url"],
            "https://github.com/NSRingo/WeatherKit/releases/download/v3.1.0/response.bundle.js",
        )

    def test_number_argument_type_is_preserved(self) -> None:
        entry = self.entry_for("/numeric")
        self.assertEqual(entry["argument"], '{"sampleRate":0.5}')
        self.assertIsInstance(json.loads(entry["argument"])["sampleRate"], float)

    def test_enable_true_keeps_script(self) -> None:
        entry = self.entry_for("bili-request-enabled")
        self.assertEqual(entry["argument"], '{"purifyComment":true,"logLevel":"off"}')
        self.assertIs(entry["require-body"], True)
        self.assertIs(entry["binary-mode"], True)
        self.assertEqual(entry["timeout"], 7)
        self.assertNotIn("enable", entry)

    def test_enable_true_resolution_is_recorded(self) -> None:
        """BRIEF decision #5: an enable=${...} resolved to true must be recorded."""

        recorded = non_blocking(self.result, "E_SCRIPT_ENABLE_RESOLVED")
        self.assertEqual(len(recorded), 1, [d.as_dict() for d in self.result.diagnostics])
        message = recorded[0].message
        self.assertIn("True", message)
        self.assertIn("enable", message.lower())
        self.assertIn("构建时", message)

    def test_enable_false_omits_script_with_intentional_ignore(self) -> None:
        self.assertFalse(
            any("bili-request-disabled" in entry["match"] for entry in self.entries),
            "a script whose enable= default is false must be omitted",
        )
        ignored = non_blocking(self.result, "E_SCRIPT_DISABLED_BY_DEFAULT")
        self.assertEqual(len(ignored), 1, [d.as_dict() for d in self.result.diagnostics])
        self.assertIn("Bilibili_proto_request_kokoryh.js", ignored[0].message)
        self.assertIn("Stash 无逐脚本 enable 字段", ignored[0].message)

    def test_script_that_never_reads_argument_drops_it_with_ignore(self) -> None:
        entry = self.entry_for("/ignored")
        self.assertNotIn("argument", entry)
        ignored = non_blocking(self.result, "E_SCRIPT_ARG_IGNORED")
        self.assertEqual(len(ignored), 1, [d.as_dict() for d in self.result.diagnostics])
        self.assertIn("Bilibili_json_kokoryh.js", ignored[0].message)

    def test_argument_entries_count_and_no_placeholders(self) -> None:
        materialized = [entry for entry in self.entries if "argument" in entry]
        # 7 declarations in this fixture, one enable=${...} resolved false (dropped),
        # one script that never reads $argument (argument dropped) -> 5 emitted.
        self.assertEqual(self.result.counts.get("argument_entries"), 5)
        self.assertEqual(len(materialized), 5)
        self.assertEqual(self.result.counts.get("argument_decls"), 17)
        dumped = json.dumps(self.result.stash, ensure_ascii=False)
        self.assertNotIn("${", dumped)
        self.assertNotIn("[Argument]", dumped)

    def test_all_provider_references_and_assets_resolve(self) -> None:
        for entry in self.entries:
            self.assertIn(entry["name"], self.providers)
        for name, info in self.providers.items():
            self.assertRegex(info["url"], r"^(https://example\.test/scripts/[0-9a-f]{12}-|https://github\.com/)")
            self.assertEqual(info["interval"], 86400)

    def test_unknown_argument_protocol_blocks(self) -> None:
        result = convert("script_unknown_protocol.lpx")
        self.assertTrue(result.blocking)
        self.assertIn("E_SCRIPT_ARG_PROTOCOL", all_codes(result))
        payload = " ".join(d.message for d in result.diagnostics if d.blocking)
        self.assertIn("argument_consumer.js", payload)
        self.assertFalse(result.stash.get("http", {}).get("script"))


# ---------------------------------------------------------------------------
# 3. Capture URL rewrites (redirect / url.replace -> generated request scripts)
# ---------------------------------------------------------------------------


class CaptureRewriteTests(unittest.TestCase):
    """Stash has no capture substitution in url-rewrite; the converter emits scripts."""

    def setUp(self) -> None:
        self.tmp = scratch_dir(self)
        self.result, self.resolver = convert_with_resolver("capture_rewrites.lpx")
        self.entries = self.result.stash["http"]["script"]
        self.providers = self.result.stash["script-providers"]
        self.generated = self.result.generated_scripts

    def script_for(self, marker: str) -> tuple[dict, dict]:
        for record in self.result.generated_scripts:
            for entry in self.entries:
                if entry["name"] == record["provider"] and marker in entry["match"]:
                    return record, entry
        self.fail(f"no generated script matching {marker!r}")

    def test_generated_scripts_and_providers_shape(self) -> None:
        self.assertEqual(self.result.counts.get("generated_scripts"), 3)
        self.assertEqual(len(self.generated), 3)
        self.assertEqual(len(self.entries), 3)
        self.assertEqual(len(self.providers), 3)
        modes = [record["mode"] for record in self.generated]
        self.assertEqual(modes, ["redirect", "redirect", "replace"])
        self.assertEqual([record["status"] for record in self.generated], [307, 307, 0])
        for record in self.generated:
            provider = self.providers[record["provider"]]
            self.assertRegex(
                provider["url"],
                r"^https://example\.test/scripts/[0-9a-f]{12}-[a-z0-9-]+\.js$",
            )
            self.assertEqual(provider["interval"], 86400)
            content = publish_script_text(self.resolver, provider["url"])
            self.assertIn(b"$request.url", content)
            self.assertIn(b"const re = new RegExp(", content)
            if record["mode"] == "redirect":
                self.assertIn(b"$done({ response: { status: 307, headers: { Location: newUrl } } });", content)
            else:
                self.assertIn(b"$done({ url: newUrl });", content)
        for entry in self.entries:
            self.assertEqual(entry["type"], "request")
            self.assertTrue(entry["match"].startswith("(?i)"))
            self.assertIn(entry["name"], self.providers)

    @NODE_REQUIRED
    def test_redirect_script_replaces_matched_range(self) -> None:
        record, _entry = self.script_for("c\\.example\\.com/go")
        content = publish_script_text(self.resolver, self.providers[record["provider"]]["url"])
        output = run_node_script(
            content,
            cwd=scratch_dir(self),
            request={"url": "https://c.example.com/go?u=https://media.example.org/v.mp4&sig=abc"},
            response={},
        )
        done = output["done"]
        self.assertTrue(done["called"], output)
        self.assertEqual(done["value"]["response"]["status"], 307)
        self.assertEqual(
            done["value"]["response"]["headers"]["Location"],
            "https://media.example.org/v.mp4",
        )
        self.assertNotIn("url", done["value"])

    @NODE_REQUIRED
    def test_redirect_script_preserves_unmatched_suffix(self) -> None:
        record, _entry = self.script_for("t\\.example\\.com/go")
        content = publish_script_text(self.resolver, self.providers[record["provider"]]["url"])
        output = run_node_script(
            content,
            cwd=scratch_dir(self),
            request={"url": "https://t.example.com/go/PAYLOAD/end?q=1&x=2"},
            response={},
        )
        self.assertEqual(
            output["done"]["value"]["response"]["headers"]["Location"],
            "https://target.example.com/PAYLOAD/done?q=1&x=2",
            "only the regex-matched URL range may be replaced; the suffix must survive",
        )
        self.assertEqual(output["done"]["value"]["response"]["status"], 307)

    @NODE_REQUIRED
    def test_url_replace_script_modifies_url_with_suffix_preserved(self) -> None:
        record, _entry = self.script_for("artist")
        content = publish_script_text(self.resolver, self.providers[record["provider"]]["url"])
        output = run_node_script(
            content,
            cwd=scratch_dir(self),
            request={"url": "https://api.example.com/artist/abc123&platform=iphone&extra=1"},
            response={},
        )
        done = output["done"]
        self.assertEqual(done["value"]["url"], "https://api.example.com/artist/abc123&platform=ipad&extra=1")
        self.assertNotIn("response", done["value"], "url.replace must not short-circuit a redirect")

    @NODE_REQUIRED
    def test_scripts_return_empty_done_for_unmatched_urls(self) -> None:
        for marker in ("c\\.example\\.com/go", "artist"):
            record, _entry = self.script_for(marker)
            content = publish_script_text(self.resolver, self.providers[record["provider"]]["url"])
            output = run_node_script(
                content,
                cwd=scratch_dir(self),
                request={"url": "https://no-match.example.com/whatever"},
                response={},
            )
            self.assertEqual(output["done"]["value"], {}, f"{marker} must not rewrite unmatched URLs")

    def test_generated_scripts_are_deterministic(self) -> None:
        again, _resolver = convert_with_resolver("capture_rewrites.lpx")
        self.assertEqual(
            [(record["provider"], record["filename"]) for record in self.result.generated_scripts],
            [(record["provider"], record["filename"]) for record in again.generated_scripts],
        )
        self.assertEqual(self.providers, again.stash["script-providers"])


# ---------------------------------------------------------------------------
# 4. Body replace (keli regression)
# ---------------------------------------------------------------------------


class BodyReplaceTests(unittest.TestCase):
    """response.body.replace -> native response-replace-regex with escapes intact."""

    KELI_RULE = (
        "(?i)^https?://cmds\\.omesoft\\.com/HypnotistWS\\.asmx response-replace-regex "
        "<GetADResult>\\.\\*\\?</GetADResult> "
        '<GetADResult>{"ret":0,"msg":"正常","err_code":0,"data":{"ad":[]}}</GetADResult>'
    )

    def setUp(self) -> None:
        self.result = convert("body_replace.lpx")
        self.body = self.result.stash["http"]["body-rewrite"]

    def test_keli_body_replace_is_preserved_exactly(self) -> None:
        self.assertEqual(len(self.body), 2, self.body)
        self.assertEqual(self.body[0], self.KELI_RULE)
        self.assertIn("<GetADResult>\\.\\*\\?</GetADResult>", self.body[0])
        self.assertNotIn("\\/", self.body[0].split("response-replace-regex", 1)[0])
        self.assertNotIn(".*?", self.body[0], "escaped quantifiers must not be rewritten")

    def test_body_replace_keeps_source_order(self) -> None:
        self.assertTrue(self.body[0].startswith("(?i)^https?://cmds\\.omesoft\\.com/"))
        self.assertEqual(
            self.body[1],
            "(?i)^https://api\\.example\\.com/plain response-replace-regex foo/bar baz",
        )

    def test_body_rewrite_yaml_is_one_quoted_scalar(self) -> None:
        rule_lines = [
            line
            for line in self.result.yaml.splitlines()
            if line.lstrip().startswith("- ") and "response-replace-regex" in line
        ]
        self.assertEqual(len(rule_lines), 2, rule_lines)
        for line in rule_lines:
            stripped = line.strip()
            self.assertTrue(stripped.startswith("- '"), line)
            self.assertTrue(stripped.endswith("'"), line)
        parsed = yaml.safe_load(self.result.yaml)
        self.assertEqual(parsed["http"]["body-rewrite"], self.body)

    def test_body_replace_with_literal_space_blocks(self) -> None:
        result = convert("body_replace_space.lpx")
        self.assertTrue(result.blocking)
        self.assertIn("E_BODY_REPLACE_SPACE", all_codes(result))
        self.assertFalse(result.stash.get("http", {}).get("body-rewrite"))

    def test_keli_escaped_quantifier_warns_without_rewriting(self) -> None:
        warnings = non_blocking(self.result, "E_BODY_REPLACE_ESCAPED_QUANTIFIER")
        self.assertEqual(len(warnings), 1, [d.as_dict() for d in self.result.diagnostics])
        self.assertIn("量词", warnings[0].message)
        # The warning must not change the (verbatim) regex that is shipped.
        self.assertIn("<GetADResult>\\.\\*\\?</GetADResult>", self.result.yaml)
        self.assertNotIn(".*?", self.result.stash["http"]["body-rewrite"][0])


# ---------------------------------------------------------------------------
# 5. jq comment ordering / inlining
# ---------------------------------------------------------------------------


class JqCommentTests(unittest.TestCase):
    """The old bug flattened newlines before stripping comments (comment swallowed program)."""

    def setUp(self) -> None:
        self.result = convert("jq_comments.lpx")
        self.body = self.result.stash["http"]["body-rewrite"]
        self.program = ".data.items |= map(select(.ad == null))"

    def test_comments_are_stripped_before_flattening(self) -> None:
        self.assertEqual(len(self.body), 2, self.body)
        for rule in self.body:
            self.assertNotIn("#", rule, rule)
            self.assertTrue(rule.endswith(self.program), rule)
            self.assertIn(self.program, rule)
        self.assertTrue(balanced_jq(self.body[0].split(" response-jq ", 1)[1]))

    def test_remote_jq_is_inlined_flattened_and_not_truncated(self) -> None:
        remote_rule = next(rule for rule in self.body if "/remote" in rule)
        self.assertTrue(remote_rule.endswith(self.program), remote_rule)
        self.assertNotIn("\n", remote_rule)
        self.assertEqual(self.result.counts.get("remote_jq"), 1)
        self.assertIn(
            "https://jq.example.test/clean.jq",
            self.result.upstream_resources,
            self.result.upstream_resources,
        )
        self.assertEqual(self.result.upstream_resources["https://jq.example.test/clean.jq"]["kind"], "jq")

    def test_comment_brackets_do_not_swallow_program(self) -> None:
        rule = self.body[0]
        payload = rule.split(" response-jq ", 1)[1]
        self.assertGreater(len(payload), 20, "program looks truncated after comment stripping")
        self.assertEqual(payload, self.program)


# ---------------------------------------------------------------------------
# 6. Regex flag conversion
# ---------------------------------------------------------------------------


class RegexFlagTests(unittest.TestCase):
    def setUp(self) -> None:
        self.result = convert("regex_flags.lpx")
        self.url_rewrites = self.result.stash["http"]["url-rewrite"]
        self.scripts = self.result.stash["http"]["script"]

    def test_case_insensitive_regex(self) -> None:
        self.assertIn(
            "(?i)^https://api\\.example\\.com/CaseSensitive\\? - reject",
            self.url_rewrites,
        )

    def test_multiple_flags_use_canonical_ims_order(self) -> None:
        self.assertIn("(?ims)^https://api\\.example\\.com/MultiFlag/ - reject", self.url_rewrites)

    def test_escaped_slash_is_unescaped_and_other_escapes_kept(self) -> None:
        rule = next(rule for rule in self.url_rewrites if "escaped" in rule)
        self.assertEqual(rule, "(?i)^https?://api\\.example\\.com/escaped/path - reject")
        self.assertNotIn("\\/", rule)
        self.assertIn("api\\.example\\.com", rule)

    def test_script_match_keeps_flags(self) -> None:
        self.assertEqual(len(self.scripts), 1)
        self.assertEqual(self.scripts[0]["match"], "(?i)^https://api\\.example\\.com/script/Case/")

    def test_unsupported_flag_blocks_and_names_the_flag(self) -> None:
        result = convert("regex_bad_flag.lpx")
        self.assertTrue(result.blocking)
        messages = " ".join(d.message for d in result.diagnostics if d.blocking)
        self.assertIn("'x'", messages)
        self.assertIn("regex flag", messages)
        self.assertFalse(result.stash.get("http", {}).get("url-rewrite"))


# ---------------------------------------------------------------------------
# 7. Rules / policies
# ---------------------------------------------------------------------------


class RulePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.result = convert("rules_policies.lpx")
        self.rules = self.result.stash["rules"]
        self.compacted = [compact(rule) for rule in self.rules]

    def test_policy_mapping_and_option_order(self) -> None:
        self.assertIn("DOMAIN,proxy.example.com,Proxy", self.compacted)
        self.assertIn("DOMAIN,direct.example.com,DIRECT", self.compacted)
        self.assertIn("DOMAIN,reject.example.com,REJECT", self.compacted)
        self.assertIn("DOMAIN-SUFFIX,drop.example.com,REJECT-DROP", self.compacted)
        self.assertIn("IP-CIDR,10.0.0.0/8,DIRECT,no-resolve", self.compacted)

    def test_nested_and_or_structure_is_kept(self) -> None:
        expected = (
            "AND,((DOMAIN-SUFFIX,ads.example.com),"
            "(OR,((DOMAIN-KEYWORD,stun),(DOMAIN-KEYWORD,tracker),(DOMAIN-KEYWORD,p2p)))),REJECT"
        )
        self.assertIn(expected, self.compacted)

    def test_source_rule_order_is_preserved(self) -> None:
        source = [
            "DOMAIN,proxy.example.com,Proxy",
            "DOMAIN,direct.example.com,DIRECT",
            "DOMAIN,reject.example.com,REJECT",
            "DOMAIN-SUFFIX,drop.example.com,REJECT-DROP",
            "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
            (
                "AND,((DOMAIN-SUFFIX,ads.example.com),"
                "(OR,((DOMAIN-KEYWORD,stun),(DOMAIN-KEYWORD,tracker),(DOMAIN-KEYWORD,p2p)))),REJECT"
            ),
            "DOMAIN,img.example.com,REJECT",
            "DOMAIN,arr.example.com,REJECT",
            "DOMAIN,code.example.com,REJECT",
            "DOMAIN,img.example.com,REJECT",
            "DOMAIN-SUFFIX,normal.example.com,Proxy",
        ]
        without_mirrors = [
            rule for rule in self.compacted if "(NETWORK,UDP)" not in rule
        ]
        self.assertEqual(without_mirrors, source)

    def test_degraded_loon_reject_policies_become_reject_with_patch(self) -> None:
        for raw in ("REJECT-DICT", "REJECT-IMG", "REJECT-ARRAY", "REJECT-200"):
            self.assertNotIn(raw, " ".join(self.rules), f"{raw} must not survive in Stash rules")
        patches = [p for p in self.result.patches if p["kind"] == "rule-policy-degradation"]
        self.assertEqual(len(patches), 4, patches)
        evidence = " ".join(p["evidence"] for p in patches)
        for raw in ("REJECT-DICT", "REJECT-IMG", "REJECT-ARRAY", "REJECT-200"):
            self.assertIn(raw, evidence)
        self.assertIn("DOMAIN,img.example.com,REJECT", self.compacted)

    def test_quic_udp_mirror_only_for_still_rejecting_domain_rules(self) -> None:
        self.assertIn(
            "AND,((DOMAIN,reject.example.com),(NETWORK,UDP),(DST-PORT,443)),REJECT",
            self.compacted,
        )
        mirrors = [rule for rule in self.compacted if "(NETWORK,UDP)" in rule]
        self.assertEqual(len(mirrors), 4, mirrors)
        enhancement = {item["id"]: item["count"] for item in self.result.enhancements}
        self.assertEqual(enhancement.get("quic-udp-mirror"), 4)

    def test_unknown_policy_blocks(self) -> None:
        result = convert("rules_unknown_policy.lpx")
        self.assertTrue(result.blocking)
        self.assertIn("E_RULE_POLICY", all_codes(result))
        self.assertIn("UNKNOWN-POLICY", " ".join(d.message for d in result.diagnostics))

    def test_unknown_rule_type_blocks(self) -> None:
        """Reported defect #2: an unknown rule type must block the target."""

        result = convert("rules_unknown_type.lpx")
        self.assertTrue(result.blocking)
        messages = " ".join(d.message for d in result.diagnostics if d.blocking)
        self.assertIn("FOOBAR", messages)


# ---------------------------------------------------------------------------
# 8. Header rewrites
# ---------------------------------------------------------------------------


class HeaderRewriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.result = convert("header_rewrites.lpx")
        self.lines = self.result.stash["http"]["header-rewrite"]

    def test_header_actions_become_header_rewrite_lines(self) -> None:
        self.assertEqual(
            self.lines,
            [
                "(?i)^https://api\\.example\\.com/req/ request-add x-req 1",
                "(?i)^https://api\\.example\\.com/req/ request-replace user-agent test",
                "(?i)^https://api\\.example\\.com/req/ request-del x-drop",
                "(?i)^https://api\\.example\\.com/resp/ response-add cache-control no-store",
            ],
        )
        self.assertEqual(self.result.counts.get("header_rewrite"), 4)


# ---------------------------------------------------------------------------
# 9. Integration patches (upstream vs local Stash main config)
# ---------------------------------------------------------------------------


class IntegrationPatchTests(unittest.TestCase):
    def test_taobao_poplayer_extra_reject_is_dropped_upstream_script_kept(self) -> None:
        result = convert("Taobao_remove_ads.lpx", url_name="Taobao_remove_ads.lpx")
        self.assertFalse(result.blocking, [d.as_dict() for d in result.diagnostics])
        scripts = result.stash["http"]["script"]
        self.assertTrue(
            any("poplayer\\.template\\.alibaba\\.com" in entry["match"] for entry in scripts),
            "the upstream poplayer response script must be kept",
        )
        self.assertFalse(
            any("poplayer" in rule.lower() for rule in result.stash["http"].get("url-rewrite", [])),
            "the extra poplayer reject-dict must not preempt the upstream response script",
        )
        patches = [p for p in result.patches if p["kind"] == "drop-extra-rewrite"]
        self.assertEqual(len(patches), 1, result.patches)
        self.assertIn("响应脚本", patches[0]["detail"])
        enhancement = {item["id"]: item["count"] for item in result.enhancements}
        self.assertEqual(enhancement.get("extra-ad-domains"), 33)
        self.assertIn("DOMAIN-SUFFIX,afpssp.alimama.com,REJECT", result.stash["rules"])

    def test_block_httpdns_wechat_domains_become_direct_exceptions(self) -> None:
        result = convert("Block_HTTPDNS.lpx", url_name="Block_HTTPDNS.lpx")
        self.assertFalse(result.blocking, [d.as_dict() for d in result.diagnostics])
        rules = result.stash["rules"]
        # The three DIRECT exceptions are emitted at the front of the rule block
        # (before any resolved upstream rule); the QUIC UDP/443 mirror for the
        # remaining plain-REJECT rule may be prepended before them by the
        # generic mirror enhancement, so locate them explicitly.
        expected_direct = [
            "DOMAIN,dns.weixin.qq.com,DIRECT",
            "DOMAIN,dns.weixin.qq.com.cn,DIRECT",
            "DOMAIN,aedns.weixin.qq.com,DIRECT",
        ]
        for index, rule in enumerate(expected_direct):
            self.assertIn(rule, rules)
        positions = [rules.index(rule) for rule in expected_direct]
        self.assertEqual(
            positions,
            [positions[0], positions[0] + 1, positions[0] + 2],
            f"the three DIRECT exceptions must stay contiguous: {rules}",
        )
        for domain in ("dns.weixin.qq.com", "dns.weixin.qq.com.cn", "aedns.weixin.qq.com"):
            self.assertNotIn(f"DOMAIN,{domain},REJECT", rules)
            self.assertNotIn(
                f"AND,((DOMAIN,{domain}),(NETWORK,UDP),(DST-PORT,443)),REJECT",
                rules,
                "a domain that is DIRECT must not get the REJECT UDP/443 mirror",
            )
        self.assertIn(
            "AND,((DOMAIN,other-httpdns.example.com),(NETWORK,UDP),(DST-PORT,443)),REJECT",
            rules,
        )
        patches = [p for p in result.patches if p["kind"] == "wechat-httpdns-direct-exceptions"]
        self.assertEqual(len(patches), 1, result.patches)

    def test_keli_reject_dict_rule_degrades_with_patch(self) -> None:
        # The real keli plugin uses REJECT-DICT at rule level; same code path as
        # rules_policies but exercised through the plugin-specific frame.
        result = convert("rules_policies.lpx")
        self.assertFalse(any("REJECT-DICT" in rule for rule in result.stash["rules"]))


# ---------------------------------------------------------------------------
# 10. Output hygiene / legacy helper compatibility
# ---------------------------------------------------------------------------


LEGACY_MARKERS = (
    "data-type",
    "requires-body",
    "requires_body",
    "binary-body-mode",
    "binary_body_mode",
    "statusCode",
    "mock-data-is-base64",
    "${",
    "script-path=",
    "[Argument]",
    "enable:",
    "tag:",
)


class OutputHygieneTests(unittest.TestCase):
    def test_generated_yaml_has_no_legacy_markers(self) -> None:
        for name in ("mock_contract.lpx", "body_replace.lpx", "script_arguments.lpx", "rules_policies.lpx"):
            with self.subTest(fixture=name):
                result = convert(name)
                for marker in LEGACY_MARKERS:
                    self.assertNotIn(marker, result.yaml, f"{marker!r} leaked into {name}")
                parsed = yaml.safe_load(result.yaml)
                self.assertIsInstance(parsed, dict)
                self.assertTrue(parsed.get("name"))

    def test_stash_yaml_roundtrip_keeps_structures(self) -> None:
        result = convert("mock_contract.lpx")
        parsed = yaml.safe_load(result.yaml)
        self.assertEqual(parsed["name"], "Mock Contract")
        self.assertEqual(len(parsed["http"]["mock"]), 5)
        self.assertEqual(result.stash["http"]["mock"], parsed["http"]["mock"])
        self.assertIn("mitm", parsed["http"])

    def test_body_rewrite_yaml_quoted_scalar_representer(self) -> None:
        data = {
            "name": "Representer",
            "http": {"body-rewrite": ["(?i)^https://x\\.test response-replace-regex a b"]},
        }
        rendered = dump_stash_yaml(dict(data))
        self.assertIn("- '(?i)^https://x\\.test response-replace-regex a b'", rendered)

    def test_legacy_helper_raises_on_blocking_and_returns_yaml_otherwise(self) -> None:
        text = (FIXTURES / "mock_invalid_base64.lpx").read_text(encoding="utf-8")
        resolver = ResourceResolver(PUBLIC_BASE, fetch=False, snapshots={})
        with self.assertRaises(ConversionError) as caught:
            convert_lpx_to_stash(text, f"{LPX_BASE_URL}/mock_invalid_base64.lpx", resolver=resolver)
        self.assertIn("E_MOCK_BASE64", [d.code for d in caught.exception.diagnostics])

        ok_text = (FIXTURES / "body_replace.lpx").read_text(encoding="utf-8")
        yaml_text = convert_lpx_to_stash(
            ok_text,
            f"{LPX_BASE_URL}/body_replace.lpx",
            resolver=ResourceResolver(PUBLIC_BASE, fetch=False, snapshots=fixture_snapshots()),
        )
        self.assertIsInstance(yaml_text, str)
        self.assertIn("response-replace-regex", yaml_text)

    def test_conversion_result_summary_contract(self) -> None:
        result = convert("mock_contract.lpx")
        self.assertEqual(result.name, "Mock Contract")
        self.assertRegex(result.source_sha256, r"^[0-9a-f]{64}$")
        summary = result.summary(result.yaml)
        json.dumps(summary)  # must be JSON-serializable (build manifest input)
        self.assertEqual(summary["counts"].get("mock"), 5)
        self.assertIn("diagnostics", summary)
        self.assertEqual(result.yaml, result.yaml)


if __name__ == "__main__":
    unittest.main()
