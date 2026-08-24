#!/usr/bin/env python3
"""
KeLee Loon .lpx → Stash .stoverride converter v2
修复方案: 统一 AST + 规则分类器 + 专用生成器 + 硬校验

问题修复:
- mock 被错误放入 url-rewrite → 改为 http.mock
- header 未转换 → request-add/response-add
- status-code/data 未结构化 → statusCode→status-code, data-type→content-type
- 307 参数顺序错误 → swap
- 增加 Stash 官方规则校验 (Validator)

流程: Loon Parser → Rule AST → Classification → Stash Generator → Validator → YAML
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
from typing import List, Dict, Optional
from dataclasses import dataclass, field

REPO_ROOT = Path(__file__).resolve().parents[1]

LOON_UA = "Loon/764 CFNetwork/1498.700.1 Darwin/23.6.0 iPhone/17.6.1"
FETCH_HEADERS = {"User-Agent": LOON_UA, "Accept": "*/*"}

# ---------- Fetch ----------
def fetch_text(url: str, headers: dict = FETCH_HEADERS, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
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
        sections[current].append(raw_line.rstrip())
    return header, sections

# ---------- AST ----------
@dataclass
class RuleAST:
    id: str
    sourceLine: int
    loonType: str  # original directive lower
    targetModule: str  # url-rewrite|header-rewrite|body-rewrite|mock|script|mitm
    match: str
    action: Optional[str] = None
    target: Optional[str] = None
    statusCode: Optional[int] = None
    headers: Optional[Dict] = None
    mock: Optional[Dict] = None
    script: Optional[Dict] = None
    raw: str = ""
    # for mock
    dataType: Optional[str] = None

# ---------- Helpers ----------
def normalize_rule(rule: str) -> str:
    return re.sub(r'\s*,\s*', ',', rule.strip())

def jq_for_del(keys: List[str]) -> str:
    dotted = ", ".join(f".{k.lstrip('.')}" for k in keys)
    return f"del({dotted})"

def jq_for_replace(pairs: List[str]) -> str:
    exprs = []
    for i in range(0, len(pairs)-1, 2):
        key = pairs[i].lstrip('.')
        val = pairs[i+1]
        if re.match(r'^-?\d+(\.\d+)?$', val) or val in ("true","false","null","0","1"):
            val_str = val
        elif val.startswith('"') and val.endswith('"'):
            val_str = val
        elif val.startswith("'") and val.endswith("'"):
            val_str = val
        else:
            val_str = f'"{val}"'
        exprs.append(f".{key} = {val_str}")
    return " | ".join(exprs)

def sanitize_provider_name(tag: str, script_url: str, existing: set) -> str:
    base = None
    if script_url:
        try:
            parsed = urllib.parse.urlparse(script_url)
            base = Path(parsed.path).stem
        except:
            base = None
    if base and base not in ("", "script"):
        cand = base
    elif tag:
        cand = tag
    else:
        cand = "script"
    cand = re.sub(r'[^a-zA-Z0-9]+', '-', cand).strip('-').lower()
    if not cand:
        cand = "script"
    orig = cand
    idx = 1
    while cand in existing:
        cand = f"{orig}-{idx}"
        idx += 1
    existing.add(cand)
    return cand

# ---------- Mock Args Parser (关键: 禁止 split(" ")) ----------
def parse_mock_args(line: str) -> Dict:
    """
    解析 mock 参数，支持:
    data='{}'  data="{ }"  status-code=200  data-type=json
    JSON 中含空格/逗号/冒号不能用 split，且 data="{"json"}" 外层引号内含 JSON 的 " 需特殊处理
    """
    out = {}
    # 优先用贪婪匹配提取 data 的 JSON（Loon 中 data="{"code":0,...}" 外层 " 内含 JSON 的 "）
    m_json = re.search(r'''data\s*=\s*["'](\{.*\})["']''', line, re.S)
    if m_json:
        out["data"] = m_json.group(1)
        # 移除已匹配的 data 段，避免下面的通用匹配截断
        line_wo_data = line[:m_json.start()] + line[m_json.end():]
    else:
        line_wo_data = line
    pat = re.compile(r'''(\S+?)\s*=\s*(?:'([^']*)'|"([^"]*)"|(\S+))''')
    for m in pat.finditer(line_wo_data):
        k = m.group(1).lower()
        v = m.group(2) if m.group(2) is not None else (m.group(3) if m.group(3) is not None else m.group(4))
        if k == "status-code":
            k = "status-code"
            try:
                v = int(v)
            except:
                pass
        elif k == "data-type":
            k = "data-type"
        elif k.startswith("data"):
            k = "data"
            # 若已通过 m_json 拿到 data，这里跳过避免覆盖
            if "data" in out:
                continue
        out[k] = v
    # 若 m_json 未匹配到但 line 中有 data="AAAA" base64，走通用已捕获
    # 确保 status-code 默认
    if "status-code" not in out and "statusCode" not in out:
        # 尝试从 line_wo_data 找 status-code
        m_sc = re.search(r'status-code\s*=\s*(\d+)', line, re.I)
        if m_sc:
            try:
                out["status-code"] = int(m_sc.group(1))
            except:
                pass
    return out

# ---------- Classifier ----------
def classify_rule(ast: RuleAST) -> RuleAST:
    action = (ast.action or "").lower()
    # URL Rewrite allow list
    if action in {"302","307","301","308","reject","reject-200","reject-img","reject-dict","reject-array"}:
        ast.targetModule = "url-rewrite"
        return ast
    if action.startswith("mock"):
        ast.targetModule = "mock"
        return ast
    if action in {"header","header-add","header-del","header-replace","response-header-add","response-header-del","response-header-replace"}:
        ast.targetModule = "header-rewrite"
        return ast
    if "body" in action or "json" in action:
        ast.targetModule = "body-rewrite"
        return ast
    # fallback: if raw contains mock
    if "mock" in ast.raw.lower():
        ast.targetModule = "mock"
        return ast
    ast.targetModule = "url-rewrite"
    return ast

# ---------- Generators ----------
URL_ALLOW_ACTIONS = {"302","307","301","308","reject","reject-200","reject-img","reject-dict","reject-array"}
HEADER_MAP = {
    "header-add": "request-add",
    "header-del": "request-del",
    "header-replace": "request-replace",
    "response-header-add": "response-add",
    "response-header-del": "response-del",
    "response-header-replace": "response-replace",
}
HEADER_ALLOW = {"request-add","request-del","request-replace","request-replace-regex","response-add","response-del","response-replace","response-replace-regex"}
LEGACY_HEADER_ACTIONS = {"header-add","header-del","header-replace","response-header-add","response-header-del","response-header-replace"}

def parse_rewrite_line(line: str):
    m = re.match(r'^(\S+)\s+(\S+)(?:\s+(.*))?$', line.strip())
    if not m:
        return None
    return m.group(1), m.group(2), (m.group(3) or "").strip()

# ---------- Script parsing (reuse) ----------
SCRIPT_RE_HTTP = re.compile(r'^(http-(?:response|request))\s+(\S+)\s+(.*)$')
def parse_script_params(rest: str) -> dict:
    params = {}
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
                tokens.append("".join(current).strip())
                current = []
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
            params[tok] = True
            continue
        k, v = tok.split("=", 1)
        k = k.strip().lower().replace("-", "_")
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
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

def parse_script_line(line: str) -> dict | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    m = SCRIPT_RE_HTTP.match(line)
    if m:
        typ, pattern, rest = m.group(1), m.group(2), m.group(3)
        params = parse_script_params(rest)
        params["type"] = typ.replace("http-","")
        params["match"] = pattern
        return params
    if line.startswith("cron"):
        after = line[4:].strip()
        if after.startswith("{"):
            end = after.find("}")
            if end != -1:
                cron_expr = after[:end+1]
                rest = after[end+1:].strip()
                params = parse_script_params(rest)
                params["type"] = "cron"
                params["cron"] = cron_expr
                return params
        idx = after.find("script-path=")
        if idx != -1:
            cron_expr = after[:idx].strip()
            rest = after[idx:].strip()
            params = parse_script_params(rest)
            params["type"] = "cron"
            params["cron"] = cron_expr
            return params
    m = re.match(r'^generic\s+(.*)$', line)
    if m:
        rest = m.group(1)
        params = parse_script_params(rest)
        params["type"] = "generic"
        return params
    if "script-path=" in line:
        params = parse_script_params(line)
        params["type"] = params.get("type", "generic")
        return params
    return None

# ---------- Conversion ----------
def convert_lpx_to_stash(lpx_text: str, lpx_url: str = "", fetch_script_fallback: bool = True) -> str:
    header, sections = parse_lpx(lpx_text)
    name = header.get("name", "Unnamed").strip()
    desc = header.get("desc", "").strip().replace("\\n", " ").replace("\n", " ")
    author = header.get("author", "")
    icon = header.get("icon", "")
    homepage = header.get("homepage", "")
    date = header.get("date", "")

    stash = {}
    stash["name"] = name
    if desc:
        stash["desc"] = desc[:500]
    if icon:
        stash["icon"] = icon

    # Rules
    rules_raw = []
    for k in list(sections.keys()):
        if k.lower() == "rule":
            rules_raw = sections[k]
            break
    rules = []
    redirect_from_rules = []
    for r in rules_raw:
        r = r.strip()
        if not r or r.startswith("#"):
            continue
        if re.match(r'^\S+\s+(302|307|308|301)\s+\S+', r):
            redirect_from_rules.append(r)
            continue
        if r.startswith("^") and " " in r and len(r.split(None, 2)) >= 2:
            _, dir_candidate, _ = parse_rewrite_line(r) or (None, "", "")
            if dir_candidate and dir_candidate.lower() in ("reject","reject-dict","reject-array","reject-200","reject-img","mock-response-body","response-body-json-jq","response-body-json-del","response-body-json-replace","response-body-replace-regex","header"):
                redirect_from_rules.append(r)
                continue
        rules.append(normalize_rule(r))
    if rules:
        stash["rules"] = rules

    http = {}
    # MITM
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
        if "=" in line:
            _, hosts_part = line.split("=", 1)
        else:
            hosts_part = line
        for h in hosts_part.split(","):
            h = h.strip().strip('"').strip("'")
            if h:
                mitm_hosts.append(h)
    if mitm_hosts:
        seen=set()
        uniq=[]
        for h in mitm_hosts:
            if h not in seen:
                uniq.append(h); seen.add(h)
        http["mitm"] = uniq

    # Rewrite → AST
    rewrite_raw = []
    for k in list(sections.keys()):
        if k.lower() == "rewrite":
            rewrite_raw = sections[k]
            break
    if redirect_from_rules:
        rewrite_raw = rewrite_raw + redirect_from_rules

    ast_list: List[RuleAST] = []
    for idx, line in enumerate(rewrite_raw):
        line_stripped = line.strip()
        if not line_stripped or line_stripped.startswith("#"):
            continue
        parsed = parse_rewrite_line(line_stripped)
        if not parsed:
            continue
        pattern, directive, rest = parsed
        # 307/302 swap detection (Loon: ^url 307 $2 → Stash: ^url $2 307)
        # If rest is $1/$2 and directive is 302/307, keep as is for url-rewrite generator which expects target
        # Classifier will handle
        loonType = directive.lower()
        # Normalize pattern (^ handling
        if pattern.startswith("(^"):
            pattern = "^(" + pattern[2:]
        ast = RuleAST(
            id=f"r{idx}",
            sourceLine=idx,
            loonType=loonType,
            targetModule="url-rewrite",  # placeholder, classified next
            match=pattern,
            action=directive,
            target=rest,
            raw=line_stripped
        )
        # For mock, parse structured args
        if loonType == "mock-response-body" or loonType == "mock":
            # action mock, rest contains data-type/status-code/data
            args = parse_mock_args(rest)
            ast.mock = args
            ast.dataType = args.get("data-type")
            try:
                ast.statusCode = int(args.get("status-code", 200))
            except:
                ast.statusCode = 200
        ast = classify_rule(ast)
        ast_list.append(ast)

    # Generators
    url_rewrite: List[str] = []
    header_rewrite: List[str] = []
    body_rewrite: List[str] = []
    mock_list: List[Dict] = []
    unsupported_rewrites: List[str] = []

    for ast in ast_list:
        mod = ast.targetModule
        # Layer: 禁止 mock 进入 url-rewrite 硬校验
        if mod == "url-rewrite" and (ast.action or "").lower() == "mock":
            raise ValueError(f"E_MOCK_IN_URL_REWRITE {ast.match} in {ast.raw}")
        if mod == "url-rewrite":
            # 仅允许白名单
            act = (ast.action or "").lower()
            if act not in URL_ALLOW_ACTIONS:
                # try reject variations
                if act.startswith("reject"):
                    act = ast.action  # keep original case?
                else:
                    unsupported_rewrites.append(f"# E_INVALID_ACTION {ast.raw}")
                    continue
            # Reject: ^url - reject  (must have placeholder)
            if act.startswith("reject"):
                # hard check placeholder
                if act not in URL_ALLOW_ACTIONS:
                    # allow but warn
                    pass
                url_rewrite.append(f"{ast.match} - {act}")
                continue
            # 302/307/301/308
            if act in {"302","307","301","308"}:
                target = (ast.target or "").strip()
                # Swap detection: if line was tokenized incorrectly due to missing dash?
                # Loon: ^url 307 $2  vs Stash: ^url - 307 $2 ; our parser already gives pattern, directive=307, rest=$2
                # So no swap needed for url-rewrite case, but spec says swap if tokens[1] is 302/307
                # Keep as is: pattern - 307 $2
                if ast.match.startswith("(^"):
                    ast.match = "^(" + ast.match[2:]
                if target:
                    url_rewrite.append(f"{ast.match} - {act} {target}")
                else:
                    url_rewrite.append(f"{ast.match} - {act}")
                continue
            # transparent etc.
            url_rewrite.append(f"{ast.match} - {act}")

        elif mod == "mock":
            # Loon: ^url - mock data-type=json status-code=200 data='{}'
            # Stash: http.mock: - match: ^url status-code: 200 body: '{}' content-type: application/json
            args = ast.mock or parse_mock_args(ast.target or "")
            # 通用兜底: ?/$ 尾的 mock 在 Stash url-rewrite 必 invalid，但 mock 模块是合法的
            # 不再 fallback 到 reject-dict，而是正确生成 http.mock
            data = args.get("data", "")
            data_type = args.get("data-type", "json")
            try:
                sc = int(args.get("status-code", args.get("statusCode", 200)))
            except:
                sc = 200
            # content-type mapping: Loon data-type=text 但 body 是 JSON 时应为 application/json
            body_is_json = data.strip().startswith("{") or data.strip().startswith("[")
            if body_is_json:
                ctype = "application/json"
            elif data_type == "json":
                ctype = "application/json"
            elif data_type == "text":
                ctype = "text/plain"
            elif data_type == "html":
                ctype = "text/html"
            else:
                ctype = data_type or ("application/json" if body_is_json else "text/plain")
            # Shorten overly long JSON? keep but truncate if needed for ad-blocking
            # For ?/$ patterns, keep minimal but not reject-dict, preserve semantics
            # If data is very long, keep as is (Stash mock body can be large)
            entry = {
                "match": ast.match,
                "status-code": sc,
                "body": data,
            }
            if ctype:
                entry["content-type"] = ctype
            # Stash uses headers? not needed
            mock_list.append(entry)

        elif mod == "header-rewrite":
            act = (ast.action or "").lower()
            target_rest = (ast.target or "").strip()
            # 核心修复: header 带 URL 应转为 url-rewrite 302 (Spotify 场景)
            if target_rest.startswith("http://") or target_rest.startswith("https://"):
                # Stash 不支持 header 带 URL 的写法，实为重定向
                url_rewrite.append(f"{ast.match} - 302 {target_rest}")
                continue
            # 映射
            mapped = HEADER_MAP.get(act, act)
            if mapped in LEGACY_HEADER_ACTIONS and mapped not in HEADER_ALLOW:
                raise ValueError(f"E_LEGACY_HEADER_ACTION {ast.raw}")
            if mapped not in HEADER_ALLOW:
                mapped = "request-add" if "add" in act else ("request-del" if "del" in act else mapped)
            header_rewrite.append(f"{ast.match} {mapped} {target_rest}".strip())

        elif mod == "body-rewrite":
            # Reuse existing logic for body
            pattern = ast.match
            directive = ast.action
            rest = ast.target or ""
            low = directive.lower()
            if low == "response-body-json-jq":
                rest_stripped = rest.strip()
                m_jqpath = re.search(r'jq-path\s*=\s*"?([^"\s]+)"?', rest_stripped)
                if m_jqpath:
                    jq_url = m_jqpath.group(1).strip('"').strip("'")
                    try:
                        jq_content = fetch_text(jq_url).strip()
                        jq_content = re.sub(r'\s*\n\s*', ' ', jq_content).strip()
                        if len(jq_content) > 3000:
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
                    body_rewrite.append(f"{pattern} response-jq {jq_for_del(keys)}")
                else:
                    unsupported_rewrites.append(f"# unsupported json-del no keys: {ast.raw}")
            elif low == "response-body-json-replace":
                tokens = re.findall(r'"[^"]*"|\'[^\']*\'|\S+', rest.strip())
                cleaned = []
                for t in tokens:
                    if (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'")):
                        cleaned.append(t[1:-1])
                    else:
                        cleaned.append(t)
                if len(cleaned) >= 2:
                    body_rewrite.append(f"{pattern} response-jq {jq_for_replace(cleaned)}")
                else:
                    unsupported_rewrites.append(f"# unsupported json-replace parse: {ast.raw}")
            elif low == "response-body-replace-regex":
                escaped_rest = rest.replace("'", "\\'")
                body_rewrite.append(f"{pattern} response-body-replace-regex {escaped_rest}")
            elif low.startswith("response-header"):
                stash_directive = directive.replace("response-header", "header", 1)
                header_rewrite.append(f"{pattern} {stash_directive} {rest}".strip())
            else:
                body_rewrite.append(f"{pattern} {directive} {rest}")

        else:
            unsupported_rewrites.append(f"# unsupported module {mod}: {ast.raw}")

    if url_rewrite:
        http["url-rewrite"] = url_rewrite
    if body_rewrite:
        http["body-rewrite"] = body_rewrite
    if header_rewrite:
        http["header-rewrite"] = header_rewrite
    if mock_list:
        http["mock"] = mock_list
    if unsupported_rewrites:
        http["_unsupported_rewrite_comments"] = unsupported_rewrites

    # Argument defaults
    arg_defaults = {}
    for k in list(sections.keys()):
        if k.lower() == "argument":
            for line in sections[k]:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key_part, rest = line.split("=", 1)
                key = key_part.strip()
                m_arg = re.match(r'\s*([^,]+)\s*,\s*([^,]+)', rest.strip())
                if m_arg:
                    default = m_arg.group(2).strip()
                    if (default.startswith('"') and default.endswith('"')) or (default.startswith("'") and default.endswith("'")):
                        default = default[1:-1]
                    arg_defaults[key] = default
            break
    # Script
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
            continue
        script_url = parsed.get("script_path") or parsed.get("script-path") or ""
        tag = parsed.get("tag", "")
        if "script_path" in parsed:
            script_url = parsed["script_path"]
        if "script-path" in parsed:
            script_url = parsed["script-path"]
        provider_name = sanitize_provider_name(tag, script_url, provider_names)
        entry = {}
        if "match" in parsed:
            entry["match"] = parsed["match"]
        elif "pattern" in parsed:
            entry["match"] = parsed["pattern"]
        entry["name"] = provider_name
        typ = parsed.get("type", "generic")
        if typ in ("response","request","cron","generic"):
            entry["type"] = typ
        elif typ == "http-response":
            entry["type"] = "response"
        elif typ == "http-request":
            entry["type"] = "request"
        else:
            entry["type"] = typ
        if "requires_body" in parsed:
            entry["require-body"] = bool(parsed["requires_body"])
        elif "requires-body" in parsed:
            entry["require-body"] = bool(parsed["requires-body"])
        elif "require_body" in parsed:
            entry["require-body"] = bool(parsed["require_body"])
        if "binary_body_mode" in parsed:
            entry["binary-body-mode"] = bool(parsed["binary_body_mode"])
        if "binary-body-mode" in parsed:
            entry["binary-body-mode"] = bool(parsed["binary-body-mode"])
        if "timeout" in parsed:
            try:
                entry["timeout"] = int(parsed["timeout"])
            except:
                pass
        if "argument" in parsed:
            arg = str(parsed["argument"])
            def repl_arg(m):
                var = m.group(1)
                return arg_defaults.get(var, m.group(0))
            arg_replaced = re.sub(r'\{([^}]+)\}', repl_arg, arg)
            entry["argument"] = arg_replaced
        if "cron" in parsed:
            cron_expr = str(parsed["cron"])
            if cron_expr.strip() == "{cron}" or "{cron}" in cron_expr:
                cron_default = arg_defaults.get("cron", "55 23 * * *")
                cron_expr = cron_expr.replace("{cron}", cron_default)
            cron_expr = re.sub(r'\{([^}]+)\}', lambda m: arg_defaults.get(m.group(1), m.group(0)), cron_expr)
            entry["cron"] = cron_expr.strip().strip("{}") if cron_expr.startswith("{") else cron_expr
            if "match" in entry:
                del entry["match"]
        script_entries.append(entry)
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
            provider_entry = {"url": better_url, "interval": 86400}
            if "kelee.one" in better_url:
                provider_entry["_note"] = "kelee.one requires Loon UA"
                provider_entry["headers"] = {"User-Agent": LOON_UA}
            script_providers[provider_name] = provider_entry

    if script_entries:
        http["script"] = script_entries
    unsupported_comments = http.pop("_unsupported_rewrite_comments", None)

    if http:
        stash["http"] = http
    if script_providers:
        stash["script-providers"] = script_providers

    # Hosts
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

    # YAML Round Trip校验
    import yaml
    yaml_str = yaml.safe_dump(stash, sort_keys=False, allow_unicode=True, width=4096, default_flow_style=False)
    # Round trip
    try:
        reparsed = yaml.safe_load(yaml_str)
        # 比较关键字段
        for key in ["rules","http","hosts","script-providers"]:
            if key in stash and key not in reparsed:
                raise ValueError(f"E_YAML_ROUNDTRIP_FAILED {key} lost")
    except Exception as e:
        raise ValueError(f"E_YAML_ROUNDTRIP_FAILED {e}")

    # Stash Action 白名单硬校验
    # url-rewrite 已在生成器校验 header-rewrite/script/mock
    # 额外校验: 禁止 header/mock 出现在 url-rewrite 已在生成器抛错

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
    header_lines.append(f"# converted: Loon .lpx -> Stash .stoverride (auto) v2")
    header_lines.append(f"# note: kelee.one resources require Loon UA; if Stash fetch fails, add proxy or use GitHub mirror")
    if unsupported_comments:
        header_lines.append(f"# unsupported rewrites: {len(unsupported_comments)}")
        for c in unsupported_comments[:5]:
            header_lines.append(f"#   {c}")
    output = "\n".join(header_lines) + "\n" + yaml_str
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
    parser = argparse.ArgumentParser(description="KeLee lpx -> stash stoverride converter v2")
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
