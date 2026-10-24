#!/usr/bin/env python3
"""Loon → Stash semantic conversion.

This module implements the *meaning* layer: it turns parsed Loon plugin rules
into the Stash override schema verified against stash.wiki (2026-10-25):

* ``response.body.mock(...)``            → ``http.mock`` items (text/base64,
  status-code, headers) with sibling ``response.header.*`` actions merged in;
* ``response.body.replace(re, text)``    → ``body-rewrite ... response-replace-regex``;
* ``response.header.add/set/del/...``   → ``http.header-rewrite`` (or mock headers);
* ``response.json.jq / jq_file``         → ``body-rewrite ... response-jq`` (remote
  jq inlined after comment stripping, with a blocking diagnostic if unavailable);
* ``response.json.delete/add/replace``   → native json actions (or a jq fallback
  when a Loon key path is not expressible as a Stash jq identifier);
* capture ``redirect`` / ``url.replace`` → minimal generated request scripts,
  because Stash does not document capture substitution in url-rewrite targets;
  unmatched URL content/encoding is preserved exactly like Loon;
* ``script(path, {${args}})``            → ``http.script`` entries with the
  upstream script mirrored content-addressed and the argument encoded with the
  protocol the script actually parses (JSON object or ``&``-joined key=value);
* ``enable=${switch}``                   → resolved at build time; disabled
  scripts are intentionally omitted (Stash has no per-script enable switch);
* ``[Rule]`` lines                       → Stash rules with an explicit policy map;
* local enhancements / conflict patches  → tagged separately, never preempting
  upstream behaviour.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import urlparse

import kelee_config as cfg
from kelee_diagnostics import Diagnostic, DiagnosticCollection
import kelee_enhancements as enh
import loon_expr as lx
from kelee_assets import PublishedAsset, ResourceError, ResourceResolver

LOON_REJECT_RULE_ACTIONS = {"reject", "reject-200", "reject-img", "reject-dict", "reject-array"}
STASH_REJECT_ACTIONS = {
    "reject": "reject",
    "reject-200": "reject-200",
    "reject-img": "reject-img",
    "reject-dict": "reject-dict",
    "reject-array": "reject-array",
}
BODY_ACTION_MAP = {
    "response.body.replace": "response-replace-regex",
    "request.body.replace": "request-replace-regex",
    "response.json.jq": "response-jq",
    "request.json.jq": "request-jq",
    "response.json.jq_file": "response-jq",
    "request.json.jq_file": "request-jq",
    "response.json.delete": "response-json-del",
    "request.json.delete": "request-json-del",
    "response.json.add": "response-json-add",
    "request.json.add": "request-json-add",
    "response.json.replace": "response-json-replace",
    "request.json.replace": "request-json-replace",
}
HEADER_ACTION_MAP = {
    "request.header.add": "request-add",
    "request.header.set": "request-replace",
    "request.header.del": "request-del",
    "request.header.replace": "request-replace-regex",
    "response.header.add": "response-add",
    "response.header.set": "response-replace",
    "response.header.del": "response-del",
    "response.header.replace": "response-replace-regex",
}
HEADER_OPTIONAL_VALUE = {"request-del", "response-del"}


# ---------------------------------------------------------------------------
# Parser facade (loon_expr API; kept in one place to isolate the integration)
# ---------------------------------------------------------------------------


def _attr(obj, *names, default=None):
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return default


def _phase(expr) -> str:
    return str(_attr(expr, "phase", default="response")).lower()


def _regex(expr):
    return _attr(expr, "regex", "url_regex")


def _capture_name(expr) -> str:
    return str(_attr(expr, "capture_name", "capture", "as_name", default="") or "")


def _actions(expr):
    return list(_attr(expr, "actions", "action_list", default=[]))


def _call_name(action) -> str:
    name = _attr(action, "name", "function", default="")
    return str(name or "")


def _call_args(action):
    return list(_attr(action, "args", "arguments", default=[]))


def _literal_value(value):
    """Normalise a loon_expr literal into a plain Python value."""

    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return value
    for name in ("value",):
        if hasattr(value, name):
            inner = getattr(value, name)
            return _literal_value(inner) if not isinstance(inner, (int, float, bool)) else inner
    entries = _attr(value, "entries", "pairs")
    if entries is not None and not callable(entries):
        return {str(k): _literal_value(v) for k, v in entries}
    items = _attr(value, "items")
    if items is not None and not callable(items):
        return [_literal_value(item) for item in items]
    if _attr(value, "name") is not None and type(value).__name__ == "VariableRef":
        return "${" + str(_attr(value, "name")) + "}"
    return value


def _string_value(value) -> Optional[str]:
    """Extract a literal string (String/RawString literal) or None."""

    if isinstance(value, str):
        return value
    text = _attr(value, "value", default=None)
    if isinstance(text, str):
        return text
    raw = _attr(value, "raw", default=None)
    if isinstance(raw, str):
        return raw
    return None


def _regex_literal(value):
    return value


def _stash_regex(regex_obj, ctx: "ConversionContext", source: str) -> str:
    if regex_obj is None:
        ctx.error("E_REGEX_MISSING", f"rewrite has no match regex: {source[:160]}")
        return ""
    try:
        return lx.stash_regex(regex_obj)
    except lx.LoonUnsupportedError as exc:  # type: ignore[attr-defined]
        ctx.error(exc.code, exc.detail if hasattr(exc, "detail") else str(exc), source=source)
        return ""
    except lx.LoonParseError as exc:  # type: ignore[attr-defined]
        ctx.error(exc.code, exc.detail if hasattr(exc, "detail") else str(exc), source=source)
        return ""


def _flags_from_regex(regex_obj) -> str:
    return str(_attr(regex_obj, "flags", default="") or "")


def _regex_body(regex_obj) -> str:
    for name in ("pattern", "body", "source"):
        value = _attr(regex_obj, name, default=None)
        if isinstance(value, str):
            return value
    return str(regex_obj)


def _regex_to_stash(regex_obj, ctx: "ConversionContext", source: str) -> str:
    return _stash_regex(regex_obj, ctx, source)


def _count_groups(pattern: str) -> int:
    try:
        return re.compile(pattern).groups
    except re.error:
        # Fallback: count '(' not escaped and not followed by '?'
        count = 0
        escaped = False
        for index, char in enumerate(pattern):
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
            elif char == "(" and (index + 1 >= len(pattern) or pattern[index + 1] != "?"):
                count += 1
        return count


# ---------------------------------------------------------------------------
# Context / result
# ---------------------------------------------------------------------------


@dataclass
class ConversionContext:
    name: str
    lpx_url: str
    source_filename: str
    public_base: str
    resolver: Optional[ResourceResolver]
    diagnostics: DiagnosticCollection = field(default_factory=DiagnosticCollection)
    counts: dict[str, int] = field(default_factory=dict)
    providers: dict[str, dict] = field(default_factory=dict)
    provider_sources: dict[str, str] = field(default_factory=dict)
    provider_names_by_url: dict[str, str] = field(default_factory=dict)
    script_entries: list[dict] = field(default_factory=list)
    mitm: list[str] = field(default_factory=list)
    force_http_engine: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    url_rewrite: list[str] = field(default_factory=list)
    header_rewrite: list[str] = field(default_factory=list)
    body_rewrite: list[str] = field(default_factory=list)
    mock: list[dict] = field(default_factory=list)
    hosts: dict[str, str] = field(default_factory=dict)
    resources: dict[str, dict] = field(default_factory=dict)
    patches: list[dict] = field(default_factory=list)
    enhancements: list[dict] = field(default_factory=list)
    generated_scripts: list[dict] = field(default_factory=list)
    arguments: dict[str, "ArgumentValue"] = field(default_factory=dict)
    mirror_scripts: bool = True

    def bump(self, key: str, amount: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + amount

    def error(self, code: str, detail: str, *, source: str = "", path: str = "") -> None:
        self.diagnostics.append(Diagnostic(code, detail, path=path or self.source_filename, source="converter"))
        self.bump("errors")

    def warn(self, code: str, detail: str, *, source: str = "") -> None:
        self.diagnostics.append(
            Diagnostic(
                code,
                detail,
                path=self.source_filename,
                blocking=False,
                severity="warning",
                source="converter",
            )
        )
        self.bump("warnings")

    def ignore(self, code: str, detail: str) -> None:
        """An intentional, documented omission (not an error)."""

        self.diagnostics.append(
            Diagnostic(
                code,
                detail,
                path=self.source_filename,
                blocking=False,
                severity="info",
                source="converter",
            )
        )
        self.bump("ignored")

    def add_patch(self, kind: str, detail: str, evidence: str = "") -> None:
        self.patches.append({"kind": kind, "detail": detail, "evidence": evidence})
        self.bump("patches")

    def add_enhancement(self, enhancement_id: str, count: int = 1) -> None:
        for item in self.enhancements:
            if item["id"] == enhancement_id:
                item["count"] += count
                return
        self.enhancements.append({"id": enhancement_id, "count": count})


@dataclass
class ConversionResult:
    name: str
    stash: dict
    diagnostics: DiagnosticCollection
    counts: dict
    source_sha256: str
    upstream_resources: dict
    providers: dict
    patches: list
    enhancements: list
    generated_scripts: list
    yaml: str = ""

    @property
    def blocking(self) -> bool:
        return self.diagnostics.has_blocking

    def summary(self, yaml_text: str = "") -> dict:
        return {
            "name": self.name,
            "source_sha256": self.source_sha256,
            "counts": dict(sorted(self.counts.items())),
            "diagnostics": [d.as_dict() for d in self.diagnostics],
            "providers": sorted(self.providers),
            "patches": self.patches,
            "enhancements": self.enhancements,
            "upstream_resources": sorted(self.upstream_resources),
            "yaml_bytes": len(yaml_text.encode("utf-8")) if yaml_text else 0,
        }


# ---------------------------------------------------------------------------
# LPX sections / arguments
# ---------------------------------------------------------------------------


HEADER_RE = re.compile(r"^#!(.*?)=(.*)$")
SECTION_RE = re.compile(r"^\[(.*?)\]$")


def parse_lpx(text: str) -> tuple[dict, dict[str, list[str]]]:
    header: dict[str, str] = {}
    sections: dict[str, list[str]] = {}
    current: Optional[str] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = HEADER_RE.match(line)
        if match:
            header[match.group(1).strip()] = match.group(2).strip()
            continue
        match = SECTION_RE.match(line)
        if match:
            current = match.group(1).strip()
            sections.setdefault(current, [])
            continue
        if line.startswith("#") or line.startswith(";"):
            continue
        if current is None:
            continue
        sections[current].append(raw_line.rstrip())
    return header, sections


def _section(sections: dict[str, list[str]], name: str) -> list[str]:
    for key in sections:
        if key.lower() == name.lower():
            return sections[key]
    return []


@dataclass
class ArgumentValue:
    name: str
    kind: str
    default: object
    raw: str = ""


def _argument_default(decl) -> object:
    fn = getattr(decl, "default_typed", None)
    if callable(fn):
        return fn()
    for name in ("default", "default_value", "default_raw"):
        value = getattr(decl, name, None)
        if value is not None:
            return value
    values = getattr(decl, "values", None) or getattr(decl, "options", None) or []
    if values:
        return values[0]
    return ""


def parse_arguments(sections: dict[str, list[str]], ctx: ConversionContext) -> dict[str, ArgumentValue]:
    arguments: dict[str, ArgumentValue] = {}
    for line in _section(sections, "Argument"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            decl = lx.parse_argument_decl(line)
        except lx.LoonParseError as exc:  # type: ignore[attr-defined]
            ctx.error("E_ARGUMENT_DECL", f"{getattr(exc, 'detail', exc)} in {line[:160]}")
            continue
        name = str(getattr(decl, "name"))
        kind = str(getattr(decl, "kind", "input")).lower()
        default = _argument_default(decl)
        if kind == "switch":
            default = bool(default)
        elif kind == "select":
            default = default
        arguments[name] = ArgumentValue(name=name, kind=kind, default=default, raw=line)
        ctx.bump("argument_decls")
    return arguments


def _format_argument_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if value is None:
        return "null"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def _split_commas(text: str) -> list[str]:
    """Split a rule/path argument list on top-level commas.

    This deliberately does not treat ``/`` as a regex delimiter: Loon rule
    payloads contain URL-REGEX patterns with escaped slashes and no surrounding
    literal, which the rewrite-expression tokenizer is right to reject.
    """

    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote = None
    escaped = False
    for char in text:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            current.append(char)
            escaped = True
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            current.append(char)
            continue
        if char in "([{":
            depth += 1
            current.append(char)
            continue
        if char in ")]}":
            depth = max(0, depth - 1)
            current.append(char)
            continue
        if char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    tail = "".join(current).strip()
    if tail or parts:
        parts.append(tail)
    return [part for part in parts if part]


RULE_HEAD_RE = re.compile(r"^([A-Z][A-Z0-9_-]*)(?:,(.*))?$", re.S)


def _unquote_rule_value(value: str) -> str:
    """Strip Loon rule-value quoting/escaping (``"httpdns*"`` -> ``httpdns*``)."""

    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        inner = text[1:-1]
        if text[0] == '"':
            inner = inner.replace('\\"', '"')
        return inner
    return text


def _unquote_logical_body(body: str) -> str:
    """Unquote quoted values inside nested AND/OR/NOT rule groups."""

    def replace(match):
        return _unquote_rule_value(match.group(0))

    return re.sub(r'"[^"]*"', replace, body)


def _strip_rule_comment(line: str) -> str:
    # Loon allows trailing "// comment" (whitespace separated); URLs contain
    # "//" without leading whitespace so they stay intact.
    return re.sub(r"\s+//.*$", "", line).strip()


def _find_policy_index(parts: list[str]) -> Optional[int]:
    """Locate the policy token (a known policy; trailing options may follow)."""

    for index in range(len(parts) - 1, -1, -1):
        if parts[index].strip() in cfg.POLICY_MAP:
            return index
    return None


def _map_rule_policy(line: str, ctx: ConversionContext) -> str:
    match = RULE_HEAD_RE.match(line.strip())
    if not match:
        ctx.error("E_RULE_SYNTAX", f"cannot parse rule: {line[:160]}")
        return line
    rule_type = match.group(1)
    if rule_type not in cfg.KNOWN_LOON_RULE_TYPES:
        ctx.error("E_RULE_TYPE", f"unsupported Loon rule type {rule_type!r}: {line[:160]}", source=line)
        return ""
    rest = match.group(2) or ""
    parts = _split_commas(rest)
    policy_index = _find_policy_index(parts)
    if policy_index is None:
        ctx.error("E_RULE_POLICY", f"rule has no recognised policy: {line[:160]}", source=line)
        return line
    raw_policy = parts[policy_index].strip()
    options = [part for part in parts[policy_index + 1 :] if part.strip()]
    if rule_type in ("AND", "OR", "NOT"):
        body = _unquote_logical_body(",".join(parts[:policy_index]).strip())
        try:
            mapped = cfg.map_policy(raw_policy)
        except KeyError as exc:
            ctx.error("E_RULE_POLICY", str(exc), source=line)
            return line
        return f"{rule_type},{body},{mapped}"
    if not parts[:policy_index]:
        ctx.error("E_RULE_SYNTAX", f"rule has no payload: {line[:160]}")
        return line
    try:
        mapped = cfg.map_policy(raw_policy)
    except KeyError as exc:
        ctx.error("E_RULE_POLICY", str(exc), source=line)
        return line
    if raw_policy in cfg.LOON_RULE_REJECT_DEGRADED:
        ctx.add_patch(
            "rule-policy-degradation",
            f"Loon 规则策略 {raw_policy} 在 Stash 规则层无等价策略，降级为 {mapped}"
            "（仍为拦截，但返回状态码/内容与 Loon 不同）",
            evidence=line[:200],
        )
    payload_parts = [_unquote_rule_value(part) for part in parts[:policy_index]]
    payload = ",".join(payload_parts).strip()
    suffix = "," + ",".join(options) if options else ""
    return f"{rule_type},{payload},{mapped}{suffix}"


def convert_rules(rule_lines: list[str], ctx: ConversionContext) -> None:
    rules: list[str] = []
    for raw in rule_lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = _strip_rule_comment(line)
        line = re.sub(r"\s*,\s*", ",", line)
        converted = _map_rule_policy(line, ctx)
        if converted:
            rules.append(converted)
            ctx.bump("rules")
    if not rules:
        return
    extra_rules = [re.sub(r"\s*,\s*", ",", rule.strip()) for rule in enh.APP_EXTRA_RULES.get(ctx.source_filename, [])]
    for rule in extra_rules:
        ctx.bump("enhancement_rules")
    if extra_rules:
        ctx.add_enhancement("extra-ad-domains", len(extra_rules))
    # Conflict patches: do not preempt upstream fine-grained handling, and do
    # not add QUIC mirrors for upstream rules that were deliberately replaced.
    extra_rules = _apply_conflict_patches(extra_rules, ctx)
    if ctx.source_filename == "PinDuoDuo_remove_ads.lpx":
        fallback = "AND,((DOMAIN,api.pinduoduo.com),(NETWORK,UDP),(DST-PORT,443)),REJECT"
        if fallback not in rules:
            rules.insert(0, fallback)
            ctx.add_enhancement("pinduoduo-quic-fallback")
    rules = extra_rules + rules
    # Block_HTTPDNS vs the local Stash main-config WeChat HTTPDNS DIRECT exceptions.
    if ctx.source_filename == "Block_HTTPDNS.lpx":
        before = set(rules)
        removed = [rule for rule in rules if rule in cfg.WECHAT_HTTPDNS_REJECTED_UPSTREAM]
        if removed:
            kept = [rule for rule in rules if rule not in cfg.WECHAT_HTTPDNS_REJECTED_UPSTREAM]
            additions = [rule for rule in cfg.WECHAT_HTTPDNS_DIRECT_EXCEPTIONS if rule not in before]
            rules = additions + kept
            ctx.add_patch(
                "wechat-httpdns-direct-exceptions",
                "上游 Block_HTTPDNS 把三个微信 HTTPDNS 域名 REJECT；本仓库 Stash 主配置明确放行它们以保持微信可用，"
                "生成物改为显式 DIRECT 例外并记录冲突",
                evidence=", ".join(removed),
            )
    if ctx.source_filename not in enh.QUIC_MIRROR_EXCLUDE:
        mirrored = []
        seen = set(rules)
        for rule in rules:
            match = re.match(r"^(DOMAIN|DOMAIN-SUFFIX),([^,]+),REJECT$", rule)
            if not match:
                continue
            and_rule = f"AND,(({match.group(1)},{match.group(2)}),(NETWORK,UDP),(DST-PORT,443)),REJECT"
            if and_rule not in seen:
                mirrored.append(and_rule)
                seen.add(and_rule)
        if mirrored:
            ctx.add_enhancement("quic-udp-mirror", len(mirrored))
            rules = mirrored + rules
    ctx.rules = rules


def _filter_conflicting_extra_rewrites(lines: list[str], ctx: ConversionContext) -> list[str]:
    kept: list[str] = []
    for line in lines:
        dropped = False
        for patch in cfg.UPSTREAM_CONFLICT_PATCHES:
            if patch["plugin"] != ctx.source_filename:
                continue
            if re.search(patch["match"], line.replace("\\", "")):
                ctx.add_patch(
                    patch["kind"],
                    f"丢弃会抢先于上游响应脚本的额外重写：{line}",
                    evidence=patch["reason"],
                )
                dropped = True
                break
        if not dropped:
            kept.append(line)
    return kept


def _apply_conflict_patches(extra_rules: list[str], ctx: ConversionContext) -> list[str]:
    kept: list[str] = []
    for rule in extra_rules:
        dropped = False
        for patch in cfg.UPSTREAM_CONFLICT_PATCHES:
            if patch["plugin"] != ctx.source_filename:
                continue
            if re.search(patch["match"], rule.replace("\\", "")):
                ctx.add_patch(
                    patch["kind"],
                    f"丢弃会抢先于上游精细处理的额外规则：{rule}",
                    evidence=patch["reason"],
                )
                dropped = True
                break
        if not dropped:
            kept.append(rule)
    return kept


# ---------------------------------------------------------------------------
# jq helpers (comment handling order matters!)
# ---------------------------------------------------------------------------


def strip_jq_comments(expression: str) -> str:
    """Remove jq ``#`` comments while newlines are still meaningful.

    The previous converter flattened newlines into spaces *before* dropping
    comments, so the first ``#`` swallowed the rest of the program. Comments are
    stripped here line-by-line first; the caller may flatten afterwards.
    """

    lines: list[str] = []
    for raw_line in expression.splitlines():
        out: list[str] = []
        in_string = False
        escaped = False
        for char in raw_line:
            if in_string:
                out.append(char)
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
                out.append(char)
            elif char == "#":
                break
            else:
                out.append(char)
        stripped = "".join(out).strip()
        if stripped:
            lines.append(stripped)
    return "\n".join(lines)


def flatten_jq(expression: str) -> str:
    return " ".join(line.strip() for line in expression.splitlines() if line.strip())


def jq_balanced(expression: str) -> bool:
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


def _fetch_remote_jq(url: str, ctx: ConversionContext, *, source: str) -> str:
    if ctx.resolver is None:
        ctx.error("E_JQ_NO_RESOLVER", f"cannot inline remote jq {url} without a resource resolver", source=source)
        return ""
    try:
        raw = ctx.resolver.fetch_text(url)
    except ResourceError as exc:
        ctx.error("E_JQ_FETCH", f"failed to fetch remote jq {url}: {exc.detail}", source=source)
        return ""
    ctx.resources[url] = {
        "sha256": ctx.resolver.resource_manifest().get(url, {}).get("sha256", ""),
        "kind": "jq",
    }
    ctx.bump("remote_jq")
    cleaned = strip_jq_comments(raw)
    if not cleaned.strip():
        ctx.error("E_JQ_EMPTY_REMOTE", f"remote jq {url} is empty after comment removal", source=source)
        return ""
    return flatten_jq(cleaned)


# ---------------------------------------------------------------------------
# Rewrite conversion
# ---------------------------------------------------------------------------


@dataclass
class PendingHeader:
    pattern: str
    action: str
    name: str
    value: str
    source: str
    merged: bool = False


def _host_tokens(pattern: str) -> set[str]:
    tokens = set()
    for match in re.finditer(r"([A-Za-z0-9-]+(?:\\\.[A-Za-z0-9-]+)+)", pattern):
        tokens.add(match.group(1).replace("\\.", "."))
    return tokens


ACTION_VARIABLE_RE = re.compile(r"\$\{([^}]+)\}")


def _expand_action_variables(
    value: str,
    ctx: ConversionContext,
    source: str,
    *,
    allow_capture: bool = False,
    context: str = "action value",
) -> str:
    """Bake Loon ``${param}`` defaults into an action string.

    ``${urlMatch.2}`` style capture references are only meaningful in the
    capture-aware url.replace/redirect template (``allow_capture=True``); any
    other unresolved ``${...}`` is a blocking diagnostic so it can never be
    silently copied into a generated script or Stash rule.
    """

    def replace(match):
        key = match.group(1)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.\d+", key):
            if allow_capture:
                return match.group(0)
            ctx.error(
                "E_CAPTURE_UNSUPPORTED_CONTEXT",
                f"捕获变量 ${{{key}}} 只能用于 url.replace/redirect 模板，当前为 {context}",
                source=source,
            )
            return match.group(0)
        if key in ctx.arguments:
            return _format_argument_scalar(ctx.arguments[key].default)
        ctx.error(
            "E_ACTION_VARIABLE",
            f"{context} 引用了未知变量 ${{{key}}}（[Argument] 未声明且不是捕获变量）",
            source=source,
        )
        return match.group(0)

    return ACTION_VARIABLE_RE.sub(replace, value)


def _parse_mock_action(args, ctx: ConversionContext, source: str) -> Optional[dict]:
    if len(args) < 2:
        ctx.error("E_MOCK_ARGS", f"response.body.mock expects contentType, body[, status[, base64]]: {source[:160]}")
        return None
    content_type_name = _string_value(args[0])
    body = _string_value(args[1])
    if content_type_name is None or body is None:
        ctx.error("E_MOCK_ARGS", f"mock arguments must be literals: {source[:160]}")
        return None
    try:
        content_type = cfg.mock_content_type(content_type_name)
    except KeyError:
        ctx.error(
            "E_MOCK_CONTENT_TYPE",
            f"unsupported mock content type {content_type_name!r} (Loon types: {sorted(cfg.MOCK_CONTENT_TYPES)})",
        )
        return None
    status = _literal_value(args[2]) if len(args) > 2 else 200
    if isinstance(status, str) and status.isdigit():
        status = int(status)
    if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
        ctx.error("E_MOCK_STATUS", f"mock status code must be 100..599, got {status!r}")
        return None
    is_base64 = bool(_literal_value(args[3])) if len(args) > 3 else False
    body = _expand_action_variables(body, ctx, source, context="mock body")
    entry = {"match": "", "status-code": status, "headers": {"Content-Type": content_type}}
    if is_base64:
        try:
            base64.b64decode(body, validate=True)
        except (binascii.Error, ValueError) as exc:
            ctx.error("E_MOCK_BASE64", f"mock base64 body does not decode: {exc}")
            return None
        entry["base64"] = body
    else:
        entry["text"] = body
    ctx.bump("mock")
    return entry


def _resolve_json_action_value(value, ctx: ConversionContext, source: str, *, context: str):
    """Resolve a Loon JSON action value, preserving the parameter's type.

    A bare ``${level}`` referencing an ``[Argument]`` of ``type=number`` must
    become the JSON number 9.99, not the string "9.99"; unknown/capture
    variables are blocking diagnostics.
    """

    if not isinstance(value, str):
        return value
    stripped = value.strip()
    match = re.fullmatch(r"\$\{([^}]+)\}", stripped)
    if match is None:
        if "${" in value:
            return _expand_action_variables(value, ctx, source, context=context)
        return value
    key = match.group(1)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.\d+", key):
        ctx.error(
            "E_CAPTURE_UNSUPPORTED_CONTEXT",
            f"捕获变量 ${{{key}}} 不能用于 {context}",
            source=source,
        )
        return None
    if key in ctx.arguments:
        return ctx.arguments[key].default
    ctx.error("E_ACTION_VARIABLE", f"{context} 引用了未知变量 ${{{key}}}", source=source)
    return None


def _json_literal(value) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _as_array_literal(value) -> Optional[list]:
    """Return a Python list for ArrayLiteral/list values, else None."""

    if isinstance(value, list):
        return list(value)
    if type(value).__name__ == "ArrayLiteral":
        items = _attr(value, "items")
        if items is not None and not callable(items):
            return list(items)
    return None


def _flatten_json_paths(value) -> list[str]:
    raw_values: list[str] = []
    array = _as_array_literal(value)
    if array is not None:
        raw_values = [_string_value(item) or "" for item in array]
    else:
        text = _string_value(value) or ""
        if text.strip().startswith("[") and text.strip().endswith("]"):
            text = text.strip()[1:-1]
        parts = _split_commas(text) if hasattr(lx, "split_top_level") else text.split(",")
        raw_values = [part.strip().strip('"').strip("'") for part in parts if part.strip()]
    paths: list[str] = []
    for raw in raw_values:
        path = raw.strip()
        if not path:
            continue
        path = path[1:] if path.startswith("$.") else (path[1:] if path.startswith(".") else path)
        paths.append(path)
    return paths


def is_stash_json_path(path: str) -> bool:
    match = re.match(r"^[A-Za-z0-9_@-]+(?:\.[A-Za-z0-9_@-]+|\[\d+\])*$", path)
    if not match:
        return False
    return all(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", segment.split("[", 1)[0]) for segment in path.split("."))


def _json_path_segments(path: str) -> list[str]:
    segments: list[str] = []
    for part in path.split("."):
        match = re.match(r"^([^\[\]]*)((?:\[\d+\])*)$", part)
        if not match:
            segments.append(part)
            continue
        base = match.group(1)
        if base:
            segments.append(base)
        for index in re.findall(r"\[(\d+)\]", match.group(2)):
            segments.append(int(index))
    return segments


def _json_del_expression(paths: list[str], ctx: ConversionContext, source: str) -> Optional[str]:
    if not paths:
        ctx.error("E_JSON_DEL_EMPTY", f"json.delete has no paths: {source[:160]}")
        return None
    if all(is_stash_json_path(path) for path in paths):
        return " ".join(paths)
    # Arbitrary keys (e.g. "920bestwishes") cannot be expressed as Stash json-del
    # paths, but jq `delpaths` preserves the exact same deletion semantics.
    segments = [_json_path_segments(path) for path in paths]
    ctx.add_patch(
        "json-del-jq-fallback",
        f"Loon JSON 路径含非 jq 标识符，改用 jq delpaths 保留删除语义：{paths}",
        evidence=source[:200],
    )
    return "delpaths(" + json.dumps(segments, ensure_ascii=False, separators=(",", ":")) + ")"


def _json_pairs(args, ctx: ConversionContext, source: str) -> list[tuple[str, object]]:
    if len(args) < 2:
        ctx.error("E_JSON_PAIR_ARGS", f"json action needs path and value: {source[:160]}")
        return []
    paths = _flatten_json_paths(args[0])
    values = args[1]
    array_values = _as_array_literal(values)
    if array_values is not None:
        raw_values = [_literal_value(item) for item in array_values]
    elif len(args) > 2 and all(_as_array_literal(arg) is None for arg in args[2:]):
        raw_values = [_literal_value(values)] + [_literal_value(arg) for arg in args[2:]]
    else:
        raw_values = [_literal_value(values)]
    if len(paths) != len(raw_values):
        ctx.error(
            "E_JSON_PAIR_COUNT",
            f"json action path/value count mismatch: {paths} vs {raw_values}",
            source=source,
        )
        return []
    return list(zip(paths, raw_values))


def _replace_template_parts(template: str, capture_name: str, group_count: int, ctx: ConversionContext, source: str) -> list:
    """Split a Loon replacement template into literal / capture parts."""

    pattern = re.compile(r"\$\{([A-Za-z_][\w]*)\.(\d+)\}")
    parts: list = []
    position = 0
    for match in pattern.finditer(template):
        if match.start() > position:
            parts.append(template[position:match.start()])
        name, index = match.group(1), int(match.group(2))
        if capture_name and name != capture_name:
            ctx.error(
                "E_CAPTURE_NAME",
                f"replacement references ${{{name}.{index}}} but condition binds {capture_name!r}",
                source=source,
            )
        if index > group_count:
            ctx.error(
                "E_CAPTURE_INDEX",
                f"replacement references group {index} but the regex only has {group_count} groups",
                source=source,
            )
        parts.append({"group": index})
        position = match.end()
    if position < len(template):
        parts.append(template[position:])
    return parts


def _js_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _generated_script(
    ctx: ConversionContext,
    *,
    pattern: str,
    flags: str,
    mode: str,
    status: int,
    template: str,
    capture_name: str,
    source: str,
) -> Optional[dict]:
    """Emit a minimal request script reproducing Loon url.replace/redirect."""

    template = _expand_action_variables(template, ctx, source, allow_capture=True, context=mode)
    group_count = _count_groups(pattern)
    before_errors = len(ctx.diagnostics.blocking_errors)
    parts = _replace_template_parts(template, capture_name, group_count, ctx, source)
    if len(ctx.diagnostics.blocking_errors) > before_errors:
        return None
    js_parts = json.dumps(
        [part if isinstance(part, str) else part for part in parts], ensure_ascii=False
    )
    fn = generated_script_filename(ctx.source_filename, ctx.counts.get("generated_scripts", 0))
    body = "\n".join(
        [
            "// Generated by scripts/convert_kelee_lpx.py — minimal request rewrite.",
            f"// Loon source: {source[:180]}",
            "const url = $request.url;",
            f"const re = new RegExp({_js_string(pattern)}, {_js_string(flags)});",
            "const m = url.match(re);",
            "if (!m) {",
            "  $done({});",
            "} else {",
            f"  const parts = {js_parts};",
            '  const replacement = parts.map((part) => (typeof part === "string" ? part : (m[part.group] || ""))).join("");',
            "  const newUrl = url.slice(0, m.index) + replacement + url.slice(m.index + m[0].length);",
            f"  $done({{ {'url: newUrl' if mode == 'replace' else 'response: { status: %d, headers: { Location: newUrl } }' % status} }});",
            "}",
            "",
        ]
    )
    content = body.encode("utf-8")
    provider_name = _provider_name_for_generated(ctx, "rewrite", content)
    ctx.providers[provider_name] = {
        "url": "",
        "interval": 86400,
    }
    # Providers are rewritten later once the resolver publishes the bytes.
    script_entry = {
        "match": "",
        "name": provider_name,
        "type": "request",
    }
    record = {
        "filename": fn,
        "content": content,
        "provider": provider_name,
        "mode": mode,
        "status": status,
        "source": source,
        "entry": script_entry,
    }
    ctx.generated_scripts.append(record)
    ctx.bump("generated_scripts")
    return record


def generated_script_filename(lpx_filename: str, index: int) -> str:
    stem = re.sub(r"\.lpx$", "", lpx_filename, flags=re.I)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").lower() or "plugin"
    return f"{slug}-rewrite-{index + 1}.js"


def _provider_name_for_generated(ctx: ConversionContext, kind: str, content: bytes) -> str:
    base = re.sub(r"\.lpx$", "", ctx.source_filename, flags=re.I)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", base).strip("-").lower() or "plugin"
    digest = hashlib.sha256(content).hexdigest()[:8]
    return f"{slug}-{kind}-{digest}"


def _provider_name_for_script(url: str, ctx: ConversionContext) -> str:
    from pathlib import PurePosixPath

    try:
        stem = PurePosixPath(url.split("?")[0]).stem
    except Exception:  # noqa: BLE001
        stem = "script"
    name = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").lower() or "script"
    original = name
    index = 1
    while name in ctx.providers and ctx.provider_sources.get(name) != url:
        name = f"{original}-{index}"
        index += 1
    ctx.provider_sources[name] = url
    return name


MIRRORED_SCRIPT_HOSTS = {"kelee.one"}


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _publish_script(url: str, ctx: ConversionContext) -> tuple[Optional[PublishedAsset], str, str]:
    """Return (published asset or None, script content, provider URL).

    Hosts that gate on the Loon User-Agent (kelee.one) are mirrored to our own
    content-addressed storage; other scripts keep their upstream URL but are
    still fetched so their content participates in the build fingerprint and
    their ``$argument`` protocol can be verified. ``mirror_scripts=False`` is a
    test/legacy mode: no fetch, no mirror, and arguments become blocking.
    """

    if not ctx.mirror_scripts:
        if _host(url) in MIRRORED_SCRIPT_HOSTS:
            ctx.error(
                "E_SCRIPT_NOT_MIRRORED",
                f"kelee.one 脚本必须镜像后发布（当前为不镜像模式）: {url}",
            )
            return None, "", url
        return None, "", url
    if ctx.resolver is None:
        ctx.error("E_SCRIPT_NO_RESOLVER", f"cannot fetch script without a resolver: {url}")
        return None, "", url
    try:
        content_bytes = ctx.resolver.fetch_bytes(url)
    except ResourceError as exc:
        ctx.error(exc.code, f"failed to fetch script {url}: {exc.detail}")
        return None, "", url
    content = content_bytes.decode("utf-8", "replace")
    ctx.resources[url] = {
        "sha256": ctx.resolver.resource_manifest().get(url, {}).get("sha256", ""),
        "kind": "script",
    }
    if _host(url) not in MIRRORED_SCRIPT_HOSTS:
        ctx.bump("external_scripts")
        return None, content, url
    try:
        asset, content = ctx.resolver.resolve_script_url(url)
    except ResourceError as exc:
        ctx.error(exc.code, f"failed to mirror script {url}: {exc.detail}")
        return None, "", url
    ctx.bump("mirrored_scripts")
    return asset, content, asset.url


def _register_provider(ctx: ConversionContext, name: str, url: str, *, source: str = "") -> None:
    ctx.providers.setdefault(
        name,
        {"url": url, "interval": 86400},
    )
    if source:
        ctx.provider_sources.setdefault(name, source)


def _is_variable_ref(value) -> bool:
    return type(value).__name__ in ("VariableRef",) or (
        _attr(value, "name", default=None) is not None and _attr(value, "value", default=None) is None
    )


def _argument_refs(argument) -> list:
    """Normalise a Loon object/array argument expression into refs/literals.

    Loon ``{${a}, ${b}}`` is object shorthand (keys are the parameter names);
    ``[${a}, ${b}]`` was the pre-[Argument] array form. The parser may model
    either as an object of entries or as an array; both are accepted here, but
    anything else is reported instead of guessed.
    """

    if argument is None:
        return []
    if isinstance(argument, list):
        return list(argument)
    if isinstance(argument, str):
        return [argument]
    entries = _attr(argument, "entries", "pairs")
    if entries is not None and not callable(entries):
        refs = []
        for entry in entries:
            if isinstance(entry, (tuple, list)) and len(entry) == 2:
                key, value = entry
                if _is_variable_ref(value) or _is_variable_ref(key):
                    refs.append(value if _is_variable_ref(value) else key)
                elif _string_value(value) is not None and _string_value(key) is not None:
                    refs.append({"name": _string_value(key), "value": _string_value(value)})
                else:
                    refs.append(entry)
            else:
                refs.append(entry)
        return refs
    items = _attr(argument, "items")
    if items is not None and not callable(items):
        return list(items)
    return [argument]


def _encode_script_argument(
    argument,
    script_content: str,
    script_url: str,
    ctx: ConversionContext,
    *,
    source: str,
) -> Optional[str]:
    refs: list = _argument_refs(argument)
    if not refs:
        return None
    names: list[str] = []
    values: list = []
    literal_mode = False
    for ref in refs:
        if isinstance(ref, str):
            # A bare string argument (e.g. script(url, "name=loon")) is a static
            # argument, not a parameter reference.
            literal_mode = True
            names.append("value")
            values.append(ref)
            continue
        if isinstance(ref, dict):
            literal_mode = True
            names.append(str(ref.get("name", "value")))
            values.append(ref.get("value"))
            continue
        name = _attr(ref, "name", default=None)
        if name is None or not _is_variable_ref(ref):
            # Literal argument expression.
            literal_mode = True
            names.append("value")
            values.append(_literal_value(ref))
            continue
        name = str(name)
        if name not in ctx.arguments:
            ctx.error(
                "E_SCRIPT_ARG_UNKNOWN",
                f"script argument references undeclared Loon parameter ${{{name}}}",
                source=source,
            )
            return None
        names.append(name)
        values.append(ctx.arguments[name].default)
    if literal_mode:
        # Static arguments are passed through as-is (Loon quote the string).
        if len(values) == 1 and isinstance(values[0], str):
            ctx.bump("argument_entries")
            return values[0]
        ctx.error(
            "E_SCRIPT_ARG_LITERAL",
            f"无法把静态 script 参数无损编码为 Stash 字符串: {values!r}",
            source=source,
        )
        return None
    if not cfg.script_consumes_argument(script_content):
        ctx.ignore(
            "E_SCRIPT_ARG_IGNORED",
            f"上游脚本未读取 $argument，按构建时默认值丢弃参数 {names}（脚本 {script_url}）",
        )
        return None
    protocol = cfg.script_argument_protocol(script_url)
    if protocol is None:
        ctx.error(
            "E_SCRIPT_ARG_PROTOCOL",
            f"无法确认脚本 {script_url} 的 $argument 解析协议，拒绝猜测；请在 kelee_config.SCRIPT_ARGUMENT_PROTOCOLS 登记",
            source=source,
        )
        return None
    if protocol == "json-object":
        payload = {}
        for name, value in zip(names, values):
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
                ctx.error(
                    "E_SCRIPT_ARG_NAME",
                    f"参数名 {name!r} 无法作为 JSON key 传给脚本 {script_url}",
                    source=source,
                )
                return None
            payload[name] = value
        ctx.bump("argument_entries")
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if protocol == "amp-key-value":
        pairs: list[str] = []
        for name, value in zip(names, values):
            text = _format_argument_scalar(value)
            if any(char in text for char in ("&", "=")):
                ctx.error(
                    "E_SCRIPT_ARG_ENCODING",
                    f"WeatherKit 协议按 & 与 = 拆分，参数 {name} 默认值 {text!r} 无法无损编码",
                    source=source,
                )
                return None
            pairs.append(f"{name}={text}")
        ctx.bump("argument_entries")
        return "&".join(pairs)
    ctx.error("E_SCRIPT_ARG_PROTOCOL", f"unknown argument protocol {protocol!r}", source=source)
    return None


def _call_literal_arguments(args) -> list:
    return [_literal_value(arg) for arg in args]


def _resolve_enable(enable, ctx: ConversionContext, source: str) -> Optional[bool]:
    if enable is None:
        return True
    name = _attr(enable, "name", default=None)
    if name is None and isinstance(enable, str):
        match = re.match(r"^\$?\{([^}]+)\}$", enable.strip())
        if match:
            name = match.group(1)
        elif enable.strip().lower() in ("true", "false"):
            return enable.strip().lower() == "true"
        else:
            name = enable.strip()
    if isinstance(enable, bool):
        return enable
    if name is not None:
        name = str(name)
        if name not in ctx.arguments:
            ctx.error(
                "E_SCRIPT_ENABLE_UNKNOWN",
                f"enable= 引用了未声明的 Loon 参数 ${{{name}}}",
                source=source,
            )
            return None
        value = ctx.arguments[name].default
        if not isinstance(value, bool):
            ctx.error(
                "E_SCRIPT_ENABLE_TYPE",
                f"enable= 参数 ${{{name}}} 默认值不是布尔值：{value!r}",
                source=source,
            )
            return None
        return value
    ctx.error("E_SCRIPT_ENABLE_SYNTAX", f"无法解析 enable 条件：{source[:160]}")
    return None


def convert_rewrites(rewrite_lines: list[str], ctx: ConversionContext) -> None:
    pending_headers: list[PendingHeader] = []
    for raw in rewrite_lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        expression = None
        try:
            expression = lx.parse_rewrite_expression(line)
        except lx.LoonUnsupportedError as exc:  # type: ignore[attr-defined]
            ctx.error("E_REWRITE_UNSUPPORTED", f"{getattr(exc, 'detail', exc)} | {line[:200]}")
            continue
        except lx.LoonParseError as exc:  # type: ignore[attr-defined]
            ctx.error("E_REWRITE_SYNTAX", f"{getattr(exc, 'detail', exc)} | {line[:200]}")
            continue
        if expression is None:
            _convert_legacy_rewrite(line, ctx, pending_headers)
            continue
        _convert_expression_rewrite(expression, line, ctx, pending_headers)
    # Loon generates the mock response first, then runs response.header.* actions:
    # merge matching header actions into the mock entries, then emit the rest.
    for mock in ctx.mock:
        if mock.get("match"):
            _merge_pending_into_mock(mock, pending_headers, ctx)
    for header in pending_headers:
        if header.merged:
            continue
        ctx.header_rewrite.append(f"{header.pattern} {header.action} {header.name} {header.value}".strip())
        ctx.bump("header_rewrite")


def _literal_prefix(pattern: str) -> str:
    """Longest literal URL prefix of a regex (used to compare match sets).

    Loon header actions apply only where their own regex matches; merging a
    header into a mock is only safe when the header regex still matches the
    mock URL. Exact RE2 intersection is undecidable here, but a broader literal
    prefix (with the same host) is a sound approximation: a header prefix that
    is itself a prefix of the mock's literal prefix certainly still covers it.
    """

    text = pattern.strip()
    # Drop RE2 inline flags emitted by stash_regex (e.g. "(?i)").
    while text.startswith("(?") and ")" in text:
        end = text.index(")")
        flags = text[2:end]
        if not flags or any(char not in "ims" for char in flags):
            break
        text = text[end + 1 :].strip()
    if text.startswith("^"):
        text = text[1:]
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            nxt = text[index + 1]
            if nxt in "\\./_-?":
                out.append({"/": "/", ".": ".", "?": "?", "_": "_", "-": "-"}.get(nxt, nxt))
                index += 2
                continue
            break
        if char in "()[]{}|*+?$^":
            break
        out.append(char)
        index += 1
    return "".join(out)


def _merge_pending_into_mock(mock: dict, pending: list[PendingHeader], ctx: ConversionContext) -> None:
    mock_hosts = _host_tokens(mock["match"])
    if not mock_hosts:
        return
    mock_prefix = _literal_prefix(mock["match"])
    headers = mock.setdefault("headers", {})
    for header in pending:
        if header.action != "response-add" or not header.value:
            continue
        if not (_host_tokens(header.pattern) & mock_hosts):
            continue
        header_prefix = _literal_prefix(header.pattern)
        if not header_prefix or not mock_prefix.startswith(header_prefix):
            # The header regex may not cover this mock URL; do not over-apply it.
            continue
        headers[header.name] = header.value
        header.merged = True
        ctx.add_patch(
            "mock-header-merge",
            f"mock 响应头合并 response.header.add({header.name}={header.value})（Loon 先生成 mock 再执行 header action）",
            evidence=header.source[:200],
        )


def _convert_expression_rewrite(expression, line: str, ctx: ConversionContext, pending: list[PendingHeader]) -> None:
    phase = _phase(expression)
    match_pattern = _stash_regex(_regex(expression), ctx, line)
    capture_name = _capture_name(expression)
    integrated = False
    for action in _actions(expression):
        name = _call_name(action).lower()
        args = _call_args(action)
        if name in ("script", "script-path"):
            _convert_expression_script(phase, match_pattern, args, line, ctx)
            integrated = True
            continue
        if name == "response.body.mock" or name == "body.mock":
            mock = _parse_mock_action(args, ctx, line)
            if mock is not None:
                mock["match"] = match_pattern
                ctx.mock.append(mock)
            integrated = True
            continue
        if name in BODY_ACTION_MAP:
            _convert_body_action(name, phase, match_pattern, args, line, ctx)
            integrated = True
            continue
        if name in HEADER_ACTION_MAP:
            _convert_header_action(name, match_pattern, args, line, ctx, pending)
            integrated = True
            continue
        if name == "url.replace" or name == "redirect":
            _convert_url_capture_action(name, phase, match_pattern, args, expression, capture_name, line, ctx)
            integrated = True
            continue
        if name in ("reject", "reject_img", "reject_dict", "reject_array", "reject_img", "reject_video"):
            _convert_reject_action(name, match_pattern, args, line, ctx)
            integrated = True
            continue
        ctx.error("E_REWRITE_ACTION_UNSUPPORTED", f"unsupported Loon rewrite action {name!r}: {line[:200]}")
        integrated = True
    if not integrated:
        ctx.error("E_REWRITE_ACTION_EMPTY", f"rewrite has no supported action: {line[:200]}")


def _convert_expression_script(phase: str, match_pattern: str, args, line: str, ctx: ConversionContext) -> None:
    if not args:
        ctx.error("E_SCRIPT_ARGS", f"script() has no path: {line[:200]}")
        return
    url = _string_value(args[0])
    if not url:
        ctx.error("E_SCRIPT_PATH", f"script path is not a literal: {line[:200]}")
        return
    argument = None
    if len(args) > 1:
        argument = args[1]
        if isinstance(argument, list):
            argument = argument
    _add_script_entry(
        ctx,
        phase=phase,
        match_pattern=match_pattern,
        script_url=url,
        argument=argument,
        options={},
        source=line,
        default_require_body=True,
    )


def _convert_url_capture_action(name, phase, match_pattern, args, expression, capture_name, line, ctx) -> None:
    if phase != "request":
        ctx.error("E_URL_CAPTURE_PHASE", f"{name} must be a request rewrite: {line[:200]}")
        return
    if not args:
        ctx.error("E_URL_CAPTURE_ARGS", f"{name} has no argument: {line[:200]}")
        return
    regex_obj = _regex(expression)
    raw_pattern = _regex_body(regex_obj)
    flags = _flags_from_regex(regex_obj)
    if name == "redirect":
        status = _literal_value(args[0]) if len(args) > 1 else 302
        template = _string_value(args[-1])
        if not isinstance(status, int) or status not in (302, 307):
            ctx.error("E_REDIRECT_STATUS", f"Stash redirect only supports 302/307: {line[:200]}")
            return
        if template is None:
            ctx.error("E_REDIRECT_TEMPLATE", f"redirect target is not a literal: {line[:200]}")
            return
        mode, redirect_status = "redirect", status
    else:
        if len(args) < 1:
            ctx.error("E_URL_REPLACE_ARGS", f"url.replace has no argument: {line[:200]}")
            return
        template = _string_value(args[0])
        if template is None:
            ctx.error("E_URL_REPLACE_TEMPLATE", f"url.replace argument is not a literal: {line[:200]}")
            return
        mode, redirect_status = "replace", 0
    record = _generated_script(
        ctx,
        pattern=raw_pattern,
        flags=flags,
        mode=mode,
        status=redirect_status,
        template=template,
        capture_name=capture_name,
        source=line,
    )
    if record is None:
        return
    record["entry"]["match"] = match_pattern


def _convert_reject_action(name, match_pattern, args, line, ctx) -> None:
    status = _literal_value(args[0]) if args else 404
    body = _string_value(args[1]) if len(args) > 1 else None
    if not isinstance(status, int):
        ctx.error("E_REJECT_STATUS", f"reject status is not an integer: {line[:200]}")
        return
    mapped = None
    if body is None:
        if name == "reject" and status == 404:
            mapped = "reject"
        elif name == "reject" and status == 200:
            mapped = "reject-200"
        elif name in ("reject_img", "reject_image") and status == 200:
            mapped = "reject-img"
        elif name == "reject_dict" and status == 200:
            mapped = "reject-dict"
        elif name == "reject_array" and status == 200:
            mapped = "reject-array"
    if mapped is not None:
        ctx.url_rewrite.append(f"{match_pattern} - {mapped}")
        ctx.bump("url_rewrite")
        return
    # Other status codes / explicit bodies have no native Stash url-rewrite
    # action; a minimal request script keeps the exact Loon response.
    if body is not None:
        body = _expand_action_variables(body, ctx, line, context="reject body")
    headers = '{"Content-Type": "text/plain"}' if body is not None else "{}"
    content = (
        "// Generated by scripts/convert_kelee_lpx.py — minimal request reject.\n"
        f"// Loon source: {line[:180]}\n"
        f"$done({{ response: {{ status: {status}, headers: {headers}, body: {_js_string(body or '')} }} }});\n"
    ).encode("utf-8")
    provider_name = _provider_name_for_generated(ctx, "reject", content)
    ctx.providers[provider_name] = {"url": "", "interval": 86400}
    ctx.generated_scripts.append(
        {
            "filename": generated_script_filename(ctx.source_filename, ctx.counts.get("generated_scripts", 0)),
            "content": content,
            "provider": provider_name,
            "mode": "reject",
            "status": status,
            "source": line,
            "entry": {"match": match_pattern, "name": provider_name, "type": "request"},
        }
    )
    ctx.bump("generated_scripts")


def _convert_header_action(name, match_pattern, args, line, ctx, pending) -> None:
    if len(args) < 1:
        ctx.error("E_HEADER_ARGS", f"{name} needs a header name: {line[:200]}")
        return
    header_name = _string_value(args[0])
    if not header_name:
        ctx.error("E_HEADER_NAME", f"{name} header name is not a literal: {line[:200]}")
        return
    action = HEADER_ACTION_MAP[name]
    if action.endswith("-del"):
        value = ""
    elif action.endswith("replace-regex"):
        if len(args) < 3:
            ctx.error("E_HEADER_ARGS", f"{name} needs name, regex, replacement: {line[:200]}")
            return
        regex_obj = args[1]
        replacement = _string_value(args[2])
        if replacement is None:
            ctx.error("E_HEADER_VALUE", f"{name} replacement is not a literal: {line[:200]}")
            return
        replacement = _expand_action_variables(
            replacement, ctx, line, context=f"{name} replacement"
        )
        regex_text = lx.stash_regex(regex_obj) if hasattr(lx, "stash_regex") else _regex_body(regex_obj)
        value = f"{regex_text} {replacement}"
    else:
        if len(args) < 2:
            ctx.error("E_HEADER_ARGS", f"{name} needs a value: {line[:200]}")
            return
        value = _string_value(args[1])
        if value is None:
            ctx.error("E_HEADER_VALUE", f"{name} value is not a literal: {line[:200]}")
            return
        value = _expand_action_variables(value, ctx, line, context=f"{name} value")
    if action == "response-add" and header_name.lower() != "content-type":
        pending.append(
            PendingHeader(pattern=match_pattern, action=action, name=header_name, value=value, source=line)
        )
        return
    ctx.header_rewrite.append(f"{match_pattern} {action} {header_name} {value}".strip())
    ctx.bump("header_rewrite")


def _convert_body_action(name, phase, match_pattern, args, line, ctx) -> None:
    action = BODY_ACTION_MAP[name]
    if name.endswith(".body.replace"):
        if len(args) < 2:
            ctx.error("E_BODY_REPLACE_ARGS", f"{name} needs regex and replacement: {line[:200]}")
            return
        regex_obj = args[0]
        replacement = _string_value(args[1])
        if replacement is None:
            ctx.error("E_BODY_REPLACE_REPL", f"{name} replacement is not a literal: {line[:200]}")
            return
        replacement = _expand_action_variables(
            replacement, ctx, line, context=f"{name} replacement"
        )
        try:
            regex_text = lx.stash_regex(regex_obj)
        except lx.LoonParseError as exc:  # type: ignore[attr-defined]
            ctx.error("E_BODY_REPLACE_REGEX", f"{getattr(exc, 'detail', exc)}: {line[:200]}")
            return
        if re.search(r"\s", regex_text) or re.search(r"\s", replacement):
            ctx.error(
                "E_BODY_REPLACE_SPACE",
                "Stash body replace-regex cannot quote spaces; refusing to mangle: " + line[:200],
            )
            return
        if re.search(r"\\.\\\*|\\\*\\\?|\\\+\\\?", regex_text):
            ctx.warn(
                "E_BODY_REPLACE_ESCAPED_QUANTIFIER",
                "Loon 正则中的量词被转义（如 \\.\\*\\? 会按字面量匹配），"
                "转换器按上游原样保留；若上游本意是 .*? 需由上游修正，这里不擅自改写",
            )
        ctx.body_rewrite.append(f"{match_pattern} {action} {regex_text} {replacement}")
        ctx.bump("body_rewrite")
        return
    if name.endswith(".json.jq") or name.endswith(".json.jq_file"):
        if not args:
            ctx.error("E_BODY_JQ_ARGS", f"{name} needs an argument: {line[:200]}")
            return
        if name.endswith(".jq_file"):
            url = _string_value(args[0])
            if not url:
                ctx.error("E_BODY_JQ_URL", f"{name} argument is not a literal URL: {line[:200]}")
                return
            expression = _fetch_remote_jq(url, ctx, source=line)
            if not expression:
                return
        else:
            expression = _string_value(args[0])
            if expression is None:
                ctx.error("E_BODY_JQ_EXPR", f"{name} argument is not a literal: {line[:200]}")
                return
            expression = _expand_action_variables(
                expression, ctx, line, context=f"{name} program"
            )
            expression = flatten_jq(strip_jq_comments(expression))
        if not expression.strip():
            ctx.error("E_BODY_JQ_EMPTY", f"{name} produced an empty jq program: {line[:200]}")
            return
        if not jq_balanced(expression):
            ctx.error(
                "E_BODY_JQ_UNBALANCED",
                f"jq program is unbalanced and would make Stash reject the profile: {line[:200]}",
            )
            return
        ctx.body_rewrite.append(f"{match_pattern} {action} {expression}")
        ctx.bump("body_rewrite")
        return
    if name.endswith(".json.delete"):
        if not args:
            ctx.error("E_JSON_DEL_ARGS", f"{name} needs paths: {line[:200]}")
            return
        paths = _flatten_json_paths(args[0])
        expression = _json_del_expression(paths, ctx, line)
        if expression is None:
            return
        target_action = action
        if expression.startswith("delpaths("):
            target_action = action.replace("json-del", "jq")
        ctx.body_rewrite.append(f"{match_pattern} {target_action} {expression}")
        ctx.bump("body_rewrite")
        return
    if name.endswith(".json.add") or name.endswith(".json.replace"):
        pairs = _json_pairs(args, ctx, line)
        if not pairs:
            return
        rendered: list[str] = []
        for path, value in pairs:
            value = _resolve_json_action_value(
                value, ctx, line, context=f"{name} value"
            )
            if value is None:
                continue
            if not is_stash_json_path(path):
                ctx.error(
                    "E_JSON_PAIR_PATH",
                    f"Loon JSON 路径 {path!r} 无法用 Stash {action} 表达（含非 jq 标识符）: {line[:200]}",
                )
                return
            rendered.extend([path, _json_literal(value)])
        ctx.body_rewrite.append(f"{match_pattern} {action} {' '.join(rendered)}")
        ctx.bump("body_rewrite")
        return
    ctx.error("E_BODY_UNSUPPORTED", f"no conversion for Loon body action {name!r}: {line[:200]}")


# ---------------------------------------------------------------------------
# Legacy directive rewrite (Loon < 3.5.1 build 978 syntax, kept for old plugins)
# ---------------------------------------------------------------------------


LEGACY_DIRECTIVES = {
    "302", "307", "301", "308", "header", "header-add", "header-del", "header-replace",
    "header-replace-regex", "response-header-add", "response-header-del",
    "response-header-replace", "response-header-replace-regex", "reject", "reject-200",
    "reject-img", "reject-dict", "reject-array", "mock", "mock-response-body",
    "request-body-replace-regex", "response-body-replace-regex", "request-body-json-add",
    "response-body-json-add", "request-body-json-replace", "response-body-json-replace",
    "request-body-json-del", "response-body-json-del", "request-body-json-jq",
    "response-body-json-jq",
}


def _parse_legacy_rewrite_line(line: str):
    """Parse Loon <3.5.1(978) directive syntax: ``pattern directive [payload]``.

    Official example configs also contain the reversed redirect form
    ``pattern target 302``; both are accepted and normalised here.
    """

    tokens = line.strip().split(None, 2)
    if len(tokens) < 2:
        return None
    pattern, directive = tokens[0], tokens[1].lower()
    rest = tokens[2] if len(tokens) > 2 else ""
    if directive not in LEGACY_DIRECTIVES:
        # Reversed redirect: pattern target 302/307
        bits = line.strip().split()
        if bits and bits[-1] in ("302", "307", "301", "308") and len(bits) >= 3:
            pattern = bits[0]
            directive = bits[-1]
            rest = " ".join(bits[1:-1])
        else:
            return None
    if directive in ("301", "308"):
        directive = "302" if directive == "301" else "307"
    return pattern, directive, rest.strip()


def _convert_legacy_rewrite(line: str, ctx: ConversionContext, pending: list[PendingHeader]) -> None:
    parsed = _parse_legacy_rewrite_line(line)
    if parsed is None:
        ctx.error("E_REWRITE_SYNTAX", f"cannot parse rewrite line: {line[:200]}")
        return
    pattern, directive, rest = parsed
    if directive in ("302", "307") or directive == "header":
        template = rest
        if directive == "header":
            mode, status = "replace", 0
        else:
            if not rest or rest == "-":
                ctx.error("E_REDIRECT_TARGET", f"legacy redirect missing target: {line[:200]}")
                return
            mode, status = "redirect", int(directive)
        template = re.sub(r"\$(\d+)", r"${urlMatch.\1}", template)
        record = _generated_script(
            ctx,
            pattern=pattern,
            flags="",
            mode=mode,
            status=status,
            template=template,
            capture_name="urlMatch",
            source=line,
        )
        if record is not None:
            record["entry"]["match"] = pattern
        return
    if directive in LOON_REJECT_RULE_ACTIONS:
        mapped = STASH_REJECT_ACTIONS[directive]
        ctx.url_rewrite.append(f"{pattern} - {mapped}")
        ctx.bump("url_rewrite")
        return
    if directive in ("mock-response-body", "mock"):
        mock = _parse_legacy_mock(rest, ctx, line)
        if mock is not None:
            mock["match"] = pattern
            ctx.mock.append(mock)
        return
    if directive.startswith("response-body-") or directive.startswith("request-body-"):
        _convert_legacy_body(directive, pattern, rest, line, ctx)
        return
    if directive.startswith("header") or directive.startswith("response-header"):
        action = {
            "header-add": "request-add",
            "header-del": "request-del",
            "header-replace": "request-replace",
            "header-replace-regex": "request-replace-regex",
            "response-header-add": "response-add",
            "response-header-del": "response-del",
            "response-header-replace": "response-replace",
            "response-header-replace-regex": "response-replace-regex",
        }.get(directive)
        if action is None:
            ctx.error("E_HEADER_ACTION", f"unsupported legacy header action {directive!r}: {line[:200]}")
            return
        parts = rest.split(None, 2)
        if not parts:
            ctx.error("E_HEADER_ARGS", f"legacy header action without arguments: {line[:200]}")
            return
        name = parts[0]
        value = parts[1] if len(parts) > 1 else ""
        if value:
            value = _expand_action_variables(value, ctx, line, context="legacy header value")
        if action == "response-add" and name.lower() != "content-type":
            pending.append(
                PendingHeader(pattern=pattern, action=action, name=name, value=value, source=line)
            )
        else:
            ctx.header_rewrite.append(f"{pattern} {action} {name} {value}".strip())
            ctx.bump("header_rewrite")
        return
    ctx.error("E_REWRITE_ACTION_UNSUPPORTED", f"unsupported legacy rewrite directive {directive!r}: {line[:200]}")


def _parse_legacy_mock(rest: str, ctx: ConversionContext, line: str) -> Optional[dict]:
    params = _parse_key_values(rest)
    data = params.get("data")
    if data is not None:
        data = _expand_action_variables(data, ctx, line, context="legacy mock body")
    if data is None:
        ctx.error("E_MOCK_LEGACY_DATA", f"legacy mock has no data= payload: {line[:200]}")
        return None
    data_type = params.get("data-type", "json")
    try:
        content_type = cfg.mock_content_type(str(data_type))
    except KeyError:
        ctx.error("E_MOCK_CONTENT_TYPE", f"unsupported legacy mock data-type {data_type!r}")
        return None
    status = int(params.get("status-code", 200))
    is_base64 = str(params.get("mock-data-is-base64", "false")).lower() == "true"
    body = _expand_action_variables(body, ctx, source, context="mock body")
    entry = {"match": "", "status-code": status, "headers": {"Content-Type": content_type}}
    if is_base64:
        entry["base64"] = data
    else:
        entry["text"] = data
    ctx.bump("mock")
    return entry


def _parse_key_values(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    pattern = re.compile(r"""(\S+?)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|'([^']*)'|(\S+))""")
    for match in pattern.finditer(text):
        key = match.group(1).lower()
        value = match.group(2) if match.group(2) is not None else (
            match.group(3) if match.group(3) is not None else match.group(4)
        )
        if match.group(2) is not None:
            value = value.replace('\\"', '"').replace("\\\\", "\\")
        out[key] = value
    return out


def _convert_legacy_body(directive: str, pattern: str, rest: str, line: str, ctx: ConversionContext) -> None:
    direction = "request" if directive.startswith("request-") else "response"
    suffix = directive.split("-body-", 1)[-1]
    if suffix == "json-jq":
        jq_path = re.search(r'jq-path\s*=\s*"?([^"\s]+)"?', rest)
        if jq_path:
            expression = _fetch_remote_jq(jq_path.group(1), ctx, source=line)
        else:
            tokens = _split_legacy_body(rest)
            expression = tokens[0] if tokens else rest
            expression = _unwrap_quotes(expression)
            expression = flatten_jq(strip_jq_comments(expression))
        if not expression:
            return
        if not jq_balanced(expression):
            ctx.error("E_BODY_JQ_UNBALANCED", f"legacy jq is unbalanced: {line[:200]}")
            return
        ctx.body_rewrite.append(f"{pattern} {direction}-jq {expression}")
        ctx.bump("body_rewrite")
        return
    if suffix == "json-del":
        tokens = _split_legacy_body(rest)
        paths: list[str] = []
        for token in tokens:
            paths.extend(_flatten_json_paths(token))
        expression = _json_del_expression(paths, ctx, line)
        if expression is None:
            return
        action = f"{direction}-json-del"
        if expression.startswith("delpaths("):
            action = f"{direction}-jq"
        ctx.body_rewrite.append(f"{pattern} {action} {expression}")
        ctx.bump("body_rewrite")
        return
    if suffix in ("json-add", "json-replace"):
        tokens = _split_legacy_body(rest)
        if len(tokens) < 2 or len(tokens) % 2:
            ctx.error("E_BODY_JSON_PAIRS", f"legacy {suffix} expects path value pairs: {line[:200]}")
            return
        rendered: list[str] = []
        for index in range(0, len(tokens), 2):
            path = _normalize_json_path(tokens[index])
            value = tokens[index + 1]
            if not is_stash_json_path(path):
                ctx.error("E_JSON_PAIR_PATH", f"legacy JSON path {path!r} is not a jq identifier", source=line)
                return
            rendered.extend([path, value])
        ctx.body_rewrite.append(f"{pattern} {direction}-{suffix} {' '.join(rendered)}")
        ctx.bump("body_rewrite")
        return
    if suffix == "replace-regex":
        tokens = _split_legacy_body(rest)
        if len(tokens) < 2 or len(tokens) % 2:
            ctx.error("E_BODY_REPLACE_ARGS", f"legacy replace-regex expects regex/value pairs: {line[:200]}")
            return
        pairs: list[str] = []
        for index in range(0, len(tokens), 2):
            regex_text = _unwrap_quotes(tokens[index])
            replacement = _unwrap_quotes(tokens[index + 1])
            replacement = _expand_action_variables(
                replacement, ctx, line, context="legacy body replacement"
            )
            if re.search(r"\s", regex_text) or re.search(r"\s", replacement):
                ctx.error(
                    "E_BODY_REPLACE_SPACE",
                    "Stash body replace-regex cannot quote spaces; refusing to mangle: " + line[:200],
                )
                return
            pairs.extend([regex_text, replacement])
        ctx.body_rewrite.append(f"{pattern} {direction}-replace-regex {' '.join(pairs)}")
        ctx.bump("body_rewrite")
        return
    ctx.error("E_BODY_UNSUPPORTED", f"unsupported legacy body directive {directive!r}: {line[:200]}")


def _split_legacy_body(text: str) -> list[str]:
    """Split legacy body expressions on spaces outside quotes.

    Multi-line jq is already merged by the caller; legacy evil strings are
    single-quoted so spaces inside them survive.
    """

    parts: list[str] = []
    current = ""
    quote = None
    escaped = False
    for char in text:
        if escaped:
            current += char
            escaped = False
            continue
        if char == "\\":
            current += char
            escaped = True
            continue
        if quote:
            current += char
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            current += char
            continue
        if char == " ":
            if current:
                parts.append(current)
                current = ""
            continue
        current += char
    if current:
        parts.append(current)
    return parts


def _unwrap_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        return re.sub(r'\\(["\'\\])', r"\1", inner)
    return value


def _normalize_json_path(raw: str) -> str:
    path = _unwrap_quotes(raw).strip()
    if path.startswith("$."):
        return path[2:]
    return path[1:] if path.startswith(".") else path


# ---------------------------------------------------------------------------
# Scripts
# ---------------------------------------------------------------------------


def _add_script_entry(
    ctx: ConversionContext,
    *,
    phase: str,
    match_pattern: str,
    script_url: str,
    argument,
    options: dict,
    source: str,
    default_require_body: bool = False,
) -> None:
    asset, content, provider_url = _publish_script(script_url, ctx)
    if not provider_url:
        return
    provider_name = _provider_name_for_script(script_url, ctx)
    if provider_name in ctx.providers and ctx.provider_sources.get(provider_name) != script_url:
        # Same basename but different upstream URL: keep deterministic suffix.
        suffix = 1
        base = provider_name
        while provider_name in ctx.providers:
            provider_name = f"{base}-{suffix}"
            suffix += 1
    _register_provider(ctx, provider_name, provider_url, source=script_url)
    entry: dict = {"match": match_pattern, "name": provider_name, "type": phase}
    require_body = _option(options, "requires_body", "requires-body", "require_body", "require-body")
    if require_body is None:
        require_body = default_require_body
    if require_body is not None:
        entry["require-body"] = bool(require_body)
    binary_mode = _option(options, "binary_body_mode", "binary-body-mode", "binary-mode", "binary_mode")
    if binary_mode is not None:
        entry["binary-mode"] = bool(binary_mode)
    timeout = _option(options, "timeout")
    if timeout is not None:
        try:
            entry["timeout"] = int(timeout)
        except (TypeError, ValueError):
            ctx.error("E_SCRIPT_TIMEOUT", f"script timeout is not an integer: {timeout!r}", source=source)
    enable = _option(options, "enable")
    enabled = _resolve_enable(enable, ctx, source)
    if enabled is False:
        ctx.ignore(
            "E_SCRIPT_DISABLED_BY_DEFAULT",
            f"上游 enable=${{...}} 默认关闭，构建时跳过脚本 {script_url}（Stash 无逐脚本 enable 字段；"
            "默认关闭状态已保留，不声称保留运行时开关）",
        )
        return
    if enabled is None:
        return
    if _option(options, "enable") is not None:
        ctx.ignore(
            "E_SCRIPT_ENABLE_RESOLVED",
            f"enable=${{...}} 按构建时默认值 {enabled} 解析；Stash 没有逐脚本 enable 字段，"
            "运行时可切换状态不会被保留（构建清单记录此限制）",
        )
    if argument is not None:
        if not content:
            ctx.error(
                "E_SCRIPT_ARG_UNVERIFIED",
                f"未获取脚本内容，无法确认 $argument 协议，拒绝猜测：{script_url}",
                source=source,
            )
            return
        encoded = _encode_script_argument(argument, content, script_url, ctx, source=source)
        if encoded is None:
            # Argument loss is blocking only when the script actually consumes it.
            if cfg.script_consumes_argument(content):
                return
        else:
            entry["argument"] = encoded
    ctx.script_entries.append(entry)
    ctx.bump("script")


def _option(options: dict, *names):
    for name in names:
        if name in options:
            return _literal_value(options[name]) if not isinstance(options[name], bool) else options[name]
    return None


def convert_scripts(script_lines: list[str], ctx: ConversionContext) -> None:
    for raw in script_lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        expression = None
        try:
            expression = lx.parse_script_expression(line)
        except lx.LoonUnsupportedError as exc:  # type: ignore[attr-defined]
            ctx.error("E_SCRIPT_UNSUPPORTED", f"{getattr(exc, 'detail', exc)} | {line[:200]}")
            continue
        except lx.LoonParseError as exc:  # type: ignore[attr-defined]
            ctx.error("E_SCRIPT_SYNTAX", f"{getattr(exc, 'detail', exc)} | {line[:200]}")
            continue
        if expression is not None:
            phase = _phase(expression)
            match_pattern = _stash_regex(_regex(expression), ctx, line)
            script_url = _string_value(_attr(expression, "script_path", "path", "url"))
            if not script_url:
                ctx.error("E_SCRIPT_PATH", f"script() path is not a literal: {line[:200]}")
                continue
            argument = _attr(expression, "argument", "argument_expr", default=None)
            options = dict(_attr(expression, "options", "params", default={}) or {})
            _add_script_entry(
                ctx,
                phase=phase,
                match_pattern=match_pattern,
                script_url=script_url,
                argument=argument,
                options=options,
                source=line,
            )
            continue
        legacy = lx.parse_legacy_script_line(line) if hasattr(lx, "parse_legacy_script_line") else None
        if legacy is None:
            ctx.error("E_SCRIPT_SYNTAX", f"cannot parse [Script] line: {line[:200]}")
            continue
        _convert_legacy_script(legacy, line, ctx)


def _convert_legacy_script(parsed, line: str, ctx: ConversionContext) -> None:
    phase = str(_attr(parsed, "phase", "type", default="response")).lower()
    if phase not in ("request", "response"):
        phase = "request" if phase.startswith("request") else "response"
    raw_pattern = _attr(parsed, "pattern", "match", default="")
    match_pattern = _legacy_pattern_to_stash(str(raw_pattern))
    params = dict(_attr(parsed, "params", "options", default={}) or {})
    script_url = _attr(parsed, "script_path", "script-path", "path", default="")
    if not script_url:
        ctx.error("E_SCRIPT_PATH", f"legacy script has no script-path: {line[:200]}")
        return
    argument = _attr(parsed, "argument", default=None)
    if isinstance(argument, str) and argument.startswith("[") and argument.endswith("]"):
        # Legacy Loon syntax: argument=[{a},{b}]
        inner = argument[1:-1]
        names = [part.strip().strip("{}").strip() for part in _split_commas(inner)]
        argument = [lx.VariableRef(name) for name in names if name]
    _add_script_entry(
        ctx,
        phase=phase,
        match_pattern=match_pattern,
        script_url=str(script_url),
        argument=argument,
        options=params,
        source=line,
    )


def _legacy_pattern_to_stash(pattern: str) -> str:
    pattern = pattern.strip()
    if len(pattern) >= 2 and pattern[0] == "/" and pattern.rfind("/") > 0:
        end = pattern.rfind("/")
        body, flags = pattern[1:end], pattern[end + 1:]
        body = body.replace("\\/", "/")
        prefix = "".join(f"(?{flag})" for flag in "ims" if flag in flags.lower())
        return prefix + body
    return pattern.replace("\\/", "/")


# ---------------------------------------------------------------------------
# MitM / hosts
# ---------------------------------------------------------------------------


def convert_mitm(mitm_lines: list[str], ctx: ConversionContext) -> None:
    hosts: list[str] = []
    for raw in mitm_lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            _, line = line.split("=", 1)
        for host in line.split(","):
            host = host.strip().strip('"').strip("'")
            if host:
                hosts.append(host)
    hosts.extend(enh.APP_EXTRA_MITM.get(ctx.source_filename, []))
    seen: set[str] = set()
    for host in hosts:
        if host not in seen:
            seen.add(host)
            ctx.mitm.append(host)
    ctx.counts["mitm"] = len(ctx.mitm)


def convert_hosts(host_lines: list[str], ctx: ConversionContext) -> None:
    for raw in host_lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            ctx.hosts[key.strip()] = value.strip()
            ctx.bump("hosts")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def convert_lpx(
    text: str,
    lpx_url: str,
    *,
    resolver: Optional[ResourceResolver],
    public_base: str,
    mirror_scripts: bool = True,
) -> ConversionResult:
    header, sections = parse_lpx(text)
    source_filename = lpx_url.rsplit("/", 1)[-1].split("?")[0] or "plugin.lpx"
    ctx = ConversionContext(
        name=(header.get("name") or "Unnamed").strip(),
        lpx_url=lpx_url,
        source_filename=source_filename,
        public_base=public_base.rstrip("/"),
        resolver=resolver,
    )
    ctx.mirror_scripts = mirror_scripts
    ctx.arguments = parse_arguments(sections, ctx)
    convert_rules(_section(sections, "Rule"), ctx)
    convert_mitm(_section(sections, "MitM"), ctx)
    rewrite_lines = _merge_continuation_lines(_section(sections, "Rewrite"))
    extra_rewrites = _filter_conflicting_extra_rewrites(
        enh.APP_EXTRA_REWRITES.get(source_filename, []), ctx
    )
    convert_rewrites(rewrite_lines + extra_rewrites, ctx)
    convert_scripts(_merge_continuation_lines(_section(sections, "Script")), ctx)
    convert_hosts(_section(sections, "Host"), ctx)
    # Publish generated request scripts through the same content-addressed pipeline.
    _publish_generated_scripts(ctx)
    # force-http-engine is an enhancement for PDD (documented in README).
    force = enh.FORCE_HTTP_ENGINE_OVERRIDES.get(source_filename, [])
    if force:
        ctx.force_http_engine = list(force)
        ctx.add_enhancement("pinduoduo-force-http-engine")
    stash = _assemble_stash(header, ctx)
    return ConversionResult(
        name=ctx.name,
        stash=stash,
        diagnostics=ctx.diagnostics,
        counts=dict(sorted(ctx.counts.items())),
        source_sha256=_sha256_text(text),
        upstream_resources=dict(ctx.resources),
        providers=dict(ctx.providers),
        patches=list(ctx.patches),
        enhancements=list(ctx.enhancements),
        generated_scripts=[{k: v for k, v in item.items() if k != "content"} for item in ctx.generated_scripts],
    )


def _publish_generated_scripts(ctx: ConversionContext) -> None:
    if ctx.resolver is None:
        if ctx.generated_scripts:
            ctx.error("E_SCRIPT_NO_RESOLVER", "generated request scripts require a resource resolver")
        return
    for record in ctx.generated_scripts:
        logical = record["filename"]
        try:
            asset = ctx.resolver.publish_bytes(
                record["content"],
                logical_name=logical,
                source_url=f"generated:{ctx.source_filename}:{record['provider']}",
                kind="script",
                legacy_alias=False,
            )
        except ResourceError as exc:
            ctx.error(exc.code, f"failed to publish generated script {logical}: {exc.detail}")
            continue
        ctx.providers[record["provider"]] = {"url": asset.url, "interval": 86400}
        record["entry"]["name"] = record["provider"]
        ctx.script_entries.append(record["entry"])
        ctx.bump("script")


def _assemble_stash(header: dict, ctx: ConversionContext) -> dict:
    stash: dict = {"name": ctx.name}
    desc = (header.get("desc") or "").replace("\\n", " ").replace("\n", " ").strip()
    if desc:
        stash["desc"] = desc[:500]
    icon = (header.get("icon") or "").strip()
    if icon:
        stash["icon"] = icon
    author = (header.get("author") or "").strip()
    if author:
        stash["author"] = author
    homepage = (header.get("homepage") or "").strip()
    if homepage:
        stash["homepage"] = homepage
    date = (header.get("date") or "").strip()
    if date:
        stash["date"] = date
    if ctx.rules:
        stash["rules"] = ctx.rules
    http: dict = {}
    if ctx.force_http_engine:
        http["force-http-engine"] = ctx.force_http_engine
    if ctx.mitm:
        http["mitm"] = ctx.mitm
    if ctx.url_rewrite:
        http["url-rewrite"] = ctx.url_rewrite
    if ctx.header_rewrite:
        http["header-rewrite"] = ctx.header_rewrite
    if ctx.body_rewrite:
        http["body-rewrite"] = ctx.body_rewrite
    if ctx.mock:
        http["mock"] = ctx.mock
    if ctx.script_entries:
        http["script"] = ctx.script_entries
    if ctx.providers:
        stash["script-providers"] = dict(sorted(ctx.providers.items()))
    if http:
        stash["http"] = http
    if ctx.hosts:
        stash["hosts"] = ctx.hosts
    return stash


# ---------------------------------------------------------------------------
# Multi-line continuation (old plugins wrap long jq programs across lines)
# ---------------------------------------------------------------------------


def _needs_continuation(text: str) -> bool:
    return not jq_balanced(text)


def _merge_continuation_lines(entries: Iterable[str]) -> list[str]:
    merged: list[str] = []
    buffer = ""
    for entry in entries:
        candidate = f"{buffer}\n{entry}" if buffer else entry
        if _needs_continuation(candidate):
            buffer = candidate
            continue
        merged.append(candidate)
        buffer = ""
    if buffer:
        merged.append(buffer)
    return merged


def _sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()
