#!/usr/bin/env python3
"""Tests for ``scripts/loon_expr.py`` — the Loon 3.5.1+ expression parser.

Run with::

    python tests/test_loon_expr.py

The suite is stdlib-only (``unittest``).  It is written in two layers:

* unit tests embed *minimal representative* Loon strings (never whole upstream
  plugins) and pin the parser's exact behaviour, including every documented
  rewrite/action/option form used by the 21 upstream plugins;
* an optional integration test parses **every** line of the read-only audit
  snapshots in ``.local/upstream-audit/*.lpx`` and asserts the expected counts
  and semantic details.  It skips cleanly when that internal snapshot directory
  is absent (e.g. CI checkouts), so the unit layer stays hermetic.
"""

from __future__ import annotations

import base64
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import loon_expr as lx  # noqa: E402  (import after sys.path setup)

AUDIT_DIR = ROOT / ".local" / "upstream-audit"


def parse(line: str):
    """Shorthand: parse a rewrite line and assert it is an expression."""
    expr = lx.parse_rewrite_expression(line)
    if expr is None:
        raise AssertionError(f"line was not parsed as a rewrite expression: {line!r}")
    return expr


def parse_script(line: str):
    expr = lx.parse_script_expression(line)
    if expr is None:
        raise AssertionError(f"line was not parsed as a script expression: {line!r}")
    return expr


# A few real upstream lines are quoted as *short* representative fixtures.
QQ_REDIRECT = (
    'request if ${url} ~= /(^https:\\/\\/c\\.pc\\.qq\\.com\\/middlem\\.html\\?pfurl=)'
    '(http.*)(&pfuin=.*)/i as urlMatch then redirect(307, "${urlMatch.2}")'
)
SPOTIFY_URL_REPLACE = (
    "request if ${url} ~= /^https:\\/\\/(?:\\w+-spclient|spclient\\.wg)\\.spotify\\.com"
    "(?::443)?\\/artistview\\/v1\\/artist\\/(.*)&platform=iphone/i as urlMatch "
    'then url.replace("https://spclient.wg.spotify.com/artistview/v1/artist/'
    '${urlMatch.1}&platform=ipad")'
)
BILIBILI_BASE64_MOCK = (
    "response if ${url} ~= /^https:\\/\\/grpc\\.biliapi\\.net\\/bilibili\\.app\\."
    "interface\\.v1\\.Teenagers\\/ModeStatus$/i then response.body.mock("
    '"text", "AAAAABMKEQgCEgl0ZWVuYWdlcnMgAioA", 200, true)'
)
KELI_BODY_REPLACE = (
    "response if ${url} ~= /^https?:\\/\\/cmds\\.omesoft\\.com\\/HypnotistWS\\.asmx/i "
    'then response.body.replace(/<GetADResult>\\.\\*\\?<\\/GetADResult>/, '
    '"<GetADResult>{\\"ret\\":0,\\"msg\\":\\"OK\\",\\"data\\":{\\"ad\\":[]}}'
    '</GetADResult>")'
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ErrorModelTest(unittest.TestCase):
    def test_parse_error_is_value_error_with_fields(self):
        with self.assertRaises(lx.LoonParseError) as caught:
            lx.parse_call("reject(")
        error = caught.exception
        self.assertIsInstance(error, ValueError)
        self.assertEqual(error.code, "E_CALL")
        self.assertTrue(error.detail)
        self.assertEqual(error.text, "reject(")
        self.assertIn("E_CALL", str(error))

    def test_unsupported_error_is_parse_error_subclass(self):
        with self.assertRaises(lx.LoonUnsupportedError) as caught:
            parse(
                'response if ${url} ~= /x/ then response.body.unheard_of("a")'
            )
        error = caught.exception
        self.assertIsInstance(error, lx.LoonParseError)
        self.assertEqual(error.code, "E_ACTION_UNSUPPORTED")

    def test_position_is_exposed(self):
        with self.assertRaises(lx.LoonParseError) as caught:
            lx.parse_value('"abc', 0)
        self.assertEqual(caught.exception.position, 0)

    def test_format_error_includes_code_detail_and_collapsed_input(self):
        message = lx.format_error("E_X", "bad thing", "line one\nline   two")
        self.assertIn("E_X: bad thing", message)
        self.assertIn("line one line two", message)
        self.assertNotIn("\n", message)

    def test_format_error_truncates_long_payloads(self):
        message = lx.format_error("E_X", "bad", "y" * 5000)
        self.assertLess(len(message), 400)
        self.assertIn("...", message)

    def test_format_error_without_line(self):
        self.assertEqual(lx.format_error("E_X", "bad"), "E_X: bad")


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


class ValueParsingTest(unittest.TestCase):
    def test_string_literal(self):
        value, pos = lx.parse_value('"hello world"')
        self.assertIsInstance(value, lx.StringLiteral)
        self.assertEqual(value.value, "hello world")
        self.assertEqual(value.raw, '"hello world"')
        self.assertEqual(pos, len('"hello world"'))
        self.assertEqual(value.templates, ())

    def test_string_escaped_quotes_and_backslashes(self):
        value, _ = lx.parse_value(r'"{\"a\":0}"')
        self.assertEqual(value.value, '{"a":0}')
        value, _ = lx.parse_value(r'"a\\b"')
        self.assertEqual(value.value, "a\\b")
        value, _ = lx.parse_value(r'"\"quoted\""')
        self.assertEqual(value.value, '"quoted"')

    def test_string_control_escapes(self):
        value, _ = lx.parse_value(r'"a\nb\tc\rd"')
        self.assertEqual(value.value, "a\nb\tc\rd")

    def test_string_unknown_escape_is_preserved_verbatim(self):
        value, _ = lx.parse_value(r'"\d+\w"')
        self.assertEqual(value.value, r"\d+\w")

    def test_string_template_variables_are_recorded(self):
        value, _ = lx.parse_value('"price=${price}, region=${region}"')
        self.assertEqual(value.value, "price=${price}, region=${region}")
        self.assertEqual([tpl.name for tpl in value.templates], ["price", "region"])
        self.assertEqual([tpl.capture for tpl in value.templates], [None, None])

    def test_capture_template_and_escaped_template(self):
        value, _ = lx.parse_value('"${urlMatch.2}"')
        self.assertEqual(value.templates[0].capture, ("urlMatch", 2))
        literal, _ = lx.parse_value(r'"price \${price}"')
        self.assertEqual(literal.value, "price ${price}")
        self.assertEqual(literal.templates, ())

    def test_header_variable_template_keeps_single_quoted_name(self):
        value, _ = lx.parse_value('"UA=${request.header[\'User-Agent\']}"')
        self.assertEqual(value.templates[0].name, "request.header['User-Agent']")
        self.assertIsNone(value.templates[0].capture)

    def test_raw_string(self):
        value, _ = lx.parse_value('`{"code":0,"message":"ok"}`')
        self.assertIsInstance(value, lx.RawStringLiteral)
        self.assertEqual(value.value, '{"code":0,"message":"ok"}')
        self.assertEqual(value.raw, '`{"code":0,"message":"ok"}`')

    def test_raw_string_has_no_escapes_and_doubled_backtick(self):
        value, _ = lx.parse_value("`a``b\\n`")
        self.assertEqual(value.value, "a`b\\n")

    def test_raw_string_does_not_expand_templates(self):
        value, _ = lx.parse_value("`${not.a.variable}`")
        self.assertEqual(value.value, "${not.a.variable}")

    def test_regex_literal_with_flags(self):
        value, pos = lx.parse_value("/^https:\\/\\/a\\.com/i")
        self.assertIsInstance(value, lx.RegexLiteral)
        self.assertEqual(value.pattern, "^https:\\/\\/a\\.com")
        self.assertEqual(value.flags, "i")
        self.assertEqual(pos, len("/^https:\\/\\/a\\.com/i"))

    def test_regex_escaped_slash_and_character_class_slash(self):
        value, _ = lx.parse_value(r"/a\/b[/]c/")
        self.assertEqual(value.pattern, r"a\/b[/]c")
        value, _ = lx.parse_value(r"/<(GetADResult)>\.\*\?<\/GetADResult>/")
        self.assertEqual(value.pattern, r"<(GetADResult)>\.\*\?<\/GetADResult>")

    def test_regex_flags_normalized_and_duplicates_removed(self):
        value, _ = lx.parse_value("/x/ISM")
        self.assertEqual(value.flags, "ims")

    def test_number_int_and_float(self):
        value, _ = lx.parse_value("200")
        self.assertEqual((value.value, value.raw), (200, "200"))
        value, _ = lx.parse_value("9.99")
        self.assertEqual(value.value, 9.99)
        value, _ = lx.parse_value("-12")
        self.assertEqual(value.value, -12)

    def test_bool_null(self):
        self.assertEqual(lx.parse_value("true")[0], lx.BoolLiteral(True))
        self.assertEqual(lx.parse_value("false")[0], lx.BoolLiteral(False))
        self.assertEqual(lx.parse_value("null")[0], lx.NullLiteral())

    def test_bare_word_is_rejected(self):
        with self.assertRaises(lx.LoonParseError) as caught:
            lx.parse_value("hello")
        self.assertEqual(caught.exception.code, "E_VALUE")

    def test_array_nested(self):
        value, _ = lx.parse_value('["a", [1, true], /x/i]')
        self.assertIsInstance(value, lx.ArrayLiteral)
        self.assertEqual(len(value.items), 3)
        self.assertIsInstance(value.items[1], lx.ArrayLiteral)
        self.assertIsInstance(value.items[2], lx.RegexLiteral)

    def test_empty_array(self):
        value, _ = lx.parse_value("[]")
        self.assertEqual(value.items, ())

    def test_object_explicit_entries(self):
        value, _ = lx.parse_value('{"a": 1, b: true}')
        self.assertIsInstance(value, lx.ObjectLiteral)
        self.assertEqual(value.keys(), ("a", "b"))
        self.assertEqual(value.as_dict(), {"a": 1, "b": True})

    def test_object_shorthand_variables(self):
        value, _ = lx.parse_value("{${tab}, ${useractivity}}")
        self.assertEqual(value.keys(), ("tab", "useractivity"))
        self.assertIsInstance(value.get("tab"), lx.VariableRef)

    def test_object_shorthand_with_dotted_argument_names(self):
        value, _ = lx.parse_value("{${Weather.Provider}, ${API.QWeather.Token}}")
        self.assertEqual(value.keys(), ("Weather.Provider", "API.QWeather.Token"))

    def test_empty_object(self):
        self.assertEqual(lx.parse_value("{}")[0].entries, ())

    def test_parse_value_honours_start_position(self):
        text = 'prefix "value" suffix'
        value, pos = lx.parse_value(text, text.index('"'))
        self.assertEqual(value.value, "value")
        self.assertEqual(text[pos:], " suffix")

    def test_variable_reference(self):
        value, _ = lx.parse_value("${request.status}")
        self.assertIsInstance(value, lx.VariableRef)
        self.assertEqual(value.name, "request.status")

    def test_unterminated_string_raises(self):
        with self.assertRaises(lx.LoonParseError) as caught:
            lx.parse_value('"abc')
        self.assertEqual(caught.exception.code, "E_STRING_UNTERMINATED")

    def test_unterminated_regex_raises(self):
        with self.assertRaises(lx.LoonParseError) as caught:
            lx.parse_value("/abc")
        self.assertEqual(caught.exception.code, "E_REGEX_UNTERMINATED")

    def test_unsupported_regex_flag_raises(self):
        with self.assertRaises(lx.LoonUnsupportedError) as caught:
            lx.parse_value("/abc/g")
        self.assertEqual(caught.exception.code, "E_REGEX_FLAGS")

    def test_trailing_comma_in_array_rejected(self):
        with self.assertRaises(lx.LoonParseError) as caught:
            lx.parse_value("[1,]")
        self.assertEqual(caught.exception.code, "E_ARRAY")


class SplitTopLevelTest(unittest.TestCase):
    def test_simple_split(self):
        self.assertEqual(lx.split_top_level("a, b,c"), ["a", "b", "c"])

    def test_empty_input(self):
        self.assertEqual(lx.split_top_level(""), [])

    def test_commas_in_strings_are_ignored(self):
        self.assertEqual(
            lx.split_top_level('"a,b", c'), ['"a,b"', "c"]
        )

    def test_commas_in_nested_brackets_are_ignored(self):
        self.assertEqual(
            lx.split_top_level('del(.a, .b), ["x", "y"]'),
            ["del(.a, .b)", '["x", "y"]'],
        )

    def test_regex_literals_are_ignored(self):
        self.assertEqual(
            lx.split_top_level('[/false/, /disabled/], ["true", "enabled"]'),
            ["[/false/, /disabled/]", '["true", "enabled"]'],
        )

    def test_regex_with_pipe_is_not_an_action_separator(self):
        self.assertEqual(
            lx.split_top_level("/a|b/ , /c|d/i", "|"),
            ["/a|b/ , /c|d/i"],
        )

    def test_raw_string_and_escaped_quotes(self):
        self.assertEqual(
            lx.split_top_level('`a,b`, "c\\",d"', ","),
            ["`a,b`", '"c\\",d"'],
        )

    def test_legacy_mode_ignores_regex_delimiters(self):
        self.assertEqual(
            lx.split_top_level(
                "script-path=https://a.b/c.js,requires-body=true",
                ",",
                regex_literals=False,
            ),
            ["script-path=https://a.b/c.js", "requires-body=true"],
        )

    def test_separator_must_be_single_character(self):
        with self.assertRaises(ValueError):
            lx.split_top_level("a,b", ",,")


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------


class CallParsingTest(unittest.TestCase):
    def test_simple_call(self):
        call = lx.parse_call('reject_dict(200)')
        self.assertEqual(call.name, "reject_dict")
        self.assertEqual(call.args, (lx.NumberLiteral(200, "200"),))

    def test_nested_call_args(self):
        call = lx.parse_call('response.json.replace(["a.b", "c.d"], [0, true])')
        self.assertEqual(call.name, "response.json.replace")
        self.assertEqual(call.args[0].keys() if hasattr(call.args[0], "keys") else None, None)
        self.assertIsInstance(call.args[0], lx.ArrayLiteral)
        self.assertEqual(len(call.args[0].items), 2)
        self.assertIsInstance(call.args[1], lx.ArrayLiteral)

    def test_call_with_object_argument(self):
        call = lx.parse_call('script("https://x/y.js", {${a}, ${b}})')
        self.assertEqual(call.name, "script")
        self.assertEqual(call.args[0].value, "https://x/y.js")
        self.assertEqual(call.args[1].keys(), ("a", "b"))

    def test_no_argument_call(self):
        self.assertEqual(lx.parse_call("script()").args, ())

    def test_trailing_text_rejected(self):
        with self.assertRaises(lx.LoonParseError) as caught:
            lx.parse_call("reject(404) junk")
        self.assertEqual(caught.exception.code, "E_CALL_TRAILING")

    def test_missing_paren_rejected(self):
        with self.assertRaises(lx.LoonParseError) as caught:
            lx.parse_call("reject")
        self.assertEqual(caught.exception.code, "E_CALL")

    def test_unterminated_call_rejected(self):
        with self.assertRaises(lx.LoonParseError) as caught:
            lx.parse_call("reject(404")
        self.assertEqual(caught.exception.code, "E_CALL")


# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------


class RegexHelpersTest(unittest.TestCase):
    def test_stash_regex_unescapes_only_slash_and_prefixes_inline_flags(self):
        regex = lx.RegexLiteral(r"^https:\/\/a\.com\/x\?y=1", "i")
        self.assertEqual(
            lx.stash_regex(regex), r"(?i)^https://a\.com/x\?y=1"
        )

    def test_stash_regex_without_flags_has_no_prefix(self):
        self.assertEqual(lx.stash_regex(lx.RegexLiteral("a")), "a")
        self.assertEqual(lx.stash_regex(lx.RegexLiteral("a", "")), "a")

    def test_stash_regex_multi_flags_sorted_canonically(self):
        self.assertEqual(lx.stash_regex(lx.RegexLiteral("x", "si")), "(?is)x")
        self.assertEqual(lx.stash_regex(lx.RegexLiteral("x", "mis")), "(?ims)x")

    def test_stash_regex_rejects_unsupported_flags(self):
        with self.assertRaises(lx.LoonUnsupportedError) as caught:
            lx.stash_regex(lx.RegexLiteral("x", "g"))
        self.assertEqual(caught.exception.code, "E_REGEX_FLAGS")
        self.assertIn("g", caught.exception.detail)

    def test_stash_regex_requires_regex_literal(self):
        with self.assertRaises(TypeError):
            lx.stash_regex("^https://x")  # type: ignore[arg-type]

    def test_format_regex_error_flags(self):
        self.assertEqual(lx.format_regex_error_flags(""), "")
        self.assertEqual(lx.format_regex_error_flags("i"), "(?i)")
        with self.assertRaises(lx.LoonUnsupportedError):
            lx.format_regex_error_flags("u")

    def test_count_capture_groups(self):
        self.assertEqual(lx.count_capture_groups(""), 0)
        self.assertEqual(lx.count_capture_groups("^https://x"), 0)
        self.assertEqual(lx.count_capture_groups("(a)(b)"), 2)
        self.assertEqual(lx.count_capture_groups("(?:a)(b)"), 1)
        self.assertEqual(lx.count_capture_groups(r"\(literal\)"), 0)
        self.assertEqual(lx.count_capture_groups("[()]"), 0)
        self.assertEqual(lx.count_capture_groups("(?<name>a)"), 1)
        self.assertEqual(lx.count_capture_groups("(?P<name>a)"), 1)
        self.assertEqual(lx.count_capture_groups("(?=a)(?!b)(?<=c)(?<!d)"), 0)
        self.assertEqual(lx.count_capture_groups(r"(\d+)-(\w+)"), 2)

    def test_group_count_property(self):
        self.assertEqual(lx.RegexLiteral("(a)(b)(c)").group_count, 3)


# ---------------------------------------------------------------------------
# Rewrite expressions
# ---------------------------------------------------------------------------


class RewriteExpressionTest(unittest.TestCase):
    def test_reject_404(self):
        expr = parse(
            "request if ${url} ~= /^https:\\/\\/ad\\.example\\.com\\//i then reject(404)"
        )
        self.assertEqual(expr.phase, "request")
        self.assertEqual(expr.regex.pattern, r"^https:\/\/ad\.example\.com\/")
        self.assertEqual(expr.regex.flags, "i")
        self.assertEqual(expr.action_names, ("reject",))
        self.assertEqual(expr.action("reject").number(0), 404)

    def test_reject_with_body(self):
        expr = parse(
            'request if ${url} ~= /x/ then reject(451, "Unavailable for legal reasons")'
        )
        action = expr.action("reject")
        self.assertEqual(action.number(0), 451)
        self.assertEqual(action.string(1), "Unavailable for legal reasons")

    def test_reject_variants(self):
        for name, code in (
            ("reject_dict", "E_ACTION_UNSUPPORTED"),
            ("reject_array", "E_ACTION_UNSUPPORTED"),
            ("reject_img", "E_ACTION_UNSUPPORTED"),
            ("reject_video", "E_ACTION_UNSUPPORTED"),
        ):
            expr = parse(f"request if ${{url}} ~= /x/ then {name}(200)")
            self.assertEqual(expr.action_names, (name,))
            self.assertEqual(expr.action(name).number(0), 200)

    def test_yaml_chunk_header_add(self):
        expr = parse(
            'response if ${url} ~= /^https:\\/\\/grpc\\.biliapi\\.net\\/x$/i '
            'then response.header.add("grpc-status", "0")'
        )
        action = expr.action("response.header.add")
        self.assertEqual(action.string(0), "grpc-status")
        self.assertEqual(action.string(1), "0")

    def test_json_delete_string_and_batch(self):
        expr = parse(
            'response if ${url} ~= /x/ then response.json.delete("data.topList")'
        )
        self.assertEqual(expr.action("response.json.delete").string(0), "data.topList")
        expr = parse(
            "response if ${url} ~= /x/ then "
            'response.json.delete(["json.ad", "json.toc"])'
        )
        self.assertEqual(
            expr.action("response.json.delete").python_args(),
            (["json.ad", "json.toc"],),
        )

    def test_json_jq_with_escaped_quotes_and_pipes(self):
        raw = (
            "response if ${url} ~= /x/ then response.json.jq("
            '".data.items |= map(select(.type == \\"ad\\" | not))")'
        )
        expr = parse(raw)
        self.assertEqual(
            expr.action("response.json.jq").string(0),
            '.data.items |= map(select(.type == "ad" | not))',
        )

    def test_json_jq_file(self):
        expr = parse(
            'response if ${url} ~= /x/ then response.json.jq_file("https://kelee.one/a.jq")'
        )
        self.assertEqual(
            expr.action("response.json.jq_file").string(0), "https://kelee.one/a.jq"
        )

    def test_json_replace_batch_values(self):
        expr = parse(
            "response if ${url} ~= /x/ then response.json.replace("
            '["data.a", "data.b"], [0, 0])'
        )
        action = expr.action("response.json.replace")
        self.assertEqual(action.args[0].items[0].value, "data.a")
        self.assertEqual(action.python_args()[1], [0, 0])

    def test_json_add_single_value(self):
        expr = parse('response if ${url} ~= /x/ then response.json.add("a.b", true)')
        action = expr.action("response.json.add")
        self.assertEqual(action.python_args(), ("a.b", True))

    def test_response_body_mock_json_with_escaped_body(self):
        expr = parse(
            'response if ${url} ~= /x/ then response.body.mock("json", '
            '"{\\"data\\":{\\"cards\\":[{\\"type\\":\\"RECOMMENDS\\"}]},\\"ret\\":0}", 200)'
        )
        action = expr.action("response.body.mock")
        self.assertEqual(action.string(0), "json")
        body = json.loads(action.string(1))
        self.assertEqual(body["ret"], 0)
        self.assertEqual(body["data"]["cards"][0]["type"], "RECOMMENDS")
        self.assertEqual(action.number(2), 200)
        self.assertIsNone(action.boolean(3))

    def test_response_body_mock_default_status_omitted(self):
        expr = parse(
            'response if ${url} ~= /x/ then response.body.mock("json", "{}")'
        )
        action = expr.action("response.body.mock")
        self.assertEqual(len(action.args), 2)
        self.assertIsNone(action.number(2))

    def test_base64_mock_fourth_argument(self):
        expr = parse(BILIBILI_BASE64_MOCK)
        action = expr.action("response.body.mock")
        self.assertEqual(action.string(0), "text")
        self.assertEqual(action.number(2), 200)
        self.assertTrue(action.boolean(3))
        decoded = base64.b64decode(action.string(1), validate=True)
        self.assertTrue(decoded.startswith(b"\x00\x00"))

    def test_mock_with_sibling_response_header(self):
        expr = parse(
            'response if ${url} ~= /x/ then response.body.mock("json", "{}", 200) '
            '| response.header.add("grpc-status", "0")'
        )
        self.assertEqual(
            expr.action_names, ("response.body.mock", "response.header.add")
        )
        self.assertEqual(len(expr.header_actions()), 1)

    def test_body_replace_regex_and_replacement(self):
        expr = parse(KELI_BODY_REPLACE)
        action = expr.action("response.body.replace")
        regex = action.regex(0)
        self.assertEqual(regex.pattern, r"<GetADResult>\.\*\?<\/GetADResult>")
        self.assertIn('"ret":0', action.string(1))
        self.assertTrue(action.string(1).endswith("</GetADResult>"))
        self.assertNotIn('\\"', action.string(1))

    def test_body_replace_keeps_local_capture_refs(self):
        expr = parse(
            "request if ${url} ~= /x/ then request.body.replace(/price=(\\d+)/, "
            '"amount=$1")'
        )
        action = expr.action("request.body.replace")
        self.assertEqual(action.regex(0).pattern, r"price=(\d+)")
        self.assertEqual(action.string(1), "amount=$1")

    def test_header_replace_with_regex_and_capture(self):
        expr = parse(
            'response if ${url} ~= /x/ then response.header.replace("Content-Type", '
            '/^(.+); charset=.+$/i, "$1")'
        )
        action = expr.action("response.header.replace")
        self.assertEqual(action.string(0), "Content-Type")
        self.assertEqual(action.regex(1).pattern, "^(.+); charset=.+$")
        self.assertEqual(action.string(2), "$1")

    def test_batch_header_replace(self):
        expr = parse(
            "request if ${url} ~= /api/ then request.header.replace("
            '["X-A", "X-B"], [/old-a/, /old-b/i], ["new-a", "new-b"])'
        )
        action = expr.action("request.header.replace")
        self.assertEqual(len(action.args[0].items), 2)
        self.assertEqual(action.args[1].items[1].pattern, "old-b")
        self.assertEqual(action.args[1].items[1].flags, "i")

    def test_header_set_and_del(self):
        expr = parse(
            'request if ${url} ~= /x/ then request.header.set("X", "1") '
            '| request.header.del("Cookie")'
        )
        self.assertEqual(expr.action_names, ("request.header.set", "request.header.del"))
        self.assertEqual(expr.action("request.header.del").string(0), "Cookie")

    def test_capture_redirect_qq(self):
        expr = parse(QQ_REDIRECT)
        self.assertEqual(expr.phase, "request")
        self.assertEqual(expr.capture_name, "urlMatch")
        self.assertEqual(expr.regex.group_count, 3)
        redirect = expr.action("redirect")
        self.assertEqual(redirect.number(0), 307)
        self.assertEqual(redirect.string(1), "${urlMatch.2}")
        template = redirect.args[1].templates[0]
        self.assertEqual(template.capture, ("urlMatch", 2))

    def test_capture_redirect_exact_group_boundary(self):
        expr = parse(
            'request if ${url} ~= /(a)(b)(c)/i as m then redirect(307, "${m.3}")'
        )
        self.assertEqual(expr.regex.group_count, 3)
        self.assertEqual(
            expr.action("redirect").args[1].templates[0].capture, ("m", 3)
        )

    def test_url_replace_with_capture(self):
        expr = parse(SPOTIFY_URL_REPLACE)
        self.assertEqual(expr.capture_name, "urlMatch")
        action = expr.action("url.replace")
        self.assertIn("${urlMatch.1}", action.string(0))
        self.assertIn("platform=ipad", action.string(0))
        self.assertEqual(
            action.args[0].templates[0].capture,
            ("urlMatch", 1),
        )

    def test_request_body_mock(self):
        expr = parse(
            'request if ${url} ~= /x/ then request.body.mock("json", '
            '`{"price":9.99}`)'
        )
        action = expr.action("request.body.mock")
        self.assertEqual(action.string(0), "json")
        self.assertEqual(action.args[1].value, '{"price":9.99}')

    def test_request_body_mock_base64_third_argument(self):
        expr = parse(
            'request if ${url} ~= /x/ then request.body.mock("png", '
            '"iVBORw0KGgo=", true)'
        )
        action = expr.action("request.body.mock")
        self.assertEqual(action.string(0), "png")
        self.assertTrue(action.boolean(2))

    def test_request_json_side_actions(self):
        expr = parse(
            'request if ${url} ~= /x/ then request.json.add("a.b", true) '
            '| request.json.delete("a.c") | request.json.jq(".d = null")'
        )
        self.assertEqual(
            expr.action_names,
            ("request.json.add", "request.json.delete", "request.json.jq"),
        )

    def test_mock_file_variants(self):
        expr = parse(
            'response if ${url} ~= /x/ then response.body.mock_file('
            '"json", "response_body.json", 200)'
        )
        action = expr.action("response.body.mock_file")
        self.assertEqual(action.string(1), "response_body.json")
        expr = parse(
            'request if ${url} ~= /x/ then request.body.mock_file('
            '"json", "request_body.json")'
        )
        self.assertEqual(expr.action_names, ("request.body.mock_file",))

    def test_phase_and_raw_are_recorded(self):
        line = "  request if ${url} ~= /x/i then reject(404)  "
        expr = parse(line)
        self.assertEqual(expr.phase, "request")
        self.assertEqual(expr.raw, line.strip())

    def test_legacy_rewrite_lines_return_none(self):
        for line in (
            "^http://example.com reject-200",
            "^http://example.com 302 https://example.com",
            "^http://example.com header-add Connection keep-alive",
            "",
            "   ",
            "# comment",
        ):
            self.assertIsNone(lx.parse_rewrite_expression(line), line)

    def test_condition_regex_may_contain_then_and_pipe(self):
        expr = parse(
            'request if ${url} ~= /then|otherwise/ then reject(404)'
        )
        self.assertEqual(expr.regex.pattern, "then|otherwise")

    def test_multiple_actions_run_left_to_right(self):
        expr = parse(
            'request if ${url} ~= /x/ then request.header.set("X-Value", "first") '
            '| request.header.set("X-Value", "second")'
        )
        self.assertEqual(
            [action.string(1) for action in expr.actions], ["first", "second"]
        )


class RewriteUnsupportedTest(unittest.TestCase):
    def assert_unsupported(self, line: str, code: str) -> lx.LoonUnsupportedError:
        with self.assertRaises(lx.LoonUnsupportedError) as caught:
            lx.parse_rewrite_expression(line)
        self.assertEqual(caught.exception.code, code)
        self.assertTrue(caught.exception.text)
        return caught.exception

    def assert_parse_error(self, line: str, code: str) -> lx.LoonParseError:
        with self.assertRaises(lx.LoonParseError) as caught:
            lx.parse_rewrite_expression(line)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_response_status_condition_is_unsupported(self):
        self.assert_unsupported(
            "response if ${url} ~= /x/ && ${response.status} == 200 "
            'then response.json.delete("a")',
            "E_CONDITION_UNSUPPORTED",
        )

    def test_request_method_condition_is_unsupported(self):
        self.assert_unsupported(
            'request if ${request.method} == "POST" then request.header.set("X", "1")',
            "E_CONDITION_UNSUPPORTED",
        )

    def test_or_condition_is_unsupported(self):
        self.assert_unsupported(
            'request if ${request.method} == "GET" || ${url} ~= /x/ then reject(404)',
            "E_CONDITION_UNSUPPORTED",
        )

    def test_dynamic_condition_regex_is_unsupported(self):
        self.assert_unsupported(
            "request if ${url} ~= ${urlPattern} then reject(404)",
            "E_CONDITION_DYNAMIC_REGEX",
        )

    def test_unknown_action_is_unsupported(self):
        self.assert_unsupported(
            'response if ${url} ~= /x/ then response.body.header.add("a", "b")',
            "E_ACTION_UNSUPPORTED",
        )

    def test_capture_out_of_range_raises(self):
        error = self.assert_parse_error(
            'request if ${url} ~= /(a)(b)/i as m then redirect(307, "${m.3}")',
            "E_CAPTURE_OUT_OF_RANGE",
        )
        self.assertIn("2 group(s)", error.detail)

    def test_capture_undeclared_raises(self):
        self.assert_unsupported(
            'request if ${url} ~= /x/ then redirect(307, "${m.1}")',
            "E_CAPTURE_UNDECLARED",
        )

    def test_wrong_capture_name_raises(self):
        self.assert_unsupported(
            'request if ${url} ~= /(a)/i as urlMatch then redirect(307, "${item.1}")',
            "E_CAPTURE_UNDECLARED",
        )

    def test_capture_without_index_raises(self):
        self.assert_parse_error(
            'request if ${url} ~= /(a)/i as m then redirect(307, "${m}")',
            "E_CAPTURE_REF",
        )

    def test_url_replace_action_local_capture_is_rejected(self):
        self.assert_parse_error(
            'request if ${url} ~= /x/i then url.replace("$1")',
            "E_URL_REPLACE_LOCAL_CAPTURE",
        )

    def test_redirect_action_local_capture_is_rejected(self):
        self.assert_parse_error(
            'request if ${url} ~= /x/i then redirect(302, "$1")',
            "E_URL_REPLACE_LOCAL_CAPTURE",
        )

    def test_unsupported_regex_flag_raises(self):
        self.assert_unsupported(
            "request if ${url} ~= /x/g then reject(404)", "E_REGEX_FLAGS"
        )

    def test_url_action_in_response_phase_is_unsupported(self):
        self.assert_unsupported(
            "response if ${url} ~= /x/ then reject(404)", "E_PHASE_UNSUPPORTED"
        )

    def test_phase_mismatch_raises(self):
        self.assert_parse_error(
            'request if ${url} ~= /x/ then response.header.add("a", "b")',
            "E_PHASE_MISMATCH",
        )

    def test_mixed_phase_actions_raise(self):
        self.assert_parse_error(
            'request if ${url} ~= /x/ then request.header.add("a", "b") '
            '| response.header.add("c", "d")',
            "E_PHASE_MIX",
        )

    def test_multiple_mocks_rejected(self):
        self.assert_parse_error(
            'response if ${url} ~= /x/ then response.body.mock("json", "{}") '
            '| response.body.mock("json", "[]")',
            "E_MOCK_COMBO",
        )

    def test_mock_with_non_header_action_rejected(self):
        self.assert_parse_error(
            'response if ${url} ~= /x/ then response.body.mock("json", "{}") '
            '| response.json.delete("a")',
            "E_MOCK_COMBO",
        )

    def test_unsupported_mock_content_type(self):
        self.assert_unsupported(
            'response if ${url} ~= /x/ then response.body.mock("xml", "<a/>")',
            "E_MOCK_CONTENT_TYPE",
        )

    def test_missing_then_raises(self):
        self.assert_parse_error(
            "request if ${url} ~= /x/i", "E_REWRITE_SYNTAX"
        )

    def test_missing_action_after_then_raises(self):
        self.assert_parse_error(
            "request if ${url} ~= /x/i then   ", "E_REWRITE_ACTION"
        )

    def test_script_call_in_rewrite_raises_helpful_error(self):
        error = self.assert_unsupported(
            'response if ${url} ~= /x/i then script("https://x/y.js")',
            "E_NOT_REWRITE_ACTION",
        )
        self.assertIn("parse_script_expression", error.detail)

    def test_batch_and_scalar_arguments_cannot_mix(self):
        self.assert_parse_error(
            'request if ${url} ~= /api/ then request.header.set(["X-A"], "1")',
            "E_ACTION_BATCH",
        )

    def test_batch_arrays_must_have_equal_lengths(self):
        self.assert_parse_error(
            "response if ${url} ~= /api/ then response.body.replace("
            '[/a/, /b/], ["x"])',
            "E_ACTION_BATCH",
        )

    def test_empty_batch_array_rejected(self):
        self.assert_parse_error(
            'request if ${url} ~= /api/ then request.header.del([])',
            "E_ACTION_BATCH",
        )

    def test_nested_batch_array_rejected(self):
        self.assert_parse_error(
            'request if ${url} ~= /api/ then request.header.del([["a"]])',
            "E_ACTION_BATCH",
        )

    def test_wrong_argument_type(self):
        self.assert_parse_error(
            'request if ${url} ~= /x/ then redirect(302, 302)',
            "E_ACTION_ARG_TYPE",
        )

    def test_wrong_argument_count(self):
        self.assert_parse_error(
            'request if ${url} ~= /x/ then redirect(302)',
            "E_ACTION_ARITY",
        )

    def test_reject_status_out_of_range(self):
        self.assert_parse_error(
            "request if ${url} ~= /x/ then reject(999)", "E_REJECT_STATUS"
        )
        self.assert_parse_error(
            "request if ${url} ~= /x/ then reject(-1)", "E_REJECT_STATUS"
        )

    def test_empty_action_segment_raises(self):
        self.assert_parse_error(
            "request if ${url} ~= /x/ then reject(404) | ", "E_ACTION_EMPTY"
        )


# ---------------------------------------------------------------------------
# Script expressions
# ---------------------------------------------------------------------------


class ScriptExpressionTest(unittest.TestCase):
    def test_script_without_options(self):
        expr = parse_script(
            "response if ${url} ~= /^https:\\/\\/api\\.m\\.jd\\.com\\/client\\.action/i "
            'then script("https://kelee.one/Resource/JavaScript/JD/JD_remove_ads.js") '
            'with tag="移除京东广告", requires_body=true'
        )
        self.assertEqual(expr.phase, "response")
        self.assertEqual(
            expr.script_path, "https://kelee.one/Resource/JavaScript/JD/JD_remove_ads.js"
        )
        self.assertIsNone(expr.argument_expr)
        self.assertEqual(expr.options["tag"], "移除京东广告")
        self.assertIs(expr.options["requires_body"], True)
        self.assertFalse(expr.has_argument)

    def test_script_with_object_argument(self):
        expr = parse_script(
            "response if ${url} ~= /^https:\\/\\/api\\.m\\.jd\\.com/i then "
            'script("https://kelee.one/x.js", {${tab}, ${useractivity}}) '
            'with tag="Protobuf处理", timeout=5, requires_body=true, '
            "binary_body_mode=true"
        )
        self.assertEqual(expr.argument_keys, ("tab", "useractivity"))
        self.assertEqual(expr.options["timeout"], 5)
        self.assertIs(expr.options["binary_body_mode"], True)
        self.assertIsInstance(expr.argument_expr.get("tab"), lx.VariableRef)

    def test_script_argument_with_dotted_names(self):
        expr = parse_script(
            "response if ${url} ~= /x/i then script("
            '"https://github.com/x/response.bundle.js", {${Weather.Provider}, '
            "${API.QWeather.Token}}) with tag=\"天气响应\", requires_body=true"
        )
        self.assertEqual(
            expr.argument_keys, ("Weather.Provider", "API.QWeather.Token")
        )
        self.assertEqual(len(expr.argument_expr.entries), 2)

    def test_script_enable_switch_option_is_variable_ref(self):
        expr = parse_script(
            "request if ${url} ~= /x/i then script(\"https://kelee.one/x.js\", "
            "{${logLevel}}) with enable=${sponsorBlock}, tag=\"空降助手\", "
            "timeout=10, requires_body=true, binary_body_mode=true"
        )
        self.assertIsInstance(expr.options["enable"], lx.VariableRef)
        self.assertEqual(expr.options["enable"].name, "sponsorBlock")
        self.assertEqual(expr.options["tag"], "空降助手")
        self.assertEqual(expr.option("timeout"), 10)

    def test_script_payload_binary_body_mode(self):
        expr = parse_script(
            "request if ${url} ~= /x/i then script(\"https://kelee.one/x.js\") "
            "with tag=\"Proto\", requires_body=true, binary_body_mode=true"
        )
        self.assertIs(expr.options["requires_body"], True)
        self.assertIs(expr.options["binary_body_mode"], True)

    def test_all_31_script_forms_are_representable(self):
        # One representative line per distinct upstream script form.  The
        # exhaustive check over the 31 real lines lives in AuditSnapshotTest.
        lines = [
            # plain: only tag+requires_body (25 of the 31 upstream lines)
            'response if ${url} ~= /^https:\\/\\/x\\.com\\/a\\?/i then '
            'script("https://kelee.one/a.js") with tag="A", requires_body=true',
            # binary proto
            'request if ${url} ~= /^https:\\/\\/x\\.com\\/b/i then '
            'script("https://kelee.one/b.js") with tag="B", requires_body=true, '
            "binary_body_mode=true",
            # timeout
            'response if ${url} ~= /^https:\\/\\/x\\.com\\/c/i then '
            'script("https://kelee.one/c.js") with tag="C", timeout=5, '
            "requires_body=true",
            # single-parameter object argument
            'request if ${url} ~= /^https:\\/\\/x\\.com\\/d$/i then '
            'script("https://kelee.one/d.js", {${logLevel}}) with '
            'enable=${sponsorBlock}, tag="D", timeout=10, requires_body=true, '
            "binary_body_mode=true",
            # two-parameter object argument
            'request if ${url} ~= /^https:\\/\\/x\\.com\\/e$/i then '
            'script("https://kelee.one/e.js", {${purifyComment}, ${logLevel}}) '
            'with enable=${optimizeRequest}, tag="E", timeout=10, '
            "requires_body=true, binary_body_mode=true",
            # four-parameter object argument
            'response if ${url} ~= /^https:\\/\\/x\\.com\\/f$/i then '
            'script("https://kelee.one/f.js", {${displayUpList}, '
            '${purifyComment}, ${sponsorBlock}, ${logLevel}}) with '
            'tag="F", requires_body=true, binary_body_mode=true',
            # nine-parameter object argument (WeatherKit)
            'response if ${url} ~= /^https?:\\/\\/weatherkit\\.apple\\.com\\/api\\/v2/i '
            'then script("https://github.com/x/response.bundle.js", '
            "{${Weather.Provider}, ${NextHour.Provider}, "
            "${AirQuality.Calculate.Algorithm}, ${API.ColorfulClouds.Token}, "
            "${API.QWeather.Host}, ${API.QWeather.Token}, ${API.WAQI.Token}, "
            "${Storage}, ${LogLevel}}) with tag=\"天气响应\", requires_body=true, "
            "binary_body_mode=true",
            # escaped regex groups in the condition
            'request if ${url} ~= /^https:\\/\\/x\\.com\\/(solution\\-|common\\-)?'
            '(a|b)\\/api\\//i then script("https://kelee.one/g.js") with '
            'tag="G", requires_body=true',
        ]
        for line in lines:
            expr = parse_script(line)
            self.assertTrue(expr.script_path.startswith("https://"))
            self.assertEqual(expr.regex.flags, "i")

    def test_script_path_may_be_raw_string(self):
        expr = parse_script(
            "response if ${url} ~= /x/i then script(`https://x/y.js`)"
        )
        self.assertEqual(expr.script_path, "https://x/y.js")

    def test_script_expression_rejects_path_variable(self):
        with self.assertRaises(lx.LoonUnsupportedError) as caught:
            lx.parse_script_expression(
                "response if ${url} ~= /x/i then script(${scriptPath})"
            )
        self.assertEqual(caught.exception.code, "E_SCRIPT_PATH_VARIABLE")

    def test_script_requires_a_path(self):
        with self.assertRaises(lx.LoonParseError) as caught:
            lx.parse_script_expression("response if ${url} ~= /x/i then script()")
        self.assertEqual(caught.exception.code, "E_SCRIPT_PATH")

    def test_unknown_script_option_is_unsupported(self):
        with self.assertRaises(lx.LoonUnsupportedError) as caught:
            lx.parse_script_expression(
                'response if ${url} ~= /x/i then script("https://x/y.js") '
                "with mystery=1"
            )
        self.assertEqual(caught.exception.code, "E_SCRIPT_OPTION_UNKNOWN")

    def test_unquoted_tag_is_a_parse_error(self):
        with self.assertRaises(lx.LoonParseError) as caught:
            lx.parse_script_expression(
                'response if ${url} ~= /x/i then script("https://x/y.js") '
                "with tag=hello"
            )
        self.assertEqual(caught.exception.code, "E_SCRIPT_OPTION_VALUE")

    def test_bad_option_value_types(self):
        cases = (
            ('with timeout=ten', "E_SCRIPT_OPTION_VALUE"),
            ('with timeout=-1', "E_SCRIPT_OPTION"),
            ('with tag=10', "E_SCRIPT_OPTION"),
            ('with enables=true', "E_SCRIPT_OPTION_UNKNOWN"),
        )
        for tail, code in cases:
            with self.assertRaises(lx.LoonParseError) as caught:
                lx.parse_script_expression(
                    f'response if ${{url}} ~= /x/i then script("https://x/y.js") {tail}'
                )
            self.assertEqual(caught.exception.code, code, tail)

    def test_enable_must_be_boolean_or_variable(self):
        with self.assertRaises(lx.LoonParseError) as caught:
            lx.parse_script_expression(
                'response if ${url} ~= /x/i then script("https://x/y.js") '
                'with enable="yes"'
            )
        self.assertEqual(caught.exception.code, "E_SCRIPT_OPTION")

    def test_trailing_text_after_script_rejected(self):
        with self.assertRaises(lx.LoonParseError) as caught:
            lx.parse_script_expression(
                'response if ${url} ~= /x/i then script("https://x/y.js") nonsense'
            )
        self.assertEqual(caught.exception.code, "E_SCRIPT_TRAILING")

    def test_duplicate_argument_keys_rejected(self):
        with self.assertRaises(lx.LoonParseError) as caught:
            lx.parse_script_expression(
                'response if ${url} ~= /x/i then script("https://x/y.js", '
                "{${a}, ${a}})"
            )
        self.assertEqual(caught.exception.code, "E_SCRIPT_ARGUMENT")

    def test_script_expression_returns_none_for_rewrite_lines(self):
        self.assertIsNone(
            lx.parse_script_expression(
                'response if ${url} ~= /x/i then response.json.delete("a")'
            )
        )
        self.assertIsNone(lx.parse_script_expression("http-response ^x script-path=a.js"))
        self.assertIsNone(lx.parse_script_expression("# comment"))
        self.assertIsNone(lx.parse_script_expression(""))

    def test_script_supports_capture_binding_syntax(self):
        expr = parse_script(
            'response if ${url} ~= /^https:\\/\\/x\\/(\\w+)/i as item then '
            'script("https://x/y.js") with tag="t", requires_body=true'
        )
        self.assertEqual(expr.capture_name, "item")
        self.assertEqual(expr.regex.group_count, 1)


# ---------------------------------------------------------------------------
# Legacy script lines
# ---------------------------------------------------------------------------


class LegacyScriptTest(unittest.TestCase):
    def test_http_request(self):
        line = (
            "http-request ^https?:\\/\\/example\\.com script-path=request.js,"
            "tag=请求脚本,requires-body=true,binary-body-mode=false,timeout=10,"
            'argument="name=loon",enable=true'
        )
        parsed = lx.parse_legacy_script_line(line)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.kind, "http-request")
        self.assertEqual(parsed.match, "^https?:\\/\\/example\\.com")
        self.assertEqual(parsed.script_path, "request.js")
        self.assertIs(parsed.option("requires_body"), True)
        self.assertIs(parsed.option("binary_body_mode"), False)
        self.assertEqual(parsed.option("timeout"), 10)
        self.assertEqual(parsed.option("argument"), "name=loon")
        self.assertIs(parsed.option("enable"), True)

    def test_http_response_without_tag(self):
        parsed = lx.parse_legacy_script_line(
            "http-response ^https?:\\/\\/example\\.com script-path=response.js,"
            "requires-body=true"
        )
        self.assertEqual(parsed.kind, "http-response")
        self.assertEqual(parsed.script_path, "response.js")

    def test_legacy_plugin_argument_array(self):
        parsed = lx.parse_legacy_script_line(
            "http-response ^https?:\\/\\/example\\.com\\/conf\\/server-mapping "
            "script-path=remove_ads.js,requires-body=true,tag=移除广告,"
            "argument=[{name},{region},{enabled}]"
        )
        self.assertEqual(parsed.option("argument"), "[{name},{region},{enabled}]")

    def test_cron_quoted(self):
        parsed = lx.parse_legacy_script_line(
            'cron "0 8 * * *" script-path=cron.js,tag=定时任务,timeout=300,'
            'argument="1234",enable=true'
        )
        self.assertEqual(parsed.kind, "cron")
        self.assertEqual(parsed.cron, "0 8 * * *")
        self.assertEqual(parsed.script_path, "cron.js")

    def test_cron_braced_expression(self):
        parsed = lx.parse_legacy_script_line(
            "cron {cronExpression} script-path=task.js,timeout=300,tag=自动运行"
        )
        self.assertEqual(parsed.kind, "cron")
        self.assertEqual(parsed.cron, "{cronExpression}")
        self.assertEqual(parsed.option("timeout"), 300)

    def test_cron_six_field_expression(self):
        parsed = lx.parse_legacy_script_line(
            'cron "* * * * * *" script-path=cron.js,timeout=300'
        )
        self.assertEqual(parsed.cron, "* * * * * *")

    def test_network_changed(self):
        parsed = lx.parse_legacy_script_line(
            "network-changed script-path=network-changed.js,tag=网络变化,timeout=300"
        )
        self.assertEqual(parsed.kind, "network-changed")
        self.assertEqual(parsed.script_path, "network-changed.js")

    def test_generic_with_img_url(self):
        parsed = lx.parse_legacy_script_line(
            "generic script-path=generic.js,tag=通用脚本,"
            "img-url=location.fill.viewfinder.system,timeout=300,"
            'argument="1234"'
        )
        self.assertEqual(parsed.kind, "generic")
        self.assertEqual(parsed.option("img_url"), "location.fill.viewfinder.system")
        self.assertEqual(parsed.option("argument"), "1234")

    def test_non_script_lines_return_none(self):
        for line in ("", "   ", "# comment", "not a script at all"):
            self.assertIsNone(lx.parse_legacy_script_line(line), line)

    def test_old_converter_alias_attributes(self):
        # scripts/kelee_semantics.py historically consumed a dict with
        # type/phase/pattern/params/argument keys; keep those aliases working.
        request = lx.parse_legacy_script_line(
            "http-request ^https?:\\/\\/x script-path=req.js,requires-body=true,"
            'argument="name=loon"'
        )
        self.assertEqual(request.type, "http-request")
        self.assertEqual(request.phase, "request")
        self.assertEqual(request.pattern, "^https?:\\/\\/x")
        self.assertIs(request.params["requires_body"], True)
        self.assertEqual(request.argument, "name=loon")
        response = lx.parse_legacy_script_line(
            "http-response ^https?:\\/\\/x script-path=res.js"
        )
        self.assertEqual(response.phase, "response")
        self.assertEqual(response.type, "http-response")
        cron = lx.parse_legacy_script_line(
            'cron "0 8 * * *" script-path=cron.js,timeout=300'
        )
        self.assertEqual(cron.phase, "")
        self.assertEqual(cron.type, "cron")

    def test_missing_script_path_raises(self):
        with self.assertRaises(lx.LoonParseError) as caught:
            lx.parse_legacy_script_line(
                "http-request ^https?:\\/\\/x requires-body=true"
            )
        self.assertEqual(caught.exception.code, "E_LEGACY_SCRIPT")

    def test_missing_options_raises(self):
        with self.assertRaises(lx.LoonParseError) as caught:
            lx.parse_legacy_script_line("http-request ^https?:\\/\\/x")
        self.assertEqual(caught.exception.code, "E_LEGACY_SCRIPT")


# ---------------------------------------------------------------------------
# [Argument] declarations
# ---------------------------------------------------------------------------


class ArgumentDeclTest(unittest.TestCase):
    def test_input_with_default(self):
        decl = lx.parse_argument_decl('name = input,"Loon",tag=名称,desc=输入一个名称')
        self.assertEqual(decl.name, "name")
        self.assertEqual(decl.kind, "input")
        self.assertEqual(decl.default_raw, '"Loon"')
        self.assertEqual(decl.default_typed(), "Loon")
        self.assertEqual(decl.tag, "名称")
        self.assertEqual(decl.desc, "输入一个名称")

    def test_input_empty_default(self):
        decl = lx.parse_argument_decl('API.Token = input, "", tag=[API]令牌')
        self.assertEqual(decl.name, "API.Token")
        self.assertEqual(decl.default_typed(), "")
        self.assertEqual(decl.tag, "[API]令牌")

    def test_input_without_default(self):
        decl = lx.parse_argument_decl("token = input,tag=令牌")
        self.assertEqual(decl.kind, "input")
        self.assertEqual(decl.default_raw, "")
        self.assertEqual(decl.default_typed(), "")

    def test_select_first_value_is_default(self):
        decl = lx.parse_argument_decl(
            'region = select,"CN","US","JP",tag=地区'
        )
        self.assertEqual(decl.kind, "select")
        self.assertEqual(decl.default_typed(), "CN")
        self.assertEqual(decl.options, ("CN", "US", "JP"))

    def test_switch_default_is_first_value(self):
        decl = lx.parse_argument_decl("sponsorBlock = switch, false, true, tag=空降助手")
        self.assertEqual(decl.kind, "switch")
        self.assertIs(decl.default_typed(), False)
        self.assertEqual(decl.options, (False, True))

    def test_switch_without_values_defaults_false(self):
        decl = lx.parse_argument_decl("enabled = switch,tag=启用")
        self.assertIs(decl.default_typed(), False)
        self.assertEqual(decl.options, ())

    def test_number_typed_input(self):
        decl = lx.parse_argument_decl("price = input,9.99,type=number,tag=价格")
        self.assertEqual(decl.type_name, "number")
        self.assertEqual(decl.default_typed(), 9.99)
        self.assertIsInstance(decl.default_typed(), float)

    def test_number_typed_select(self):
        decl = lx.parse_argument_decl("level = select,1,2,3,type=number,tag=等级")
        self.assertEqual(decl.default_typed(), 1)
        self.assertEqual(decl.options, (1, 2, 3))

    def test_untyped_numeric_input_stays_string(self):
        # Loon docs: input/select default to String unless type=number.
        decl = lx.parse_argument_decl("price = input,9.99,tag=价格")
        self.assertEqual(decl.default_typed(), "9.99")
        self.assertIsInstance(decl.default_typed(), str)

    def test_dotted_and_hyphenated_names(self):
        self.assertEqual(
            lx.parse_argument_decl("Weather.Provider = select,\"A\",\"B\"").name,
            "Weather.Provider",
        )
        self.assertEqual(
            lx.parse_argument_decl("age-secret-key = input,\"\"").name,
            "age-secret-key",
        )

    def test_attributes_are_preserved(self):
        decl = lx.parse_argument_decl(
            'AirQuality = select,"EU_EAQI","None",tag=[天气]算法,'
            "desc=使用内置算法，通过污染物数据本地计算空气指数。"
        )
        self.assertEqual(decl.attrs["tag"], "[天气]算法")
        self.assertEqual(
            decl.desc, "使用内置算法，通过污染物数据本地计算空气指数。"
        )

    def test_unknown_widget_is_unsupported(self):
        with self.assertRaises(lx.LoonUnsupportedError) as caught:
            lx.parse_argument_decl('x = slider,"0","100"')
        self.assertEqual(caught.exception.code, "E_ARGUMENT_KIND")

    def test_unknown_type_is_unsupported(self):
        with self.assertRaises(lx.LoonUnsupportedError) as caught:
            lx.parse_argument_decl("x = input,1,type=boolean")
        self.assertEqual(caught.exception.code, "E_ARGUMENT_TYPE")

    def test_malformed_lines_raise(self):
        for line in (
            "broken",
            ' = input,"x"',
            'name = "x"',
            'name = input,"a","b"',
            'name = select',
            'name = switch,yes,no',
            'name = switch,true,false,true',
        ):
            with self.assertRaises(lx.LoonParseError, msg=line):
                lx.parse_argument_decl(line)

    def test_blank_and_comment_return_none(self):
        self.assertIsNone(lx.parse_argument_decl(""))
        self.assertIsNone(lx.parse_argument_decl("# comment"))


# ---------------------------------------------------------------------------
# Adversarial / edge cases
# ---------------------------------------------------------------------------


class AdversarialTest(unittest.TestCase):
    def test_nested_object_and_array_arguments(self):
        call = lx.parse_call('f({"a": [1, {"b": "c,d"}]}, [{${x}}, 2])')
        self.assertEqual(call.name, "f")
        self.assertIsInstance(call.args[0], lx.ObjectLiteral)
        nested = call.args[0].get("a")
        self.assertEqual(len(nested.items), 2)
        self.assertEqual(nested.items[1].get("b").value, "c,d")
        self.assertEqual(call.args[1].items[0].keys(), ("x",))

    def test_json_body_survives_commas_braces_quotes(self):
        body = '{"a":1,"b":[{"c":"d,e"}]}'
        escaped = body.replace('"', '\\"')
        expr = parse(
            'response if ${url} ~= /x/ then response.body.mock("json", '
            f'"{escaped}")'
        )
        self.assertEqual(json.loads(expr.action("response.body.mock").string(1)), json.loads(body))

    def test_raw_string_body_with_backticks_inside(self):
        expr = parse(
            'response if ${url} ~= /x/ then response.body.mock("text", '
            "`line `` tick`)"
        )
        self.assertEqual(
            expr.action("response.body.mock").args[1].value, "line ` tick"
        )

    def test_jq_program_with_as_binding_del_and_arithmetic(self):
        jq = (
            '.data.items |= map(.price as $p | if $p > 10 then del(.ad) '
            'else . end) | .meta = {n: (1 + 2)}'
        )
        escaped = jq.replace('"', '\\"')
        expr = parse(
            'response if ${url} ~= /x/ then response.json.jq('
            f'"{escaped}")'
        )
        self.assertEqual(expr.action("response.json.jq").string(0), jq)

    def test_template_with_nested_braces(self):
        value, _ = lx.parse_value('"${request.header[\'X\']}-${a}"')
        self.assertEqual([tpl.name for tpl in value.templates], [
            "request.header['X']",
            "a",
        ])

    def test_escaped_dollar_brace_is_literal_not_template(self):
        expr = parse(
            'request if ${url} ~= /x/ then request.header.set("X", '
            '"cost=\\${price}")'
        )
        self.assertEqual(
            expr.action("request.header.set").string(1), "cost=${price}"
        )
        self.assertEqual(expr.action("request.header.set").args[1].templates, ())

    def test_script_tag_with_comma_in_quotes(self):
        expr = parse_script(
            'response if ${url} ~= /x/i then script("https://x/y.js") '
            'with tag="a, b", requires_body=true'
        )
        self.assertEqual(expr.options["tag"], "a, b")

    def test_tag_with_equals_sign_in_quotes(self):
        expr = parse_script(
            'response if ${url} ~= /x/i then script("https://x/y.js") '
            'with tag="a=b", requires_body=true'
        )
        self.assertEqual(expr.options["tag"], "a=b")

    def test_double_comma_tolerated_in_legacy_official_example(self):
        parsed = lx.parse_legacy_script_line(
            "generic script-path=node_parser_ex.js,tag=NodeParser,timeout=10,,"
            "argument=[{UA},{resourceOnly}]"
        )
        self.assertEqual(parsed.option("timeout"), 10)
        self.assertEqual(parsed.option("argument"), "[{UA},{resourceOnly}]")

    def test_capture_ref_in_array_batch_validated(self):
        expr = parse(
            'request if ${url} ~= /(a)(b)/i as m then request.header.set('
            '["X-A", "X-B"], ["${m.1}", "${m.2}"])'
        )
        self.assertEqual(expr.capture_name, "m")
        with self.assertRaises(lx.LoonParseError) as caught:
            parse(
                'request if ${url} ~= /(a)/i as m then request.header.set('
                '["X-A"], ["${m.2}"])'
            )
        self.assertEqual(caught.exception.code, "E_CAPTURE_OUT_OF_RANGE")

    def test_capture_ref_inside_mock_body_or_jq_string(self):
        with self.assertRaises(lx.LoonParseError) as caught:
            parse(
                'response if ${url} ~= /(a)/i as item then response.json.jq('
                '"${item.2}")'
            )
        self.assertEqual(caught.exception.code, "E_CAPTURE_OUT_OF_RANGE")

    def test_plain_plugin_parameter_templates_are_not_captures(self):
        expr = parse(
            'response if ${url} ~= /x/ then response.json.replace("a.b", ${price})'
        )
        self.assertIsInstance(
            expr.action("response.json.replace").args[1], lx.VariableRef
        )
        expr = parse(
            'request if ${url} ~= /x/ then request.header.set("X-Region", '
            '"${region}")'
        )
        self.assertEqual(
            expr.action("request.header.set").args[1].templates[0].name, "region"
        )

    def test_group_count_with_bracket_and_escape_tricks(self):
        pattern = r"(?:x)(a)[(](?<named>b)\\(c)"
        self.assertEqual(lx.count_capture_groups(pattern), 3)

    def test_long_jq_payload_is_not_truncated(self):
        jq = " | ".join(f'.k{i} |= map(select(.x != "v{i}"))' for i in range(40))
        escaped = jq.replace('"', '\\"')
        expr = parse(
            f'response if ${{url}} ~= /x/ then response.json.jq("{escaped}")'
        )
        self.assertEqual(expr.action("response.json.jq").string(0), jq)

    def test_action_helpers_on_values(self):
        expr = parse(BILIBILI_BASE64_MOCK)
        action = expr.action("response.body.mock")
        self.assertIsInstance(action.arg(1), lx.StringLiteral)
        self.assertIsNone(action.regex(0))
        self.assertIsNone(action.arg(99))
        self.assertEqual(action.arg(99, "fallback"), "fallback")

    def test_merged_multiline_jq_continuation_still_parses(self):
        # The converter merges over-long jq programs with embedded newlines
        # before calling the parser; the payload must survive byte-for-byte.
        line = (
            'response if ${url} ~= /x/ then response.json.jq(".items |= map(\n'
            "  select(.type == \\\"ad\\\")\n"
            '))")'
        )
        expr = parse(line)
        self.assertEqual(
            expr.action("response.json.jq").string(0),
            '.items |= map(\n  select(.type == "ad")\n))',
        )

    def test_double_escaped_backslashes(self):
        # Source `"a\\\\b"` is two escaped backslashes -> decoded `a\\b`.
        value, _ = lx.parse_value(r'"a\\\\b"')
        self.assertEqual(value.value, "a\\\\b")
        self.assertEqual(value.value, "a" + "\\" * 2 + "b")

    def test_string_ending_in_escaped_backslash_then_quote(self):
        # `"x\\"` decodes to x + one backslash and terminates at the final quote.
        value, pos = lx.parse_value(r'"x\\" rest')
        self.assertEqual(value.value, "x\\")
        self.assertEqual(pos, 5)

    def test_capture_index_zero_is_always_allowed(self):
        expr = parse(
            'request if ${url} ~= /^https:\\/\\/x/i as m then redirect(302, "${m.0}")'
        )
        self.assertEqual(expr.regex.group_count, 0)
        self.assertEqual(
            expr.action("redirect").args[1].templates[0].capture, ("m", 0)
        )

    def test_invalid_capture_name_raises(self):
        with self.assertRaises(lx.LoonParseError) as caught:
            lx.parse_rewrite_expression(
                'request if ${url} ~= /x/i as 1bad then reject(404)'
            )
        self.assertEqual(caught.exception.code, "E_CAPTURE_NAME")

    def test_capture_must_bind_a_regex_condition(self):
        with self.assertRaises(lx.LoonParseError) as caught:
            lx.parse_rewrite_expression(
                'request if ${request.method} == "POST" as m then reject(404)'
            )
        # The unsupported condition is diagnosed before the dangling `as`.
        self.assertEqual(caught.exception.code, "E_CONDITION_UNSUPPORTED")

    def test_response_mock_rejects_base64_without_status(self):
        # Loon: response mock's 4th arg is base64, 3rd must be the status code.
        with self.assertRaises(lx.LoonParseError) as caught:
            parse(
                'response if ${url} ~= /x/ then response.body.mock("png", "AAAA", true)'
            )
        self.assertEqual(caught.exception.code, "E_ACTION_ARG_TYPE")

    def test_script_option_variable_for_requires_body(self):
        expr = parse_script(
            'response if ${url} ~= /x/i then script("https://x/y.js") '
            "with requires_body=${readBody}"
        )
        self.assertIsInstance(expr.options["requires_body"], lx.VariableRef)

    def test_argument_quoted_comma_and_single_switch_value(self):
        decl = lx.parse_argument_decl('x = input,"a,b",tag=t')
        self.assertEqual(decl.default_typed(), "a,b")
        decl = lx.parse_argument_decl("x = switch,true,tag=t")
        self.assertIs(decl.default_typed(), True)
        self.assertEqual(decl.options, (True,))

    def test_argument_negative_number_default(self):
        decl = lx.parse_argument_decl("x = input,-1.5,type=number")
        self.assertEqual(decl.default_typed(), -1.5)

    def test_separator_inside_template_name_not_split(self):
        # `${request.header['a,b']}` is inside a quoted string here, but the
        # bare-variable value form must also scan the template braces atomically.
        call = lx.parse_call("f(${request.header['a,b']}, 1)")
        self.assertEqual(call.args[0].name, "request.header['a,b']")
        self.assertEqual(len(call.args), 2)

    def test_utf8_payloads_preserved(self):
        expr = parse(
            'response if ${url} ~= /x/ then response.json.jq("'
            'select(.name == \\"大赛\\" or .name == \\"直播\\")")'
        )
        self.assertIn("大赛", expr.action("response.json.jq").string(0))


# ---------------------------------------------------------------------------
# Audit snapshot integration (internal, read-only; skipped when absent)
# ---------------------------------------------------------------------------

AUDIT_FILES = sorted(AUDIT_DIR.glob("*.lpx")) if AUDIT_DIR.is_dir() else []


@unittest.skipUnless(AUDIT_FILES, "internal .local/upstream-audit snapshots absent")
class AuditSnapshotTest(unittest.TestCase):
    """Parse *every* line of the 21 real upstream plugin snapshots."""

    @classmethod
    def setUpClass(cls):
        cls.rewrites = []
        cls.scripts = []
        cls.arguments = []
        for path in AUDIT_FILES:
            section = None
            for number, raw_line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                line = raw_line.strip()
                header = None
                if line.startswith("[") and line.endswith("]"):
                    header = line[1:-1]
                if header is not None:
                    section = header
                    continue
                if not line or line.startswith("#"):
                    continue
                if section == "Rewrite":
                    cls.rewrites.append((path.name, number, line))
                elif section == "Script":
                    cls.scripts.append((path.name, number, line))
                elif section == "Argument":
                    cls.arguments.append((path.name, number, line))

    def test_snapshot_inventory(self):
        self.assertEqual(len(AUDIT_FILES), 21)
        self.assertEqual(len(self.rewrites), 394)
        self.assertEqual(len(self.scripts), 31)
        self.assertEqual(len(self.arguments), 16)

    def test_every_rewrite_line_parses_without_guessing(self):
        for filename, number, line in self.rewrites:
            with self.subTest(file=filename, line=number):
                expr = lx.parse_rewrite_expression(line)
                self.assertIsNotNone(expr)
                self.assertEqual(expr.phase, line.split()[0].lower())
                # Every upstream condition regex is case-insensitive; the
                # converter must keep all 31+394 /i conversions observable.
                self.assertEqual(expr.regex.flags, "i")
                self.assertTrue(lx.stash_regex(expr.regex).startswith("(?i)"))

    def test_every_script_line_parses_without_guessing(self):
        for filename, number, line in self.scripts:
            with self.subTest(file=filename, line=number):
                expr = lx.parse_script_expression(line)
                self.assertIsNotNone(expr)
                self.assertTrue(expr.script_path.startswith("https://"))
                self.assertEqual(expr.regex.flags, "i")
                self.assertTrue(lx.stash_regex(expr.regex).startswith("(?i)"))
                self.assertIs(expr.option("requires_body"), True)

    def test_every_argument_declaration_parses_with_typed_default(self):
        for filename, number, line in self.arguments:
            with self.subTest(file=filename, line=number):
                decl = lx.parse_argument_decl(line)
                self.assertIsNotNone(decl)
                self.assertIn(decl.kind, ("input", "select", "switch"))
                # Must not raise and must be a stable concrete type.
                self.assertIn(
                    type(decl.default_typed()), (str, int, float, bool)
                )

    def test_action_inventory_matches_audit_summary(self):
        counts = {}
        for _, _, line in self.rewrites:
            expr = lx.parse_rewrite_expression(line)
            for action in expr.actions:
                counts[action.name] = counts.get(action.name, 0) + 1
        self.assertEqual(
            counts,
            {
                "reject_dict": 212,
                "response.json.delete": 32,
                "response.json.jq_file": 4,
                "response.json.jq": 51,
                "response.body.mock": 8,
                "response.header.add": 1,
                "reject": 69,
                "response.json.replace": 1,
                "redirect": 8,
                "reject_img": 6,
                "response.body.replace": 1,
                "url.replace": 1,
            },
        )

    def test_all_capture_bindings_are_the_qq_and_spotify_ones(self):
        captured = [
            (name, number, expr)
            for name, number, line in self.rewrites
            if (expr := lx.parse_rewrite_expression(line)).capture_name
        ]
        self.assertEqual(len(captured), 9)
        qq = [entry for entry in captured if entry[0] == "QQ_Redirect.lpx"]
        self.assertEqual(len(qq), 8)
        for _, _, expr in qq:
            self.assertEqual(expr.capture_name, "urlMatch")
            self.assertEqual(expr.action_names, ("redirect",))
            redirect = expr.action("redirect")
            self.assertEqual(redirect.number(0), 307)
            self.assertEqual(redirect.string(1), "${urlMatch.2}")
            self.assertEqual(
                redirect.args[1].templates[0].capture, ("urlMatch", 2)
            )
            # 7 patterns capture the payload URL in group 2 and one
            # (cgi.connect.qq.com) has only two groups; both are valid because
            # the capture index validator allows index <= group count.
            self.assertIn(expr.regex.group_count, (2, 3))
            self.assertLessEqual(2, expr.regex.group_count)
        spotify = [
            entry for entry in captured if entry[0] == "Spotify_remove_ads.lpx"
        ]
        self.assertEqual(len(spotify), 1)
        expr = spotify[0][2]
        self.assertEqual(expr.action_names, ("url.replace",))
        self.assertIn("${urlMatch.1}", expr.action("url.replace").string(0))

    def test_twenty_three_mock_and_body_details(self):
        mocks = []
        for name, number, line in self.rewrites:
            expr = lx.parse_rewrite_expression(line)
            if expr.has_mock:
                mocks.append((name, number, expr))
        self.assertEqual(len(mocks), 8)
        base64_mocks = 0
        for name, number, expr in mocks:
            action = expr.mock
            content_type = action.string(0)
            self.assertIn(content_type, lx.MOCK_CONTENT_TYPES)
            if action.boolean(3):
                base64_mocks += 1
                base64.b64decode(action.string(1), validate=True)
            elif content_type == "json" or (
                action.string(1) or ""
            ).lstrip().startswith(("{", "[")):
                json.loads(action.string(1))
        self.assertEqual(base64_mocks, 3)
        files = {name for name, _, _ in mocks}
        self.assertEqual(
            files,
            {
                "Bilibili_remove_ads.lpx",
                "Himalaya_remove_ads.lpx",
                "Remove_ads_by_keli.lpx",
            },
        )

    def test_keli_body_replace_preserves_regex_escapes_and_replacement(self):
        found = None
        for name, number, line in self.rewrites:
            expr = lx.parse_rewrite_expression(line)
            if expr.action("response.body.replace"):
                found = (name, expr)
        self.assertIsNotNone(found)
        name, expr = found
        self.assertEqual(name, "Remove_ads_by_keli.lpx")
        action = expr.action("response.body.replace")
        self.assertEqual(action.regex(0).pattern, r"<GetADResult>\.\*\?<\/GetADResult>")
        self.assertIn('"ret":0', action.string(1))
        self.assertNotIn('\\"', action.string(1))

    def test_script_argument_inventory_matches_six_object_arguments(self):
        with_argument = []
        for name, number, line in self.scripts:
            expr = lx.parse_script_expression(line)
            if expr.has_argument:
                with_argument.append((name, expr))
        self.assertEqual(len(with_argument), 6)
        keys = [expr.argument_keys for _, expr in with_argument]
        self.assertIn(
            (
                "Weather.Provider",
                "NextHour.Provider",
                "AirQuality.Calculate.Algorithm",
                "API.ColorfulClouds.Token",
                "API.QWeather.Host",
                "API.QWeather.Token",
                "API.WAQI.Token",
                "Storage",
                "LogLevel",
            ),
            keys,
        )
        self.assertIn(("tab", "useractivity"), keys)
        self.assertIn(("displayUpList", "purifyComment", "sponsorBlock", "logLevel"), keys)

    def test_script_enable_options_resolve_to_switch_parameters(self):
        enable_refs = []
        for name, number, line in self.scripts:
            expr = lx.parse_script_expression(line)
            if "enable" in dict(expr.options):
                value = expr.options["enable"]
                self.assertIsInstance(value, lx.VariableRef)
                enable_refs.append((name, value.name))
        self.assertEqual(
            enable_refs,
            [
                ("Bilibili_remove_ads.lpx", "sponsorBlock"),
                ("Bilibili_remove_ads.lpx", "optimizeRequest"),
            ],
        )

    def test_argument_default_inventory(self):
        expected = {
            ("AppleWeatherEnhancer.lpx", "Weather.Provider"): "ColorfulClouds",
            ("AppleWeatherEnhancer.lpx", "LogLevel"): "WARN",
            ("Bilibili_remove_ads.lpx", "sponsorBlock"): False,
            ("Bilibili_remove_ads.lpx", "purifyComment"): True,
            ("Bilibili_remove_ads.lpx", "logLevel"): "off",
            ("Spotify_remove_ads.lpx", "tab"): False,
            ("Spotify_remove_ads.lpx", "useractivity"): True,
        }
        actual = {}
        for name, number, line in self.arguments:
            decl = lx.parse_argument_decl(line)
            actual[(name, decl.name)] = decl.default_typed()
        for key, value in expected.items():
            self.assertEqual(actual.get(key), value, key)
        kinds = [lx.parse_argument_decl(line).kind for _, _, line in self.arguments]
        self.assertEqual(kinds.count("select"), 7)
        self.assertEqual(kinds.count("input"), 4)
        self.assertEqual(kinds.count("switch"), 5)

    def test_capture_group_count_matches_python_re_on_audit_patterns(self):
        # Cross-validate the allocation-free RE2-style counter against CPython's
        # regex compiler for every real upstream condition pattern it accepts.
        checked = 0
        for _, _, line in self.rewrites:
            expr = lx.parse_rewrite_expression(line)
            try:
                compiled = re.compile(expr.regex.pattern)
            except re.error:
                continue
            checked += 1
            self.assertEqual(
                lx.count_capture_groups(expr.regex.pattern),
                compiled.groups,
                expr.regex.pattern,
            )
        self.assertGreater(checked, 380)

    def test_stash_regex_unescapes_only_slashes_for_all_audit_patterns(self):
        for filename, number, line in self.rewrites:
            with self.subTest(file=filename, line=number):
                expr = lx.parse_rewrite_expression(line)
                converted = lx.stash_regex(expr.regex)
                self.assertNotIn("\\/", converted)
                self.assertEqual(converted, "(?i)" + expr.regex.pattern.replace("\\/", "/"))
                # Regex escapes other than the delimiter must be untouched.
                for escape in ("\\.", "\\d", "\\w", "\\?", "\\*"):
                    if escape in expr.regex.pattern:
                        self.assertIn(escape, converted)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
