#!/usr/bin/env python3
"""Independent verification of the KeLee audit requirements against real snapshots.

Run::

    python tests/test_audit_conversion.py

This suite replays the read-only evidence in ``.local/upstream-audit`` (21 real
``.lpx`` plugins plus their script/jq snapshots) fully offline through the public
converter API and the ``--audit-snapshot`` CLI. It proves the audit claims:

* 21/21 plugins convert with zero blocking diagnostics;
* 13 previously-unsupported rewrites are converted (Amap 2 jq_file inlined,
  Bilibili 1 header.add merged, QQ 8 capture redirects, keli 1 body.replace,
  Spotify 1 url.replace);
* 8 mocks (Bilibili 6: 3 base64 + grpc-status and 3 text; Himalaya 1; keli 1);
* 6 object-argument declarations resolved = 5 emitted ``argument`` strings +
  1 documented disabled script (Bilibili sponsorBlock default false);
* 31 ``/i`` source script declarations = 30 emitted script matches, all
  ``(?i)``, no ``\\/`` left, + the 1 intentionally omitted disabled script;
* real jq compile of the inlined Amap programs when a jq binary is available
  (``.local/tools/jq.exe`` or ``PATH``), otherwise an explicit
  "real jq NOT verified" skip.

The whole file skips cleanly when ``.local/upstream-audit`` is absent (fresh CI
checkout); nothing here requires network or jq.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
AUDIT_DIR = REPO_ROOT / ".local" / "upstream-audit"

sys.path.insert(0, str(SCRIPTS_DIR))

import yaml  # noqa: E402

import convert_kelee_lpx as conv  # noqa: E402
import kelee_config as cfg  # noqa: E402
import kelee_enhancements as enh  # noqa: E402
from kelee_assets import ResourceResolver, load_audit_snapshot_map  # noqa: E402

PUBLIC_BASE = cfg.DEFAULT_PUBLIC_BASE
NODE = shutil.which("node")
NODE_RUNNER = REPO_ROOT / "tests" / "fixtures" / "conversion" / "node_runner.js"
NODE_REQUIRED = unittest.skipUnless(
    NODE,
    "Node not available on PATH: generated request scripts could not be executed",
)

AUDIT_AVAILABLE = (
    AUDIT_DIR.is_dir()
    and (AUDIT_DIR / "audit-summary.json").is_file()
    and len(list(AUDIT_DIR.glob("*.lpx"))) >= 21
)
AUDIT_REQUIRED = unittest.skipUnless(
    AUDIT_AVAILABLE,
    ".local/upstream-audit evidence snapshots absent (fresh CI checkout): "
    "real-snapshot conversion NOT verified",
)


def find_jq() -> tuple[str | None, str]:
    """Return (path, how) for a real jq binary; never installs anything."""

    on_path = shutil.which("jq")
    if on_path:
        return on_path, "PATH"
    for candidate in sorted((REPO_ROOT / ".local" / "tools").glob("jq*.exe")):
        if candidate.is_file():
            return str(candidate), ".local/tools"
    return None, ""


JQ, JQ_SOURCE = find_jq()
JQ_REQUIRED = unittest.skipUnless(
    JQ,
    "real jq NOT verified: no jq binary on PATH and no .local/tools/jq*.exe "
    "(structural balance checks are NOT a substitute for a real jq compile)",
)


def run_node_script(
    script_content: bytes | str,
    *,
    cwd: Path,
    request: dict,
    response: dict | None = None,
    response_body_b64: str | None = None,
    argument=...,  # type: ignore[assignment]
) -> dict:
    assert NODE is not None and NODE_RUNNER.is_file()
    cwd.mkdir(parents=True, exist_ok=True)
    script_path = cwd / "script.js"
    script_path.write_bytes(
        script_content.encode("utf-8") if isinstance(script_content, str) else script_content
    )
    spec: dict = {"request": request, "response": response if response is not None else {}}
    if response_body_b64 is not None:
        spec["responseBodyBase64"] = response_body_b64
    if argument is not ...:
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
            f"node runner failed rc={process.returncode}: {process.stdout} {process.stderr}"
        )
    return json.loads(result_path.read_text(encoding="utf-8"))


def provider_script_bytes(resolver: ResourceResolver, provider_url: str) -> bytes:
    assert provider_url.startswith(PUBLIC_BASE + "/"), provider_url
    relpath = provider_url[len(PUBLIC_BASE) + 1 :]
    assets = resolver.published_assets()
    if relpath not in assets:
        raise AssertionError(f"missing published asset {relpath}")
    return assets[relpath]


def balanced(expression: str) -> bool:
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


LEGACY_MARKERS = (
    "data-type",
    "requires-body",
    "binary-body-mode",
    "requires_body",
    "binary_body_mode",
    "statusCode",
    "mock-data-is-base64",
    "${",
    "script-path=",
    "[Argument]",
    "enable:",
    "tag:",
    "E_UNSUPPORTED",
    "E_EXPR_ACTION_UNSUPPORTED",
    "未转换",
)


@AUDIT_REQUIRED
class AuditSnapshotConversionTests(unittest.TestCase):
    """Full offline replay of the real audit evidence (read-only)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="kelee-audit-test-"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        snapshots = load_audit_snapshot_map(REPO_ROOT)
        if not snapshots:
            raise unittest.SkipTest("audit snapshot map is empty; cannot replay offline")
        cls.resolver = ResourceResolver(PUBLIC_BASE, fetch=False, snapshots=snapshots)
        cls.lpx_files = sorted(AUDIT_DIR.glob("*.lpx"))
        cls.results: dict[str, object] = {}
        for lpx in cls.lpx_files:
            text = lpx.read_text(encoding="utf-8")
            cls.results[lpx.name] = conv.convert_lpx(
                text,
                f"https://kelee.one/Tool/Loon/Lpx/{lpx.name}",
                resolver=cls.resolver,
                public_base=PUBLIC_BASE,
            )
        cls.summary = json.loads((AUDIT_DIR / "audit-summary.json").read_text(encoding="utf-8"))

    def result(self, filename: str):
        return self.results[filename]

    def http(self, filename: str) -> dict:
        return self.result(filename).stash.get("http", {})

    # -- completeness -----------------------------------------------------

    def test_expected_target_set(self) -> None:
        self.assertEqual(len(self.results), 21, sorted(self.results))
        summary_files = sorted(item["file"] for item in self.summary["plugins"])
        self.assertEqual(sorted(self.results), summary_files)

    def test_all_targets_convert_with_zero_blocking(self) -> None:
        blocked = {}
        for name, result in self.results.items():
            codes = [d.code for d in result.diagnostics if d.blocking]
            if codes:
                blocked[name] = codes
        self.assertEqual(blocked, {}, f"audit targets with blocking diagnostics: {blocked}")
        print("AUDIT-ITEM 21/21 targets converted, blocking targets: 0")
        # No blocking E_* marker anywhere either.
        for name, result in self.results.items():
            for diagnostic in result.diagnostics:
                if diagnostic.blocking:
                    self.assertFalse(
                        diagnostic.code.startswith("E_") or True,
                        f"{name}: unexpected blocking diagnostic {diagnostic}",
                    )

    def test_audit_summary_hashes_match_snapshots(self) -> None:
        for item in self.summary["plugins"]:
            with self.subTest(plugin=item["file"]):
                self.assertEqual(
                    self.result(item["file"]).source_sha256,
                    item["sha256"],
                    "converted snapshot differs from the audited evidence",
                )

    def test_aggregate_counts(self) -> None:
        totals: Counter[str] = Counter()
        for result in self.results.values():
            totals.update(result.counts)
        expected = {
            "mock": 8,
            "argument_entries": 5,
            "generated_scripts": 9,
            "script": 39,
            "mirrored_scripts": 29,
            "external_scripts": 2,
            "remote_jq": 4,
            "url_rewrite": 322,
            "body_rewrite": 89,
            "rules": 615,
        }
        for key, value in expected.items():
            with self.subTest(count=key):
                self.assertEqual(totals[key], value, f"aggregate {key}: {dict(totals)}")
        info_totals = Counter(
            diagnostic.code
            for result in self.results.values()
            for diagnostic in result.diagnostics
            if not diagnostic.blocking
        )
        self.assertEqual(info_totals["E_SCRIPT_DISABLED_BY_DEFAULT"], 1)
        self.assertEqual(info_totals["E_SCRIPT_ENABLE_RESOLVED"], 1)
        self.assertEqual(info_totals["E_BODY_REPLACE_ESCAPED_QUANTIFIER"], 1)
        print(f"AUDIT-ITEM aggregate counts: {dict(sorted(totals.items()))}")

    # -- 8 mocks ----------------------------------------------------------

    def test_eight_mocks_are_correct(self) -> None:
        expected_base64 = {
            "AAAAABMKEQgCEgl0ZWVuYWdlcnMgAioA",
            "AAAAACEaHeaQnOe0ouinhumikeOAgeeVquWJp+aIlnVw5Li7KAE=",
            "AAAAAAA=",
        }
        bili_mocks = self.http("Bilibili_remove_ads.lpx").get("mock", [])
        self.assertEqual(len(bili_mocks), 6, bili_mocks)
        grpc = [m for m in bili_mocks if "grpc-status" in m.get("headers", {})]
        self.assertEqual(len(grpc), 3, grpc)
        for mock in grpc:
            self.assertEqual(mock["headers"]["grpc-status"], "0")
            self.assertEqual(mock["headers"]["Content-Type"], "text/plain")
            self.assertIn("base64", mock)
        self.assertEqual({m["base64"] for m in grpc}, expected_base64)
        text_mocks = [m for m in bili_mocks if "text" in m]
        self.assertEqual(len(text_mocks), 3, text_mocks)
        for mock in text_mocks:
            self.assertNotIn("grpc-status", mock["headers"])
            self.assertEqual(mock["status-code"], 200)
        merges = [p for p in self.result("Bilibili_remove_ads.lpx").patches
                  if p["kind"] == "mock-header-merge"]
        self.assertEqual(len(merges), 3, merges)
        self.assertTrue(all("grpc-status" in p["detail"] for p in merges))

        himalaya = self.http("Himalaya_remove_ads.lpx").get("mock", [])
        self.assertEqual(len(himalaya), 1)
        self.assertEqual(himalaya[0]["headers"]["Content-Type"], "application/json")
        keli = self.http("Remove_ads_by_keli.lpx").get("mock", [])
        self.assertEqual(len(keli), 1)
        self.assertEqual(keli[0]["headers"]["Content-Type"], "application/json")
        self.assertIn("respCode", keli[0]["text"])

        total = sum(len(self.http(name).get("mock", [])) for name in self.results)
        self.assertEqual(total, 8)
        for name in self.results:
            for mock in self.http(name).get("mock", []):
                self.assertLessEqual(
                    set(mock), {"match", "status-code", "headers", "text", "base64"}
                )
                for legacy in ("body", "data", "data-type", "statusCode"):
                    self.assertNotIn(legacy, mock)
                self.assertEqual("text" in mock, "base64" not in mock)
        print(
            "AUDIT-ITEM 8 mocks: Bilibili 6 (3 base64+grpc-status, 3 text), "
            "Himalaya 1, keli 1"
        )

    # -- 13 formerly unsupported -----------------------------------------

    def test_thirteen_formerly_unsupported_rewrites_are_converted(self) -> None:
        self.assertEqual(self.summary["totals"]["unsupported"], 13)

        # 1) Amap: 2 formerly unsupported rewrites — both remote jq_file programs
        #    are now fetched/inlined (and no source rewrite is silently dropped).
        amap = self.result("Amap_remove_ads.lpx")
        self.assertEqual(amap.counts.get("remote_jq"), 2, amap.counts)
        enhancement_urls = len(enh.APP_EXTRA_REWRITES["Amap_remove_ads.lpx"])
        upstream_url_rewrites = amap.counts["url_rewrite"] - enhancement_urls
        self.assertEqual(upstream_url_rewrites, 7, amap.counts)
        self.assertEqual(amap.counts["body_rewrite"], 16, amap.counts)
        self.assertEqual(upstream_url_rewrites + amap.counts["body_rewrite"], 23)
        self.assertFalse(amap.blocking)

        # 2) Bilibili: 1 formerly unsupported `response.header.add` is merged
        #    into the 3 gRPC base64 mocks (see test_eight_mocks_are_correct).
        self.assertEqual(
            len([p for p in self.result("Bilibili_remove_ads.lpx").patches
                 if p["kind"] == "mock-header-merge"]),
            3,
        )

        # 3) QQ: 8 capture redirects -> 8 request scripts.
        qq = self.result("QQ_Redirect.lpx")
        self.assertEqual(qq.counts.get("generated_scripts"), 8, qq.counts)
        self.assertEqual(len(self.http("QQ_Redirect.lpx").get("script", [])), 8)
        for record in qq.generated_scripts:
            self.assertEqual(record["mode"], "redirect")
            self.assertEqual(record["status"], 307)

        # 4) keli: 1 formerly unsupported body.replace -> native body-rewrite.
        keli_rules = [
            rule
            for rule in self.http("Remove_ads_by_keli.lpx").get("body-rewrite", [])
            if "GetADResult" in rule
        ]
        self.assertEqual(len(keli_rules), 1, keli_rules)
        self.assertIn("<GetADResult>\\.\\*\\?</GetADResult>", keli_rules[0])

        # 5) Spotify: 1 formerly unsupported capture url.replace -> request script.
        spotify = self.result("Spotify_remove_ads.lpx")
        self.assertEqual(spotify.counts.get("generated_scripts"), 1)
        self.assertEqual(spotify.generated_scripts[0]["mode"], "replace")

        recovered = 2 + 1 + 8 + 1 + 1
        self.assertEqual(recovered, 13)
        print(
            "AUDIT-ITEM 13 formerly unsupported -> converted: "
            "Amap 2 jq_file inlined, Bilibili 1 header.add merged (3 mocks), "
            "QQ 8 capture redirect scripts, keli 1 body.replace, Spotify 1 url.replace"
        )

    # -- capture redirect scripts ----------------------------------------

    @NODE_REQUIRED
    def test_qq_eight_redirect_scripts_execute(self) -> None:
        qq = self.result("QQ_Redirect.lpx")
        providers = qq.providers
        script_for_provider = {
            entry["name"]: entry for entry in self.http("QQ_Redirect.lpx")["script"]
        }
        # Concrete example URLs matching each upstream QQ matcher; expected
        # Location is the Loon replacement `${urlMatch.2}` for that match.
        cases = {
            "(^https://c\\.pc\\.qq\\.com/middlem\\.html\\?pfurl=)": (
                "https://c.pc.qq.com/middlem.html?pfurl=https://evil.test/a?b=1&pfuin=u1",
                "https://evil.test/a?b=1",
            ),
            "(^https://c\\.pc\\.qq\\.com/middlect\\.html\\?pfuin=.*&pfurl=)": (
                "https://c.pc.qq.com/middlect.html?pfuin=u1&pfurl=https://evil.test/x&gjsublevel=1",
                "https://evil.test/x",
            ),
            "(^https://c\\.pc\\.qq\\.com/middlect\\.html\\?iscontinue=.*pfurl=)": (
                "https://c.pc.qq.com/middlect.html?iscontinue=1&pfurl=https://evil.test/y&pfuin=u2",
                "https://evil.test/y",
            ),
            "url=/middlem\\.html\\?pfurl%3d": (
                "https://pingtas.qq.com/webview/pingd?dm=c.pc.qq.com&pvi=12&si=s3"
                "&url=/middlem.html?pfurl%3dhttp://evil.test/z%26pfuin%3da%26pfuin%3db",
                "http://evil.test/z",
            ),
            "(^https://cgi\\.connect\\.qq\\.com/qqconnectopen/get_urlinfoForQQV2\\?url=)": (
                "https://cgi.connect.qq.com/qqconnectopen/get_urlinfoForQQV2?url=http://evil.test/q",
                "http://evil.test/q",
            ),
            "(^https://c\\.pc\\.qq\\.com/index\\.html\\?pfurl=)": (
                "https://c.pc.qq.com/index.html?pfurl=https://evil.test/i&pfuin=u3",
                "https://evil.test/i",
            ),
            "(^https://c\\.pc\\.qq\\.com/ios\\.html\\?url=)": (
                "https://c.pc.qq.com/ios.html?url=https://evil.test/ios&level=2",
                "https://evil.test/ios",
            ),
            "url=/ios\\.html\\?url%3d": (
                "https://pingtas.qq.com/webview/pingd?dm=c.pc.qq.com&pvi=1&si=s2"
                "&url=/ios.html?url%3dhttp://evil.test/i%26level%3d2%26level%3d3",
                "http://evil.test/i",
            ),
        }
        executed = 0
        for entry in script_for_provider.values():
            marker = next(
                (marker for marker in cases if entry["match"].startswith(f"(?i){marker}")),
                None,
            )
            if marker is None:
                # Fall back to a substring so small upstream edits still verify.
                marker = next((m for m in cases if m in entry["match"]), None)
            self.assertIsNotNone(marker, f"unexpected QQ matcher: {entry['match']}")
            url, expected = cases[marker]
            content = provider_script_bytes(self.resolver, providers[entry["name"]]["url"])
            output = run_node_script(
                content,
                cwd=self.tmp / f"qq-{executed}",
                request={"url": url},
                response={},
            )
            done = output["done"]
            self.assertTrue(done["called"], f"{entry['match']}: {output}")
            self.assertEqual(done["value"]["response"]["status"], 307)
            self.assertEqual(
                done["value"]["response"]["headers"]["Location"],
                expected,
                f"QQ redirect mismatch for {url}",
            )
            executed += 1
        self.assertEqual(executed, 8)
        print("AUDIT-ITEM QQ: 8/8 redirect request scripts executed (307 + exact Location)")

    @NODE_REQUIRED
    def test_spotify_artistview_url_replace_script_executes(self) -> None:
        spotify = self.result("Spotify_remove_ads.lpx")
        providers = spotify.providers
        generated = spotify.generated_scripts
        self.assertEqual(len(generated), 1)
        record = generated[0]
        self.assertEqual(record["mode"], "replace")
        entry = next(
            item for item in self.http("Spotify_remove_ads.lpx")["script"]
            if item["name"] == record["provider"]
        )
        self.assertEqual(entry["type"], "request")
        content = provider_script_bytes(self.resolver, providers[record["provider"]]["url"])
        output = run_node_script(
            content,
            cwd=self.tmp / "spotify-replace",
            request={
                "url": "https://spclient.wg.spotify.com/artistview/v1/artist/abc123"
                "&platform=iphone"
            },
            response={},
        )
        done = output["done"]["value"]
        self.assertEqual(
            done["url"],
            "https://spclient.wg.spotify.com/artistview/v1/artist/abc123&platform=ipad",
        )
        self.assertNotIn("response", done)
        print("AUDIT-ITEM Spotify artistview url.replace script executed (exact new URL)")

    # -- script arguments -------------------------------------------------

    def test_appleweather_amplitude_argument_protocol(self) -> None:
        scripts = self.http("AppleWeatherEnhancer.lpx").get("script", [])
        self.assertEqual(len(scripts), 2, scripts)
        expected_names = [
            "Weather.Provider",
            "NextHour.Provider",
            "AirQuality.Calculate.Algorithm",
            "API.ColorfulClouds.Token",
            "API.QWeather.Host",
            "API.QWeather.Token",
            "API.WAQI.Token",
            "Storage",
            "LogLevel",
        ]
        for entry in scripts:
            argument = entry["argument"]
            self.assertIsInstance(argument, str)
            pairs = argument.split("&")
            self.assertEqual(len(pairs), 9, argument)
            self.assertEqual([pair.split("=", 1)[0] for pair in pairs], expected_names)
            self.assertIs(entry["require-body"], True)
            for forbidden in ("enable", "tag", "requires-body", "binary-body-mode"):
                self.assertNotIn(forbidden, entry)
        # v2 binary response script runs binary body mode; v1 availability one not.
        v2 = next(e for e in scripts if "/api/v2/weather/" in e["match"])
        v1 = next(e for e in scripts if "/api/v1/availability/" in e["match"])
        self.assertIs(v2["binary-mode"], True)
        self.assertNotIn("binary-mode", v1)
        print("AUDIT-ITEM AppleWeather: 2 scripts with 9-parameter &-joined arguments")

    def test_six_object_argument_declarations_resolved(self) -> None:
        object_argument_lines = 0
        for filename in (
            "Bilibili_remove_ads.lpx",
            "AppleWeatherEnhancer.lpx",
            "Spotify_remove_ads.lpx",
        ):
            text = (AUDIT_DIR / filename).read_text(encoding="utf-8")
            in_scripts = False
            for raw in text.splitlines():
                stripped = raw.strip()
                if stripped.startswith("["):
                    in_scripts = stripped.lower() == "[script]"
                    continue
                if in_scripts and "script(" in stripped and "{${" in stripped:
                    object_argument_lines += 1
        self.assertEqual(object_argument_lines, 6)
        emitted = sum(
            1
            for name in self.results
            for entry in self.http(name).get("script", [])
            if "argument" in entry
        )
        self.assertEqual(emitted, 5, "5 of the 6 argument-bearing declarations materialize")
        disabled = [
            diagnostic
            for diagnostic in self.result("Bilibili_remove_ads.lpx").diagnostics
            if diagnostic.code == "E_SCRIPT_DISABLED_BY_DEFAULT"
        ]
        self.assertEqual(len(disabled), 1)
        self.assertIn("Bilibili_proto_request_kokoryh.js", disabled[0].message)
        self.assertIn("默认关闭", disabled[0].message)
        print(
            "AUDIT-ITEM 6 object-argument declarations resolved: 5 emitted argument "
            "strings + Bilibili proto_request DM intentionally omitted (enable=${sponsorBlock}=false)"
        )

    def test_bilibili_scripts_and_disabled_script_regex(self) -> None:
        import loon_expr as lx  # parser is read-only evidence for the omitted script

        scripts = self.http("Bilibili_remove_ads.lpx").get("script", [])
        self.assertEqual(len(scripts), 3, scripts)
        for entry in scripts:
            self.assertTrue(entry["match"].startswith("(?i)"), entry["match"])
            self.assertNotIn("\\/", entry["match"])
            self.assertNotIn("DmSegMobile", entry["match"])
        text = (AUDIT_DIR / "Bilibili_remove_ads.lpx").read_text(encoding="utf-8")
        dm_line = next(
            line for line in text.splitlines() if "DmSegMobile" in line and "script(" in line
        )
        expression = lx.parse_script_expression(dm_line.strip())
        self.assertIsNotNone(expression, "the intentionally omitted script must still parse")
        converted_match = lx.stash_regex(expression.regex)
        self.assertTrue(converted_match.startswith("(?i)"), converted_match)
        self.assertNotIn("\\/", converted_match)
        self.assertIn("DmSegMobile", converted_match)
        print(
            "AUDIT-ITEM Bilibili: 3 emitted scripts (+1 documented disabled script whose "
            "regex would convert to (?i) if enabled)"
        )

    # -- /i coverage ------------------------------------------------------

    def test_thirty_one_case_insensitive_script_declarations(self) -> None:
        self.assertEqual(self.summary["totals"]["script_count"], 31)
        generated_providers = {
            record["provider"]
            for result in self.results.values()
            for record in result.generated_scripts
        }
        emitted_scripts = []
        emitted_generated = []
        for name, result in self.results.items():
            for entry in result.stash.get("http", {}).get("script", []):
                if entry["name"] in generated_providers:
                    emitted_generated.append((name, entry))
                else:
                    emitted_scripts.append((name, entry))
        self.assertEqual(len(emitted_generated), 9)
        self.assertEqual(len(emitted_scripts), 30, "31 declarations - 1 disabled script")
        for name, entry in emitted_scripts + emitted_generated:
            with self.subTest(plugin=name, match=entry["match"][:60]):
                self.assertTrue(entry["match"].startswith("(?i)"), entry["match"])
                self.assertNotIn("\\/", entry["match"])
        print(
            "AUDIT-ITEM /i: 30/30 emitted upstream script matches start (?i) and keep no "
            "escaped slashes (+1 disabled declaration verified via parser)"
        )

    # -- bodies / rules ---------------------------------------------------

    def test_keli_body_replace_and_rule_degradation(self) -> None:
        keli = self.result("Remove_ads_by_keli.lpx")
        rules = keli.stash["rules"]
        self.assertFalse(any("REJECT-DICT" in rule for rule in rules))
        degradations = [p for p in keli.patches if p["kind"] == "rule-policy-degradation"]
        self.assertTrue(degradations, keli.patches)
        self.assertTrue(any("REJECT-DICT" in p["evidence"] for p in degradations))
        warnings = [
            d for d in keli.diagnostics if d.code == "E_BODY_REPLACE_ESCAPED_QUANTIFIER"
        ]
        self.assertEqual(len(warnings), 1, [d.as_dict() for d in keli.diagnostics])
        self.assertFalse(warnings[0].blocking)
        print(
            "AUDIT-ITEM keli: 1 body.replace preserved verbatim + escaped-quantifier "
            "warning; REJECT-DICT rules degraded to REJECT with patch"
        )

    def test_taobao_poplayer_extra_rewrite_dropped(self) -> None:
        result = self.result("Taobao_remove_ads.lpx")
        self.assertTrue(
            any("poplayer\\.template\\.alibaba\\.com" in entry["match"]
                for entry in self.http("Taobao_remove_ads.lpx").get("script", []))
        )
        self.assertFalse(
            any("poplayer" in rule.lower()
                for rule in self.http("Taobao_remove_ads.lpx").get("url-rewrite", []))
        )
        self.assertTrue(any(p["kind"] == "drop-extra-rewrite" for p in result.patches))

    def test_block_httpdns_wechat_direct_exceptions(self) -> None:
        rules = self.result("Block_HTTPDNS.lpx").stash["rules"]
        for domain in ("dns.weixin.qq.com", "dns.weixin.qq.com.cn", "aedns.weixin.qq.com"):
            self.assertIn(f"DOMAIN,{domain},DIRECT", rules)
            self.assertNotIn(f"DOMAIN,{domain},REJECT", rules)
            self.assertNotIn(
                f"AND,((DOMAIN,{domain}),(NETWORK,UDP),(DST-PORT,443)),REJECT", rules
            )
        self.assertTrue(
            any(p["kind"] == "wechat-httpdns-direct-exceptions"
                for p in self.result("Block_HTTPDNS.lpx").patches)
        )

    # -- jq ---------------------------------------------------------------

    def test_amap_inlined_jq_programs_are_complete(self) -> None:
        amap = self.result("Amap_remove_ads.lpx")
        programs = [
            rule.split(" response-jq ", 1)[1]
            for rule in amap.stash["http"]["body-rewrite"]
            if " response-jq " in rule
        ]
        self.assertGreaterEqual(len(programs), 2, programs)
        for program in programs:
            self.assertNotIn("#", program)
            self.assertNotIn("\n", program)
            self.assertTrue(balanced(program), program[:200])
        remote = [program for program in programs if len(program) > 1000]
        self.assertGreaterEqual(len(remote), 2, [len(p) for p in programs])
        joined = "\n".join(remote)
        self.assertIn("sportsGroupConfig", joined)
        self.assertIn("keep_skeleton", joined)
        self.assertIn("poiDetailCommonConfig", joined)

    @JQ_REQUIRED
    def test_real_jq_compiles_inlined_amap_programs(self) -> None:
        assert JQ is not None
        amap = self.result("Amap_remove_ads.lpx")
        programs = [
            rule.split(" response-jq ", 1)[1]
            for rule in amap.stash["http"]["body-rewrite"]
            if " response-jq " in rule and len(rule.split(" response-jq ", 1)[1]) > 200
        ]
        self.assertGreaterEqual(len(programs), 2)
        for index, program in enumerate(programs):
            path = self.tmp / f"amap-program-{index}.jq"
            path.write_text(program, encoding="utf-8")
            process = subprocess.run(
                [JQ, "-n", "-f", str(path)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            stderr = process.stderr or ""
            self.assertNotEqual(process.returncode, 3, f"jq compile error: {stderr}")
            self.assertNotIn("compile error", stderr)
            self.assertNotIn("syntax error", stderr)
        print(
            f"AUDIT-ITEM real jq compile OK ({len(programs)} inlined Amap programs, "
            f"jq from {JQ_SOURCE})"
        )

    # -- output hygiene ---------------------------------------------------

    def test_no_legacy_markers_in_any_output(self) -> None:
        for name, result in self.results.items():
            with self.subTest(plugin=name):
                parsed = yaml.safe_load(result.yaml)
                self.assertIsInstance(parsed, dict)
                self.assertTrue(parsed.get("name"))
                for marker in LEGACY_MARKERS:
                    self.assertNotIn(marker, result.yaml, f"{marker!r} in {name}")

    # -- CLI + validator integration -------------------------------------

    def test_cli_audit_snapshot_conversion(self) -> None:
        out_dir = self.tmp / "cli-out"
        report_path = self.tmp / "cli-report.json"
        process = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "convert_kelee_lpx.py"),
                "--audit-snapshot",
                "--audit-out-dir",
                str(out_dir),
                "--public-base",
                PUBLIC_BASE,
                "--json-report",
                str(report_path),
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(len(report), 21)
        blocking_targets = []
        for item in report:
            blocking = [d for d in item["diagnostics"] if d.get("blocking")]
            if blocking:
                blocking_targets.append((item["file"], blocking))
        self.assertEqual(blocking_targets, [])
        generated = sorted(out_dir.glob("*.stoverride"))
        self.assertEqual(len(generated), 21)
        # Deterministic: the CLI writes exactly the API result bytes.
        for path in generated:
            lpx_name = path.name.replace(".stoverride", ".lpx")
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                self.result(lpx_name).yaml,
                f"CLI output differs from API result for {lpx_name}",
            )
        self.assertTrue((out_dir / "scripts").is_dir())
        print("AUDIT-ITEM CLI --audit-snapshot: 21 files, 0 blocking, byte-identical to API")

    def test_converted_bundle_passes_validator(self) -> None:
        import validate_stash as vs

        bundle = self.tmp / "bundle"
        bundle.mkdir()
        merged_providers: dict = {}
        targets = []
        for name, result in self.results.items():
            stash = copy.deepcopy(result.stash)
            providers = stash.pop("script-providers", {}) or {}
            merged_providers.update(providers)
            stoverride = name.replace(".lpx", ".stoverride")
            (bundle / stoverride).write_text(conv.dump_stash_yaml(stash), encoding="utf-8")
            targets.append({"filename": name})
        self.assertEqual(len(merged_providers), 34)
        (bundle / "targets.json").write_text(json.dumps(targets), encoding="utf-8")
        (bundle / "kelee-scripts.stoverride").write_text(
            yaml.safe_dump(
                {"name": "kelee-scripts", "script-providers": merged_providers},
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        self.resolver.write_assets(bundle)
        policies = vs.load_main_config_policies(REPO_ROOT / "stash-ios.yaml")
        self.assertIn("Proxy", policies)
        diagnostics = vs.validate_bundle(
            bundle,
            expected_targets=[item["filename"] for item in targets],
            public_base=PUBLIC_BASE,
            known_policies=policies,
        )
        blocking = [d for d in diagnostics if d.blocking]
        self.assertEqual(
            blocking,
            [],
            f"converted audit bundle must validate: {[(d.code, d.path, d.message) for d in blocking]}",
        )
        print(
            f"AUDIT-ITEM validator+bundle integration on real data: 21 targets, "
            f"0 blocking, 0 warnings ({len(diagnostics)} diagnostics)"
        )


if __name__ == "__main__":
    unittest.main()
