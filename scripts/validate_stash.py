#!/usr/bin/env python3
"""Offline validator for generated KeLee Stash overrides.

The previous validator encoded the converter's old invented schema (``- mock``
url-rewrite lines, ``data-type``/``statusCode`` mock fields, ``enable``/``tag``
script keys, ``PATTERN - reject`` being the only reject form, ...). This version
validates against the *current* Stash documentation (stash.wiki fetched
2026-10-25) and focuses on semantic completeness instead of "YAML parses" or
"rule count is similar":

* schema/field allow-lists for every ``http.*`` section and ``script-providers``;
* mock text/base64/status/headers correctness (base64 decodes, content type set);
* URL/header/body rewrite grammar, RE2-compatible patterns, quoted body scalars;
* script definitions reference existing providers, correct field names/types;
* every rule policy/provider referenced by the bundle actually exists;
* no remaining Loon artefacts (``kelee.one`` provider URLs, ``${...}``/``[Argument]``
  placeholders, ``data-type``, ``requires-body``, ``binary-body-mode``, ...);
* dangling assets: every ``/scripts/<file>`` referenced by a provider exists in
  the bundle, every content-addressed URL is actually content-addressed;
* optional cross-check against the conversion diagnostics: a target with a
  blocking diagnostic must never be published.

Usage::

    python scripts/validate_stash.py [--dir DIR] [--public-base URL] [--json]
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import kelee_config as cfg  # noqa: E402
from kelee_diagnostics import (  # noqa: E402,F401  (Diagnostic re-exported)
    Diagnostic,
    DiagnosticCollection,
    blocking_errors,
)

DEFAULT_DIR = REPO_ROOT / "stash/overrides/kelee"
PROVIDER_BUNDLE = "kelee-scripts.stoverride"

try:  # optional real RE2 binding (pip install re2); never required
    import re2 as re2lib  # type: ignore

    HAS_RE2 = True
except Exception:  # noqa: BLE001
    re2lib = None
    HAS_RE2 = False


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


class ValidationError(RuntimeError):
    def __init__(self, diagnostics: Iterable[Diagnostic]):
        self.diagnostics = list(diagnostics)
        super().__init__("; ".join(str(d) for d in self.diagnostics[:5]))


# ---------------------------------------------------------------------------
# Regex compatibility
# ---------------------------------------------------------------------------

# RE2 does not implement look-around, backreferences or named backrefs. Python
# `re` is used as an approximation when the real `re2` module is unavailable;
# unsupported constructs are rejected explicitly instead of being accepted by a
# permissive engine.
RE2_UNSUPPORTED = [
    (re.compile(r"\(\?[=!]"), "look-ahead"),
    (re.compile(r"\(\?<[=!]"), "look-behind"),
    (re.compile(r"\(\?P="), "named backreference"),
    (re.compile(r"\\(?:[1-9])"), "backreference"),
    (re.compile(r"\\(?:k|K)"), r"\k backreference"),
    (re.compile(r"\[[^\]]*&&[^\]]*\]"), "character-class intersection"),
]


def regex_problem(pattern: str) -> Optional[str]:
    for regex, label in RE2_UNSUPPORTED:
        if regex.search(pattern):
            return f"RE2 unsupported feature: {label}"
    try:
        if HAS_RE2:
            re2lib.compile(pattern)
        else:
            re.compile(pattern)
    except Exception as exc:  # noqa: BLE001
        return f"regex does not compile: {exc}"
    return None


def strip_stash_regex_escapes(pattern: str) -> str:
    """Stash accepts escaped slashes in regex values (documented examples)."""

    return pattern.replace("\\/", "/")


def stash_regex_problem(pattern: str) -> Optional[str]:
    # Validate the expression as written; Stash examples keep \/ escapes.
    return regex_problem(pattern)


# ---------------------------------------------------------------------------
# Small parsing helpers
# ---------------------------------------------------------------------------


def split_top_level(value: str, sep: str = ",") -> list[str]:
    """Split on a separator outside quotes, parentheses and brackets."""

    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote: Optional[str] = None
    i = 0
    while i < len(value):
        ch = value[i]
        if quote:
            current.append(ch)
            if ch == "\\" and i + 1 < len(value):
                current.append(value[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            current.append(ch)
        elif ch in "([{":
            depth += 1
            current.append(ch)
        elif ch in ")]}":
            depth = max(0, depth - 1)
            current.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
        i += 1
    tail = "".join(current).strip()
    if tail or parts:
        parts.append(tail)
    return [part for part in parts if part != ""]


def iter_strings(node) -> Iterable[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield str(key)
            yield from iter_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from iter_strings(value)


PLACEHOLDER_MARKERS = (
    "${",
    "script-path=",
    "requires_body",
    "binary_body_mode",
    "data-type",
    "statusCode",
    "mock-data-is-base64",
    "[Argument]",
    "未转换",
    "E_UNSUPPORTED",
)


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


@dataclass
class BundleContext:
    root: Path
    public_base: str = ""
    known_policies: set[str] = field(
        default_factory=lambda: set(cfg.STASH_BUILTIN_POLICIES) | {"Proxy"}
    )
    known_rule_types: set[str] = field(
        default_factory=lambda: {
            "DOMAIN",
            "DOMAIN-SUFFIX",
            "DOMAIN-KEYWORD",
            "DOMAIN-REGEX",
            "DOMAIN-WILDCARD",
            "USER-AGENT",
            "URL-REGEX",
            "IP-CIDR",
            "IP-CIDR6",
            "IP-ASN",
            "GEOIP",
            "DST-PORT",
            "SRC-PORT",
            "SRC-IP",
            "NETWORK",
            "PROTOCOL",
            "PROCESS-NAME",
            "PROCESS-PATH",
            "SCRIPT",
            "AND",
            "OR",
            "NOT",
            "MATCH",
        }
    )
    providers: dict[str, dict] = field(default_factory=dict)
    provider_file: dict[str, str] = field(default_factory=dict)
    assets: dict[str, str] = field(default_factory=dict)  # relpath -> sha256
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def error(self, code: str, message: str, path: str = "") -> None:
        self.diagnostics.append(Diagnostic(code, message, path))

    def warn(self, code: str, message: str, path: str = "") -> None:
        self.diagnostics.append(
            Diagnostic(code, message, path, blocking=False, severity="warning")
        )


def _expected_mock_headers(content_type: str) -> dict[str, str]:
    return {"Content-Type": content_type}


def validate_mock(entry, ctx: BundleContext, path: str) -> None:
    if not isinstance(entry, dict):
        ctx.error("E_MOCK_NOT_MAPPING", f"mock item is {type(entry).__name__}", path)
        return
    extra = set(entry) - cfg.MOCK_FIELDS
    if extra:
        ctx.error("E_MOCK_UNKNOWN_FIELD", f"mock has unsupported fields: {sorted(extra)}", path)
    missing = cfg.MOCK_FIELDS - set(entry)
    for required in ("match", "status-code"):
        if required in missing:
            ctx.error("E_MOCK_MISSING_FIELD", f"mock is missing {required!r}", path)
    if "text" in entry and "base64" in entry:
        ctx.error("E_MOCK_TEXT_AND_BASE64", "mock must use exactly one of text/base64", path)
    if "text" not in entry and "base64" not in entry:
        ctx.error("E_MOCK_BODY_MISSING", "mock must define text or base64", path)
    if isinstance(entry.get("match"), str):
        problem = stash_regex_problem(strip_stash_regex_escapes(entry["match"]))
        if problem:
            ctx.error("E_MOCK_REGEX", f"mock match {problem}: {entry['match'][:120]}", path)
    status = entry.get("status-code")
    if not isinstance(status, int) or isinstance(status, bool):
        ctx.error("E_MOCK_STATUS_TYPE", f"mock status-code must be an integer, got {status!r}", path)
    elif not 100 <= status <= 599:
        ctx.error("E_MOCK_STATUS_RANGE", f"mock status-code out of range: {status}", path)
    headers = entry.get("headers")
    if headers is not None:
        if not isinstance(headers, dict):
            ctx.error("E_MOCK_HEADERS_TYPE", "mock headers must be a mapping", path)
        else:
            for name, value in headers.items():
                if not isinstance(name, str) or not name:
                    ctx.error("E_MOCK_HEADER_NAME", f"invalid mock header name {name!r}", path)
                if not isinstance(value, (str, int, float)):
                    ctx.error(
                        "E_MOCK_HEADER_VALUE_TYPE",
                        f"mock header {name!r} value must be scalar, got {type(value).__name__}",
                        path,
                    )
    content_type = ""
    if isinstance(headers, dict):
        for key, value in headers.items():
            if str(key).lower() == "content-type":
                content_type = str(value)
    if "base64" in entry:
        body = entry["base64"]
        if not isinstance(body, str):
            ctx.error("E_MOCK_BASE64_TYPE", "mock base64 body must be a string", path)
        else:
            try:
                base64.b64decode(body, validate=True)
            except (binascii.Error, ValueError) as exc:
                ctx.error("E_MOCK_BASE64_INVALID", f"mock base64 does not decode: {exc}", path)
    if "text" in entry and not isinstance(entry["text"], str):
        ctx.error("E_MOCK_TEXT_TYPE", "mock text body must be a string", path)
    if not content_type:
        ctx.warn("E_MOCK_NO_CONTENT_TYPE", "mock does not set Content-Type", path)


def validate_url_rewrite(rule, ctx: BundleContext, path: str) -> None:
    if not isinstance(rule, str):
        ctx.error("E_URL_NOT_STRING", f"url-rewrite item is {type(rule).__name__}", path)
        return
    text = rule.strip()
    if "mock" in text.lower():
        ctx.error("E_URL_MOCK", f"mock must live in http.mock, not url-rewrite: {text[:120]}", path)
        return
    parts = text.split()
    if len(parts) < 2:
        ctx.error("E_URL_TOKENS", f"url-rewrite needs at least pattern+action: {text[:120]}", path)
        return
    pattern = parts[0]
    problem = stash_regex_problem(strip_stash_regex_escapes(pattern))
    if problem:
        ctx.error("E_URL_REGEX", f"url pattern {problem}: {pattern[:120]}", path)
    action = parts[-1].lower()
    if action in cfg.URL_REJECT_ACTIONS:
        if len(parts) != 3 or parts[1] != "-":
            ctx.error(
                "E_URL_REJECT_SHAPE",
                f'reject rule must be "pattern - reject": {text[:140]}',
                path,
            )
        return
    if action in cfg.URL_REDIRECT_ACTIONS:
        if len(parts) < 3:
            ctx.error("E_URL_REDIRECT_TARGET", f"redirect rule has no target: {text[:140]}", path)
            return
        target = " ".join(parts[1:-1])
        if target == "-":
            ctx.error("E_URL_REDIRECT_PLACEHOLDER", f"redirect target is '-': {text[:140]}", path)
        if action in {"302", "307"} and not target.lower().startswith(("http://", "https://")):
            ctx.error(
                "E_URL_REDIRECT_TARGET",
                f"{action} target must be an absolute URL: {target[:120]}",
                path,
            )
        if "${" in target or "$1" in target or "$2" in target:
            ctx.error(
                "E_URL_UNDOCUMENTED_CAPTURE",
                "capture substitution in url-rewrite targets is not documented by Stash; "
                f"generate a request script instead: {text[:140]}",
                path,
            )
        return
    ctx.error("E_URL_ACTION_UNKNOWN", f"unsupported url-rewrite action {action!r}", path)


def validate_header_rewrite(rule, ctx: BundleContext, path: str) -> None:
    if not isinstance(rule, str):
        ctx.error("E_HEADER_NOT_STRING", f"header-rewrite item is {type(rule).__name__}", path)
        return
    parts = rule.split()
    if len(parts) < 2:
        ctx.error("E_HEADER_TOKENS", f"header-rewrite needs pattern+action: {rule[:120]}", path)
        return
    pattern = parts[0]
    problem = stash_regex_problem(strip_stash_regex_escapes(pattern))
    if problem:
        ctx.error("E_HEADER_REGEX", f"header pattern {problem}: {pattern[:120]}", path)
    action = parts[1].lower()
    if action not in cfg.HEADER_ACTIONS:
        ctx.error("E_HEADER_ACTION", f"unsupported header action {action!r}", path)
        return
    for legacy in ("- header", "header-add", "header-del", "response-header"):
        if legacy in rule:
            ctx.error("E_HEADER_LEGACY", f"legacy header syntax detected: {rule[:120]}", path)
            break
    name = parts[2] if len(parts) > 2 else ""
    if not name:
        ctx.error("E_HEADER_NAME", f"header action {action} needs a header name", path)
        return
    value = " ".join(parts[3:])
    if action.endswith("-del"):
        if value:
            ctx.error("E_HEADER_DEL_VALUE", f"header del must not carry a value: {rule[:120]}", path)
    elif not value:
        ctx.error("E_HEADER_VALUE_MISSING", f"header action {action} needs a value", path)
    elif "${" in value:
        ctx.error("E_HEADER_PLACEHOLDER", f"unexpanded placeholder in header value: {value[:80]}", path)


def validate_body_rewrite(rule, ctx: BundleContext, path: str) -> None:
    if not isinstance(rule, str):
        ctx.error("E_BODY_NOT_STRING", f"body-rewrite item is {type(rule).__name__}", path)
        return
    text = rule.strip()
    if not text:
        ctx.error("E_BODY_EMPTY", "empty body-rewrite rule", path)
        return
    parts = text.split(None, 2)
    if len(parts) < 3:
        ctx.error("E_BODY_TOKENS", f"body-rewrite needs match+action+expression: {text[:120]}", path)
        return
    pattern, action, expression = parts
    problem = stash_regex_problem(strip_stash_regex_escapes(pattern))
    if problem:
        ctx.error("E_BODY_REGEX", f"body pattern {problem}: {pattern[:120]}", path)
    action = action.lower()
    if action not in cfg.BODY_ACTIONS:
        ctx.error("E_BODY_ACTION", f"unsupported body action {action!r}", path)
        return
    if "jq-path" in expression or "jq_file" in expression:
        ctx.error("E_BODY_JQ_NOT_INLINED", "remote jq must be inlined before publishing", path)
        return
    if action in cfg.BODY_JQ_ACTIONS:
        if not expression:
            ctx.error("E_BODY_JQ_EMPTY", f"{action} needs a jq program", path)
            return
        if len(expression) >= 2 and expression[0] == expression[-1] and expression[0] in "\"'":
            ctx.error(
                "E_BODY_JQ_QUOTED",
                f"jq program must not be a quoted string literal: {expression[:120]}",
                path,
            )
        if not jq_structurally_balanced(expression):
            ctx.error("E_BODY_JQ_UNBALANCED", f"unbalanced jq program: {expression[:160]}", path)
        if expression.lstrip().startswith("#"):
            ctx.error("E_BODY_JQ_COMMENT_ONLY", "jq program starts with a comment (no-op)", path)
        if "${" in expression:
            ctx.error("E_BODY_PLACEHOLDER", "unexpanded placeholder in jq program", path)
        return
    if action in cfg.BODY_JSON_ACTIONS:
        tokens = expression.split()
        if not tokens:
            ctx.error("E_BODY_JSON_EMPTY", f"{action} needs a path", path)
            return
        if action.endswith("-del"):
            bad = [token for token in tokens if not is_valid_json_path(token)]
            if bad:
                ctx.error("E_BODY_JSON_PATH", f"invalid json-del path(s): {bad[:3]}", path)
        else:
            if len(tokens) % 2:
                ctx.error(
                    "E_BODY_JSON_PAIRS",
                    f"{action} needs path/value pairs, got {tokens[:6]}",
                    path,
                )
            else:
                bad = [tokens[i] for i in range(0, len(tokens), 2) if not is_valid_json_path(tokens[i])]
                if bad:
                    ctx.error("E_BODY_JSON_PATH", f"invalid json path(s): {bad[:3]}", path)
        return
    if action in cfg.BODY_REGEX_ACTIONS:
        match = re.match(r"^(\S+)\s+(\S+)$", expression, re.S)
        if not match and expression.count(" ") == 0:
            ctx.error("E_BODY_REPLACE_SHAPE", "replace-regex needs regex and replacement", path)
            return
        # Stash documents space-separated <regex> <replacement> pairs; an odd
        # token count or a raw space inside either field would be ambiguous and
        # is rejected instead of being silently truncated.
        bits = expression.split()
        if len(bits) < 2 or len(bits) % 2:
            ctx.error(
                "E_BODY_REPLACE_TOKENS",
                "replace-regex expects <regex> <replacement> pairs without spaces: "
                f"{expression[:160]}",
                path,
            )
            return
        for index in range(0, len(bits), 2):
            regex, replacement = bits[index], bits[index + 1]
            problem = stash_regex_problem(regex)
            if problem:
                ctx.error("E_BODY_REPLACE_REGEX", f"replacement regex {problem}: {regex[:120]}", path)
            if "${" in replacement:
                ctx.error("E_BODY_PLACEHOLDER", "unexpanded placeholder in replacement", path)
        return
    ctx.error("E_BODY_UNHANDLED", f"no validation for body action {action!r}", path)


def jq_structurally_balanced(expression: str) -> bool:
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    in_string = False
    i = 0
    while i < len(expression):
        ch = expression[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if not stack or stack.pop() != pairs[ch]:
                return False
        i += 1
    return not stack and not in_string


JSON_PATH_RE = re.compile(r"^[A-Za-z0-9_@-]+(?:\.[A-Za-z0-9_@-]+|\[\d+\])*$")
JQ_SEGMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_valid_json_path(path: str) -> bool:
    if not JSON_PATH_RE.match(path):
        return False
    return all(JQ_SEGMENT_RE.match(seg.split("[", 1)[0]) for seg in path.split("."))


def validate_script(entry, ctx: BundleContext, path: str) -> None:
    if not isinstance(entry, dict):
        ctx.error("E_SCRIPT_NOT_MAPPING", f"script item is {type(entry).__name__}", path)
        return
    extra = set(entry) - cfg.SCRIPT_FIELDS
    if extra:
        ctx.error("E_SCRIPT_UNKNOWN_FIELD", f"script has unsupported fields: {sorted(extra)}", path)
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        ctx.error("E_SCRIPT_NAME", "script is missing a provider name", path)
    elif name not in ctx.providers:
        ctx.error("E_SCRIPT_PROVIDER_MISSING", f"script references unknown provider {name!r}", path)
    match = entry.get("match")
    if not isinstance(match, str) or not match:
        ctx.error("E_SCRIPT_MATCH", "script is missing a match regex", path)
    else:
        problem = stash_regex_problem(strip_stash_regex_escapes(match))
        if problem:
            ctx.error("E_SCRIPT_REGEX", f"script match {problem}: {match[:120]}", path)
    script_type = entry.get("type")
    if script_type not in ("request", "response"):
        ctx.error("E_SCRIPT_TYPE", f"script type must be request/response, got {script_type!r}", path)
    for key in ("require-body", "binary-mode"):
        if key in entry and not isinstance(entry[key], bool):
            ctx.error("E_SCRIPT_BOOL", f"script {key} must be a boolean", path)
    for key in ("timeout", "max-size"):
        if key in entry and (
            not isinstance(entry[key], (int, float)) or isinstance(entry[key], bool) or entry[key] <= 0
        ):
            ctx.error("E_SCRIPT_NUMBER", f"script {key} must be a positive number", path)
    if "argument" in entry and not isinstance(entry["argument"], str):
        ctx.error("E_SCRIPT_ARGUMENT_TYPE", "script argument must be a string", path)


def validate_script_provider(name: str, info, ctx: BundleContext, path: str) -> None:
    if not isinstance(info, dict):
        ctx.error("E_PROVIDER_NOT_MAPPING", f"provider {name!r} is not a mapping", path)
        return
    extra = set(info) - cfg.SCRIPT_PROVIDER_FIELDS
    if extra:
        ctx.error("E_PROVIDER_UNKNOWN_FIELD", f"provider {name!r} has fields {sorted(extra)}", path)
    url = info.get("url")
    payload = info.get("payload")
    provider_path = info.get("path")
    if url is None and payload is None and provider_path is None:
        ctx.error("E_PROVIDER_EMPTY", f"provider {name!r} has no url/payload/path", path)
    if url is not None:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            ctx.error("E_PROVIDER_URL", f"provider {name!r} has invalid url {url!r}", path)
        else:
            if "kelee.one" in url:
                ctx.error(
                    "E_PROVIDER_KELEE_URL",
                    f"provider {name!r} still points at the Loon-UA-only kelee.one host: {url}",
                    path,
                )
            if ctx.public_base:
                validate_asset_reference(url, ctx, path, name=name)
    if "interval" in info and (
        not isinstance(info["interval"], (int, float))
        or isinstance(info["interval"], bool)
        or info["interval"] < 0
    ):
        ctx.error("E_PROVIDER_INTERVAL", f"provider {name!r} interval must be a non-negative number", path)
    headers = info.get("headers")
    if headers is not None and not isinstance(headers, dict):
        ctx.error("E_PROVIDER_HEADERS", f"provider {name!r} headers must be a mapping", path)


def validate_asset_reference(url: str, ctx: BundleContext, path: str, *, name: str = "") -> None:
    base = ctx.public_base.rstrip("/")
    if not url.startswith(base + "/"):
        return  # external provider (e.g. GitHub release asset) - checked by build
    rel = url[len(base) + 1 :]
    if not rel.startswith("scripts/"):
        ctx.error("E_ASSET_OUTSIDE_SCRIPTS", f"provider {name!r} URL is not under /scripts/: {url}", path)
        return
    filename = rel[len("scripts/") :]
    if filename not in ctx.assets:
        candidate = ctx.root / rel
        if not candidate.is_file():
            ctx.error(
                "E_ASSET_DANGLING",
                f"provider {name!r} references missing asset {rel}",
                path,
            )
            return
    if not re.match(r"^[0-9a-f]{12}-[^/]+$", filename):
        ctx.warn(
            "E_ASSET_LEGACY_URL",
            f"provider {name!r} uses a non content-addressed script URL: {url}",
            path,
        )


def validate_rules(rules, ctx: BundleContext, path: str) -> None:
    if not isinstance(rules, list):
        ctx.error("E_RULES_TYPE", "rules must be a list", path)
        return
    for rule in rules:
        if not isinstance(rule, str):
            ctx.error("E_RULE_NOT_STRING", f"rule is {type(rule).__name__}", path)
            continue
        validate_rule_line(rule.strip(), ctx, path)


def _rule_policy_index(parts: list[str], known_policies: set[str]) -> int:
    for index in range(len(parts) - 1, -1, -1):
        if parts[index].strip() in known_policies:
            return index
    return -1


def validate_rule_line(rule: str, ctx: BundleContext, path: str) -> None:
    if not rule:
        ctx.error("E_RULE_EMPTY", "empty rule", path)
        return
    if "${" in rule or "{cron}" in rule:
        ctx.error("E_RULE_PLACEHOLDER", f"unexpanded placeholder in rule: {rule[:120]}", path)
    match = re.match(r"^([A-Z][A-Z0-9-]*),(.*)$", rule, re.S)
    if not match:
        ctx.error("E_RULE_SHAPE", f"rule has no TYPE, payload, POLICY shape: {rule[:140]}", path)
        return
    rule_type, rest = match.group(1), match.group(2)
    if rule_type not in ctx.known_rule_types:
        ctx.error("E_RULE_TYPE", f"unsupported rule type {rule_type!r}: {rule[:120]}", path)
        return
    if rule_type in ("AND", "OR", "NOT"):
        validate_logical_rule(rule_type, rest, ctx, path, expect_policy=True)
        return
    parts = split_top_level(rest)
    policy_index = _rule_policy_index(parts, ctx.known_policies)
    if policy_index < 0:
        ctx.error("E_RULE_POLICY_MISSING", f"rule references no known policy: {rule[:140]}", path)
        return
    if policy_index == 0:
        ctx.error("E_RULE_TOKENS", f"rule has no payload: {rule[:140]}", path)
        return
    if rule_type in ("DOMAIN", "DOMAIN-SUFFIX", "DOMAIN-KEYWORD", "USER-AGENT"):
        if not parts[0]:
            ctx.error("E_RULE_PAYLOAD", f"{rule_type} rule has empty payload", path)
    for payload_token in parts[:policy_index]:
        stripped = payload_token.strip()
        if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'":
            ctx.error(
                "E_RULE_QUOTED_VALUE",
                f"rule payload must not keep Loon quoting ({stripped[:80]!r}); "
                f"Stash treats the quotes literally: {rule[:120]}",
                path,
            )
            break
    # Anything after the policy is a rule option (no-resolve, no-track, ...).
    for option in parts[policy_index + 1 :]:
        if option and option not in {
            "no-resolve",
            "no-track",
            "pre-matching",
            "src",
        }:
            ctx.warn("E_RULE_OPTION", f"unrecognised rule option {option!r}: {rule[:120]}", path)


def _logical_body_parts(rest: str, ctx: BundleContext, path: str, *, expect_policy: bool):
    parts = split_top_level(rest)
    policy_index = _rule_policy_index(parts, ctx.known_policies)
    if policy_index >= 0:
        body = ",".join(parts[:policy_index]).strip()
        options = parts[policy_index + 1 :]
        if options:
            ctx.error("E_RULE_LOGICAL_OPTIONS", f"logical rule options are not supported: {options}", path)
        return body, True
    if expect_policy:
        ctx.error("E_RULE_POLICY_MISSING", f"logical rule is missing its policy: {rest[:140]}", path)
        return rest, False
    return rest, False


def validate_logical_rule(
    rule_type: str, rest: str, ctx: BundleContext, path: str, *, expect_policy: bool
) -> None:
    body, _has_policy = _logical_body_parts(rest, ctx, path, expect_policy=expect_policy)
    match = re.match(r"^\((.*)\)$", body.strip(), re.S)
    if not match:
        ctx.error(
            "E_RULE_LOGICAL_GROUP",
            f"{rule_type} sub-rules must be parenthesised: {body[:120]}",
            path,
        )
        return
    inner_parts = split_top_level(match.group(1))
    if not inner_parts:
        ctx.error("E_RULE_LOGICAL", f"{rule_type} rule has no sub-rules: {rest[:120]}", path)
        return
    for inner in inner_parts:
        inner = inner.strip()
        while inner.startswith("(") and inner.endswith(")"):
            inner = inner[1:-1].strip()
        if not inner:
            continue
        nested = re.match(r"^(AND|OR|NOT)\s*,(.*)$", inner, re.I | re.S)
        if nested:
            validate_logical_rule(
                nested.group(1).upper(), nested.group(2), ctx, path, expect_policy=False
            )
            continue
        if "," not in inner:
            ctx.error("E_RULE_LOGICAL_ITEM", f"invalid nested rule {inner[:80]!r}", path)
            continue
        head = re.match(r"^([A-Z][A-Z0-9-]*),(.*)$", inner, re.S)
        if not head:
            ctx.error("E_RULE_LOGICAL_ITEM", f"invalid nested rule {inner[:80]!r}", path)
            continue
        if head.group(1) not in ctx.known_rule_types:
            ctx.error("E_RULE_TYPE", f"unsupported nested rule type {head.group(1)!r}", path)


def validate_http_section(http: dict, ctx: BundleContext, path: str) -> None:
    allowed = {
        "url-rewrite",
        "header-rewrite",
        "body-rewrite",
        "mock",
        "script",
        "mitm",
        "force-http-engine",
        "hosts",
    }
    extra = set(http) - allowed
    if extra:
        ctx.error("E_HTTP_UNKNOWN_SECTION", f"http has unsupported sections: {sorted(extra)}", path)
    for key in ("url-rewrite", "header-rewrite", "body-rewrite", "mock", "script", "mitm", "force-http-engine"):
        if key in http and not isinstance(http[key], list):
            ctx.error("E_HTTP_SECTION_TYPE", f"http.{key} must be a list", path)
    for item in http.get("url-rewrite") or []:
        validate_url_rewrite(item, ctx, path)
    for item in http.get("header-rewrite") or []:
        validate_header_rewrite(item, ctx, path)
    for item in http.get("body-rewrite") or []:
        validate_body_rewrite(item, ctx, path)
    for item in http.get("mock") or []:
        validate_mock(item, ctx, path)
    for item in http.get("script") or []:
        validate_script(item, ctx, path)
    for host in http.get("mitm") or []:
        if not isinstance(host, str) or not host:
            ctx.error("E_MITM_ENTRY", f"invalid mitm host {host!r}", path)
    for engine in http.get("force-http-engine") or []:
        if not isinstance(engine, str) or not re.match(
            r"^(\*|[A-Za-z0-9*][A-Za-z0-9.*-]*):\d+$", engine
        ):
            ctx.error("E_FORCE_ENGINE_ENTRY", f"invalid force-http-engine entry {engine!r}", path)


def validate_override(data: dict, ctx: BundleContext, path: str) -> None:
    if not isinstance(data, dict):
        ctx.error("E_ROOT_TYPE", "override root must be a mapping", path)
        return
    if not data:
        ctx.error("E_EMPTY_OVERRIDE", "override is empty", path)
        return
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        ctx.error("E_NAME_MISSING", "override is missing a name", path)
    if "icon" in data and not isinstance(data["icon"], str):
        ctx.error("E_ICON_TYPE", "icon must be a string", path)
    providers = data.get("script-providers")
    if providers is not None:
        if not isinstance(providers, dict):
            ctx.error("E_PROVIDERS_TYPE", "script-providers must be a mapping", path)
            providers = {}
        for provider_name, info in providers.items():
            validate_script_provider(str(provider_name), info, ctx, path)
            # A .stoverride may declare its own providers (the build hoists them
            # into kelee-scripts.stoverride, but the Stash override schema allows
            # both). Keep the bundle provider precedence if the same name exists.
            ctx.providers.setdefault(str(provider_name), info)
            ctx.provider_file.setdefault(str(provider_name), path)
    http = data.get("http")
    if http is not None:
        if not isinstance(http, dict):
            ctx.error("E_HTTP_TYPE", "http must be a mapping", path)
        else:
            validate_http_section(http, ctx, path)
    else:
        http = {}
    if "rules" in data:
        validate_rules(data["rules"], ctx, path)
    # Unknown top-level keys are a schema problem.
    allowed_top = {
        "name",
        "desc",
        "icon",
        "author",
        "homepage",
        "date",
        "version",
        "category",
        "openUrl",
        "rules",
        "http",
        "hosts",
        "script-providers",
    }
    extra = set(data) - allowed_top
    if extra:
        ctx.error("E_TOP_UNKNOWN_FIELD", f"override has unsupported top-level fields: {sorted(extra)}", path)
    # Completeness: a converted override must carry at least one operative section.
    operative = bool(data.get("rules")) or any(
        http.get(key) for key in ("url-rewrite", "header-rewrite", "body-rewrite", "mock", "script")
    ) or bool(data.get("hosts")) or bool(providers)
    if not operative:
        ctx.error("E_NO_OPERATIVE_CONTENT", "override has no rules/rewrites/scripts/hosts", path)
    # Forbidden legacy keys / Loon leftovers anywhere in the file.
    for text in iter_strings(data):
        for marker in PLACEHOLDER_MARKERS:
            if marker in text:
                ctx.error("E_LEGACY_MARKER", f"legacy/leftover marker {marker!r} in {text[:120]}", path)
                break
    validate_body_rewrite_yaml_style(ctx.root / path, path, ctx)


def validate_body_rewrite_yaml_style(file_path: Path, path: str, ctx: BundleContext) -> None:
    try:
        composed = yaml.compose(file_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - main parse already reported
        return
    if not isinstance(composed, yaml.MappingNode):
        return
    http_node = None
    for key_node, value_node in composed.value:
        if key_node.value == "http":
            http_node = value_node
            break
    if not isinstance(http_node, yaml.MappingNode):
        return
    body_node = None
    for key_node, value_node in http_node.value:
        if key_node.value == "body-rewrite":
            body_node = value_node
            break
    if not isinstance(body_node, yaml.SequenceNode):
        return
    for item in body_node.value:
        if isinstance(item, yaml.ScalarNode) and item.style not in {"'", '"'}:
            ctx.error(
                "E_BODY_YAML_UNQUOTED",
                f"body-rewrite must stay one quoted scalar: {item.value[:120]}",
                path,
            )


def load_provider_bundle(root: Path, ctx: BundleContext) -> None:
    bundle_path = root / PROVIDER_BUNDLE
    if not bundle_path.is_file():
        return
    try:
        data = yaml.safe_load(bundle_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        ctx.error("E_PROVIDER_BUNDLE_YAML", f"provider bundle does not parse: {exc}", PROVIDER_BUNDLE)
        return
    providers = data.get("script-providers")
    if not isinstance(providers, dict):
        ctx.error("E_PROVIDER_BUNDLE_TYPE", "provider bundle has no script-providers mapping", PROVIDER_BUNDLE)
        return
    for name, info in providers.items():
        ctx.providers.setdefault(str(name), info)
        ctx.provider_file.setdefault(str(name), PROVIDER_BUNDLE)
        validate_script_provider(str(name), info, ctx, PROVIDER_BUNDLE)
    # Provider bundle itself must not reference Loon-UA hosts.
    for name, info in providers.items():
        url = info.get("url") if isinstance(info, dict) else None
        if isinstance(url, str) and "kelee.one" in url:
            ctx.error(
                "E_PROVIDER_KELEE_URL",
                f"provider bundle entry {name!r} points at kelee.one: {url}",
                PROVIDER_BUNDLE,
            )


def validate_targets_file(root: Path, ctx: BundleContext, expected_targets: Optional[Iterable[str]]) -> None:
    targets_path = root / "targets.json"
    if not targets_path.is_file():
        ctx.error("E_TARGETS_MISSING", "targets.json is missing", "targets.json")
        return
    try:
        targets = json.loads(targets_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        ctx.error("E_TARGETS_JSON", f"targets.json does not parse: {exc}", "targets.json")
        return
    if not isinstance(targets, list) or not targets:
        ctx.error("E_TARGETS_EMPTY", "targets.json must be a non-empty list", "targets.json")
        return
    filenames = []
    for item in targets:
        filename = item.get("filename") if isinstance(item, dict) else None
        if not filename:
            ctx.error("E_TARGETS_ITEM", f"target item has no filename: {item!r}", "targets.json")
            continue
        filenames.append(filename)
        output = root / str(filename).replace(".lpx", ".stoverride")
        if not output.is_file():
            ctx.error("E_TARGET_OUTPUT_MISSING", f"missing generated override {output.name}", str(output.name))
    if expected_targets is not None:
        expected = {str(name).replace(".lpx", ".stoverride") for name in expected_targets}
        actual = {name.replace(".lpx", ".stoverride") for name in filenames}
        missing = expected - actual
        extra = actual - expected
        if missing:
            ctx.error("E_TARGETS_INCOMPLETE", f"targets.json is missing: {sorted(missing)}", "targets.json")
        if extra:
            ctx.error("E_TARGETS_EXTRA", f"targets.json has unexpected targets: {sorted(extra)}", "targets.json")


def validate_assets(root: Path, ctx: BundleContext, assets: Optional[Mapping[str, str]]) -> None:
    scripts_dir = root / "scripts"
    if scripts_dir.is_dir():
        for path in scripts_dir.glob("*.js"):
            import hashlib

            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            rel = path.relative_to(root).as_posix()
            ctx.assets[rel] = digest
    if assets:
        for rel, expected in assets.items():
            path = root / rel
            if not path.is_file():
                ctx.error("E_ASSET_MISSING", f"manifest asset {rel} is missing", rel)
                continue
            import hashlib

            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if expected and digest != expected:
                ctx.error("E_ASSET_CORRUPT", f"manifest asset {rel} hash mismatch", rel)


def validate_bundle(
    root: Path | str,
    *,
    expected_targets: Optional[Iterable[str]] = None,
    public_base: str = "",
    known_policies: Optional[Iterable[str]] = None,
    assets: Optional[Mapping[str, str]] = None,
) -> DiagnosticCollection:
    """Validate a generated bundle directory. Returns all diagnostics."""

    root = Path(root)
    ctx = BundleContext(root=root, public_base=(public_base or "").rstrip("/"))
    if known_policies:
        ctx.known_policies |= {str(p) for p in known_policies}
    load_provider_bundle(root, ctx)
    validate_assets(root, ctx, assets)
    validate_targets_file(root, ctx, expected_targets)
    for file_path in sorted(root.glob("*.stoverride")):
        path = file_path.name
        try:
            data = yaml.safe_load(file_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            ctx.error("E_YAML_PARSE", f"{path} does not parse: {exc}", path)
            continue
        validate_override(data, ctx, path)
    if not list(root.glob("*.stoverride")):
        ctx.error("E_BUNDLE_EMPTY", f"no .stoverride files in {root}")
    # Every provider's URL that points at our host must resolve to a real asset.
    for name in sorted(ctx.providers):
        info = ctx.providers[name]
        if not isinstance(info, dict):
            continue
        url = info.get("url")
        if isinstance(url, str) and url.startswith((ctx.public_base + "/") if ctx.public_base else ()):
            validate_asset_reference(url, ctx, ctx.provider_file.get(name, PROVIDER_BUNDLE), name=name)
    return DiagnosticCollection(ctx.diagnostics)


def validate_bundle_or_raise(
    root: Path | str,
    *,
    expected_targets: Optional[Iterable[str]] = None,
    public_base: str = "",
    known_policies: Optional[Iterable[str]] = None,
    assets: Optional[Mapping[str, str]] = None,
) -> DiagnosticCollection:
    """Validate and raise :class:`ValidationError` when anything blocks publish."""

    diagnostics = validate_bundle(
        root,
        expected_targets=expected_targets,
        public_base=public_base,
        known_policies=known_policies,
        assets=assets,
    )
    errors = blocking_errors(diagnostics)
    if errors:
        raise ValidationError(errors)
    return diagnostics


def load_main_config_policies(path: Path) -> set[str]:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return set()
    names = set(cfg.STASH_BUILTIN_POLICIES)
    for group in data.get("proxy-groups") or []:
        if isinstance(group, dict) and isinstance(group.get("name"), str):
            names.add(group["name"])
    for proxy in data.get("proxies") or []:
        if isinstance(proxy, dict) and isinstance(proxy.get("name"), str):
            names.add(proxy["name"])
    return names


def format_report(diagnostics: list[Diagnostic], *, stream=sys.stdout) -> None:
    blocking = [d for d in diagnostics if d.blocking]
    warnings = [d for d in diagnostics if not d.blocking]
    print("Stash bundle validation", file=stream)
    print("=" * 40, file=stream)
    for diagnostic in blocking:
        print(f"  ERROR {diagnostic}", file=stream)
    for diagnostic in warnings:
        print(f"  WARN  {diagnostic}", file=stream)
    print("", file=stream)
    print(
        f"Result: {'PASS' if not blocking else 'FAIL'} "
        f"({len(blocking)} errors, {len(warnings)} warnings)",
        file=stream,
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate generated KeLee Stash bundle")
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--public-base", default="")
    parser.add_argument("--targets", type=Path, default=DEFAULT_DIR / "targets.json")
    parser.add_argument("--main-config", type=Path, default=REPO_ROOT / "stash-ios.yaml")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if not args.dir.exists():
        print(f"bundle directory does not exist: {args.dir}", file=sys.stderr)
        return 2
    expected = None
    if args.targets.is_file():
        try:
            expected = [
                item["filename"]
                for item in json.loads(args.targets.read_text(encoding="utf-8"))
                if isinstance(item, dict) and item.get("filename")
            ]
        except Exception:  # noqa: BLE001
            expected = None
    policies = load_main_config_policies(args.main_config) if args.main_config.is_file() else None
    diagnostics = validate_bundle(
        args.dir,
        expected_targets=expected,
        public_base=args.public_base,
        known_policies=policies,
    )
    if args.json:
        print(json.dumps([d.as_dict() for d in diagnostics], ensure_ascii=False, indent=2))
    else:
        format_report(diagnostics)
    return 0 if not any(d.blocking for d in diagnostics) else 2


if __name__ == "__main__":
    raise SystemExit(main())
