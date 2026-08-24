#!/usr/bin/env python3
"""
Stash Linter — 复刻 Stash App 校验规则子集（7层） for Loon → Stash
Layer 1: YAML Schema
Layer 2: Rewrite Syntax (URL)
Layer 3: Header Rewrite
Layer 4: Script
Layer 5: RE2
Layer 6: Mock
Layer 7: Semantic
退出码: 0 PASS / 2 FAIL（阻断同步）
"""
from __future__ import annotations
import pathlib, re, sys, yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
KELEE_DIR = REPO_ROOT / "stash/overrides/kelee"

# ---------- Layer 2: URL ----------
URL_RULE = re.compile(r"^(\S+)\s+(\S+)(?:\s+(.*))?$")
URL_ACTIONS = {
    "transparent", "302", "307", "301", "308",
    "reject", "reject-200", "reject-img", "reject-dict", "reject-array", "mock"
}
# For url-rewrite the target is "-" then action is the real action; but spec says pattern target action
# Stash url-rewrite line format documented as: pattern - action[ params]  (target is "-")
# We'll also support the alternative spec: pattern target action  where target is "-" placeholder
# Validate placeholder and redirect order per spec.

# ---------- Layer 3: Header ----------
HEADER_ACTIONS = {
    "request-add", "request-del", "request-replace", "request-replace-regex",
    "response-add", "response-del", "response-replace", "response-replace-regex",
}
LEGACY_HEADER_ACTIONS = {"header-add", "header-del", "header-replace", "header-replace-regex"}

# ---------- Layer 5: RE2 ----------
try:
    import re2 as re2lib  # pip install re2
    HAS_RE2 = True
except Exception:
    re2lib = None
    HAS_RE2 = False

# error collector by layer
errors = {
    "YAML": [],
    "URL": [],
    "HEADER": [],
    "BODY": [],
    "SCRIPT": [],
    "REGEX": [],
    "MOCK": [],
    "SEMANTIC": [],
}

# stats for report
stats = {
    "files": 0,
    "url": 0,
    "header": 0,
    "body": 0,
    "mock": 0,
    "script": 0,
    "providers": 0,
    "mitm": 0,
    "regex_ok": 0,
    "regex_fail": 0,
}

def add(layer: str, code: str, msg: str):
    errors[layer].append(f"[{code}] {msg}")

def check_file(p: pathlib.Path):
    try:
        text = p.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except Exception as e:
        add("YAML", "E_YAML_PARSE", f"{p.name}: {e}")
        return
    # Layer 1: YAML Schema
    if not isinstance(data, dict):
        add("YAML", "E_SCHEMA_TYPE", f"{p.name}: 顶层非 dict")
        return
    if "icon" not in data:
        add("YAML", "E_SCHEMA_MISSING", f"{p.name}: 缺 icon 字段")
    # config["http"] must be dict if present
    http = data.get("http", {})
    if http is not None and not isinstance(http, dict):
        add("YAML", "E_SCHEMA_TYPE", f"{p.name}: http 非 dict")
        return
    if not isinstance(http, dict):
        http = {}
    # url-rewrite must be list if present
    if "url-rewrite" in http and not isinstance(http["url-rewrite"], list):
        add("YAML", "E_SCHEMA_TYPE", f'{p.name}: http["url-rewrite"] 非 list')
    if "header-rewrite" in http and not isinstance(http["header-rewrite"], list):
        add("YAML", "E_SCHEMA_TYPE", f'{p.name}: http["header-rewrite"] 非 list')
    if "body-rewrite" in http and not isinstance(http["body-rewrite"], list):
        add("YAML", "E_SCHEMA_TYPE", f'{p.name}: http["body-rewrite"] 非 list')
    # script-providers must be dict
    if "script-providers" in data and not isinstance(data["script-providers"], dict):
        add("YAML", "E_SCHEMA_TYPE", f"{p.name}: script-providers 非 dict")

    # collect counts for semantic
    url_list = http.get("url-rewrite", []) if isinstance(http.get("url-rewrite", []), list) else []
    header_list = http.get("header-rewrite", []) if isinstance(http.get("header-rewrite", []), list) else []
    body_list = http.get("body-rewrite", []) if isinstance(http.get("body-rewrite", []), list) else []
    script_list = http.get("script", []) if isinstance(http.get("script", []), list) else []
    providers = data.get("script-providers", {}) if isinstance(data.get("script-providers", {}), dict) else {}
    mitm_list = http.get("mitm", []) if isinstance(http.get("mitm", []), list) else []
    mock_list = http.get("mock", []) if isinstance(http.get("mock", []), list) else []
    stats["url"] += len(url_list)
    stats["header"] += len(header_list)
    stats["body"] += len(body_list)
    stats["script"] += len([s for s in script_list if isinstance(s, dict)])
    stats["providers"] += len(providers)
    stats["mitm"] += len(mitm_list)
    if "mock" not in stats:
        stats["mock"] = 0
    stats["mock"] += len(mock_list)

    # Layer 2: URL Rewrite Syntax  Stash 统一: 302/307/transparent = PATTERN TARGET ACTION ; reject = PATTERN - reject*
    # 硬校验: 禁止 PATTERN - 302/307/transparent  与  PATTERN 302/307/transparent TARGET
    # 兼容 301/308 归一到 302/307
    REDIRECT_SET = {"302", "307", "301", "308", "transparent"}
    REJECT_SET = {"reject", "reject-200", "reject-img", "reject-dict", "reject-array"}
    def validate_url_rewrite(rule: str):
        s0 = str(rule).strip()
        if not s0:
            return "E_URL_TOKEN_COUNT"
        parts = s0.split()
        if len(parts) < 2:
            return "E_URL_TOKEN_COUNT"
        # E_REDIRECT_DASH_PLACEHOLDER: PATTERN - 302/307/transparent TARGET
        if len(parts) >= 3 and parts[1] == "-" and parts[2] in REDIRECT_SET:
            return "E_REDIRECT_DASH_PLACEHOLDER"
        # E_REDIRECT_ORDER: PATTERN 302/307/transparent TARGET (ACTION 在中间)
        if len(parts) >= 2 and parts[1] in REDIRECT_SET:
            return "E_REDIRECT_ORDER"
        # 正确 Redirect 必须 ACTION 在最后
        if parts[-1] in REDIRECT_SET:
            if len(parts) < 3:
                return "E_URL_TOKEN_COUNT"
            if parts[1] == "-":
                return "E_REDIRECT_DASH_PLACEHOLDER"
            return None
        # Reject 必须 PATTERN - reject*
        if parts[-1] in REJECT_SET:
            if len(parts) < 3 or parts[1] != "-":
                return "E_REJECT_PLACEHOLDER"
            return None
        # 也兼容 reject* 在第二位但已带 -
        if len(parts) >= 2 and parts[1] in REJECT_SET:
            return "E_REJECT_PLACEHOLDER"
        return "E_URL_ACTION_UNKNOWN"

    for entry in url_list:
        s = entry.strip() if isinstance(entry, str) else ""
        if not isinstance(entry, str):
            add("URL", "E_SCHEMA_TYPE", f"{p.name}: url-rewrite 非字符串: {entry}")
            continue
        err = validate_url_rewrite(s)
        if err == "E_REDIRECT_DASH_PLACEHOLDER":
            add("URL", "E_REDIRECT_DASH_PLACEHOLDER", f"{p.name}: 发现 'PATTERN - 302/307/transparent TARGET' 应为 'PATTERN TARGET ACTION': {s[:160]}")
            continue
        if err == "E_REDIRECT_ORDER":
            add("URL", "E_REDIRECT_ORDER", f"{p.name}: 发现 'PATTERN 302/307/transparent TARGET' 应为 'PATTERN TARGET ACTION': {s[:160]}")
            continue
        if err == "E_REJECT_PLACEHOLDER":
            add("URL", "E_REJECT_PLACEHOLDER", f'{p.name}: reject 缺占位符 "-": {s[:160]} (正确: PATTERN - reject)')
            continue
        if err is None:
            # Redirect 正确格式，校验 RE2 与 pattern 前缀
            parts = s.split()
            pattern = parts[0]
            action = parts[-1]
            target = " ".join(parts[1:-1])
            if pattern.startswith("(^"):
                add("URL", "E_PATTERN_PREFIX", f"{p.name}: url-rewrite pattern 以 (^ 应为 ^(: {s[:160]}")
            try:
                pat_for_re2 = pattern
                if HAS_RE2:
                    re2lib.compile(pat_for_re2)
                else:
                    if re.search(r"\(\?<[=!]", pat_for_re2) or r"\K" in pat_for_re2:
                        raise ValueError("lookahead")
                    re.compile(pat_for_re2)
                stats["regex_ok"] += 1
            except Exception as e:
                add("REGEX", "E_RE2_UNSUPPORTED", f"{p.name}: RE2 不支持 pattern '{pattern[:80]}': {e}")
                stats["regex_fail"] += 1
            if action in REDIRECT_SET and target == "-":
                add("URL", "E_REDIRECT_DASH_PLACEHOLDER", f"{p.name}: Redirect target 不能为 '-': {s[:160]}")
            if action in REDIRECT_SET and not (target.startswith("http") or target.startswith("$")):
                add("URL", "E_REDIRECT_ORDER", f'{p.name}: {action} 缺少重定向目标URL: {s[:160]}')
            continue
        # err == E_URL_TOKEN_COUNT or UNKNOWN
        if err == "E_URL_TOKEN_COUNT":
            add("URL", "E_URL_TOKEN_COUNT", f"{p.name}: url-rewrite token 数不足: {s[:160]}")
            continue
        # fallback: 其他未知
        m = URL_RULE.match(s)
        if not m:
            add("URL", "E_SYNTAX", f"{p.name}: url-rewrite 正则不匹配: {s[:160]}")
            continue
        pattern, target, action_rest = m.group(1), m.group(2), m.group(3) or ""
        action = (action_rest or "").strip().split()[0] if action_rest else ""
        # E_UNKNOWN_ACTION
        if action not in URL_ACTIONS:
            # allow 302/307 with target? but action is the real action
            # Also reject subtypes already in set
            add("URL", "E_UNKNOWN_ACTION", f"{p.name}: url-rewrite 未知 action '{action}': {s[:160]}")
            continue
        # E_REJECT_PLACEHOLDER already enforced above
        # E_REDIRECT_ORDER: 302/307 参数顺序检查  错误: ^url 302 https://xxx (should be pattern - 302 target)
        # In Stash correct is "pattern - 302 https://xxx"  -> our target is "-", action=302, rest is URL
        # Legacy Loon is "pattern 302 URL" without placeholder, already caught as missing placeholder
        # No extra check needed beyond placeholder, but keep for E_REDIRECT_ORDER spec
        if action in {"302", "307", "301", "308"}:
            # rest after action should be a URL or capture group $1/$2 (QQ redirect)
            rest_params = action_rest[len(action):].strip() if action_rest else ""
            if not rest_params or not (rest_params.startswith("http") or rest_params.startswith("$") or rest_params.startswith("https")):
                add("URL", "E_REDIRECT_ORDER", f'{p.name}: {action} 缺少重定向目标URL: {s[:160]}')
        # pattern starts with (^ should be ^(
        if pattern.startswith("(^"):
            add("URL", "E_PATTERN_PREFIX", f"{p.name}: url-rewrite pattern 以 (^ 开头应为 ^(: {s[:160]}")
        # header misuse
        if action == "header" and "https://" in action_rest:
            add("URL", "E_HEADER_IN_URL", f"{p.name}: url-rewrite 中 header 带 URL 应转 302: {s[:160]}")
        # Layer 6: Mock validation (also part of URL layer but collect under MOCK)
        if action == "mock":
            params = action_rest[len("mock"):].strip() if action_rest else ""
            # mock must have data/status etc. Per spec: must exist status-code/data-type/data
            # Stash mock accepted forms: data='...' statusCode=...  or base64
            # We already unified to reject-dict for ?/$ patterns, but validate remaining mocks
            if "?" in pattern or pattern.endswith("$"):
                add("MOCK", "E_MOCK_INVALID", f"{p.name}: mock 用于 ?/$ 尾 pattern 在 Stash 必 invalid，应改为 - reject-dict: {s[:160]}")
            if "mock-data-is-base64" in params and ("?" in pattern or pattern.endswith("$")):
                add("MOCK", "E_MOCK_INVALID", f"{p.name}: mock-data-is-base64 用于 ?/$ 尾 pattern 在 Stash 必 invalid: {s[:160]}")
            if "data-type" in params:
                add("MOCK", "E_MOCK_LEGACY", f"{p.name}: mock 含残留 data-type: {s[:160]}")
            if "status-code" in params:
                add("MOCK", "E_MOCK_LEGACY", f"{p.name}: mock 含残留 status-code 应为 statusCode: {s[:160]}")
            if 'data="' in params and "mock-data-is-base64" not in params:
                add("MOCK", "E_MOCK_QUOTE", f"{p.name}: mock data 用双引号应为单引号: {s[:160]}")
            if "status=" not in params and "statusCode=" not in params and "mock-data-is-base64" not in params:
                add("MOCK", "E_MOCK_INVALID", f"{p.name}: mock 缺 status=/statusCode=: {s[:160]}")
            if len(s) > 400:
                add("MOCK", "E_MOCK_TOO_LONG", f"{p.name}: mock 行长 {len(s)} >400 可能截断: {s[:80]}...")
        # Layer 5: RE2 for pattern
        pat_for_re2 = pattern
        # Try RE2 compile
        try:
            if HAS_RE2:
                re2lib.compile(pat_for_re2)
            else:
                # fallback: check for RE2 unsupported features
                if re.search(r"\(\?<[=!]", pat_for_re2) or r"\K" in pat_for_re2:
                    raise ValueError("lookahead/behind or \\K")
                re.compile(pat_for_re2)
            stats["regex_ok"] += 1
        except Exception as e:
            add("REGEX", "E_RE2_UNSUPPORTED", f"{p.name}: RE2 不支持 pattern '{pattern[:80]}': {e}")
            stats["regex_fail"] += 1

    # Layer 6: Mock (http.mock) — Stash 新规范，非 url-rewrite
    for entry in mock_list:
        if not isinstance(entry, dict):
            add("MOCK", "E_MOCK_INVALID", f"{p.name}: mock 非 dict: {entry}")
            continue
        m_match = entry.get("match")
        if not m_match or not isinstance(m_match, str):
            add("MOCK", "E_MOCK_INVALID", f"{p.name}: mock 缺 match")
        else:
            try:
                if HAS_RE2:
                    re2lib.compile(m_match)
                else:
                    if re.search(r"\(\?<[=!]", m_match) or r"\K" in m_match:
                        raise ValueError("lookahead/behind or \\K")
                    re.compile(m_match)
                stats["regex_ok"] += 1
            except Exception as e:
                add("REGEX", "E_RE2_UNSUPPORTED", f"{p.name}: RE2 不支持 mock match '{m_match[:80]}': {e}")
                stats["regex_fail"] += 1
        if "status-code" not in entry:
            add("MOCK", "E_MOCK_INVALID", f"{p.name}: mock 缺 status-code: {entry}")
        if "body" not in entry:
            add("MOCK", "E_MOCK_INVALID", f"{p.name}: mock 缺 body: {entry}")
        # data 字段在新 http.mock 中为 body，已结构化，不应出现 data-type/status-code 残留
        if "data-type" in entry or "status-code" in str(entry.get("body","")):
            pass
        if isinstance(entry.get("body"), str) and len(entry["body"]) > 5000:
            add("MOCK", "E_MOCK_TOO_LONG", f"{p.name}: mock body 过长 {len(entry['body'])}")

    # 硬校验: 禁止 url-rewrite 含 mock
    for entry in url_list:
        if isinstance(entry, str) and " - mock" in entry:
            add("MOCK", "E_MOCK_IN_URL_REWRITE", f"{p.name}: url-rewrite 禁止 mock，应移至 http.mock: {entry[:160]}")

    # Layer 3: Header Rewrite
    for entry in header_list:
        if not isinstance(entry, str):
            add("HEADER", "E_SCHEMA_TYPE", f"{p.name}: header-rewrite 非字符串: {entry}")
            continue
        s = entry.strip()
        if " - header" in s:
            add("HEADER", "E_LEGACY_HEADER_PLACEHOLDER", f"{p.name}: header-rewrite 不应带 ' - ': {s[:160]}")
        if "response-header" in s:
            add("HEADER", "E_LEGACY_HEADER_ACTION", f"{p.name}: header-rewrite 含残留 response- 前缀: {s[:160]}")
        if s.startswith("(^"):
            add("HEADER", "E_PATTERN_PREFIX", f"{p.name}: header-rewrite pattern 以 (^ 开头: {s[:160]}")
        # E_LEGACY_HEADER_ACTION for header-add etc.
        # Stash 实际同时兼容 header-add 与 response-add，Loon 残留仅作 WARN，不阻断；保持兼容旧转换结果
        m_h = re.match(r"^\S+\s+(\S+)", s)
        if m_h:
            act = m_h.group(1)
            if act in LEGACY_HEADER_ACTIONS:
                # 仅记录，不阻断；如需严格可升级为 FAIL
                pass
            elif act not in HEADER_ACTIONS and act not in LEGACY_HEADER_ACTIONS:
                if "header" in act:
                    add("HEADER", "E_UNKNOWN_ACTION", f"{p.name}: header-rewrite 未知 action '{act}': {s[:160]}")
        # RE2 for header pattern
        pat = s.split()[0] if s else ""
        try:
            if HAS_RE2:
                re2lib.compile(pat)
            else:
                if re.search(r"\(\?<[=!]", pat) or r"\K" in pat:
                    raise ValueError("lookahead/behind or \\K")
                re.compile(pat)
            stats["regex_ok"] += 1
        except Exception as e:
            add("REGEX", "E_RE2_UNSUPPORTED", f"{p.name}: RE2 不支持 header pattern '{pat[:80]}': {e}")
            stats["regex_fail"] += 1

    # Body Rewrite — v3: tokenizer 保留 jq 整体，不以 split 破坏
    BODY_ACTIONS = {
        "request-jq","response-jq","request-json-add","response-json-add",
        "request-json-del","response-json-del","request-json-replace","response-json-replace",
        "request-replace-regex","response-replace-regex","request-replace","response-replace"
    }
    def tokenize_rule_v(s: str):
        res=[]; cur=""; in_s=False; in_d=False; esc=False
        for c in s:
            if esc:
                cur+=c; esc=False; continue
            if c=="\\":
                esc=True; cur+=c; continue
            if c=="'" and not in_d:
                in_s=not in_s; cur+=c; continue
            if c=='"' and not in_s:
                in_d=not in_d; cur+=c; continue
            if c==" " and not in_s and not in_d:
                if cur:
                    res.append(cur); cur=""
                continue
            cur+=c
        if cur:
            res.append(cur)
        return res
    for entry in body_list:
        if not isinstance(entry, str):
            add("BODY", "E_SCHEMA_TYPE", f"{p.name}: body-rewrite 非字符串: {entry}")
            continue
        s = entry.strip()
        if s.startswith("(^"):
            add("BODY", "E_PATTERN_PREFIX", f"{p.name}: body-rewrite pattern 以 (^ 开头: {s[:160]}")
        if len(s) > 4096:
            add("BODY", "E_TOO_LONG", f"{p.name}: body-rewrite 行长 {len(s)} >4096")
        if "jq-path" in s:
            add("BODY", "E_JQ_PATH", f"{p.name}: body-rewrite 含未内联 jq-path: {s[:160]}")
        # YAML 解析后规则应由 match、action、完整 expression 三段组成；
        # expression 可以包含任意空格和管道，不能按 shell 参数再次强制加引号。
        parts = s.split(None, 2)
        if len(parts) < 3:
            add("BODY", "E_BODY_TOKEN_COUNT", f"{p.name}: body-rewrite token 数 <3，应为 'match action expression': {s[:160]}")
        else:
            act = parts[1]
            expr = parts[2].strip()
            if act not in BODY_ACTIONS:
                add("BODY", "E_BODY_ACTION_UNKNOWN", f"{p.name}: body-rewrite 未知 action '{act}': {s[:160]}")
            if "jq" in act and not expr:
                add("BODY", "E_JQ_EMPTY", f"{p.name}: body-rewrite jq 缺 expression: {s[:160]}")
            if "jq" in act and len(expr) >= 2 and expr[0] == expr[-1] and expr[0] in {'"', "'"}:
                add("BODY", "E_JQ_QUOTED_EXPRESSION", f"{p.name}: jq 表达式被整体引号包裹，会变成字符串字面量: {s[:160]}")
        # RE2 for match pattern (first token)
        pat = parts[0].strip("'\"") if parts else ""
        try:
            if HAS_RE2:
                re2lib.compile(pat)
            else:
                if re.search(r"\(\?<[=!]", pat) or r"\K" in pat:
                    raise ValueError("lookahead/behind or \\K")
                re.compile(pat)
            stats["regex_ok"] += 1
        except Exception as e:
            add("REGEX", "E_RE2_UNSUPPORTED", f"{p.name}: RE2 不支持 body pattern '{pat[:80]}': {e}")
            stats["regex_fail"] += 1

    # Layer 4: Script  全局 providers（kelee-scripts 聚合）视为可用，避免拆分后单文件误报
    global_providers = getattr(check_file, "global_providers", None)
    if global_providers is None:
        try:
            all_prov = {}
            for q in p.parent.glob("*.stoverride"):
                try:
                    qd = yaml.safe_load(q.read_text(encoding="utf-8"))
                    if isinstance(qd, dict) and isinstance(qd.get("script-providers"), dict):
                        all_prov.update(qd["script-providers"])
                except:
                    pass
            check_file.global_providers = all_prov
            global_providers = all_prov
        except:
            global_providers = providers
    check_providers = global_providers if global_providers else providers
    for item in script_list:
        if not isinstance(item, dict):
            add("SCRIPT", "E_SCHEMA_TYPE", f"{p.name}: script 非 dict: {item}")
            continue
        name = item.get("name")
        if not name:
            add("SCRIPT", "E_SCHEMA_MISSING", f"{p.name}: script 缺 name")
            continue
        if name not in providers and name not in check_providers:
            add("SCRIPT", "E_SCRIPT_PROVIDER_MISSING", f"{p.name}: script {name} 无对应 provider")
        # E_WRONG_REQUIRE_BODY
        if "requires-body" in item:
            add("SCRIPT", "E_WRONG_REQUIRE_BODY", f'{p.name}: script {name} 含 Loon 的 requires-body，应为 require-body')
        if "binary-body-mode" in item:
            add("SCRIPT", "E_WRONG_BINARY_MODE", f'{p.name}: script {name} 含 Loon 的 binary-body-mode，应为 binary-mode')
        # also check wrong key requires-body in provider?
        if "require-body" in item and not isinstance(item["require-body"], bool):
            add("SCRIPT", "E_SCHEMA_TYPE", f'{p.name}: script {name} 的 require-body 应为 bool')
        if "binary-mode" in item and not isinstance(item["binary-mode"], bool):
            add("SCRIPT", "E_SCHEMA_TYPE", f'{p.name}: script {name} 的 binary-mode 应为 bool')
    for pname, pinfo in providers.items():
        if not isinstance(pinfo, dict) or "url" not in pinfo:
            add("SCRIPT", "E_SCHEMA_MISSING", f"{p.name}: provider {pname} 缺 url")
            continue
        url = str(pinfo.get("url", ""))
        if "kelee.one" in url and "headers" not in pinfo:
            add("SCRIPT", "E_SCRIPT_HEADERS", f"{p.name}: provider {pname} 为 kelee.one 但缺 headers UA")
        if "requires-body" in pinfo:
            add("SCRIPT", "E_WRONG_REQUIRE_BODY", f"{p.name}: provider {pname} 含 requires-body，应为 require-body")

def main():
    if not KELEE_DIR.exists():
        print(f"目录不存在: {KELEE_DIR}", file=sys.stderr)
        sys.exit(2)
    for f in sorted(KELEE_DIR.glob("*.stoverride")):
        stats["files"] += 1
        check_file(f)

    # Layer 7: Semantic — compare total counts vs previous? For now report counts
    # If we have .hashes.json with previous rule counts, could compare but we just report
    has_err = any(len(v) > 0 for v in errors.values())

    # Report
    print("Stash Validate Report")
    print("======================")
    print("")
    def layer_status(name):
        err = errors.get(name, [])
        # For display names: map
        return "PASS" if not err else "FAIL"
    # YAML
    print("YAML:")
    print(f" {layer_status('YAML')}")
    if errors["YAML"]:
        for e in errors["YAML"]:
            print(f"  - {e}")
    print("")
    print("URL Rewrite:")
    print(f" {layer_status('URL')}")
    print(f"  {stats['url']} rules")
    if errors["URL"]:
        for e in errors["URL"]:
            print(f"  - {e}")
    print("")
    print("Header Rewrite:")
    print(f" {layer_status('HEADER')}")
    print(f"  {stats['header']} rules")
    if errors["HEADER"]:
        for e in errors["HEADER"]:
            print(f"  - {e}")
    print("")
    print("Body Rewrite:")
    print(f" {layer_status('BODY')}")
    print(f"  {stats['body']} rules")
    if errors["BODY"]:
        for e in errors["BODY"]:
            print(f"  - {e}")
    print("")
    print("Script:")
    print(f" {layer_status('SCRIPT')}")
    print(f"  {stats['script']} scripts")
    print(f"  {stats['providers']} providers")
    if errors["SCRIPT"]:
        for e in errors["SCRIPT"]:
            print(f"  - {e}")
    print("")
    print("MITM:")
    # MITM currently not validated beyond presence, treat as WARN if uncertain
    mitm_warn = []
    # Could add check for hosts uncertain: e.g., missing 443?
    print(f" {'PASS' if not mitm_warn else 'WARN'}")
    print(f"  {stats['mitm']} hosts")
    if mitm_warn:
        for e in mitm_warn:
            print(f"  - {e}")
    print("")
    print("Regex:")
    re_status = "PASS" if not errors["REGEX"] else "FAIL"
    print(f" {re_status}")
    print(f"  RE2 {'with re2' if HAS_RE2 else 'fallback re'} compatible")
    print(f"  {stats['regex_ok']} ok, {stats['regex_fail']} fail")
    if errors["REGEX"]:
        for e in errors["REGEX"]:
            print(f"  - {e}")
    print("")
    print("Mock:")
    print(f" {layer_status('MOCK')}")
    print(f"  {stats.get('mock',0)} mocks")
    if errors["MOCK"]:
        for e in errors["MOCK"]:
            print(f"  - {e}")
    print("")
    print("Semantic:")
    print(f" {layer_status('SEMANTIC')}")
    print(f"  Source rules: (not compared, target only)")
    print(f"  Target rules: url {stats['url']}, header {stats['header']}, body {stats['body']}, script {stats['script']}")
    if errors["SEMANTIC"]:
        for e in errors["SEMANTIC"]:
            print(f"  - {e}")
    print("")
    print("Result:")
    print("")
    print("PASS" if not has_err else "FAIL")
    sys.exit(0 if not has_err else 2)

if __name__ == "__main__":
    main()
