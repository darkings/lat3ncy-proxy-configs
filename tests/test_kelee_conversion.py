#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from convert_kelee_lpx import convert_lpx_to_stash


class KeLeeBodyRewriteConversionTests(unittest.TestCase):
    def convert(self, rewrite_lines: list[str]) -> list[str]:
        source = "\n".join([
            "#!name=Body Rewrite Contract",
            "[Rewrite]",
            *rewrite_lines,
            "[MitM]",
            "hostname=api.example.com",
        ])
        output = convert_lpx_to_stash(source, fetch_script_fallback=False)
        parsed = yaml.safe_load(output)
        return parsed["http"]["body-rewrite"]

    def test_body_rewrite_is_one_quoted_yaml_scalar(self) -> None:
        source = "\n".join([
            "#!name=Quoted Body Contract",
            "[Rewrite]",
            r'''^https:\/\/api\.example\.com\/feed response-body-json-jq '.data |= (del(.ad) | .name = "Stash User")' ''',
            "[MitM]",
            "hostname=api.example.com",
        ])
        output = convert_lpx_to_stash(source, fetch_script_fallback=False)
        body_line = next(line for line in output.splitlines() if "response-jq" in line)
        self.assertTrue(body_line.lstrip().startswith("- '"), body_line)
        self.assertTrue(body_line.endswith("'"), body_line)
        self.assertNotIn('response-jq "', body_line)

    def test_remote_jq_is_not_truncated_or_wrapped(self) -> None:
        expression = "." + " | ." * 1500
        source = "\n".join([
            "#!name=Long JQ Contract",
            "[Rewrite]",
            r'^https:\/\/api\.example\.com\/feed response-body-json-jq jq-path="https://example.com/long.jq"',
            "[MitM]",
            "hostname=api.example.com",
        ])
        with patch("convert_kelee_lpx.fetch_text", return_value=expression):
            output = convert_lpx_to_stash(source, fetch_script_fallback=False)
        body_line = next(line for line in output.splitlines() if "response-jq" in line)
        self.assertIn(expression, body_line)
        self.assertTrue(body_line.endswith("'"), body_line[-100:])
        parsed = yaml.safe_load(output)
        self.assertTrue(parsed["http"]["body-rewrite"][0].endswith(expression))

    def test_json_del_uses_native_stash_action(self) -> None:
        rules = self.convert([
            r"^https:\/\/api\.example\.com\/home response-body-json-del data.ad data.banner",
        ])
        self.assertEqual(
            rules[0],
            r"^https:\/\/api\.example\.com\/home response-json-del data.ad data.banner",
        )

    def test_json_replace_uses_native_stash_action(self) -> None:
        rules = self.convert([
            r'^https:\/\/api\.example\.com\/config response-body-json-replace data.enabled 0 data.name "clean"',
        ])
        self.assertEqual(
            rules[0],
            r'^https:\/\/api\.example\.com\/config response-json-replace data.enabled 0 data.name "clean"',
        )

    def test_jq_expression_is_not_wrapped_as_a_string_literal(self) -> None:
        rules = self.convert([
            r'''^https:\/\/api\.example\.com\/feed response-body-json-jq 'del(.data.ad) | .items |= map(select(.enabled))' ''',
        ])
        self.assertEqual(
            rules[0],
            r"^https:\/\/api\.example\.com\/feed response-jq del(.data.ad) | .items |= map(select(.enabled))",
        )
        expression = rules[0].split(None, 2)[2]
        self.assertFalse(expression.startswith('"') and expression.endswith('"'))

    def test_script_binary_body_mode_uses_stash_key(self) -> None:
        source = "\n".join([
            "#!name=Script Contract",
            "[Script]",
            r"http-response ^https:\/\/api\.example\.com\/feed script-path=https://example.com/clean.js, requires-body=true, binary-body-mode=true, tag=BinaryCleaner",
            "[MitM]",
            "hostname=api.example.com",
        ])
        output = convert_lpx_to_stash(source, fetch_script_fallback=False)
        parsed = yaml.safe_load(output)
        script = parsed["http"]["script"][0]
        self.assertTrue(script["require-body"])
        self.assertTrue(script["binary-mode"])
        self.assertNotIn("requires-body", script)
        self.assertNotIn("binary-body-mode", script)

    def test_pinduoduo_forces_tunnel_tcp_into_http_engine(self) -> None:
        source = "\n".join([
            "#!name=Pinduoduo Contract",
            "[Rule]",
            "AND,((DOMAIN,api.pinduoduo.com),(PROTOCOL,QUIC)),REJECT",
            "[Rewrite]",
            r"^https:\/\/api\.pinduoduo\.com\/feed response-body-json-del data.ad",
            "[MitM]",
            "hostname=api.pinduoduo.com, m.pinduoduo.net",
        ])
        output = convert_lpx_to_stash(
            source,
            "https://kelee.one/Tool/Loon/Lpx/PinDuoDuo_remove_ads.lpx",
            fetch_script_fallback=False,
        )
        parsed = yaml.safe_load(output)
        self.assertEqual(
            parsed["http"]["force-http-engine"],
            ["api.pinduoduo.com:443", "m.pinduoduo.net:443"],
        )
        self.assertEqual(
            parsed["rules"][0],
            "AND,((DOMAIN,api.pinduoduo.com),(NETWORK,UDP),(DST-PORT,443)),REJECT",
        )

if __name__ == "__main__":
    unittest.main()
