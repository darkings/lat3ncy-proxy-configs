#!/usr/bin/env python3
"""Script-level repro for the Loon->Stash ``$argument`` protocol (Spotify).

Run::

    python tests/test_spotify_argument_repro.py

Runs the ACTUAL upstream ``Spotify_remove_ads.js`` snapshot from the read-only
audit evidence (``.local/upstream-audit/scripts``, never committed) inside a Node
Stash-API shim (``$request``/``$response``/``$argument``/``$done``):

* with ``$argument`` omitted (what the old converter effectively produced), the
  script throws ``Cannot read properties of null (reading 'tab')`` and returns no
  body — i.e. ad removal silently does nothing;
* with the converted JSON argument ``{"tab":false,"useractivity":true}`` the
  script returns a modified protobuf body.

It also converts the real ``Spotify_remove_ads.lpx`` snapshot and asserts the
emitted Stash ``argument`` string is exactly that JSON object.

Skips cleanly when ``.local/upstream-audit`` is absent (fresh CI checkout).
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
AUDIT_DIR = REPO_ROOT / ".local" / "upstream-audit"
SPOTIFY_DIR = Path(__file__).resolve().parent / "fixtures" / "spotify"
NODE_RUNNER = REPO_ROOT / "tests" / "fixtures" / "conversion" / "node_runner.js"
PUBLIC_BASE = "https://example.test"

sys.path.insert(0, str(SCRIPTS_DIR))

import convert_kelee_lpx as conv  # noqa: E402
from kelee_assets import ResourceResolver, load_audit_snapshot_map  # noqa: E402

NODE = shutil.which("node")


def _spotify_script_path() -> Path | None:
    manifest = AUDIT_DIR / "script-manifest.json"
    if not manifest.is_file():
        return None
    data = json.loads(manifest.read_text(encoding="utf-8"))
    entry = data.get("https://kelee.one/Resource/JavaScript/Spotify/Spotify_remove_ads.js")
    if not entry:
        return None
    rel = entry.get("file") if isinstance(entry, dict) else entry
    if not rel:
        return None
    candidate = Path(rel)
    if not candidate.is_absolute():
        candidate = REPO_ROOT / rel
    return candidate if candidate.is_file() else None


SPOTIFY_SCRIPT = _spotify_script_path()
AUDIT_AVAILABLE = (
    SPOTIFY_SCRIPT is not None
    and (AUDIT_DIR / "Spotify_remove_ads.lpx").is_file()
    and bool(load_audit_snapshot_map(REPO_ROOT))
)
AUDIT_REQUIRED = unittest.skipUnless(
    AUDIT_AVAILABLE,
    ".local/upstream-audit evidence (Spotify_remove_ads.lpx + script manifest) absent "
    "(fresh CI checkout): script-level argument repro NOT verified",
)
NODE_REQUIRED = unittest.skipUnless(
    NODE,
    "Node not available on PATH: the real Spotify_remove_ads.js repro could not run",
)

BODY_SPEC = json.loads((SPOTIFY_DIR / "ucs_response.json").read_text(encoding="utf-8"))


def convert_spotify():
    snapshots = load_audit_snapshot_map(REPO_ROOT)
    resolver = ResourceResolver(PUBLIC_BASE, fetch=False, snapshots=snapshots)
    text = (AUDIT_DIR / "Spotify_remove_ads.lpx").read_text(encoding="utf-8")
    result = conv.convert_lpx(
        text,
        "https://kelee.one/Tool/Loon/Lpx/Spotify_remove_ads.lpx",
        resolver=resolver,
        public_base=PUBLIC_BASE,
    )
    return result, resolver


def run_spotify_script(*, argument=..., cwd: Path) -> dict:
    assert NODE is not None and SPOTIFY_SCRIPT is not None
    cwd.mkdir(parents=True, exist_ok=True)
    script_path = cwd / "Spotify_remove_ads.js"
    script_path.write_bytes(SPOTIFY_SCRIPT.read_bytes())
    spec = {
        "request": {"url": BODY_SPEC["requestUrl"], "headers": {}},
        "response": {"status": 200},
        "responseBodyBase64": BODY_SPEC["base64"],
    }
    if argument is not ...:
        spec["argument"] = argument
    context_path = cwd / "context.json"
    context_path.write_text(json.dumps(spec), encoding="utf-8")
    result_path = cwd / "result.json"
    process = subprocess.run(
        [NODE, str(NODE_RUNNER), str(script_path), str(context_path), str(result_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if process.returncode != 0 or not result_path.is_file():
        raise AssertionError(
            f"node runner failed rc={process.returncode}: {process.stdout} {process.stderr}"
        )
    return json.loads(result_path.read_text(encoding="utf-8"))


@AUDIT_REQUIRED
class SpotifyArgumentReproTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="kelee-spotify-test-"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.result, cls.resolver = convert_spotify()
        cls.entries = cls.result.stash.get("http", {}).get("script", [])
        cls.response_entry = next(
            entry for entry in cls.entries if "bootstrap|user-customization-service" in entry["match"]
        )

    def test_converted_spotify_lpx_argument_is_exact_json_object(self) -> None:
        self.assertFalse(self.result.blocking, [d.as_dict() for d in self.result.diagnostics])
        self.assertEqual(
            self.response_entry["argument"],
            '{"tab":false,"useractivity":true}',
            "the emitted Stash argument must be the JSON object the script JSON.parse()s",
        )
        self.assertEqual(
            json.loads(self.response_entry["argument"]),
            {"tab": False, "useractivity": True},
        )
        self.assertIs(self.response_entry["require-body"], True)
        self.assertIs(self.response_entry["binary-mode"], True)
        self.assertEqual(self.response_entry["timeout"], 5)
        for forbidden in ("enable", "tag", "requires-body", "binary-body-mode"):
            self.assertNotIn(forbidden, self.response_entry)

    def test_mirrored_script_bytes_are_verbatim(self) -> None:
        provider_url = self.result.providers[self.response_entry["name"]]["url"]
        relpath = provider_url[len(PUBLIC_BASE) + 1 :]
        published = self.resolver.published_assets()[relpath]
        snapshot = SPOTIFY_SCRIPT.read_bytes().replace(b"\r\n", b"\n")
        self.assertEqual(published, snapshot, "mirrored script must not be modified")

    @NODE_REQUIRED
    def test_omitted_argument_breaks_the_script(self) -> None:
        output = run_spotify_script(cwd=self.tmp / "omitted")
        logs = [item["args"][0] for item in output["logs"] if item["args"]]
        self.assertTrue(
            any("Cannot read properties of null (reading 'tab')" in log for log in logs),
            f"expected the null.tab failure log, got {logs}",
        )
        done = output["done"]
        self.assertTrue(done["called"], output)
        self.assertNotIn(
            "body",
            done["value"],
            "without $argument the script must return $done({}) (no modified body)",
        )
        print(
            "SPOTIFY-REPRO omitted $argument -> logs "
            f"{logs!r}, no body returned"
        )

    @NODE_REQUIRED
    def test_converted_argument_returns_modified_body(self) -> None:
        argument = self.response_entry["argument"]
        output = run_spotify_script(argument=argument, cwd=self.tmp / "with-argument")
        done = output["done"]
        self.assertTrue(done["called"], output)
        self.assertIn("body", done["value"], f"expected a modified body: {output}")
        body = done["value"]["body"]
        self.assertEqual(body["__bytes"], "base64")
        decoded = base64.b64decode(body["data"], validate=True)
        self.assertGreater(len(decoded), 0)
        self.assertNotEqual(
            decoded,
            base64.b64decode(BODY_SPEC["base64"], validate=True),
            "the script must return a modified body, not the input bytes",
        )
        error_logs = [
            item for item in output["logs"] if "Cannot read properties" in " ".join(item["args"])
        ]
        self.assertEqual(error_logs, [], output["logs"])
        print(
            "SPOTIFY-REPRO converted argument -> modified body "
            f"{len(decoded)} bytes (input {BODY_SPEC['length']} bytes)"
        )

    def test_script_is_actually_the_upstream_evidence(self) -> None:
        content = SPOTIFY_SCRIPT.read_text(encoding="utf-8", errors="replace")
        self.assertIn("$argument", content)
        self.assertIn("$request.url", content)
        self.assertIn("$response.body", content)
        self.assertIn("JSON.parse($argument)", content)
        self.assertGreater(len(content), 5000)


if __name__ == "__main__":
    unittest.main()
