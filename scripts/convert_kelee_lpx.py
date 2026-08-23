#!/usr/bin/env python3
"""
KeLee Loon .lpx → Stash .stoverride converter
Fetches .lpx from https://kelee.one / https://hub.kelee.one with Loon UA (Cloudflare requires it)
and converts to Stash override format.

Reference:
- Loon plugin format: https://www.nsmao.net/thread-2001.htm (Loon plugin spec)
- Stash override: inferred from repo stash/overrides/pinduoduo-cleanup.stoverride + Clash/Stash docs
- Mapping rules documented inline
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from typing import List, Tuple, Dict

REPO_ROOT = Path(__file__).resolve().parents[1]

LOON_UA = "Loon/764 CFNetwork/1498.700.1 Darwin/23.6.0 iPhone/17.6.1"
STASH_UA = "Stash/3.4.0"
# For fetching, must use Loon UA due to Cloudflare WAF
FETCH_HEADERS = {
    "User-Agent": LOON_UA,
    "Accept": "*/*",
}

# ---------- Fetch helpers ----------
def fetch_text(url: str, headers: dict = FETCH_HEADERS, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                # handle charset
                charset = resp.headers.get_content_charset() or "utf-8"
                data = resp.read()
                return data.decode(charset, errors="replace")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(1 + attempt)
                continue
            raise
        except Exception:
            if attempt < 2:
                time.sleep(1 + attempt)
                continue
            raise
    raise RuntimeError(f"fetch failed {url}")

def fetch_bytes(url: str, headers: dict = FETCH_HEADERS, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()

# ---------- LPX parsing ----------

HEADER_RE = re.compile(r"^#!(.*?)=(.*)$")
SECTION_RE = re.compile(r"^\[(.*?)\]$")

def parse_lpx(text: str) -> tuple[dict, dict]:
    header: dict = {}
    sections: dict[str, List[str]] = {}
    current = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = HEADER_RE.match(line)
        if m:
            k, v = m.group(1).strip(), m.group(2).strip()
            header[k] = v
            continue
        m = SECTION_RE.match(line)
        if m:
            current = m.group(1).strip()
            sections.setdefault(current, [])
            continue
        if line.startswith("#") or line.startswith(";"):
            continue
        if current is None:
            continue
        sections[current].append(raw_line.rstrip())  # keep original for Rewrite lines
    return header, sections

# ---------- Rewrite parsing ----------

def parse_rewrite_line(line: str) -> tuple[str, str, str]:
    """
    Returns pattern, directive, rest
    directive is second token
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    # Use split with max 2? But need to handle pattern that contains spaces? No.
    # Pattern is first whitespace-separated token
    # Directive is second token
    # Rest is remaining
    # Example: "^https://example.com/path\\? response-body-json-jq 'del(.ad)'"
    m = re.match(r'^(\S+)\s+(\S+)(?:\s+(.*))?$', stripped)
    if not m:
        return None
    pattern, directive, rest = m.group(1), m.group(2), m.group(3) or ""
    return pattern, directive, rest.strip()

def jq_for_del(keys: List[str]) -> str:
    # keys like "data.data.ad" -> ".data.data.ad"
    # produce del(.a, .b, ...)
    dotted = ", ".join(f".{k.lstrip('.')}" for k in keys)
    return f"del({dotted})"

def jq_for_replace(pairs: List[str]) -> str:
    # pairs are tokens: key value key value ...
    # Example: "wl_config.home_ad_num 0 wl_config.frs_ad_num 0"
    # Output: ".wl_config.home_ad_num = 0 | .wl_config.frs_ad_num = 0"
    # Need to handle values that are strings needing quotes
    if len(pairs) % 2 != 0:
        # odd, treat last as??
        pass
    exprs = []
    for i in range(0, len(pairs)-1, 2):
        key = pairs[i].lstrip('.')
        val = pairs[i+1]
        # Detect numeric/bool/null
        if re.match(r'^-?\d+(\.\d+)?$', val) or val in ("true","false","null","0","1"):
            val_str = val
        elif val.startswith('"') and val.endswith('"'):
            val_str = val
        elif val.startswith("'") and val.endswith("'"):
            val_str = val
        else:
            # bare string -> quote
            # need to escape
            val_str = f'"{val}"'
        exprs.append(f".{key} = {val_str}")
    return " | ".join(exprs)

def normalize_rule(rule: str) -> str:
    # Loon rule: "DOMAIN, titan.pinduoduo.com, REJECT" -> Stash: "DOMAIN,titan.pinduoduo.com,REJECT"
    # Remove spaces around commas, but preserve spaces inside quoted strings?
    # Simple: split by comma, trim, join with comma
    # For AND/OR complex rules, keep as is but trim spaces around commas outside quotes?
    # We'll handle naive: replace ", " with "," and " ," with ","
    rule = rule.strip()
    # Remove space after comma and before comma
    # Use regex to handle not inside quotes: simple replace
    rule = re.sub(r'\s*,\s*', ',', rule)
    return rule

# ---------- Script parsing ----------

SCRIPT_RE_HTTP = re.compile(r'^(http-(?:response|request))\s+(\S+)\s+(.*)$')
SCRIPT_RE_CRON = re.compile(r'^cron\s+(\S+(?:\s+\S+){4,5})\s+(.*)$')  # cron <expr> script-path=...
SCRIPT_RE_GENERIC = re.compile(r'^generic\s+(.*)$')
# For generic with pattern? Some lpx have `generic script-path=...`
# But also `http-response ^pattern script-path=...`

def parse_script_line(line: str) -> dict | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    # Try http-response/request
    m = SCRIPT_RE_HTTP.match(line)
    if m:
        typ, pattern, rest = m.group(1), m.group(2), m.group(3)
        params = parse_script_params(rest)
        params["type"] = typ.replace("http-","")  # response / request
        params["match"] = pattern
        return params
    # Try cron with argument placeholder {cron}
    # Loon cron line: "cron {cron} script-path=URL, timeout=..., tag=..."
    # Also could be "cron 0 8 * * * script-path=..."
    if line.startswith("cron"):
        # remove leading cron
        after = line[4:].strip()
        # first token(s) is cron expression (could be {cron} or 5 fields)
        # We'll try to extract cron expr: if starts with "{", then it's placeholder
        if after.startswith("{"):
            end = after.find("}")
            if end != -1:
                cron_expr = after[:end+1]  # {cron}
                rest = after[end+1:].strip()
                params = parse_script_params(rest)
                params["type"] = "cron"
                params["cron"] = cron_expr
                return params
        # else assume 5-field cron
        # Cron expr is 5 space-separated fields before script-path=
        # Find script-path index
        idx = after.find("script-path=")
        if idx != -1:
            cron_expr = after[:idx].strip()
            rest = after[idx:].strip()
            params = parse_script_params(rest)
            params["type"] = "cron"
            params["cron"] = cron_expr
            return params
    m = SCRIPT_RE_GENERIC.match(line)
    if m:
        rest = m.group(1)
        params = parse_script_params(rest)
        params["type"] = "generic"
        return params
    # Fallback: maybe line is like "script-path=... tag=..." without type? treat as generic
    if "script-path=" in line:
        params = parse_script_params(line)
        params["type"] = params.get("type", "generic")
        return params
    return None

def parse_script_params(rest: str) -> dict:
    """
    Parse comma-separated key=value pairs, handling commas inside brackets/quotes
    Example: 'script-path=https://..., requires-body=true, timeout=10, tag=xxx'
    argument value may be [{blockUpload},{blockShorts}] with commas inside
    """
    params = {}
    # We'll use regex to find key=value where value may be quoted or bracketed
    # Approach: split by ", " but not inside [] or "" ?
    # Simple state machine
    tokens = []
    current = []
    depth_bracket = 0
    in_quotes = None
    i = 0
    while i < len(rest):
        ch = rest[i]
        if in_quotes:
            if ch == in_quotes and rest[i-1] != "\\":
                in_quotes = None
            current.append(ch)
        else:
            if ch in ('"', "'"):
                in_quotes = ch
                current.append(ch)
            elif ch == "[":
                depth_bracket += 1
                current.append(ch)
            elif ch == "]":
                depth_bracket = max(0, depth_bracket-1)
                current.append(ch)
            elif ch == "," and depth_bracket == 0:
                # separator
                tokens.append("".join(current).strip())
                current = []
                # skip following space
                while i+1 < len(rest) and rest[i+1] == " ":
                    i += 1
            else:
                current.append(ch)
        i += 1
    if current:
        tokens.append("".join(current).strip())
    for tok in tokens:
        if not tok:
            continue
        if "=" not in tok:
            # maybe bare flag?
            params[tok] = True
            continue
        k, v = tok.split("=", 1)
        k = k.strip().lower().replace("-", "_")
        v = v.strip()
        # Strip quotes
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        # Convert booleans/numbers
        if v.lower() == "true":
            v = True
        elif v.lower() == "false":
            v = False
        elif v.isdigit():
            try:
                v = int(v)
            except:
                pass
        params[k] = v
    return params

def sanitize_provider_name(tag: str, script_url: str, existing: set) -> str:
    # derive from script_url basename if tag is Chinese or generic
    # fallback to tag sanitized
    base = None
    if script_url:
        try:
            parsed = urllib.parse.urlparse(script_url)
            base = Path(parsed.path).stem  # without extension
        except:
            base = None
    if base and base not in ("", "script"):
        cand = base
    elif tag:
        cand = tag
    else:
        cand = "script"
    # sanitize: lower, replace non-alnum with -, collapse
    cand = re.sub(r'[^a-zA-Z0-9]+', '-', cand).strip('-').lower()
    if not cand:
        cand = "script"
    # ensure unique
    orig = cand
    idx = 1
    while cand in existing:
        cand = f"{orig}-{idx}"
        idx += 1
    existing.add(cand)
    return cand

# ---------- Conversion ----------

def convert_lpx_to_stash(lpx_text: str, lpx_url: str = "", fetch_script_fallback: bool = True) -> str:
    header, sections = parse_lpx(lpx_text)
    name = header.get("name", "Unnamed").strip()
    desc = header.get("desc", "").strip().replace("\\n", " ").replace("\n", " ")
    author = header.get("author", "")
    icon = header.get("icon", "")
    homepage = header.get("homepage", "")
    date = header.get("date", "")
    # Build stash structure
    # Use dict preserving order
    stash = {}
    stash["name"] = name
    if desc:
        # truncate to avoid too long
        stash["desc"] = desc[:500]
    if icon:
        stash["icon"] = icon
    # Rules - handle case-insensitive section names
    # Find rule section with case variations
    rules_raw = []
    for k in list(sections.keys()):
        if k.lower() == "rule":
            rules_raw = sections[k]
            break
    rules = []
    # Also collect potential redirect rules that were mistakenly under Rule but are actually rewrites
    # We'll detect them and move to url-rewrite later
    redirect_from_rules = []
    for r in rules_raw:
        r = r.strip()
        if not r or r.startswith("#"):
            continue
        # Detect if rule line is actually a rewrite redirect like "^(http://)... 307 $1"
        if re.match(r'^\S+\s+(302|307|308|301)\s+\S+', r):
            redirect_from_rules.append(r)
            continue
        # Detect lines starting with ^ and containing header/mock? unlikely in Rule but handle
        if r.startswith("^") and " " in r and len(r.split(None, 2)) >= 2:
            # Check if second token is known rewrite directive
            _, dir_candidate, _ = parse_rewrite_line(r) or (None, "", "")
            if dir_candidate and dir_candidate.lower() in ("reject","reject-dict","reject-array","reject-200","reject-img","mock-response-body","response-body-json-jq","response-body-json-del","response-body-json-replace","response-body-replace-regex","header"):
                redirect_from_rules.append(r)
                continue
        rules.append(normalize_rule(r))
    if rules:
        stash["rules"] = rules

    # HTTP section
    http = {}
    # MITM - case insensitive
    mitm_raw = []
    for k in list(sections.keys()):
        if k.lower() == "mitm":
            mitm_raw = sections[k]
            break
    mitm_hosts = []
    for line in mitm_raw:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Format: "hostname = a, b, c" or "hostname= a,b"
        if "=" in line:
            _, hosts_part = line.split("=", 1)
        else:
            hosts_part = line
        for h in hosts_part.split(","):
            h = h.strip().strip('"').strip("'")
            if h:
                mitm_hosts.append(h)
    if mitm_hosts:
        # deduplicate preserve order
        seen = set()
        uniq = []
        for h in mitm_hosts:
            if h not in seen:
                uniq.append(h)
                seen.add(h)
        http["mitm"] = uniq

    # Rewrite - also include redirect_from_rules
    rewrite_raw = []
    for k in list(sections.keys()):
        if k.lower() == "rewrite":
            rewrite_raw = sections[k]
            break
    # Append redirect rules detected from Rule section
    if redirect_from_rules:
        rewrite_raw = rewrite_raw + redirect_from_rules
    url_rewrite = []
    body_rewrite = []
    header_rewrite = []
    # For mock handling, may also need url-rewrite
    # Keep track of unsupported
    unsupported_rewrites = []

    for line in rewrite_raw:
        line_stripped = line.strip()
        if not line_stripped or line_stripped.startswith("#"):
            continue
        parsed = parse_rewrite_line(line_stripped)
        if not parsed:
            unsupported_rewrites.append(f"# unsupported parse: {line_stripped}")
            continue
        pattern, directive, rest = parsed
        # Map directive
        low = directive.lower()
        if low in ("reject","reject-dict","reject-array","reject-200","reject-img","reject-video","reject-tinygif","reject-video","reject-302"):
            # Some have hyphen variations
            url_rewrite.append(f"{pattern} - {directive}")
        elif low.startswith("reject"):
            url_rewrite.append(f"{pattern} - {directive}")
        elif low in ("302","307","308","301"):
            # redirect - Stash expects "pattern - 302 target"
            target = rest.strip()
            # Normalize pattern: Loon sometimes has "(^https..." -> should be "^(..."
            if pattern.startswith("(^"):
                pattern = "^(" + pattern[2:]
            if target:
                url_rewrite.append(f"{pattern} - {directive} {target}")
            else:
                url_rewrite.append(f"{pattern} - {directive}")
        elif low == "header" or low.startswith("header-"):
            # Loon: "^url header target" - often target is a URL (Spotify case)
            # Stash header-rewrite expects "header-add/header-replace" not bare "header"
            # If target looks like a URL, treat as url-rewrite 302
            rest_stripped = rest.strip()
            # Normalize pattern like 302 case
            if pattern.startswith("(^"):
                pattern = "^(" + pattern[2:]
            if rest_stripped.startswith("http://") or rest_stripped.startswith("https://"):
                # Convert to url-rewrite redirect (302 is safest)
                # Preserve original query param handling
                url_rewrite.append(f"{pattern} - 302 {rest_stripped}")
                unsupported_rewrites.append(f"# header->url-rewrite converted: {line_stripped}")
            else:
                # Try to map to valid header-rewrite: ensure dash and directive
                # Stash expects "pattern - header-add" etc. Fallback to comment if unknown
                if low == "header":
                    # Generic header without operation is invalid - comment out
                    unsupported_rewrites.append(f"# invalid header-rewrite skipped: {line_stripped}")
                    # Do not add to header_rewrite to avoid Stash invalid syntax
                    continue
                else:
                    header_rewrite.append(f"{pattern} - {directive} {rest_stripped}".strip())
        elif low == "mock-response-body":
            # Normalize pattern
            if pattern.startswith("(^"):
                pattern = "^(" + pattern[2:]
            rest_fixed = rest.strip()
            # Fix for Stash mock: Loon uses data-type=text, status-code, data="{"json"}"
            # Stash expects: - mock status=200 data='{"json"}' (no data-type, status not status-code, single quotes)
            # Strip data-type (Stash uses header for content-type, mock assumes json/text auto)
            rest_fixed = re.sub(r'\bdata-type\s*=\s*\S+\s*', '', rest_fixed)
            rest_fixed = re.sub(r'\bstatus-code\s*=', 'status=', rest_fixed)
            # Fix quoting for data="{"json"}" -> data='{"json"}'
            m_json = re.search(r'data\s*=\s*"(\{.*\})"', rest_fixed, re.S)
            if m_json:
                json_str = m_json.group(1)
                rest_fixed = re.sub(r'data\s*=\s*"\{.*\}"', f"data='{json_str}'", rest_fixed, count=1, flags=re.S)
            else:
                def _repl_base64(m):
                    key = m.group(1)
                    val = m.group(2)
                    return f"{key}='{val}'"
                rest_fixed = re.sub(r'(data(?:-path)?)\s*=\s*"([^"]+)"', _repl_base64, rest_fixed)
            if 'data="' in rest_fixed:
                rest_fixed = re.sub(r'(data(?:-path)?)\s*=\s*"', r"\1='", rest_fixed)
                rest_fixed = re.sub(r"'([^']*?)\"(?=\s+\w+=|\s*$)", r"'\1'", rest_fixed)
                rest_fixed = re.sub(r"'([^']*?)\"(?=\s+\w+\s*=|\s*$)", r"'\1'", rest_fixed)
            # Ensure mock has status, default 200 if missing
            if 'status=' not in rest_fixed:
                rest_fixed = f"status=200 {rest_fixed}".strip()
            url_rewrite.append(f"{pattern} - mock {rest_fixed}".strip())
            # Also add comment about original
        elif low.startswith("response-body-") or low.startswith("response-header-"):
            # body rewrite variants
            if low == "response-body-json-jq":
                # rest may be "'del(...)'" or "jq-path=\"URL\""
                rest_stripped = rest.strip()
                # Check jq-path
                m_jqpath = re.search(r'jq-path\s*=\s*"?([^"\s]+)"?', rest_stripped)
                if m_jqpath:
                    jq_url = m_jqpath.group(1).strip('"').strip("'")
                    # Fetch external jq
                    try:
                        jq_content = fetch_text(jq_url).strip()
                        jq_content = re.sub(r'\s*\n\s*', ' ', jq_content).strip()
                        if len(jq_content) > 3000:
                            # too long, keep note but still inline truncated with comment appended outside yaml?
                            # For stash, large jq may be heavy; keep reference and inline truncated
                            body_rewrite.append(f"{pattern} response-jq {jq_content[:3000]}")
                            unsupported_rewrites.append(f"# jq-path {jq_url} truncated ({len(jq_content)} chars)")
                        else:
                            body_rewrite.append(f"{pattern} response-jq {jq_content}")
                    except Exception as e:
                        body_rewrite.append(f"{pattern} response-jq # failed to fetch jq-path {jq_url}: {e}")
                else:
                    jq = rest_stripped
                    if (jq.startswith("'") and jq.endswith("'")) or (jq.startswith('"') and jq.endswith('"')):
                        jq = jq[1:-1]
                    body_rewrite.append(f"{pattern} response-jq {jq}")
            elif low == "response-body-json-del":
                keys = rest.strip().split()
                if keys:
                    jq = jq_for_del(keys)
                    body_rewrite.append(f"{pattern} response-jq {jq}")
                else:
                    unsupported_rewrites.append(f"# unsupported json-del no keys: {line_stripped}")
            elif low == "response-body-json-replace":
                tokens = re.findall(r'"[^"]*"|\'[^\']*\'|\S+', rest.strip())
                cleaned = []
                for t in tokens:
                    if (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'")):
                        cleaned.append(t[1:-1])
                    else:
                        cleaned.append(t)
                if len(cleaned) >= 2:
                    jq = jq_for_replace(cleaned)
                    body_rewrite.append(f"{pattern} response-jq {jq}")
                else:
                    unsupported_rewrites.append(f"# unsupported json-replace parse: {line_stripped}")
            elif low == "response-body-replace-regex":
                escaped_rest = rest.replace("'", "\\'")
                body_rewrite.append(f"{pattern} response-body-replace-regex {escaped_rest}")
            elif low == "response-header-add" or low == "response-header-del" or low.startswith("response-header"):
                # Stash uses header-add/header-del without response- prefix, and NO dash for header-rewrite
                stash_directive = directive.replace("response-header", "header", 1)
                header_rewrite.append(f"{pattern} {stash_directive} {rest}".strip())
            else:
                body_rewrite.append(f"{pattern} {directive} {rest}")
        elif low.startswith("response-"):
            body_rewrite.append(f"{pattern} {directive} {rest}")
        else:
            unsupported_rewrites.append(f"# unsupported rewrite directive {directive}: {line_stripped}")
            # also add as url-rewrite with comment
            url_rewrite.append(f"# {line_stripped} # unsupported")

    if url_rewrite:
        http["url-rewrite"] = url_rewrite
    if body_rewrite:
        http["body-rewrite"] = body_rewrite
    if header_rewrite:
        http["header-rewrite"] = header_rewrite
    if unsupported_rewrites:
        # Add as comment list under http? Not standard, but we can add as yaml comment via extra key?
        # We'll store as separate comment in output file header
        http["_unsupported_rewrite_comments"] = unsupported_rewrites

    # Argument defaults for placeholder replacement
    arg_defaults = {}
    for k in list(sections.keys()):
        if k.lower() == "argument":
            for line in sections[k]:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Format: key=type, default, ... e.g., "tab=switch, false, true, tag=..."
                # Extract key and default (second comma-separated value after =)
                # Use split on '='
                if "=" not in line:
                    continue
                key_part, rest = line.split("=", 1)
                key = key_part.strip()
                # rest: "switch, false, true, tag=..."
                # default is the second token before first tag=
                # Simplify: split by ',' and take first two tokens after type
                # tokens: ["switch", " false", " true", " tag=..."]
                # default is tokens[1] if exists
                # Use regex to find type and default
                m_arg = re.match(r'\s*([^,]+)\s*,\s*([^,]+)', rest.strip())
                if m_arg:
                    # type = m_arg.group(1).strip()
                    default = m_arg.group(2).strip()
                    # Strip quotes
                    if (default.startswith('"') and default.endswith('"')) or (default.startswith("'") and default.endswith("'")):
                        default = default[1:-1]
                    arg_defaults[key] = default
            break
    # Script - case insensitive
    script_raw = []
    for k in list(sections.keys()):
        if k.lower() == "script":
            script_raw = sections[k]
            break
    script_entries = []
    script_providers = {}
    provider_names = set()
    for line in script_raw:
        line_s = line.strip()
        if not line_s or line_s.startswith("#"):
            continue
        parsed = parse_script_line(line_s)
        if not parsed:
            # Could be unsupported, add as comment
            continue
        script_url = parsed.get("script_path") or parsed.get("script-path") or ""
        tag = parsed.get("tag", "")
        # Normalize keys: script_path vs script-path
        if "script_path" in parsed:
            script_url = parsed["script_path"]
        if "script-path" in parsed:
            script_url = parsed["script-path"]
        # provider name
        provider_name = sanitize_provider_name(tag, script_url, provider_names)
        entry = {}
        # match
        if "match" in parsed:
            entry["match"] = parsed["match"]
        elif "pattern" in parsed:
            entry["match"] = parsed["pattern"]
        else:
            # For cron/generic, no match
            pass
        entry["name"] = provider_name
        # type
        typ = parsed.get("type", "generic")
        # Map Loon type to Stash type
        # Loon: http-response -> response, http-request -> request, cron -> cron, generic -> generic
        # Stash uses same: response, request, cron, generic ?
        if typ in ("response","request","cron","generic"):
            entry["type"] = typ
        elif typ == "http-response":
            entry["type"] = "response"
        elif typ == "http-request":
            entry["type"] = "request"
        else:
            entry["type"] = typ
        # require-body
        if "requires_body" in parsed:
            entry["require-body"] = bool(parsed["requires_body"])
        elif "requires-body" in parsed:
            entry["require-body"] = bool(parsed["requires-body"])
        elif "require_body" in parsed:
            entry["require-body"] = bool(parsed["require_body"])
        # binary-body-mode
        if "binary_body_mode" in parsed:
            entry["binary-body-mode"] = bool(parsed["binary_body_mode"])
        if "binary-body-mode" in parsed:
            entry["binary-body-mode"] = bool(parsed["binary-body-mode"])
        # timeout
        if "timeout" in parsed:
            try:
                entry["timeout"] = int(parsed["timeout"])
            except:
                pass
        # argument - replace placeholders {key} with defaults
        if "argument" in parsed:
            arg = str(parsed["argument"])
            # Replace {var} with defaults
            def repl_arg(m):
                var = m.group(1)
                return arg_defaults.get(var, m.group(0))
            arg_replaced = re.sub(r'\{([^}]+)\}', repl_arg, arg)
            entry["argument"] = arg_replaced
        # cron
        if "cron" in parsed:
            cron_expr = str(parsed["cron"])
            # Replace placeholder {cron} with default from Argument
            if cron_expr.strip() == "{cron}" or "{cron}" in cron_expr:
                cron_default = arg_defaults.get("cron", "55 23 * * *")
                cron_expr = cron_expr.replace("{cron}", cron_default)
            # If still contains {var}, replace
            cron_expr = re.sub(r'\{([^}]+)\}', lambda m: arg_defaults.get(m.group(1), m.group(0)), cron_expr)
            entry["cron"] = cron_expr.strip().strip("{}") if cron_expr.startswith("{") else cron_expr
            # cron scripts may not have match
            if "match" in entry:
                del entry["match"]
        # max-size? Not in stash? ignore
        script_entries.append(entry)
        # provider
        if script_url:
            better_url = script_url
            if fetch_script_fallback:
                try:
                    if "kelee.one" in script_url and script_url.endswith(".js"):
                        js_content = fetch_text(script_url)[:5000]
                        m_raw = re.search(r'https://raw\.githubusercontent\.com/[^\s"\']+', js_content)
                        if m_raw:
                            better_url = m_raw.group(0)
                except:
                    pass
            # For provider, Stash expects url with interval
            # Some JS URLs are like https://github.com/.../releases/download/.../*.js  - those need to use jsDelivr maybe?
            # Keep as is
            provider_entry = {"url": better_url, "interval": 86400}
            # If original was kelee.one, add header hint? Stash provider headers?
            if "kelee.one" in better_url:
                # Add header to bypass Cloudflare? Stash may support headers, but not documented.
                # We'll add headers as comment, not actual yaml key, to avoid breaking.
                # Instead, add note in desc
                provider_entry["_note"] = "kelee.one requires Loon UA; Stash may fail to fetch without proxy or headers"
                # If stash supports headers, we could add:
                # provider_entry["headers"] = {"User-Agent": LOON_UA}
                # But we will include it conditionally if better_url is kelee.one
                # Try to add headers field anyway - stash may ignore unknown?
                # We'll add headers for kelee.one
                provider_entry["headers"] = {"User-Agent": LOON_UA}
            script_providers[provider_name] = provider_entry

    if script_entries:
        http["script"] = script_entries
    # Need to decide http placement: stash expects http at top-level? In pinduoduo stoverride, http is top-level key containing mitm etc, script is also inside http? Actually in pinduoduo stoverride, script is inside http? Let's check: In pinduoduo stoverride, they have:
    # http:
    #   mitm:
    #   url-rewrite:
    #   body-rewrite:
    #   script:
    # Then script-providers at top-level separate.
    # But also stash-ios.yaml has http: mitm: script: script-providers at top-level.
    # So script inside http, providers at top-level.
    # We'll follow that: http.script = script_entries, providers separate.
    # Clean up temporary unsupported key
    unsupported_comments = http.pop("_unsupported_rewrite_comments", None)

    if http:
        stash["http"] = http
    if script_providers:
        stash["script-providers"] = script_providers

    # Hosts (if any) - case insensitive
    host_raw = []
    for k in list(sections.keys()):
        if k.lower() == "host":
            host_raw = sections[k]
            break
    hosts = {}
    for line in host_raw:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            hosts[k.strip()] = v.strip()
    if hosts:
        stash["hosts"] = hosts

    # Build YAML output manually to preserve order and comments
    import yaml
    # Use yaml.safe_dump with sort_keys=False
    # But we want to keep order and have header comments
    yaml_str = yaml.safe_dump(stash, sort_keys=False, allow_unicode=True, width=4096, default_flow_style=False)
    # Prepend header comments
    header_lines = []
    header_lines.append(f"# {name}")
    if desc:
        header_lines.append(f"# {desc}")
    if author:
        header_lines.append(f"# author: {author}")
    if homepage:
        header_lines.append(f"# homepage: {homepage}")
    if icon:
        header_lines.append(f"# icon: {icon}")
    if date:
        header_lines.append(f"# date: {date}")
    if lpx_url:
        header_lines.append(f"# lpx: {lpx_url}")
    header_lines.append(f"# converted: Loon .lpx -> Stash .stoverride (auto)")
    header_lines.append(f"# note: kelee.one resources require Loon UA; if Stash fetch fails, add proxy or use GitHub mirror")
    if unsupported_comments:
        header_lines.append(f"# unsupported rewrites: {len(unsupported_comments)}")
        for c in unsupported_comments[:5]:
            header_lines.append(f"#   {c}")
    # Also add reordered yaml_str but need to ensure `rules`, `http`, etc formatting matches pinduoduo example
    # yaml.safe_dump will quote strings with special chars, but we have body-rewrite entries already quoted with single quotes, may double quote?
    # Our body-rewrite entries include single quotes around whole string: f"'{pattern} response-jq ...'"
    # That string starts with ', yaml will keep it as "'...'"? Let's check.
    # We'll post-process: yaml will dump strings with single quotes as "'...'"? For body-rewrite we already have outer single quotes, yaml will escape?
    # Alternative: store body-rewrite as plain without outer single quotes, let yaml handle quoting.
    # But our generated entries have outer single quotes intentional to match pinduoduo stash style: they have "'^https://... response-jq del(...)'" with outer single quotes in yaml.
    # In yaml, "'^https://...'" is a string with outer single quotes, content includes pattern and directive.
    # Our f"'{pattern} response-jq {jq}'" produces string that starts with single quote, ends with single quote, content inside may contain single quotes escaped.
    # yaml will output that string with appropriate quoting (maybe double quotes). That's okay.
    # Let's just keep yaml dump.
    output = "\n".join(header_lines) + "\n" + yaml_str
    # Fix: yaml may output `rules:` list with dash and space, but we want `rules:` as we set.
    # Ensure empty sections not present.
    return output

def convert_file(lpx_path: Path, out_path: Path, lpx_url: str = ""):
    text = lpx_path.read_text(encoding="utf-8")
    out = convert_lpx_to_stash(text, lpx_url)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(out, encoding="utf-8")
    print(f"converted {lpx_path} -> {out_path}")

def fetch_and_convert(url: str, out_path: Path, fetch_script_fallback: bool = True):
    text = fetch_text(url)
    out = convert_lpx_to_stash(text, url, fetch_script_fallback=fetch_script_fallback)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(out, encoding="utf-8")
    print(f"fetched {url} -> {out_path}")

def batch_fetch_convert(list_json_path: Path, out_dir: Path, skip_existing: bool = True, fetch_script_fallback: bool = False):
    data = json.loads(list_json_path.read_text(encoding="utf-8"))
    lists = data.get("lists", data if isinstance(data, list) else [])
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    skipped = 0
    total = len(lists)
    for idx, item in enumerate(lists):
        url_field = item.get("url","") if isinstance(item, dict) else str(item)
        m = re.search(r'https://kelee\.one[^"\']+\.lpx', url_field)
        if not m:
            if url_field.startswith("https://") and url_field.endswith(".lpx"):
                lpx_url = url_field
            else:
                continue
        else:
            lpx_url = m.group(0)
        fname = lpx_url.split("/")[-1].replace(".lpx", ".stoverride")
        out_path = out_dir / fname
        if skip_existing and out_path.exists():
            skipped += 1
            continue
        try:
            fetch_and_convert(lpx_url, out_path, fetch_script_fallback=fetch_script_fallback)
            count += 1
            if count % 20 == 0:
                print(f"progress {idx+1}/{total} done {count} skipped {skipped}")
            time.sleep(0.2)
        except Exception as e:
            print(f"failed {lpx_url}: {e}")
    print(f"batch done: {count} converted, {skipped} skipped to {out_dir}")

def main():
    parser = argparse.ArgumentParser(description="KeLee lpx -> stash stoverride converter")
    parser.add_argument("--lpx-url", help="Single lpx URL to fetch and convert (requires Loon UA)")
    parser.add_argument("--lpx-file", type=Path, help="Local .lpx file to convert")
    parser.add_argument("--out", type=Path, help="Output .stoverride path")
    parser.add_argument("--batch-list", type=Path, help="Path to hub.kelee.one/list.json for batch")
    parser.add_argument("--batch-out-dir", type=Path, default=REPO_ROOT / "stash" / "overrides" / "kelee", help="Batch output dir")
    parser.add_argument("--fetch-lpx", type=Path, help="Fetch list and save raw lpx files to dir")
    args = parser.parse_args()
    if args.lpx_url and args.out:
        fetch_and_convert(args.lpx_url, args.out)
    elif args.lpx_file and args.out:
        convert_file(args.lpx_file, args.out, lpx_url=str(args.lpx_file))
    elif args.batch_list:
        batch_fetch_convert(args.batch_list, args.batch_out_dir)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
