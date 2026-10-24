#!/usr/bin/env python3
"""Regression tests for Loon ``${...}`` action variables and rule quoting.

Two reviewer-found latent issues are covered here:

* non-capture ``${param}`` in ``url.replace``/``redirect``/header/json/mock
  values must be baked from the ``[Argument]`` build-time default (or reported
  as a blocking diagnostic when unknown) — it must never be silently copied
  into a generated JavaScript asset;
* Loon rule payload quoting (``USER-AGENT, "httpdns*", REJECT`` /
  ``URL-REGEX, "^http..."``) must be stripped for Stash, and the validator must
  reject a regression that ships the quotes literally.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from convert_kelee_lpx import convert_lpx  # noqa: E402
from kelee_assets import ResourceResolver  # noqa: E402
from validate_stash import validate_bundle  # noqa: E402


def convert(source: str):
    """Convert one LPX and return (result, resolver) for asset inspection."""

    resolver = ResourceResolver("https://vars.example.test", fetch=False)
    result = convert_lpx(
        source,
        "https://vars.example.test/Vars.lpx",
        resolver=resolver,
        public_base="https://vars.example.test",
    )
    return result, resolver


def fixture(
    rewrite_lines: list[str],
    argument_lines: list[str] | None = None,
    rules: list[str] | None = None,
) -> str:
    lines = ["#!name=Variables Contract"]
    if argument_lines:
        lines += ["[Argument]", *argument_lines]
    if rules:
        lines += ["[Rule]", *rules]
    lines += ["[Rewrite]", *rewrite_lines, "[MitM]", "hostname=api.example.com"]
    return "\n".join(lines)


def generated_script_text(resolver: ResourceResolver) -> str:
    assets = resolver.published_assets()
    scripts = [content for relpath, content in assets.items() if relpath.endswith(".js")]
    if not scripts:
        return ""
    text = b"\n".join(scripts).decode("utf-8", "replace")
    # Comments intentionally echo the Loon source (which contains ${...}); only
    # the executable JavaScript must be free of unresolved variables.
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    )


class ParamTemplateTests(unittest.TestCase):
    def test_param_default_is_baked_into_url_replace_script(self) -> None:
        result, resolver = convert(
            fixture(
                [
                    r'request if ${url} ~= /^https:\/\/old\.example\.com(\/.*)$/ as item '
                    r'then url.replace("https://new.example.com/${region}${item.1}")',
                ],
                ['region = input,"CN",tag=地区'],
            )
        )
        self.assertFalse(result.blocking, [str(d) for d in result.diagnostics])
        script = generated_script_text(resolver)
        self.assertIn("https://new.example.com/CN", script)
        self.assertNotIn("${region}", script)
        self.assertNotIn("${region}", result.yaml)
        self.assertIn('{"group": 1}', script)

    def test_numeric_and_boolean_params_bake_with_their_type(self) -> None:
        result, resolver = convert(
            fixture(
                [
                    r'request if ${url} ~= /^https:\/\/old\.example\.com(\/.*)$/ as item '
                    r'then redirect(307, "https://new.example.com/${level}/${enabled}${item.1}")',
                ],
                [
                    "level = input,9.99,type=number,tag=等级",
                    "enabled = switch,true,tag=开关",
                ],
            )
        )
        self.assertFalse(result.blocking, [str(d) for d in result.diagnostics])
        script = generated_script_text(resolver)
        self.assertIn("https://new.example.com/9.99/true", script)
        self.assertNotIn("${level}", script)
        self.assertNotIn("${enabled}", script)

    def test_unknown_param_is_a_blocking_diagnostic(self) -> None:
        result, _resolver = convert(
            fixture(
                [
                    r'request if ${url} ~= /^https:\/\/old\.example\.com(\/.*)$/ as item '
                    r'then url.replace("https://new.example.com/${missing}${item.1}")',
                ],
            )
        )
        self.assertTrue(result.blocking)
        self.assertIn("E_ACTION_VARIABLE", [d.code for d in result.diagnostics])

    def test_capture_in_non_template_value_is_blocking(self) -> None:
        result, _resolver = convert(
            fixture(
                [
                    r'response if ${url} ~= /^https:\/\/api\.example\.com\/(\d+)$/ as item '
                    r'then response.body.mock("text", "id=${item.1}", 200)',
                ],
            )
        )
        self.assertTrue(result.blocking)
        self.assertIn("E_CAPTURE_UNSUPPORTED_CONTEXT", [d.code for d in result.diagnostics])

    def test_json_replace_value_uses_argument_type(self) -> None:
        result, _resolver = convert(
            fixture(
                [
                    r'response if ${url} ~= /^https:\/\/api\.example\.com\/p$/i '
                    r'then response.json.replace("data.price", ${level})',
                ],
                ["level = input,9.99,type=number,tag=等级"],
            )
        )
        self.assertFalse(result.blocking, [str(d) for d in result.diagnostics])
        rule = result.stash["http"]["body-rewrite"][0]
        self.assertIn("response-json-replace data.price 9.99", rule)
        self.assertNotIn("${level}", rule)

    def test_header_value_param_is_baked(self) -> None:
        result, _resolver = convert(
            fixture(
                [
                    r'response if ${url} ~= /^https:\/\/api\.example\.com\/h$/i '
                    r'then response.header.add("x-region", "${region}")',
                ],
                ['region = select,"CN","US",tag=地区'],
            )
        )
        self.assertFalse(result.blocking, [str(d) for d in result.diagnostics])
        self.assertIn("x-region CN", result.yaml)
        self.assertNotIn("${region}", result.yaml)


class RuleQuotingTests(unittest.TestCase):
    def test_quoted_rule_payloads_are_unquoted(self) -> None:
        result, _resolver = convert(
            fixture(
                [],
                rules=[
                    'USER-AGENT, "httpdns*", REJECT',
                    'URL-REGEX, "^http:\\/\\/api\\.weibo\\.cn\\/httpdns\\/config", REJECT',
                    "DOMAIN, quoted.example.com, REJECT",
                ],
            )
        )
        self.assertFalse(result.blocking, [str(d) for d in result.diagnostics])
        rules = result.stash["rules"]
        self.assertIn("USER-AGENT,httpdns*,REJECT", rules)
        self.assertIn(
            "URL-REGEX,^http:\\/\\/api\\.weibo\\.cn\\/httpdns\\/config,REJECT", rules
        )
        self.assertIn("DOMAIN,quoted.example.com,REJECT", rules)
        self.assertFalse(any('"' in rule for rule in rules), rules)

    def test_nested_logical_rule_quotes_are_unquoted(self) -> None:
        result, _resolver = convert(
            fixture(
                [],
                rules=['AND,((DOMAIN,x.example.com),(USER-AGENT,"agent*")),REJECT'],
            )
        )
        self.assertFalse(result.blocking, [str(d) for d in result.diagnostics])
        self.assertIn(
            "AND,((DOMAIN,x.example.com),(USER-AGENT,agent*)),REJECT", result.stash["rules"]
        )


class ValidatorRuleShapeTests(unittest.TestCase):
    def _bundle(self, root: Path, override_text: str) -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / "Vars.stoverride").write_text(override_text, encoding="utf-8")
        (root / "targets.json").write_text('[{"filename": "Vars.lpx"}]', encoding="utf-8")

    def _errors(self, root: Path) -> list[str]:
        diagnostics = validate_bundle(
            root, expected_targets=["Vars.lpx"], public_base="https://vars.example.test"
        )
        return [d.code for d in diagnostics if d.blocking]

    def test_quoted_rule_payload_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bundle"
            self._bundle(
                root,
                'name: Vars\nicon: "https://x.example/i.png"\n'
                "rules:\n  - 'USER-AGENT,\"httpdns*\",REJECT'\n",
            )
            self.assertIn("E_RULE_QUOTED_VALUE", self._errors(root))

    def test_multi_pair_replace_regex_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bundle"
            self._bundle(
                root,
                'name: Vars\nicon: "https://x.example/i.png"\n'
                "http:\n  body-rewrite:\n"
                "    - '^https://api\\.example\\.com/ response-replace-regex old new disabled enabled'\n",
            )
            self.assertEqual(self._errors(root), [])

    def test_parenthesised_alternation_pattern_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bundle"
            self._bundle(
                root,
                'name: Vars\nicon: "https://x.example/i.png"\n'
                "http:\n  url-rewrite:\n"
                "    - '^(https://a\\.example/|https://b\\.example/) - reject'\n",
            )
            self.assertEqual(self._errors(root), [])


class MockHeaderMergeScopeTests(unittest.TestCase):
    """REVIEW round 2: header actions must not be merged across regex branches."""

    def test_header_from_other_alternation_branch_is_not_merged(self) -> None:
        result, _resolver = convert(
            fixture(
                [
                    r'response if ${url} ~= /^https:\/\/api\.example\.com\/v1\/(public|private)$/i '
                    r'then response.body.mock("json", "{}", 200)',
                    r'response if ${url} ~= /^https:\/\/api\.example\.com\/v1\/(admin|internal)$/i '
                    r'then response.header.add("X-Admin", "true")',
                ],
            )
        )
        self.assertFalse(result.blocking, [str(d) for d in result.diagnostics])
        mock = result.stash["http"]["mock"][0]
        self.assertNotIn("X-Admin", mock.get("headers", {}))
        header_rules = result.stash["http"].get("header-rewrite") or []
        self.assertTrue(any("X-Admin" in rule for rule in header_rules), header_rules)

    def test_header_with_broader_path_is_merged(self) -> None:
        result, _resolver = convert(
            fixture(
                [
                    r'response if ${url} ~= /^https:\/\/grpc\.example\.com\/app\.(Teenagers\/ModeStatus|Search\/DefaultWords)$/i '
                    r'then response.body.mock("text", "ok", 200)',
                    r'response if ${url} ~= /^https:\/\/grpc\.example\.com\/app\.(Teenagers\/ModeStatus|Search\/DefaultWords)$/i '
                    r'then response.header.add("grpc-status", "0")',
                ],
            )
        )
        self.assertFalse(result.blocking, [str(d) for d in result.diagnostics])
        mock = result.stash["http"]["mock"][0]
        self.assertEqual(mock.get("headers", {}).get("grpc-status"), "0")
        self.assertFalse(result.stash["http"].get("header-rewrite"))

    def test_header_with_other_host_is_not_merged(self) -> None:
        result, _resolver = convert(
            fixture(
                [
                    r'response if ${url} ~= /^https:\/\/api\.example\.com\/x$/i '
                    r'then response.body.mock("text", "x", 200)',
                    r'response if ${url} ~= /^https:\/\/other\.example\.com\/x$/i '
                    r'then response.header.add("X-Other", "1")',
                ],
            )
        )
        self.assertFalse(result.blocking, [str(d) for d in result.diagnostics])
        mock = result.stash["http"]["mock"][0]
        self.assertNotIn("X-Other", mock.get("headers", {}))


if __name__ == "__main__":
    unittest.main()
