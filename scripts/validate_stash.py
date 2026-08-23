#!/usr/bin/env python3
"""
离线 Stash 校验脚本（先分析 Loon vs Stash 差异后编写，不依赖 Stash 运行时）
基于 findings.md 的 7 类差异静态检查，退出码 0 通过 / 2 失败（阻断同步）
"""
from __future__ import annotations
import pathlib, re, sys, yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
KELEE_DIR = REPO_ROOT / "stash/overrides/kelee"

# Stash 允许的 url-rewrite 指令（带 - 前缀）
URL_REWRITE_DIRECTIVES = {"reject", "reject-dict", "reject-200", "reject-img", "302", "307", "308", "301", "mock", "header", "302", "307"}
HEADER_DIRECTIVES = {"header-add", "header-del", "header-replace", "header_replace"}
ALLOW_MOCK_PARAMS = {"status", "data", "header", "mock-data-is-base64", "data-type"}  # data-type 应已去除，此处仅用于检测残留

errs = []

def check_file(p: pathlib.Path):
    try:
        text = p.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except Exception as e:
        errs.append(f"{p.name}: YAML 解析失败: {e}")
        return
    if not isinstance(data, dict):
        errs.append(f"{p.name}: 顶层非 dict")
        return
    # icon 必须存在（ findings 要求）
    if "icon" not in data:
        errs.append(f"{p.name}: 缺 icon 字段")
    # http 段
    http = data.get("http", {})
    if not isinstance(http, dict):
        return
    # url-rewrite
    for entry in http.get("url-rewrite", []):
        if not isinstance(entry, str):
            errs.append(f"{p.name}: url-rewrite 非字符串: {entry}")
            continue
        s = entry.strip()
        # 必须含 " - "
        if " - " not in s:
            errs.append(f"{p.name}: url-rewrite 缺 ' - ': {s[:120]}")
            continue
        pattern, rest = s.split(" - ", 1)
        pattern = pattern.strip()
        rest = rest.strip()
        if pattern.startswith("(^"):
            errs.append(f"{p.name}: url-rewrite pattern 以 (^ 开头应为 ^(: {s[:120]}")
        # 已通过 " - " 拆分，rest 以指令开头为正确；仅当原串含 " 307 " 但不含 " - 307 " 时才算缺 dash（已在上一行涵盖）
        # mock 专项（对所有脚本生效：?/$ 尾的 mock JSON/base64 在 Stash 必 invalid）
        if rest.startswith("mock"):
            if "?" in pattern or pattern.endswith("$"):
                errs.append(f"{p.name}: mock 用于 ?/$ 尾 pattern 在 Stash 必 invalid，应改为 - reject-dict: {s[:120]}")
            if "data-type" in rest:
                errs.append(f"{p.name}: mock 含残留 data-type: {s[:120]}")
            if "status-code" in rest:
                errs.append(f"{p.name}: mock 含残留 status-code 应为 statusCode: {s[:120]}")
            if 'data="' in rest and "mock-data-is-base64" not in rest:
                errs.append(f"{p.name}: mock data 用双引号应为单引号: {s[:120]}")
            if "status=" not in rest and "statusCode=" not in rest and "mock-data-is-base64" not in rest:
                # mock-data-is-base64 的 base64 mock 可无 statusCode，单独校验
                errs.append(f"{p.name}: mock 缺 status=/statusCode=: {s[:120]}")
            if "mock-data-is-base64" in rest and ("?" in pattern or pattern.endswith("$")):
                errs.append(f"{p.name}: mock-data-is-base64 用于 ?/$ 尾 pattern 在 Stash 必 invalid: {s[:120]}")
            # 行长
            if len(s) > 400:
                errs.append(f"{p.name}: mock 行长 {len(s)} >400 可能截断: {s[:80]}...")
            # 检查 data 内 JSON 是否含 [] 空数组被误写为 [I
            if "data='{" in rest and len(rest) > 500:
                errs.append(f"{p.name}: mock data 过长 {len(rest)} 需精简: {s[:80]}...")
        # 检查 header 误入 url-rewrite
        if rest.startswith("header ") and "https://" in rest:
            errs.append(f"{p.name}: url-rewrite 中 header 带 URL 应转 302: {s[:120]}")
    # header-rewrite
    for entry in http.get("header-rewrite", []):
        if not isinstance(entry, str):
            errs.append(f"{p.name}: header-rewrite 非字符串: {entry}")
            continue
        s = entry.strip()
        if " - header" in s:
            errs.append(f"{p.name}: header-rewrite 不应带 ' - ': {s[:120]}")
        if "response-header" in s:
            errs.append(f"{p.name}: header-rewrite 含残留 response- 前缀: {s[:120]}")
        if s.startswith("(^"):
            errs.append(f"{p.name}: header-rewrite pattern 以 (^ 开头: {s[:120]}")
        # header-rewrite 应为 "pattern header-add ..." 无 dash
        if not re.match(r"^\S+\s+header-(add|del|replace)\s", s):
            # 允许 header-replace 等
            if "header" in s and " - " in s:
                errs.append(f"{p.name}: header-rewrite 格式应为 'pattern header-add ...' 无 dash: {s[:120]}")
    # body-rewrite
    for entry in http.get("body-rewrite", []):
        if not isinstance(entry, str):
            errs.append(f"{p.name}: body-rewrite 非字符串: {entry}")
            continue
        s = entry.strip()
        if s.startswith("(^"):
            errs.append(f"{p.name}: body-rewrite pattern 以 (^ 开头: {s[:120]}")
        if len(s) > 4096:
            errs.append(f"{p.name}: body-rewrite 行长 {len(s)} >4096")
        # 检查 jq 是否含未内联的 jq-path
        if "jq-path" in s:
            errs.append(f"{p.name}: body-rewrite 含未内联 jq-path: {s[:120]}")
    # script / providers 一致性
    scripts = http.get("script", [])
    providers = data.get("script-providers", {})
    script_names = {s.get("name") for s in scripts if isinstance(s, dict) and s.get("name")}
    for name in script_names:
        if name not in providers:
            errs.append(f"{p.name}: script {name} 无对应 provider")
    for pname, pinfo in providers.items():
        if not isinstance(pinfo, dict) or "url" not in pinfo:
            errs.append(f"{p.name}: provider {pname} 缺 url")
        url = str(pinfo.get("url", ""))
        if "kelee.one" in url and "headers" not in pinfo:
            errs.append(f"{p.name}: provider {pname} 为 kelee.one 但缺 headers UA")

def main():
    if not KELEE_DIR.exists():
        print(f"目录不存在: {KELEE_DIR}", file=sys.stderr)
        sys.exit(2)
    for f in sorted(KELEE_DIR.glob("*.stoverride")):
        check_file(f)
    if errs:
        print("校验失败:")
        for e in errs:
            print(f"  - {e}")
        sys.exit(2)
    else:
        print(f"校验通过: {len(list(KELEE_DIR.glob('*.stoverride')))} 个文件")
        sys.exit(0)

if __name__ == "__main__":
    main()
