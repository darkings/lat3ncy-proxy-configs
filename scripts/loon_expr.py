#!/usr/bin/env python3
"""Typed tokenizer/parser for Loon 3.5.1 (978)+ plugin expression syntax.

This module is the single parser used by the KeLee Loon→Stash converter.  It
understands the *new* Loon syntax (``request|response if <condition> then
<action> [| <action>...]``) as well as the legacy ``[Script]`` declaration
forms and the typed ``[Argument]`` declarations.  It never rewrites a line with
broad regular expressions: string literals, raw strings, regex literals,
``${...}`` variables and nested ``[]``/``{}``/``()`` are scanned structurally so
nested payloads (jq programs, JSON bodies, object arguments) survive intact.

Grammar implemented (Loon docs snapshot ``loon-references_rewrite.md``)::

    rewrite      := phase "if" url_condition "then" action ("|" action)*
    url_condition:= "${url}" "~=" regex ("as" IDENT)?
    phase        := "request" | "response"
    action       := NAME "(" [value ("," value)*] ")"
    script_expr  := phase "if" url_condition "then"
                    "script" "(" string ["," value] ")" ("with" option ("," option)*)?
    option       := KEY "=" value
    value        := string | raw_string | regex | number | "true" | "false"
                  | "null" | variable | array | object
    string       := '"' (escaped char | ${template})* '"'
    raw_string   := '`' (char | '``')* '`'
    regex        := '/' pattern '/' flags?

Deliberately supported *condition subset*: exactly one ``${url} ~= /re/flags``
condition, optionally bound with ``as <name>``.  Any other valid new-syntax
condition (``${request.method} == "POST"``, ``&&``/``||`` combinations,
``${response.status}``, a regex supplied by a parameter, ...) raises
:class:`LoonUnsupportedError` instead of being silently mis-converted: Stash
rewrites can only key on the URL, so the converter must fail loudly.

Actions outside the documented Loon action table, unknown script options and
capture references whose index exceeds the regex capture count are likewise
explicit errors.  Non-expression/legacy lines return ``None`` so the caller can
route them to its legacy path.

The module is stdlib-only and has no side effects (safe to import from tests,
the converter and the validator).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Optional, Sequence

__all__ = [
    # errors
    "LoonParseError",
    "LoonUnsupportedError",
    "format_error",
    # value model
    "TemplateRef",
    "StringLiteral",
    "RawStringLiteral",
    "RegexLiteral",
    "VariableRef",
    "NumberLiteral",
    "BoolLiteral",
    "NullLiteral",
    "ArrayLiteral",
    "ObjectLiteral",
    "Call",
    # parsed expressions
    "Action",
    "RewriteExpr",
    "ScriptExpr",
    "LegacyScriptLine",
    "ArgumentDecl",
    # parsing entry points
    "parse_value",
    "parse_call",
    "split_top_level",
    "parse_rewrite_expression",
    "parse_script_expression",
    "parse_legacy_script_line",
    "parse_argument_decl",
    # helpers
    "stash_regex",
    "format_regex_error_flags",
    "count_capture_groups",
    "value_to_python",
    # constants
    "LOON_REGEX_FLAGS",
    "MOCK_CONTENT_TYPES",
    "SCRIPT_OPTION_KEYS",
    "SUPPORTED_ACTIONS",
]

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

_ERROR_SNIPPET_LIMIT = 240


def format_error(code: str, detail: str, line: str = "") -> str:
    """Human-readable blocking diagnostic: ``CODE: detail | input: ...``.

    ``line`` is the offending source text (usually the whole plugin line).  It
    is collapsed to a single line and truncated so a 4 KiB jq payload does not
    swamp build logs, while the full text stays available on the exception.
    """
    message = f"{code}: {detail}" if code else str(detail)
    snippet = " ".join((line or "").split())
    if snippet:
        if len(snippet) > _ERROR_SNIPPET_LIMIT:
            snippet = snippet[: _ERROR_SNIPPET_LIMIT - 3] + "..."
        message = f"{message} | input: {snippet}"
    return message


class LoonParseError(ValueError):
    """A Loon line is invalid or cannot be parsed without guessing.

    Attributes
    ----------
    code:
        Stable machine-readable diagnostic code (``E_...``), suitable for the
        converter's diagnostics/manifest output.
    detail:
        Human-readable explanation without the trailing input snippet.
    text:
        The offending source text (whole line/expression where available).
    position:
        Optional character offset inside ``text`` where the problem was found.
    """

    default_code = "E_LOON_PARSE"

    def __init__(
        self,
        code: str,
        detail: str,
        text: str = "",
        *,
        position: Optional[int] = None,
    ) -> None:
        self.code = code or self.default_code
        self.detail = detail
        self.text = text
        self.position = position
        super().__init__(format_error(self.code, detail, text))

    def __str__(self) -> str:  # pragma: no cover - ValueError already formats it
        return format_error(self.code, self.detail, self.text)


class LoonUnsupportedError(LoonParseError):
    """Valid Loon construct that this parser/converter does not support.

    Kept as a distinct subclass so callers can decide whether an unsupported
    construct is a blocking build error (it is, for the KeLee conversion) or an
    explicitly allow-listed ignore.
    """

    default_code = "E_LOON_UNSUPPORTED"


# ---------------------------------------------------------------------------
# Value model
# ---------------------------------------------------------------------------

LOON_REGEX_FLAGS = "ims"
"""Flags Loon itself documents for ``/pattern/flags`` (``i``, ``m``, ``s``)."""

MOCK_CONTENT_TYPES = frozenset(
    {
        "json",
        "text",
        "css",
        "html",
        "javascript",
        "plain",
        "png",
        "gif",
        "jpeg",
        "tiff",
        "svg",
        "mp4",
        "form-data",
    }
)
"""Content types accepted by ``request/response.body.mock*`` (Loon docs)."""

_CAPTURE_REF_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\.(\d+)\Z")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BARE_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*")
_CALL_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_NUMBER_RE = re.compile(r"-?(?:\d+\.\d+|\d+|\.\d+)(?:[eE][+-]?\d+)?")
_ARGUMENT_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*\Z")
_SCRIPT_OPTION_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_\-]*\Z")


@dataclass(frozen=True)
class TemplateRef:
    """A ``${...}`` interpolation found inside a double-quoted string."""

    name: str
    raw: str = ""

    @property
    def capture(self) -> Optional[tuple[str, int]]:
        """``("urlMatch", 2)`` when this is a ``${urlMatch.2}`` capture ref."""
        match = _CAPTURE_REF_RE.fullmatch(self.name)
        if match is None:
            return None
        return match.group(1), int(match.group(2))


@dataclass(frozen=True)
class StringLiteral:
    """Double-quoted Loon string, decoded; ``templates`` keeps ``${...}`` refs.

    Escapes handled per Loon docs: ``\\"``, ``\\\\``, ``\\n``, ``\\r``, ``\\t``
    and ``\\${`` (literal ``${``).  Unknown escapes are preserved verbatim so
    no byte of a payload is silently dropped.  ``raw`` keeps the source form.
    """

    value: str
    raw: str = ""
    templates: tuple[TemplateRef, ...] = ()

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class RawStringLiteral:
    """Backtick raw string.  No escapes and no ``${...}`` expansion.

    A doubled backtick inside the literal denotes one literal backtick.
    """

    value: str
    raw: str = ""

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class RegexLiteral:
    """Loon ``/pattern/flags`` literal; ``pattern`` keeps regex escapes intact."""

    pattern: str
    flags: str = ""
    raw: str = ""

    @property
    def group_count(self) -> int:
        return count_capture_groups(self.pattern)


@dataclass(frozen=True)
class VariableRef:
    """Bare ``${...}`` value (built-in variable, plugin parameter or capture)."""

    name: str
    raw: str = ""

    def __str__(self) -> str:
        return "${" + self.name + "}"


@dataclass(frozen=True)
class NumberLiteral:
    value: Any  # int | float
    raw: str = ""


@dataclass(frozen=True)
class BoolLiteral:
    value: bool


@dataclass(frozen=True)
class NullLiteral:
    pass


@dataclass(frozen=True)
class ArrayLiteral:
    items: tuple = ()
    raw: str = ""

    def __len__(self) -> int:
        return len(self.items)


@dataclass(frozen=True)
class ObjectLiteral:
    """Object literal; shorthand ``{${a}, ${b}}`` entries keep ``key='a'``."""

    entries: tuple = ()  # tuple[tuple[str, Any], ...]
    raw: str = ""

    def keys(self) -> tuple[str, ...]:
        return tuple(key for key, _ in self.entries)

    def items(self) -> tuple[tuple[str, Any], ...]:
        return tuple(self.entries)

    def get(self, key: str, default: Any = None) -> Any:
        for entry_key, value in self.entries:
            if entry_key == key:
                return value
        return default

    def as_dict(self) -> dict:
        return {key: value_to_python(value) for key, value in self.entries}


@dataclass(frozen=True)
class Call:
    """A parsed ``name(arg, arg...)`` call (action or ``script(...)``)."""

    name: str
    args: tuple = ()
    raw: str = ""


# ---------------------------------------------------------------------------
# Parsed expression model
# ---------------------------------------------------------------------------


def _string_arg(action_name: str, args: tuple, index: int) -> Optional[str]:
    if index >= len(args):
        return None
    value = args[index]
    if isinstance(value, (StringLiteral, RawStringLiteral)):
        return value.value
    return None


def _literal_arg(args: tuple, index: int, kind: type) -> Any:
    if index >= len(args):
        return None
    value = args[index]
    return value if isinstance(value, kind) else None


@dataclass(frozen=True)
class Action:
    """One action call of a rewrite expression, with convenience accessors."""

    name: str
    args: tuple = ()
    raw: str = ""

    def arg(self, index: int, default: Any = None) -> Any:
        if 0 <= index < len(self.args):
            return self.args[index]
        return default

    def python_args(self) -> tuple:
        return tuple(value_to_python(value) for value in self.args)

    def string(self, index: int, default: Optional[str] = None) -> Optional[str]:
        value = _string_arg(self.name, self.args, index)
        return default if value is None else value

    def regex(self, index: int) -> Optional[RegexLiteral]:
        return _literal_arg(self.args, index, RegexLiteral)

    def number(self, index: int) -> Optional[Any]:
        literal = _literal_arg(self.args, index, NumberLiteral)
        return None if literal is None else literal.value

    def boolean(self, index: int) -> Optional[bool]:
        literal = _literal_arg(self.args, index, BoolLiteral)
        return None if literal is None else literal.value


@dataclass(frozen=True)
class RewriteExpr:
    """``<phase> if ${url} ~= /.../flags [as name] then <action> [| ...]``."""

    phase: str
    regex: RegexLiteral
    actions: tuple = ()
    capture_name: Optional[str] = None
    raw: str = ""

    @property
    def action_names(self) -> tuple[str, ...]:
        return tuple(action.name for action in self.actions)

    def action(self, name: str) -> Optional[Action]:
        wanted = name.lower()
        for action in self.actions:
            if action.name == wanted:
                return action
        return None

    def actions_named(self, name: str) -> tuple[Action, ...]:
        wanted = name.lower()
        return tuple(action for action in self.actions if action.name == wanted)

    @property
    def mock(self) -> Optional[Action]:
        for name in ("response.body.mock", "response.body.mock_file"):
            action = self.action(name)
            if action is not None:
                return action
        for name in ("request.body.mock", "request.body.mock_file"):
            action = self.action(name)
            if action is not None:
                return action
        return None

    @property
    def has_mock(self) -> bool:
        return self.mock is not None

    def header_actions(self) -> tuple[Action, ...]:
        return tuple(action for action in self.actions if ".header." in action.name)

    @property
    def url_actions(self) -> tuple[Action, ...]:
        return tuple(action for action in self.actions if not _is_sided(action.name))


@dataclass(frozen=True)
class ScriptExpr:
    """New-syntax ``script(...)`` declaration inside ``[Script]``."""

    phase: str
    regex: RegexLiteral
    script_path: str
    argument_expr: Any = None
    options: Mapping[str, Any] = field(default_factory=dict)
    capture_name: Optional[str] = None
    raw: str = ""

    def option(self, key: str, default: Any = None) -> Any:
        """Option as a Python value (``VariableRef`` for ``${param}`` values)."""
        return dict(self.options).get(key, default)

    @property
    def argument_keys(self) -> tuple[str, ...]:
        if isinstance(self.argument_expr, ObjectLiteral):
            return self.argument_expr.keys()
        return ()

    @property
    def has_argument(self) -> bool:
        if self.argument_expr is None:
            return False
        if isinstance(self.argument_expr, ObjectLiteral):
            return bool(self.argument_expr.entries)
        return True


@dataclass(frozen=True)
class LegacyScriptLine:
    """Legacy ``[Script]`` declaration (``http-request``/``cron``/... forms).

    ``options`` keys are normalized (``requires-body`` → ``requires_body``) and
    values are decoded to bool/int/float/str.  The ``type``/``phase``/
    ``pattern``/``params``/``argument`` aliases mirror the old converter's dict
    shape so downstream integration code can consume either representation.
    """

    kind: str
    script_path: str = ""
    match: str = ""
    cron: str = ""
    options: Mapping[str, Any] = field(default_factory=dict)
    raw: str = ""

    def option(self, key: str, default: Any = None) -> Any:
        return dict(self.options).get(key, default)

    @property
    def type(self) -> str:
        """Old-converter ``type`` alias for :attr:`kind`."""
        return self.kind

    @property
    def phase(self) -> str:
        """``request``/``response`` for the HTTP forms, ``""`` otherwise."""
        if self.kind == "http-request":
            return "request"
        if self.kind == "http-response":
            return "response"
        return ""

    @property
    def pattern(self) -> str:
        return self.match

    @property
    def params(self) -> Mapping[str, Any]:
        return self.options

    @property
    def argument(self) -> Any:
        return dict(self.options).get("argument")


@dataclass(frozen=True)
class ArgumentDecl:
    """One ``[Argument]`` declaration: ``name = kind,value...,tag=...,desc=...``."""

    name: str
    kind: str
    default_raw: str = ""
    options: tuple = ()
    attrs: Mapping[str, str] = field(default_factory=dict)
    raw: str = ""

    @property
    def type_name(self) -> str:
        return str(self.attrs.get("type", "")).strip().lower()

    @property
    def tag(self) -> str:
        return str(self.attrs.get("tag", ""))

    @property
    def desc(self) -> str:
        return str(self.attrs.get("desc", ""))

    def default_typed(self) -> Any:
        """Default value with its declared runtime type (bool/int/float/str)."""
        if self.kind == "switch":
            decoded = _decode_argument_token(self.default_raw)
            if isinstance(decoded, bool):
                return decoded
            raise LoonParseError(
                "E_ARGUMENT_DEFAULT",
                f"switch parameter {self.name!r} default {self.default_raw!r} is not a boolean",
                self.raw,
            )
        if self.type_name == "number":
            return _coerce_number(self.default_raw, self.raw)
        decoded = _decode_argument_token(self.default_raw)
        if isinstance(decoded, str):
            return decoded
        return self.default_raw


def value_to_python(value: Any) -> Any:
    """Convert a parsed literal to the nearest Python value.

    ``VariableRef`` and ``RegexLiteral`` are returned unchanged because they
    carry semantics a plain ``str`` would lose.
    """
    if isinstance(value, StringLiteral):
        return value.value
    if isinstance(value, RawStringLiteral):
        return value.value
    if isinstance(value, NumberLiteral):
        return value.value
    if isinstance(value, BoolLiteral):
        return value.value
    if isinstance(value, NullLiteral):
        return None
    if isinstance(value, ArrayLiteral):
        return [value_to_python(item) for item in value.items]
    if isinstance(value, ObjectLiteral):
        return {key: value_to_python(item) for key, item in value.entries}
    if isinstance(value, (VariableRef, RegexLiteral)):
        return value
    if isinstance(value, Call):
        return value
    return value


# ---------------------------------------------------------------------------
# Low-level scanners (strings / raw strings / regex / templates)
# ---------------------------------------------------------------------------


def _scan_brace_block(text: str, pos: int) -> int:
    """Return the index just past the ``}`` matching a ``{`` at ``pos``.

    Brace contents may contain single-quoted names (``${header['x']}``); nested
    braces are balanced.  Used for ``${...}`` templates *and* the legacy
    ``cron {cronExpression}`` placeholder form.
    """
    start = pos
    if pos >= len(text) or text[pos] != "{":
        raise LoonParseError(
            "E_BRACE", "expected '{'", text, position=pos
        )
    i = pos + 1
    depth = 1
    while i < len(text):
        char = text[i]
        if char == "'":  # single-quoted header name inside {...}
            i += 1
            while i < len(text) and text[i] != "'":
                i += 2 if text[i] == "\\" else 1
            if i >= len(text):
                raise LoonParseError(
                    "E_BRACE_UNTERMINATED",
                    "unterminated single-quoted name inside {...}",
                    text,
                    position=start,
                )
            i += 1
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise LoonParseError(
        "E_BRACE_UNTERMINATED",
        "unterminated {...} block",
        text,
        position=start,
    )


def _scan_template_end(text: str, pos: int) -> int:
    """Return the index just past the ``}`` of a ``${...}`` starting at ``pos``."""
    if not text.startswith("${", pos):
        raise LoonParseError(
            "E_VARIABLE", "expected a ${...} variable reference", text, position=pos
        )
    return _scan_brace_block(text, pos + 1)


def _scan_double_quoted_full(
    text: str, pos: int
) -> tuple[str, tuple[TemplateRef, ...], str, int]:
    """Decode a double-quoted Loon string starting at ``pos``.

    Returns ``(value, templates, raw, end)``.  Templates are recorded for
    unescaped ``${...}`` occurrences only (``\\${`` is a literal ``${``).
    """
    start = pos
    if pos >= len(text) or text[pos] != '"':
        raise LoonParseError(
            "E_STRING", "expected a double-quoted string", text, position=pos
        )
    i = pos + 1
    out: list[str] = []
    templates: list[TemplateRef] = []
    while i < len(text):
        char = text[i]
        if char == '"':
            return "".join(out), tuple(templates), text[start : i + 1], i + 1
        if char == "\\":
            if i + 1 >= len(text):
                raise LoonParseError(
                    "E_STRING_UNTERMINATED",
                    "double-quoted string ends with a dangling backslash",
                    text,
                    position=start,
                )
            nxt = text[i + 1]
            if nxt == "\\":
                out.append("\\")
                i += 2
                continue
            if nxt == '"':
                out.append('"')
                i += 2
                continue
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == "r":
                out.append("\r")
                i += 2
                continue
            if nxt == "t":
                out.append("\t")
                i += 2
                continue
            if nxt == "$" and text.startswith("${", i + 1):
                out.append("${")
                i += 3
                continue
            # Unknown escape: keep both characters verbatim (no silent loss).
            out.append("\\")
            out.append(nxt)
            i += 2
            continue
        if char == "$" and text.startswith("${", i):
            end = _scan_template_end(text, i)
            raw = text[i:end]
            templates.append(TemplateRef(name=text[i + 2 : end - 1].strip(), raw=raw))
            out.append(raw)
            i = end
            continue
        out.append(char)
        i += 1
    raise LoonParseError(
        "E_STRING_UNTERMINATED",
        "unterminated double-quoted string",
        text,
        position=start,
    )


def _scan_raw_string_full(text: str, pos: int) -> tuple[str, str, int]:
    """Decode a backtick raw string starting at ``pos``; `` `` `` yields a backtick."""
    start = pos
    if pos >= len(text) or text[pos] != "`":
        raise LoonParseError(
            "E_RAW_STRING", "expected a backtick raw string", text, position=pos
        )
    i = pos + 1
    out: list[str] = []
    while i < len(text):
        char = text[i]
        if char == "`":
            if text.startswith("``", i):
                out.append("`")
                i += 2
                continue
            return "".join(out), text[start : i + 1], i + 1
        out.append(char)
        i += 1
    raise LoonParseError(
        "E_RAW_STRING_UNTERMINATED",
        "unterminated backtick raw string",
        text,
        position=start,
    )


def _scan_regex_full(text: str, pos: int) -> tuple[str, str, str, int]:
    """Scan ``/pattern/flags`` starting at ``pos``.

    Returns ``(pattern, flags, raw, end)``.  Regex escapes are preserved
    verbatim (``pattern``); ``\\/`` is an escaped slash, not a delimiter, and a
    ``/`` inside a ``[...]`` character class does not terminate the literal.
    """
    start = pos
    if pos >= len(text) or text[pos] != "/":
        raise LoonParseError(
            "E_REGEX", "expected a /pattern/flags regex literal", text, position=pos
        )
    i = pos + 1
    chars: list[str] = []
    in_class = False
    while i < len(text):
        char = text[i]
        if char == "\\":
            if i + 1 >= len(text):
                raise LoonParseError(
                    "E_REGEX_UNTERMINATED",
                    "regex literal ends with a dangling backslash",
                    text,
                    position=start,
                )
            chars.append(char)
            chars.append(text[i + 1])
            i += 2
            continue
        if in_class:
            chars.append(char)
            if char == "]":
                in_class = False
            i += 1
            continue
        if char == "[":
            in_class = True
            chars.append(char)
            i += 1
            continue
        if char == "/":
            j = i + 1
            while j < len(text) and text[j].isalpha():
                j += 1
            return "".join(chars), text[i + 1 : j], text[start:j], j
        chars.append(char)
        i += 1
    raise LoonParseError(
        "E_REGEX_UNTERMINATED",
        "unterminated /pattern/flags regex literal",
        text,
        position=start,
    )


def _scan_string_end(text: str, pos: int) -> int:
    """End offset (exclusive) of the double-quoted string at ``pos``."""
    return _scan_double_quoted_full(text, pos)[3]


def _scan_raw_string_end(text: str, pos: int) -> int:
    return _scan_raw_string_full(text, pos)[2]


def _scan_regex_end(text: str, pos: int) -> int:
    return _scan_regex_full(text, pos)[3]


def _skip_single_quoted(text: str, pos: int) -> int:
    """Skip a single-quoted header name (only legal inside ``${...}``)."""
    i = pos + 1
    while i < len(text) and text[i] != "'":
        i += 2 if text[i] == "\\" else 1
    return i + 1


def _find_top_level_char(text: str, char: str, *, regex_literals: bool = True) -> int:
    """First top-level occurrence of ``char`` outside strings/brackets; -1 if none."""
    i = 0
    stack: list[str] = []
    while i < len(text):
        current = text[i]
        if current == '"':
            i = _scan_string_end(text, i)
            continue
        if current == "`":
            i = _scan_raw_string_end(text, i)
            continue
        if current == "'":
            i = _skip_single_quoted(text, i)
            continue
        if regex_literals and current == "/":
            i = _scan_regex_end(text, i)
            continue
        if current in "([{":
            stack.append(current)
            i += 1
            continue
        if current in ")]}":
            if stack:
                stack.pop()
            i += 1
            continue
        if current == char and not stack:
            return i
        i += 1
    return -1


def _match_keyword(text: str, pos: int, word: str) -> Optional[int]:
    """Return the end offset when ``word`` matches at ``pos`` with boundaries."""
    lowered = word.lower()
    if text[pos : pos + len(lowered)].lower() != lowered:
        return None
    end = pos + len(lowered)
    before_ok = pos == 0 or not (
        text[pos - 1].isalnum() or text[pos - 1] in "_.$-"
    )
    after_ok = end >= len(text) or not (
        text[end].isalnum() or text[end] in "_.$-"
    )
    return end if before_ok and after_ok else None


def _find_top_level_keyword(
    text: str, word: str, start: int = 0, *, regex_literals: bool = True
) -> Optional[tuple[int, int]]:
    """Find a top-level keyword; returns ``(start_offset, end_offset)``."""
    i = start
    stack: list[str] = []
    while i < len(text):
        current = text[i]
        if current == '"':
            i = _scan_string_end(text, i)
            continue
        if current == "`":
            i = _scan_raw_string_end(text, i)
            continue
        if current == "'":
            i = _skip_single_quoted(text, i)
            continue
        if regex_literals and current == "/":
            i = _scan_regex_end(text, i)
            continue
        if current in "([{":
            stack.append(current)
            i += 1
            continue
        if current in ")]}":
            if stack:
                stack.pop()
            i += 1
            continue
        if not stack and (current.isalpha() or current == "_"):
            match = _IDENT_RE.match(text, i)
            if match is not None:
                end = match.end()
                if match.group(0).lower() == word and _match_keyword(text, i, word):
                    return i, end
                i = end
                continue
        i += 1
    return None


def split_top_level(
    text: str, sep: str = ",", *, regex_literals: bool = True
) -> list[str]:
    """Split on a single-character separator at bracket depth zero.

    Double-quoted strings (with escapes), backtick raw strings, ``${...}``
    templates, ``/regex/flags`` literals and nested ``()``/``[]``/``{}`` are
    honoured, so commas/separators inside JSON bodies, jq programs, regex
    character classes and object arguments never split an item.

    ``regex_literals=False`` disables ``/.../`` recognition for legacy
    space/URL oriented lines (``script-path=https://...``) where a slash is
    ordinary text.  Parts are stripped; empty input yields ``[]``.
    """
    if not isinstance(text, str):
        raise TypeError("split_top_level() expects str")
    if not isinstance(sep, str) or len(sep) != 1:
        raise ValueError("split_top_level() expects a single-character separator")
    if not text:
        return []
    parts: list[str] = []
    start = 0
    i = 0
    stack: list[str] = []
    while i < len(text):
        char = text[i]
        if char == '"':
            i = _scan_string_end(text, i)
            continue
        if char == "`":
            i = _scan_raw_string_end(text, i)
            continue
        if char == "'":
            i = _skip_single_quoted(text, i)
            continue
        if regex_literals and char == "/":
            i = _scan_regex_end(text, i)
            continue
        if char in "([{":
            stack.append(char)
            i += 1
            continue
        if char in ")]}":
            if stack:
                stack.pop()
            i += 1
            continue
        if char == sep and not stack:
            parts.append(text[start:i].strip())
            start = i + 1
        i += 1
    parts.append(text[start:].strip())
    return parts


# ---------------------------------------------------------------------------
# Value / call parser
# ---------------------------------------------------------------------------


class _Parser:
    """Recursive-descent parser for Loon values and calls."""

    def __init__(self, text: str, pos: int = 0) -> None:
        self.text = text
        self.pos = pos

    # -- diagnostics -----------------------------------------------------
    def error(self, code: str, detail: str, *, position: Optional[int] = None) -> None:
        raise LoonParseError(
            code,
            detail,
            self.text,
            position=self.pos if position is None else position,
        )

    def unsupported(
        self, code: str, detail: str, *, position: Optional[int] = None
    ) -> None:
        raise LoonUnsupportedError(
            code,
            detail,
            self.text,
            position=self.pos if position is None else position,
        )

    # -- cursor helpers --------------------------------------------------
    def eof(self) -> bool:
        return self.pos >= len(self.text)

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def skip_ws(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] in " \t\r\n":
            self.pos += 1

    def expect_char(self, char: str, code: str, detail: str) -> None:
        if self.eof() or self.text[self.pos] != char:
            found = "end of input" if self.eof() else repr(self.peek())
            self.error(code, f"{detail}; expected {char!r}, found {found}")

    def read_identifier(self, code: str, detail: str) -> str:
        match = _IDENT_RE.match(self.text, self.pos)
        if match is None:
            self.error(code, detail)
        self.pos = match.end()
        return match.group(0)

    # -- values ----------------------------------------------------------
    def parse_value(self) -> Any:
        self.skip_ws()
        if self.eof():
            self.error("E_VALUE", "expected a value, found end of input")
        char = self.peek()
        if char == '"':
            return self.parse_string()
        if char == "`":
            return self.parse_raw_string()
        if char == "/":
            return self.parse_regex()
        if char == "$":
            return self.parse_variable()
        if char == "[":
            return self.parse_array()
        if char == "{":
            return self.parse_object()
        if char in "-0123456789":
            return self.parse_number()
        if char.isalpha() or char == "_":
            word_match = _IDENT_RE.match(self.text, self.pos)
            word = word_match.group(0) if word_match is not None else char
            lowered = word.lower()
            if lowered == "true":
                self.pos += len(word)
                return BoolLiteral(True)
            if lowered == "false":
                self.pos += len(word)
                return BoolLiteral(False)
            if lowered == "null":
                self.pos += len(word)
                return NullLiteral()
            self.error(
                "E_VALUE",
                f"unexpected bare token {word!r}; fixed Loon strings must be "
                "double-quoted",
            )
        self.error("E_VALUE", f"unexpected character {char!r} while parsing a value")

    def parse_string(self) -> StringLiteral:
        value, templates, raw, end = _scan_double_quoted_full(self.text, self.pos)
        self.pos = end
        return StringLiteral(value=value, raw=raw, templates=templates)

    def parse_raw_string(self) -> RawStringLiteral:
        value, raw, end = _scan_raw_string_full(self.text, self.pos)
        self.pos = end
        return RawStringLiteral(value=value, raw=raw)

    def parse_regex(self) -> RegexLiteral:
        pattern, flags, raw, end = _scan_regex_full(self.text, self.pos)
        normalized = _validate_regex_flags(flags, self.text, self.pos)
        self.pos = end
        return RegexLiteral(pattern=pattern, flags=normalized, raw=raw)

    def parse_number(self) -> NumberLiteral:
        match = _NUMBER_RE.match(self.text, self.pos)
        if match is None:
            self.error("E_NUMBER", "expected a number")
        raw = match.group(0)
        self.pos = match.end()
        if "." in raw or "e" in raw.lower():
            return NumberLiteral(value=float(raw), raw=raw)
        return NumberLiteral(value=int(raw), raw=raw)

    def parse_variable(self) -> VariableRef:
        start = self.pos
        end = _scan_template_end(self.text, start)
        inner = self.text[start + 2 : end - 1].strip()
        if not inner:
            self.error("E_VARIABLE_EMPTY", "empty ${} variable reference", position=start)
        self.pos = end
        return VariableRef(name=inner, raw=self.text[start:end])

    def parse_array(self) -> ArrayLiteral:
        start = self.pos
        self.expect_char("[", "E_ARRAY", "invalid array literal")
        self.pos += 1
        items: list[Any] = []
        self.skip_ws()
        if not self.eof() and self.peek() == "]":
            self.pos += 1
            return ArrayLiteral(items=tuple(), raw=self.text[start : self.pos])
        while True:
            items.append(self.parse_value())
            self.skip_ws()
            if self.eof():
                self.error("E_ARRAY", "unterminated array literal")
            char = self.peek()
            if char == ",":
                self.pos += 1
                self.skip_ws()
                if not self.eof() and self.peek() == "]":
                    self.error("E_ARRAY", "trailing comma in array literal")
                continue
            if char == "]":
                self.pos += 1
                break
            self.error(
                "E_ARRAY", f"expected ',' or ']' in array literal, found {char!r}"
            )
        return ArrayLiteral(items=tuple(items), raw=self.text[start : self.pos])

    def parse_object(self) -> ObjectLiteral:
        start = self.pos
        self.expect_char("{", "E_OBJECT", "invalid object literal")
        self.pos += 1
        entries: list[tuple[str, Any]] = []
        self.skip_ws()
        if not self.eof() and self.peek() == "}":
            self.pos += 1
            return ObjectLiteral(entries=tuple(), raw=self.text[start : self.pos])
        while True:
            self.skip_ws()
            if self.eof():
                self.error("E_OBJECT", "unterminated object literal")
            char = self.peek()
            if char == "$":
                reference = self.parse_variable()
                key = reference.name
                value: Any = reference
            elif char in ('"', "`"):
                key_literal = self.parse_value()
                key = str(value_to_python(key_literal))
                self.skip_ws()
                self.expect_char(":", "E_OBJECT", "expected ':' after object key")
                self.pos += 1
                value = self.parse_value()
            elif char.isalpha() or char == "_":
                match = _BARE_KEY_RE.match(self.text, self.pos)
                if match is None:
                    self.error("E_OBJECT", "invalid object key")
                key = match.group(0)
                self.pos = match.end()
                self.skip_ws()
                self.expect_char(":", "E_OBJECT", "expected ':' after object key")
                self.pos += 1
                value = self.parse_value()
            else:
                self.error("E_OBJECT", f"unexpected character {char!r} in object literal")
            if not key:
                self.error("E_OBJECT", "empty object key")
            entries.append((key, value))
            self.skip_ws()
            if self.eof():
                self.error("E_OBJECT", "unterminated object literal")
            char = self.peek()
            if char == ",":
                self.pos += 1
                self.skip_ws()
                if not self.eof() and self.peek() == "}":
                    self.error("E_OBJECT", "trailing comma in object literal")
                continue
            if char == "}":
                self.pos += 1
                break
            self.error(
                "E_OBJECT", f"expected ',' or '}}' in object literal, found {char!r}"
            )
        return ObjectLiteral(entries=tuple(entries), raw=self.text[start : self.pos])

    def parse_call(self) -> Call:
        start = self.pos
        self.skip_ws()
        match = _CALL_NAME_RE.match(self.text, self.pos)
        if match is None:
            self.error("E_CALL", "expected an action call such as name(...)")
        name = match.group(0)
        self.pos = match.end()
        self.skip_ws()
        if self.eof() or self.peek() != "(":
            self.error("E_CALL", f"expected '(' after {name!r}")
        self.pos += 1
        args: list[Any] = []
        self.skip_ws()
        if self.eof():
            self.error("E_CALL", f"unterminated call {name}(...)")
        if self.peek() == ")":
            self.pos += 1
            return Call(name=name, args=tuple(), raw=self.text[start : self.pos].strip())
        while True:
            args.append(self.parse_value())
            self.skip_ws()
            if self.eof():
                self.error("E_CALL", f"unterminated call {name}(...)")
            char = self.peek()
            if char == ",":
                self.pos += 1
                self.skip_ws()
                if not self.eof() and self.peek() == ")":
                    self.error("E_CALL", f"trailing comma in {name}(...) call")
                continue
            if char == ")":
                self.pos += 1
                break
            self.error(
                "E_CALL",
                f"expected ',' or ')' in {name}(...) call, found {char!r}",
            )
        return Call(name=name, args=tuple(args), raw=self.text[start : self.pos].strip())


def parse_value(text: str, pos: int = 0) -> tuple[Any, int]:
    """Parse one Loon value at ``pos``; return ``(value, new_pos)``."""
    parser = _Parser(text, pos)
    value = parser.parse_value()
    return value, parser.pos


def parse_call(text: str) -> Call:
    """Parse a complete ``name(arg, ...)`` call with no trailing text."""
    parser = _Parser(text)
    parser.skip_ws()
    call = parser.parse_call()
    parser.skip_ws()
    if not parser.eof():
        parser.error(
            "E_CALL_TRAILING", "unexpected text after the call expression"
        )
    return call


def _parse_complete_value(text: str, code: str, detail: str) -> Any:
    parser = _Parser(text)
    parser.skip_ws()
    if parser.eof():
        parser.error(code, detail)
    value = parser.parse_value()
    parser.skip_ws()
    if not parser.eof():
        parser.error(code, f"{detail}; unexpected trailing text")
    return value


# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------


def _validate_regex_flags(flags: str, text: str = "", position: Optional[int] = None) -> str:
    """Validate Loon regex flags (``i``/``m``/``s`` only) and canonicalize."""
    seen: set[str] = set()
    for flag in flags or "":
        lowered = flag.lower()
        if lowered not in LOON_REGEX_FLAGS:
            raise LoonUnsupportedError(
                "E_REGEX_FLAGS",
                f"unsupported Loon regex flag {flag!r} in /.../{flags}; Loon only "
                f"supports {', '.join(LOON_REGEX_FLAGS)}",
                text,
                position=position,
            )
        seen.add(lowered)
    return "".join(flag for flag in LOON_REGEX_FLAGS if flag in seen)


def format_regex_error_flags(flags: str) -> str:
    """Return the RE2 inline-modifier prefix for Loon flags (``(?ims)``/``""``).

    Raises :class:`LoonUnsupportedError` for flags Loon does not support; Stash
    matches with RE2 and has no ``/.../flags`` delimiter syntax.
    """
    normalized = _validate_regex_flags(flags)
    if not normalized:
        return ""
    ordered = "".join(flag for flag in LOON_REGEX_FLAGS if flag in normalized)
    return f"(?{ordered})"


def stash_regex(regex: RegexLiteral) -> str:
    """Convert a Loon regex literal to the RE2 string Stash expects.

    Only ``\\/`` is unescaped (a Loon delimiter escape); every other regex
    escape is preserved.  Loon flags become an inline RE2 modifier.
    """
    if not isinstance(regex, RegexLiteral):
        raise TypeError("stash_regex() expects a RegexLiteral")
    prefix = format_regex_error_flags(regex.flags)
    return prefix + regex.pattern.replace("\\/", "/")


_NAMED_GROUP_PREFIXES = ("(?<=", "(?<!", "(?<", "(?P<", "(?'")


def count_capture_groups(pattern: str) -> int:
    """Count capturing groups in a regex pattern (RE2/NSRegularExpression-ish).

    Non-capturing and lookaround groups do not count; named groups do.  Bracket
    expressions and escaped parentheses are skipped.  Good enough to validate
    Loon capture indices (``${name.3}``) against the condition regex.
    """
    count = 0
    i = 0
    in_class = False
    while i < len(pattern):
        char = pattern[i]
        if char == "\\":
            i += 2
            continue
        if in_class:
            if char == "]":
                in_class = False
            i += 1
            continue
        if char == "[":
            in_class = True
            i += 1
            continue
        if char == "(":
            rest = pattern[i:]
            if rest.startswith("(?"):
                if rest.startswith("(?<=") or rest.startswith("(?<!"):
                    pass  # lookbehind: non-capturing
                elif rest.startswith("(?<"):
                    count += 1
                elif rest.startswith("(?P<"):
                    count += 1
                elif rest.startswith("(?'"):
                    count += 1
                # (?: (?= (?! (?> (?# (?ims: (?ims) and friends: no capture
            else:
                count += 1
        i += 1
    return count


# ---------------------------------------------------------------------------
# Rewrite expressions
# ---------------------------------------------------------------------------

_EXPR_START_RE = re.compile(r"(request|response)\s+if\b", re.IGNORECASE)
_URL_CONDITION_RE = re.compile(r"\$\{\s*url\s*\}\s*~=\s*", re.IGNORECASE)

# Positional argument signatures: ``(kind, required)`` tuples, optional tail.
# Kinds: string | regex | number | bool | any (``VariableRef`` always allowed).
_ACTION_SIGNATURES: dict[str, tuple[tuple[str, bool], ...]] = {
    "url.replace": (("string", True),),
    "redirect": (("number", True), ("string", True)),
    "reject": (("number", True), ("string", False)),
    "reject_img": (("number", True),),
    "reject_dict": (("number", True),),
    "reject_array": (("number", True),),
    "reject_video": (("number", True),),
}
for _side in ("request", "response"):
    _ACTION_SIGNATURES[f"{_side}.header.add"] = (("string", True), ("string", True))
    _ACTION_SIGNATURES[f"{_side}.header.set"] = (("string", True), ("string", True))
    _ACTION_SIGNATURES[f"{_side}.header.del"] = (("string", True),)
    _ACTION_SIGNATURES[f"{_side}.header.replace"] = (
        ("string", True),
        ("regex", True),
        ("string", True),
    )
    _ACTION_SIGNATURES[f"{_side}.body.replace"] = (("regex", True), ("string", True))
    # ``response.body.mock`` documented order is
    # (contentType, body[, status[, isBase64]]); request mock has no status code.
    if _side == "response":
        _ACTION_SIGNATURES[f"{_side}.body.mock"] = (
            ("string", True),
            ("string", True),
            ("number", False),
            ("bool", False),
        )
        _ACTION_SIGNATURES[f"{_side}.body.mock_file"] = (
            ("string", True),
            ("string", True),
            ("number", False),
            ("bool", False),
        )
    else:
        _ACTION_SIGNATURES[f"{_side}.body.mock"] = (
            ("string", True),
            ("string", True),
            ("bool", False),
        )
        _ACTION_SIGNATURES[f"{_side}.body.mock_file"] = (
            ("string", True),
            ("string", True),
            ("bool", False),
        )
    _ACTION_SIGNATURES[f"{_side}.json.add"] = (("string", True), ("any", True))
    _ACTION_SIGNATURES[f"{_side}.json.delete"] = (("string", True),)
    _ACTION_SIGNATURES[f"{_side}.json.replace"] = (("string", True), ("any", True))
    _ACTION_SIGNATURES[f"{_side}.json.jq"] = (("string", True),)
    _ACTION_SIGNATURES[f"{_side}.json.jq_file"] = (("string", True),)
del _side

SUPPORTED_ACTIONS = frozenset(_ACTION_SIGNATURES)
_SUPPORTED_ACTIONS = SUPPORTED_ACTIONS

_URL_ACTIONS = frozenset(
    {
        "url.replace",
        "redirect",
        "reject",
        "reject_img",
        "reject_dict",
        "reject_array",
        "reject_video",
    }
)
_MOCK_ACTIONS = frozenset(
    {
        "request.body.mock",
        "request.body.mock_file",
        "response.body.mock",
        "response.body.mock_file",
    }
)
_BATCH_CAPABLE = frozenset(
    {f"{side}.header.{op}" for side in ("request", "response") for op in ("add", "set", "del", "replace")}
    | {f"{side}.body.replace" for side in ("request", "response")}
    | {f"{side}.json.{op}" for side in ("request", "response") for op in ("add", "delete", "replace")}
)

_SCRIPT_OPTION_KEYS = frozenset(
    {"enable", "timeout", "tag", "requires_body", "binary_body_mode", "max_size"}
)
SCRIPT_OPTION_KEYS = _SCRIPT_OPTION_KEYS


def _is_sided(action_name: str) -> bool:
    return action_name.startswith(("request.", "response."))


def _iter_values(value: Any) -> Iterator[Any]:
    """Yield ``value`` and every nested literal inside arrays/objects."""
    yield value
    if isinstance(value, ArrayLiteral):
        for item in value.items:
            yield from _iter_values(item)
    elif isinstance(value, ObjectLiteral):
        for _, item in value.entries:
            yield from _iter_values(item)


def _check_value_kind(
    action_name: str, kind: str, value: Any, line: str
) -> None:
    if kind == "any" or isinstance(value, VariableRef):
        return
    if kind == "string":
        if isinstance(value, (StringLiteral, RawStringLiteral)):
            return
    elif kind == "regex":
        if isinstance(value, RegexLiteral):
            return
    elif kind == "number":
        if isinstance(value, NumberLiteral):
            return
    elif kind == "bool":
        if isinstance(value, BoolLiteral):
            return
    raise LoonParseError(
        "E_ACTION_ARG_TYPE",
        f"{action_name}: expected a {kind} argument, got "
        f"{type(value).__name__}",
        line,
    )


def _validate_action_args(action_name: str, args: tuple, line: str) -> None:
    signature = _ACTION_SIGNATURES[action_name]
    required = sum(1 for _, is_required in signature if is_required)
    if len(args) < required or len(args) > len(signature):
        raise LoonParseError(
            "E_ACTION_ARITY",
            f"{action_name}() expects {required}"
            + (f"..{len(signature)}" if len(signature) != required else "")
            + f" positional arguments, got {len(args)}",
            line,
        )
    has_array = any(isinstance(arg, ArrayLiteral) for arg in args)
    if has_array and action_name in _BATCH_CAPABLE:
        if not all(isinstance(arg, ArrayLiteral) for arg in args):
            raise LoonParseError(
                "E_ACTION_BATCH",
                f"{action_name}: batch form requires every argument to be an array "
                "(array and scalar arguments cannot be mixed)",
                line,
            )
        lengths = {len(arg.items) for arg in args}
        if 0 in lengths:
            raise LoonParseError(
                "E_ACTION_BATCH", f"{action_name}: batch arrays must not be empty", line
            )
        if len(lengths) != 1:
            raise LoonParseError(
                "E_ACTION_BATCH",
                f"{action_name}: batch arrays must all have the same length, got "
                f"{sorted(lengths)}",
                line,
            )
        for (kind, _), arg in zip(signature, args):
            for item in arg.items:
                if isinstance(item, ArrayLiteral):
                    raise LoonParseError(
                        "E_ACTION_BATCH",
                        f"{action_name}: batch arrays must not be nested",
                        line,
                    )
                _check_value_kind(action_name, kind, item, line)
        return
    for (kind, _), arg in zip(signature, args):
        _check_value_kind(action_name, kind, arg, line)


def _validate_action_semantics(action_name: str, args: tuple, line: str) -> None:
    if action_name in ("url.replace", "redirect"):
        target = _string_arg(action_name, args, 1 if action_name == "redirect" else 0)
        if target is not None and re.search(r"(?<!\\)\$[0-9]", target):
            raise LoonParseError(
                "E_URL_REPLACE_LOCAL_CAPTURE",
                f"{action_name}() must reference `if` matches as ${{name.n}}, not "
                "action-local $n (Loon rejects `$1` here)",
                line,
            )
    if action_name in _MOCK_ACTIONS:
        content_type = _string_arg(action_name, args, 0)
        if (
            content_type is not None
            and content_type.strip().lower() not in MOCK_CONTENT_TYPES
        ):
            raise LoonUnsupportedError(
                "E_MOCK_CONTENT_TYPE",
                f"{action_name}: unsupported mock content type {content_type!r}; "
                f"Loon supports {', '.join(sorted(MOCK_CONTENT_TYPES))}",
                line,
            )
    if action_name in _URL_ACTIONS and action_name != "url.replace":
        status = args[0] if args else None
        if isinstance(status, NumberLiteral):
            value = status.value
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 100 <= value <= 599
            ):
                raise LoonParseError(
                    "E_REJECT_STATUS",
                    f"{action_name}() status must be an integer in 100..599, got "
                    f"{status.raw}",
                    line,
                )


def _validate_capture_refs(
    values: Sequence[Any],
    *,
    capture_name: Optional[str],
    group_count: int,
    line: str,
) -> None:
    """Validate ``${capture.n}`` substitutions against the condition regex."""
    for root in values:
        for value in _iter_values(root):
            if isinstance(value, VariableRef):
                match = _CAPTURE_REF_RE.fullmatch(value.name)
                if match is None:
                    if capture_name and value.name == capture_name:
                        raise LoonParseError(
                            "E_CAPTURE_REF",
                            f"capture {capture_name!r} must be referenced as "
                            f"${{{capture_name}.0}}..${{{capture_name}.N}}, not "
                            f"${{{capture_name}}}",
                            line,
                        )
                    continue
                _check_capture_ref(
                    match,
                    capture_name=capture_name,
                    group_count=group_count,
                    line=line,
                )
            elif isinstance(value, StringLiteral):
                for template in value.templates:
                    match = _CAPTURE_REF_RE.fullmatch(template.name)
                    if match is not None:
                        _check_capture_ref(
                            match,
                            capture_name=capture_name,
                            group_count=group_count,
                            line=line,
                        )
                    elif capture_name and template.name == capture_name:
                        raise LoonParseError(
                            "E_CAPTURE_REF",
                            f"capture {capture_name!r} must be referenced as "
                            f"${{{capture_name}.0}}..${{{capture_name}.N}}",
                            line,
                        )


def _check_capture_ref(
    match: "re.Match[str]",
    *,
    capture_name: Optional[str],
    group_count: int,
    line: str,
) -> None:
    name, index = match.group(1), int(match.group(2))
    if capture_name is None:
        raise LoonUnsupportedError(
            "E_CAPTURE_UNDECLARED",
            f"substitution ${{{name}.{index}}} references a capture, but the "
            "condition has no `as <name>` binding",
            line,
        )
    if name != capture_name:
        raise LoonUnsupportedError(
            "E_CAPTURE_UNDECLARED",
            f"substitution ${{{name}.{index}}} references undeclared capture "
            f"{name!r}; the condition binds {capture_name!r}",
            line,
        )
    if index > group_count:
        raise LoonParseError(
            "E_CAPTURE_OUT_OF_RANGE",
            f"capture {name!r} only has {group_count} group(s) in the condition "
            f"regex, cannot reference ${{{name}.{index}}}",
            line,
        )


def _validate_rewrite_actions(actions: Sequence[Action], *, phase: str, line: str) -> None:
    sides = {action.name.split(".", 1)[0] for action in actions if _is_sided(action.name)}
    if len(sides) > 1:
        raise LoonParseError(
            "E_PHASE_MIX",
            "a single rewrite may not mix request.* and response.* actions",
            line,
        )
    if sides and phase not in sides:
        raise LoonParseError(
            "E_PHASE_MISMATCH",
            f"a {phase} rewrite cannot run {next(iter(sides))}.* actions",
            line,
        )
    url_actions = [action.name for action in actions if action.name in _URL_ACTIONS]
    if url_actions and phase != "request":
        raise LoonUnsupportedError(
            "E_PHASE_UNSUPPORTED",
            f"{url_actions[0]}() is only supported in a `request` rewrite",
            line,
        )
    mocks = [action for action in actions if action.name in _MOCK_ACTIONS]
    if len(mocks) > 1:
        raise LoonParseError(
            "E_MOCK_COMBO", "a rewrite may contain at most one mock action", line
        )
    if mocks and mocks[0].name.startswith("response."):
        foreign = [
            action
            for action in actions
            if action is not mocks[0] and not action.name.startswith("response.header.")
        ]
        if foreign:
            raise LoonParseError(
                "E_MOCK_COMBO",
                "response.body.mock may only be combined with response.header.* "
                f"actions, found {foreign[0].name}()",
                line,
            )


def _parse_action(
    segment: str,
    *,
    phase: str,
    group_count: int,
    capture_name: Optional[str],
    line: str,
) -> Action:
    parser = _Parser(segment)
    parser.skip_ws()
    call = parser.parse_call()
    parser.skip_ws()
    if not parser.eof():
        parser.error("E_ACTION_TRAILING", "unexpected text after the action call")
    name = call.name.strip().lower()
    if name == "script":
        raise LoonUnsupportedError(
            "E_NOT_REWRITE_ACTION",
            "script(...) is a [Script] declaration, not a rewrite action; use "
            "parse_script_expression()",
            line,
        )
    if name not in _SUPPORTED_ACTIONS:
        raise LoonUnsupportedError(
            "E_ACTION_UNSUPPORTED",
            f"unsupported Loon action {call.name!r}; converter supports "
            f"{len(_SUPPORTED_ACTIONS)} documented actions",
            line,
        )
    _validate_action_args(name, call.args, line)
    _validate_action_semantics(name, call.args, line)
    _validate_capture_refs(
        call.args,
        capture_name=capture_name,
        group_count=group_count,
        line=line,
    )
    return Action(name=name, args=call.args, raw=segment.strip())


def _parse_url_condition(parser: _Parser) -> tuple[RegexLiteral, Optional[str]]:
    """Parse ``${url} ~= /re/flags [as name] then``; leave pos after ``then``."""
    text = parser.text
    start = parser.pos
    match = _URL_CONDITION_RE.match(text, start)
    if match is None:
        found = _find_top_level_keyword(text, "then", start)
        if found is None:
            parser.error(
                "E_REWRITE_CONDITION",
                "expected `if ${url} ~= /pattern/flags [as name] then <action>`",
            )
        condition = text[start : found[0]].strip()
        raise LoonUnsupportedError(
            "E_CONDITION_UNSUPPORTED",
            f"unsupported rewrite condition {condition!r}; the converter can only "
            "express a single `${url} ~= /pattern/flags` condition (Stash "
            "rewrites key on the URL only)",
            text,
            position=start,
        )
    parser.pos = match.end()
    if not parser.eof() and parser.peek() == "$":
        parser.unsupported(
            "E_CONDITION_DYNAMIC_REGEX",
            "the rewrite condition regex is a parameter reference; only a "
            "literal /pattern/flags regex can be converted",
        )
    if parser.eof() or parser.peek() != "/":
        parser.error(
            "E_REWRITE_REGEX", "expected a /pattern/flags regex after `${url} ~=`"
        )
    regex = parser.parse_regex()
    parser.skip_ws()
    capture_name: Optional[str] = None
    as_end = _match_keyword(text, parser.pos, "as")
    if as_end is not None:
        parser.pos = as_end
        parser.skip_ws()
        capture_name = parser.read_identifier(
            "E_CAPTURE_NAME", "expected an identifier capture name after `as`"
        )
        parser.skip_ws()
    then_end = _match_keyword(text, parser.pos, "then")
    if then_end is None:
        found = _find_top_level_keyword(text, "then", parser.pos)
        if found is None:
            parser.error(
                "E_REWRITE_SYNTAX", "expected `then <action>` after the condition"
            )
        condition = text[start : found[0]].strip()
        raise LoonUnsupportedError(
            "E_CONDITION_UNSUPPORTED",
            f"unsupported rewrite condition {condition!r}; only one "
            "`${url} ~= /pattern/flags` condition is supported",
            text,
            position=start,
        )
    parser.pos = then_end
    parser.skip_ws()
    if parser.eof():
        parser.error("E_REWRITE_ACTION", "expected at least one action after `then`")
    return regex, capture_name


def parse_rewrite_expression(line: str) -> Optional[RewriteExpr]:
    """Parse one new-syntax Loon rewrite line; ``None`` for legacy/non-rewrite.

    Returns a :class:`RewriteExpr` for ``<phase> if ${url} ~= /re/flags [as
    name] then <action> [| <action>...]``.  Malformed new syntax raises
    :class:`LoonParseError`; valid-but-unsupported conditions/actions raise
    :class:`LoonUnsupportedError`; lines that are not new-syntax rewrites
    (legacy ``^https://... reject-200`` directives, comments, blank lines)
    return ``None``.
    """
    if not isinstance(line, str):
        raise TypeError("parse_rewrite_expression() expects str")
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    start_match = _EXPR_START_RE.match(stripped)
    if start_match is None:
        return None
    phase = start_match.group(1).lower()
    parser = _Parser(stripped, start_match.end())
    parser.skip_ws()
    regex, capture_name = _parse_url_condition(parser)
    action_text = stripped[parser.pos :]
    parts = split_top_level(action_text, "|")
    group_count = count_capture_groups(regex.pattern)
    actions: list[Action] = []
    for part in parts:
        segment = part.strip()
        if not segment:
            raise LoonParseError(
                "E_ACTION_EMPTY",
                "empty action segment between '|' separators",
                stripped,
            )
        actions.append(
            _parse_action(
                segment,
                phase=phase,
                group_count=group_count,
                capture_name=capture_name,
                line=stripped,
            )
        )
    _validate_rewrite_actions(actions, phase=phase, line=stripped)
    return RewriteExpr(
        phase=phase,
        regex=regex,
        actions=tuple(actions),
        capture_name=capture_name,
        raw=stripped,
    )


# ---------------------------------------------------------------------------
# Script expressions
# ---------------------------------------------------------------------------


def _parse_script_options(raw: str, line: str) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if not raw.strip():
        raise LoonParseError(
            "E_SCRIPT_OPTION", "expected `key=value` options after `with`", line
        )
    for part in split_top_level(raw, ","):
        token = part.strip()
        if not token:
            # The official Loon examples contain double commas
            # (`timeout=10,,argument=...`); tolerate but never guess a value.
            continue
        equals = _find_top_level_char(token, "=")
        if equals < 0:
            raise LoonParseError(
                "E_SCRIPT_OPTION",
                f"expected a `key=value` script option, got {token!r}",
                line,
            )
        key_raw = token[:equals].strip()
        if not _SCRIPT_OPTION_NAME_RE.fullmatch(key_raw):
            raise LoonParseError(
                "E_SCRIPT_OPTION", f"invalid script option name {key_raw!r}", line
            )
        key = key_raw.lower().replace("-", "_")
        if key not in _SCRIPT_OPTION_KEYS:
            raise LoonUnsupportedError(
                "E_SCRIPT_OPTION_UNKNOWN",
                f"unsupported script option {key_raw!r}; converter knows "
                f"{', '.join(sorted(_SCRIPT_OPTION_KEYS))}",
                line,
            )
        value_raw = token[equals + 1 :].strip()
        if not value_raw:
            raise LoonParseError(
                "E_SCRIPT_OPTION", f"script option {key_raw!r} has no value", line
            )
        try:
            value = _parse_complete_value(
                value_raw,
                "E_SCRIPT_OPTION_VALUE",
                f"invalid value for script option {key_raw!r}",
            )
        except LoonUnsupportedError:
            raise
        except LoonParseError as error:
            raise LoonParseError(
                "E_SCRIPT_OPTION_VALUE",
                f"invalid value for script option {key_raw!r}: {error.detail}",
                line,
            ) from error
        options[key] = value_to_python(value)
    if not options:
        raise LoonParseError(
            "E_SCRIPT_OPTION", "expected `key=value` options after `with`", line
        )
    _validate_script_options(options, line)
    return options


def _validate_script_options(options: Mapping[str, Any], line: str) -> None:
    for key, value in options.items():
        if key in ("requires_body", "binary_body_mode"):
            if not isinstance(value, (bool, VariableRef)):
                raise LoonParseError(
                    "E_SCRIPT_OPTION",
                    f"script option {key} must be true/false or a ${...} "
                    f"parameter, got {value!r}",
                    line,
                )
        elif key == "enable":
            if not isinstance(value, (bool, VariableRef)):
                raise LoonParseError(
                    "E_SCRIPT_OPTION",
                    f"script option enable must be a boolean or a ${{...}} "
                    f"parameter, got {value!r}",
                    line,
                )
        elif key == "timeout":
            if not isinstance(value, (int, VariableRef)) or isinstance(value, bool):
                raise LoonParseError(
                    "E_SCRIPT_OPTION",
                    f"script option timeout must be a positive integer, got {value!r}",
                    line,
                )
            if isinstance(value, int) and value <= 0:
                raise LoonParseError(
                    "E_SCRIPT_OPTION",
                    f"script option timeout must be a positive integer, got {value}",
                    line,
                )
        elif key == "tag":
            if not isinstance(value, str):
                raise LoonParseError(
                    "E_SCRIPT_OPTION",
                    f"script option tag must be a string, got {value!r}",
                    line,
                )
        elif key == "max_size":
            if not isinstance(value, (int, VariableRef)) or isinstance(value, bool):
                raise LoonParseError(
                    "E_SCRIPT_OPTION",
                    f"script option max_size must be an integer, got {value!r}",
                    line,
                )


def _validate_script_argument(argument_expr: Any, line: str) -> None:
    if argument_expr is None:
        return
    if isinstance(argument_expr, ObjectLiteral):
        seen: set[str] = set()
        for key, _ in argument_expr.entries:
            if key in seen:
                raise LoonParseError(
                    "E_SCRIPT_ARGUMENT",
                    f"duplicate script argument key {key!r}",
                    line,
                )
            seen.add(key)
        return
    if isinstance(argument_expr, (VariableRef, ArrayLiteral, StringLiteral, RawStringLiteral)):
        return
    if isinstance(argument_expr, (NumberLiteral, BoolLiteral, NullLiteral)):
        raise LoonUnsupportedError(
            "E_SCRIPT_ARGUMENT",
            "script() second argument must be an object of ${...} parameters, "
            f"got {type(argument_expr).__name__}",
            line,
        )
    raise LoonParseError(
        "E_SCRIPT_ARGUMENT",
        f"invalid script() second argument {type(argument_expr).__name__}",
        line,
    )


def parse_script_expression(line: str) -> Optional[ScriptExpr]:
    """Parse a new-syntax script declaration; ``None`` when line is not one.

    Accepts ``<phase> if ${url} ~= /re/flags [as name] then script("path"[,
    object-arg]) [with key=value, ...]``.  Returns ``None`` for rewrite
    expressions, legacy ``[Script]`` lines and comments.
    """
    if not isinstance(line, str):
        raise TypeError("parse_script_expression() expects str")
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    start_match = _EXPR_START_RE.match(stripped)
    if start_match is None:
        return None
    phase = start_match.group(1).lower()
    parser = _Parser(stripped, start_match.end())
    parser.skip_ws()
    regex, capture_name = _parse_url_condition(parser)
    rest = stripped[parser.pos :]
    if re.match(r"script\s*\(", rest, re.IGNORECASE) is None:
        return None
    call_parser = _Parser(rest)
    call = call_parser.parse_call()
    if call.name.lower() != "script":
        return None
    if not call.args:
        raise LoonParseError(
            "E_SCRIPT_PATH", "script() requires a script path first argument", stripped
        )
    if len(call.args) > 2:
        raise LoonParseError(
            "E_SCRIPT_ARGS",
            f"script() accepts a path and optional argument object, got "
            f"{len(call.args)} arguments",
            stripped,
        )
    path_value = call.args[0]
    if isinstance(path_value, VariableRef):
        raise LoonUnsupportedError(
            "E_SCRIPT_PATH_VARIABLE",
            "script() path is a ${...} parameter; the converter needs a literal "
            "script path",
            stripped,
        )
    if not isinstance(path_value, (StringLiteral, RawStringLiteral)):
        raise LoonParseError(
            "E_SCRIPT_PATH", "script() path must be a string literal", stripped
        )
    script_path = path_value.value.strip()
    if not script_path:
        raise LoonParseError("E_SCRIPT_PATH", "script() path is empty", stripped)
    argument_expr = call.args[1] if len(call.args) > 1 else None
    _validate_script_argument(argument_expr, stripped)
    if argument_expr is not None:
        _validate_capture_refs(
            [argument_expr],
            capture_name=capture_name,
            group_count=count_capture_groups(regex.pattern),
            line=stripped,
        )
    tail = rest[call_parser.pos :].strip()
    options: dict[str, Any] = {}
    if tail:
        with_end = _match_keyword(tail, 0, "with")
        if with_end is None:
            raise LoonParseError(
                "E_SCRIPT_TRAILING",
                f"unexpected text after script(...): {tail[:80]!r}",
                stripped,
            )
        options = _parse_script_options(tail[with_end:].strip(), stripped)
    return ScriptExpr(
        phase=phase,
        regex=regex,
        script_path=script_path,
        argument_expr=argument_expr,
        options=options,
        capture_name=capture_name,
        raw=stripped,
    )


# ---------------------------------------------------------------------------
# Legacy [Script] declarations
# ---------------------------------------------------------------------------

_LEGACY_HTTP_RE = re.compile(r"(http-request|http-response)\b(.*)\Z", re.IGNORECASE)
_LEGACY_CRON_RE = re.compile(r"cron\b(.*)\Z", re.IGNORECASE)
_LEGACY_NAMED_RE = re.compile(
    r"(network-changed|generic)\b(.*)\Z", re.IGNORECASE
)


def _decode_legacy_value(raw: str) -> Any:
    """Decode a legacy ``key=value`` value: quoted string, bool, number or text."""
    token = raw.strip()
    if not token:
        return ""
    if token[0] in ('"', "`"):
        try:
            return value_to_python(
                _parse_complete_value(
                    token, "E_LEGACY_VALUE", "invalid quoted legacy value"
                )
            )
        except LoonParseError:
            return token
    lowered = token.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if re.fullmatch(r"-?\d+", token):
        return int(token)
    if re.fullmatch(r"-?\d+\.\d+", token):
        return float(token)
    return token


def _parse_legacy_options(raw: str, line: str, *, required: str = "") -> dict[str, Any]:
    options: dict[str, Any] = {}
    if not raw.strip():
        if required:
            raise LoonParseError(
                "E_LEGACY_SCRIPT", f"missing `{required}=` in legacy script line", line
            )
        return options
    for part in split_top_level(raw, ",", regex_literals=False):
        token = part.strip()
        if not token:
            continue
        equals = _find_top_level_char(token, "=", regex_literals=False)
        if equals < 0:
            key = token.lower().replace("-", "_")
            if not _SCRIPT_OPTION_NAME_RE.fullmatch(token.replace("-", "_")):
                raise LoonParseError(
                    "E_LEGACY_SCRIPT",
                    f"expected a `key=value` legacy script option, got {token!r}",
                    line,
                )
            options[key] = True
            continue
        key_raw = token[:equals].strip()
        if not key_raw:
            raise LoonParseError(
                "E_LEGACY_SCRIPT", f"empty legacy script option name in {token!r}", line
            )
        key = key_raw.lower().replace("-", "_")
        options[key] = _decode_legacy_value(token[equals + 1 :])
    if required and not options.get(required):
        raise LoonParseError(
            "E_LEGACY_SCRIPT", f"missing `{required}=` in legacy script line", line
        )
    return options


def _legacy_script_path(options: Mapping[str, Any], line: str) -> str:
    value = options.get("script_path")
    if not isinstance(value, str) or not value.strip():
        raise LoonParseError(
            "E_LEGACY_SCRIPT", "legacy script line has no usable script-path", line
        )
    return value.strip()


def parse_legacy_script_line(line: str) -> Optional[LegacyScriptLine]:
    """Parse a legacy ``[Script]`` declaration; ``None`` when not a script line.

    Supported forms::

        http-request PATTERN script-path=...,requires-body=true,...
        http-response PATTERN script-path=...,...
        cron "0 8 * * *" script-path=...,timeout=300
        cron {cronExpression} script-path=...
        network-changed script-path=...
        generic script-path=...,img-url=...

    ``options`` keys are normalized (``requires-body`` → ``requires_body``) and
    values are decoded to bool/int/float/str.  New-syntax script expressions
    are *not* handled here (use :func:`parse_script_expression`); they return
    ``None``.
    """
    if not isinstance(line, str):
        raise TypeError("parse_legacy_script_line() expects str")
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    http_match = _LEGACY_HTTP_RE.match(stripped)
    if http_match is not None:
        kind = http_match.group(1).lower()
        tail = http_match.group(2).strip()
        if not tail:
            raise LoonParseError(
                "E_LEGACY_SCRIPT", f"{kind} needs a match pattern and options", stripped
            )
        pieces = re.split(r"[ \t]+", tail, maxsplit=1)
        if len(pieces) != 2 or not pieces[1].strip():
            raise LoonParseError(
                "E_LEGACY_SCRIPT",
                f"{kind} line is missing its `script-path=` options",
                stripped,
            )
        options = _parse_legacy_options(pieces[1], stripped, required="script_path")
        return LegacyScriptLine(
            kind=kind,
            script_path=_legacy_script_path(options, stripped),
            match=pieces[0],
            options=options,
            raw=stripped,
        )
    cron_match = _LEGACY_CRON_RE.match(stripped)
    if cron_match is not None:
        remainder = cron_match.group(1).strip()
        if not remainder:
            raise LoonParseError(
                "E_LEGACY_SCRIPT", "cron line needs a cron expression", stripped
            )
        cron = ""
        options_raw = ""
        if remainder.startswith('"'):
            parser = _Parser(remainder)
            cron_literal = parser.parse_string()
            cron = cron_literal.value
            options_raw = remainder[parser.pos :].strip()
        elif remainder.startswith("{"):
            end = _scan_brace_block(remainder, 0)
            cron = remainder[:end]
            options_raw = remainder[end:].strip()
        else:
            marker = remainder.lower().find("script-path=")
            if marker < 0:
                raise LoonParseError(
                    "E_LEGACY_SCRIPT",
                    "cron line needs a quoted/braced expression and `script-path=`",
                    stripped,
                )
            cron = remainder[:marker].strip()
            options_raw = remainder[marker:]
        if not cron:
            raise LoonParseError(
                "E_LEGACY_SCRIPT", "cron expression is empty", stripped
            )
        options = _parse_legacy_options(options_raw, stripped, required="script_path")
        return LegacyScriptLine(
            kind="cron",
            script_path=_legacy_script_path(options, stripped),
            cron=cron,
            options=options,
            raw=stripped,
        )
    named_match = _LEGACY_NAMED_RE.match(stripped)
    if named_match is not None:
        kind = named_match.group(1).lower()
        options_raw = named_match.group(2).strip()
        options = _parse_legacy_options(options_raw, stripped, required="script_path")
        return LegacyScriptLine(
            kind=kind,
            script_path=_legacy_script_path(options, stripped),
            options=options,
            raw=stripped,
        )
    return None


# ---------------------------------------------------------------------------
# [Argument] declarations
# ---------------------------------------------------------------------------


def _decode_argument_token(raw: str) -> Any:
    """Decode one ``[Argument]`` token: quoted string, bool, number or bare text."""
    token = (raw or "").strip()
    if not token:
        return ""
    if token[0] in ('"', "`"):
        return value_to_python(
            _parse_complete_value(token, "E_ARGUMENT_VALUE", "invalid quoted value")
        )
    lowered = token.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if re.fullmatch(r"-?\d+", token):
        return int(token)
    if re.fullmatch(r"-?\d+\.\d+", token):
        return float(token)
    return token


def _coerce_number(raw: str, line: str) -> Any:
    decoded = _decode_argument_token(raw)
    if isinstance(decoded, bool):
        raise LoonParseError(
            "E_ARGUMENT_TYPE", f"expected a number, got {raw!r}", line
        )
    if isinstance(decoded, int):
        return decoded
    if isinstance(decoded, float):
        return decoded
    token = str(decoded).strip()
    if re.fullmatch(r"-?\d+", token):
        return int(token)
    if re.fullmatch(r"-?\d+\.\d+", token):
        return float(token)
    raise LoonParseError(
        "E_ARGUMENT_TYPE", f"expected a number (type=number), got {raw!r}", line
    )


def parse_argument_decl(line: str) -> Optional[ArgumentDecl]:
    """Parse one ``[Argument]`` declaration; ``None`` for blank/comment lines.

    Forms (Loon docs)::

        name = input,"default",tag=...,desc=...
        name = input,9.99,type=number
        name = select,"a","b",type=number
        name = switch,false,true,tag=...

    The first value is the default.  ``options`` holds typed values, so a
    ``type=number`` select yields ints/floats and a ``switch`` yields booleans;
    ``default_typed()`` returns the typed default.  Malformed declarations
    raise :class:`LoonParseError` (unknown widgets raise
    :class:`LoonUnsupportedError`).
    """
    if not isinstance(line, str):
        raise TypeError("parse_argument_decl() expects str")
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    equals = _find_top_level_char(stripped, "=", regex_literals=False)
    if equals < 0:
        raise LoonParseError(
            "E_ARGUMENT", "expected `name = input|select|switch,...`", stripped
        )
    name = stripped[:equals].strip()
    if not _ARGUMENT_NAME_RE.fullmatch(name):
        raise LoonParseError(
            "E_ARGUMENT", f"invalid [Argument] parameter name {name!r}", stripped
        )
    rest = stripped[equals + 1 :].strip()
    if not rest:
        raise LoonParseError(
            "E_ARGUMENT", f"[Argument] {name!r} has no widget declaration", stripped
        )
    parts = split_top_level(rest, ",", regex_literals=False)
    kind = parts[0].strip().lower()
    if kind not in ("input", "select", "switch"):
        raise LoonUnsupportedError(
            "E_ARGUMENT_KIND",
            f"unsupported [Argument] widget {parts[0]!r} for {name!r}; Loon "
            "supports input/select/switch",
            stripped,
        )
    values_raw: list[str] = []
    attrs: dict[str, str] = {}
    for token in parts[1:]:
        token = token.strip()
        if not token:
            continue
        attr_equals = _find_top_level_char(token, "=", regex_literals=False)
        if attr_equals >= 0:
            key_raw = token[:attr_equals].strip().lower()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_\-]*", key_raw):
                raise LoonParseError(
                    "E_ARGUMENT", f"invalid [Argument] attribute {key_raw!r}", stripped
                )
            if key_raw in attrs:
                raise LoonParseError(
                    "E_ARGUMENT",
                    f"duplicate [Argument] attribute {key_raw!r} for {name!r}",
                    stripped,
                )
            attrs[key_raw] = token[attr_equals + 1 :].strip()
        else:
            values_raw.append(token)
    type_name = attrs.get("type", "").strip().lower()
    if type_name and type_name not in ("string", "number"):
        raise LoonUnsupportedError(
            "E_ARGUMENT_TYPE",
            f"unsupported [Argument] type={type_name!r} for {name!r}; Loon "
            "supports type=number (default string)",
            stripped,
        )
    if kind == "switch":
        if len(values_raw) > 2:
            raise LoonParseError(
                "E_ARGUMENT",
                f"switch parameter {name!r} takes at most two values "
                "(default,true-value)",
                stripped,
            )
        for token in values_raw:
            if not isinstance(_decode_argument_token(token), bool):
                raise LoonParseError(
                    "E_ARGUMENT",
                    f"switch parameter {name!r} values must be true/false, got "
                    f"{token!r}",
                    stripped,
                )
        # Documented default for switch is false when no value is written.
        default_raw = values_raw[0] if values_raw else "false"
        options: tuple = tuple(_decode_argument_token(token) for token in values_raw)
    elif kind == "input":
        if len(values_raw) > 1:
            raise LoonParseError(
                "E_ARGUMENT",
                f"input parameter {name!r} takes at most one default value",
                stripped,
            )
        default_raw = values_raw[0] if values_raw else ""
        decoded = _decode_argument_token(default_raw)
        if type_name == "number":
            typed = _coerce_number(default_raw, stripped) if default_raw else None
        else:
            typed = decoded if isinstance(decoded, str) else str(decoded)
        options = (typed,)
    else:  # select
        if not values_raw:
            raise LoonParseError(
                "E_ARGUMENT",
                f"select parameter {name!r} needs at least its default value",
                stripped,
            )
        default_raw = values_raw[0]
        if type_name == "number":
            options = tuple(_coerce_number(token, stripped) for token in values_raw)
        else:
            options = tuple(
                value if isinstance(value, str) else str(value)
                for value in (_decode_argument_token(token) for token in values_raw)
            )
    return ArgumentDecl(
        name=name,
        kind=kind,
        default_raw=default_raw,
        options=options,
        attrs=attrs,
        raw=stripped,
    )
