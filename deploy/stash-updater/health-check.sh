#!/usr/bin/env bash
# shellcheck shell=bash
#
# Health-check a published KeLee Stash release through the local HTTP endpoint
# (Caddy inside the compose project). This intentionally checks *real bytes*
# instead of trusting "container is running" / "current/index.html exists":
#
#   * homepage               GET /            -> HTTP 200 + sha256 == release file + public-base marker
#   * homepage alias         GET /index.html  -> same bytes
#   * representative override GET /<Name>.stoverride -> HTTP 200 + sha256 + YAML name marker
#   * provider bundle        GET /kelee-scripts.stoverride -> HTTP 200 + sha256 + script-providers marker
#   * representative hashed script GET /scripts/<sha12>-<slug>.js -> HTTP 200 + sha256
#
# Host header and port come from the unified STASH_PUBLIC_HOST / STASH_HTTP_PORT
# configuration so a reverse proxy that routes by Host serves the same bytes as
# the public hostname.
#
# Exit codes: 0 = healthy, 40 = unhealthy/never healthy.

set -Eeuo pipefail
umask 022

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
    cat <<'EOF'
用法：health-check.sh --release <发布目录> [--attempts N] [--delay 秒] [--timeout 秒]
                     [--public-base URL] [--public-host HOST] [--strict-cache-headers 0|1]

环境变量（通常来自 systemd EnvironmentFile）：
  STASH_PUBLIC_HOST / KELEE_PUBLIC_BASE   公共站点地址（Host 头）
  STASH_HTTP_HOST                         本地探测地址，默认 127.0.0.1
  STASH_HTTP_PORT                         本地探测端口，默认 8080
  STASH_HEALTH_ATTEMPTS / STASH_HEALTH_DELAY / STASH_HEALTH_TIMEOUT
EOF
}

RELEASE_DIR=""
PUBLIC_BASE_ARG=""
PUBLIC_HOST_ARG=""
STRICT_CACHE_ARG=""
while (($#)); do
    case "$1" in
    --release)
        [[ $# -ge 2 ]] || die_config "--release 缺少参数"
        RELEASE_DIR="$2"
        shift 2
        ;;
    --attempts)
        [[ $# -ge 2 ]] || die_config "--attempts 缺少参数"
        STASH_HEALTH_ATTEMPTS="$2"
        shift 2
        ;;
    --delay)
        [[ $# -ge 2 ]] || die_config "--delay 缺少参数"
        STASH_HEALTH_DELAY="$2"
        shift 2
        ;;
    --timeout)
        [[ $# -ge 2 ]] || die_config "--timeout 缺少参数"
        STASH_HEALTH_TIMEOUT="$2"
        shift 2
        ;;
    --public-base)
        [[ $# -ge 2 ]] || die_config "--public-base 缺少参数"
        PUBLIC_BASE_ARG="$2"
        shift 2
        ;;
    --public-host)
        [[ $# -ge 2 ]] || die_config "--public-host 缺少参数"
        PUBLIC_HOST_ARG="$2"
        shift 2
        ;;
    --strict-cache-headers)
        [[ $# -ge 2 ]] || die_config "--strict-cache-headers 缺少参数"
        STRICT_CACHE_ARG="$2"
        shift 2
        ;;
    -h | --help)
        usage
        exit 0
        ;;
    *)
        die_config "未知参数：$1（--help 查看用法）"
        ;;
    esac
done

[[ -n "$RELEASE_DIR" ]] || die_config "--release 必填"
if [[ -n "$PUBLIC_BASE_ARG" || -n "$PUBLIC_HOST_ARG" ]]; then
    [[ -n "$PUBLIC_BASE_ARG" && -n "$PUBLIC_HOST_ARG" ]] ||
        die_config "--public-base 与 --public-host 必须同时提供"
    KELEE_PUBLIC_BASE="$PUBLIC_BASE_ARG"
    KELEE_MIRROR_BASE="$PUBLIC_BASE_ARG"
    STASH_PUBLIC_HOST="$PUBLIC_HOST_ARG"
    export KELEE_PUBLIC_BASE KELEE_MIRROR_BASE STASH_PUBLIC_HOST
fi
if [[ -n "$STRICT_CACHE_ARG" ]]; then
    STASH_STRICT_CACHE_HEADERS="$STRICT_CACHE_ARG"
fi
resolve_public_config
resolve_http_config
require_cmd curl sha256sum grep sed cut tr mktemp sort head find

# Cache-Control policy verification. Default 0 = record mismatches as warnings
# (compatible with an older Caddyfile); set 1 to make them health failures
# (the manual deploy does this when it installs the rendered Caddyfile).
STASH_STRICT_CACHE_HEADERS="${STASH_STRICT_CACHE_HEADERS:-0}"
if [[ "$STASH_STRICT_CACHE_HEADERS" != "0" && "$STASH_STRICT_CACHE_HEADERS" != "1" ]]; then
    die_config "STASH_STRICT_CACHE_HEADERS 只能是 0/1：$STASH_STRICT_CACHE_HEADERS"
fi

[[ -d "$RELEASE_DIR" ]] || die_validation "发布目录不存在：$RELEASE_DIR"
[[ -s "$RELEASE_DIR/index.html" ]] || die_validation "发布目录缺少 index.html：$RELEASE_DIR"
[[ -f "$RELEASE_DIR/kelee-scripts.stoverride" ]] || die_validation "发布目录缺少 kelee-scripts.stoverride：$RELEASE_DIR"

BASE_URL="http://${STASH_HEALTH_HOST}:${STASH_HTTP_PORT}"

pick_representative_override() {
    local override=""
    if [[ -f "$RELEASE_DIR/targets.json" ]]; then
        override="$(targets_overrides "$RELEASE_DIR/targets.json" 2>/dev/null | head -n 1 || true)"
    fi
    if [[ -z "$override" ]]; then
        override="$(find "$RELEASE_DIR" -maxdepth 1 -type f -name '*.stoverride' ! -name 'kelee-scripts.stoverride' -printf '%f\n' 2>/dev/null | sort | head -n 1 || true)"
    fi
    printf '%s' "$override"
}

pick_hashed_script() {
    local script=""
    if [[ -f "$RELEASE_DIR/.build-manifest.json" ]] && command -v python3 >/dev/null 2>&1; then
        script="$(python3 - "$RELEASE_DIR/.build-manifest.json" <<'PY' 2>/dev/null
import json
import re
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    data = json.load(handle)

pattern = re.compile(r"^scripts/[0-9a-f]{12}-[A-Za-z0-9._-]+\.js$")
found = []
resources = data.get("resources") or {}
if isinstance(resources, dict):
    for meta in resources.values():
        rel = meta.get("relpath") if isinstance(meta, dict) else None
        if isinstance(rel, str) and pattern.match(rel):
            found.append(rel)
outputs = data.get("outputs") or {}
if isinstance(outputs, dict):
    for rel in outputs:
        if isinstance(rel, str) and pattern.match(rel):
            found.append(rel)
for rel in sorted(set(found)):
    print(rel)
    break
PY
)"
    fi
    if [[ -z "$script" ]]; then
        script="$(find "$RELEASE_DIR/scripts" -maxdepth 1 -type f -name '*.js' -printf 'scripts/%f\n' 2>/dev/null |
            grep -E '^scripts/[0-9a-f]{12}-' | sort | head -n 1 || true)"
    fi
    if [[ -z "$script" ]]; then
        # Legacy bundles predate content-addressed assets; still verify one script.
        script="$(find "$RELEASE_DIR/scripts" -maxdepth 1 -type f -name '*.js' -printf 'scripts/%f\n' 2>/dev/null | sort | head -n 1 || true)"
        [[ -n "$script" ]] && warn "发布包没有内容寻址脚本（旧版产物），改用普通脚本做健康检查：$script"
    fi
    printf '%s' "$script"
}

pick_legacy_script() {
    local script=""
    if [[ -f "$RELEASE_DIR/.build-manifest.json" ]] && command -v python3 >/dev/null 2>&1; then
        script="$(python3 - "$RELEASE_DIR/.build-manifest.json" <<'PY' 2>/dev/null
import json
import re
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    data = json.load(handle)

hashed = re.compile(r"^scripts/[0-9a-f]{12}-[A-Za-z0-9._-]+\.(js|jq)$")
legacy = re.compile(r"^scripts/[^/]+\.js$")
found = []
resources = data.get("resources") or {}
if isinstance(resources, dict):
    for meta in resources.values():
        if not isinstance(meta, dict):
            continue
        for rel in (meta.get("legacy_alias"), meta.get("relpath")):
            if isinstance(rel, str) and legacy.match(rel) and not hashed.match(rel):
                found.append(rel)
outputs = data.get("outputs") or {}
if isinstance(outputs, dict):
    for rel in outputs:
        if isinstance(rel, str) and legacy.match(rel) and not hashed.match(rel):
            found.append(rel)
for rel in sorted(set(found)):
    print(rel)
    break
PY
)"
    fi
    if [[ -z "$script" ]]; then
        script="$(find "$RELEASE_DIR/scripts" -maxdepth 1 -type f -name '*.js' -printf 'scripts/%f\n' 2>/dev/null |
            grep -vE '^scripts/[0-9a-f]{12}-' | sort | head -n 1 || true)"
    fi
    printf '%s' "$script"
}

OVERRIDE_REL="$(pick_representative_override)"
HASHED_SCRIPT_REL="$(pick_hashed_script)"
LEGACY_SCRIPT_REL="$(pick_legacy_script)"
[[ -n "$OVERRIDE_REL" ]] || die_validation "无法在发布包中找到代表 .stoverride：$RELEASE_DIR"
[[ -n "$HASHED_SCRIPT_REL" ]] || die_validation "无法在发布包中找到代表脚本资源：$RELEASE_DIR/scripts"

CHECK_PATHS=()
CHECK_RELPATHS=()
CHECK_MARKERS=()
CHECK_CACHES=()
add_check() {
    CHECK_PATHS+=("$1")
    CHECK_RELPATHS+=("$2")
    CHECK_MARKERS+=("${3:-}")
    CHECK_CACHES+=("${4:-}")
}
add_check "/" "index.html" "$STASH_PUBLIC_BASE" ""
add_check "/index.html" "index.html" "$STASH_PUBLIC_BASE" ""
add_check "/$OVERRIDE_REL" "$OVERRIDE_REL" "name:" ""
add_check "/kelee-scripts.stoverride" "kelee-scripts.stoverride" "script-providers:" ""
add_check "/$HASHED_SCRIPT_REL" "$HASHED_SCRIPT_REL" "" "immutable"
if [[ -n "$LEGACY_SCRIPT_REL" ]]; then
    add_check "/$LEGACY_SCRIPT_REL" "$LEGACY_SCRIPT_REL" "" "must-revalidate"
else
    warn "发布包没有 legacy 脚本别名，跳过 legacy 缓存策略检查"
fi
CHECK_COUNT=${#CHECK_PATHS[@]}

TMP_DIR="$(mktemp -d)" || die_config "无法创建临时目录"
cleanup() { rm -rf "$TMP_DIR" 2>/dev/null || true; }
trap cleanup EXIT

log "健康检查目标：$BASE_URL（Host: $STASH_PUBLIC_HOST，release=$RELEASE_DIR，共 $CHECK_COUNT 项）"

attempt=1
while ((attempt <= STASH_HEALTH_ATTEMPTS)); do
    failures=0
    i=0
    while ((i < CHECK_COUNT)); do
        path="${CHECK_PATHS[$i]}"
        relpath="${CHECK_RELPATHS[$i]}"
        marker="${CHECK_MARKERS[$i]}"
        cache_expect="${CHECK_CACHES[$i]}"
        url="$BASE_URL$path"
        body="$TMP_DIR/body.$i"
        hdr="$TMP_DIR/headers.$i"
        err="$TMP_DIR/err.$i"
        status=""
        reason=""
        cache_reason=""

        status="$(curl --silent --show-error --max-time "$STASH_HEALTH_TIMEOUT" \
            --header "Host: $STASH_PUBLIC_HOST" \
            --dump-header "$hdr" \
            --output "$body" --write-out '%{http_code}' "$url" 2>"$err")" ||
            status="curl-error"

        if [[ "$status" != "200" ]]; then
            reason="HTTP $status"
            if [[ "$status" == "curl-error" && -s "$err" ]]; then
                reason="curl 失败：$(head -n 1 "$err")"
            fi
        elif [[ ! -s "$body" ]]; then
            reason="响应正文为空"
        else
            expected="$(sha256_of "$RELEASE_DIR/$relpath")"
            actual="$(sha256_of "$body")"
            if [[ "$expected" != "$actual" ]]; then
                reason="sha256 不一致（期望 $expected，实际 $actual）"
            elif [[ -n "$marker" ]]; then
                if ! grep -qF -- "$marker" "$body"; then
                    reason="缺少内容标记：$marker"
                elif [[ "$relpath" != "index.html" && "$relpath" != "kelee-scripts.stoverride" ]] &&
                    grep -qE '\$\{[A-Za-z_][A-Za-z0-9_.]*\}' "$body"; then
                    reason="内容仍包含未展开的 Loon 占位符"
                fi
            fi
        fi

        if [[ -n "$reason" ]]; then
            failures=$((failures + 1))
            warn "健康检查失败[$path] 第 $attempt/$STASH_HEALTH_ATTEMPTS 次：$reason"
        elif [[ -n "$cache_expect" ]]; then
            cache_value="$(grep -i '^cache-control:' "$hdr" 2>/dev/null | tail -n 1 | cut -d: -f2- | tr -d '\r' | sed 's/^[[:space:]]*//')"
            if [[ -z "$cache_value" ]]; then
                cache_reason="缺少 Cache-Control 响应头（期望包含 $cache_expect）"
            elif [[ "$cache_value" != *"$cache_expect"* ]]; then
                cache_reason="Cache-Control='$cache_value'，期望包含 '$cache_expect'（检查 Caddyfile 缓存规则与顺序）"
            fi
            if [[ -n "$cache_reason" ]]; then
                if [[ "$STASH_STRICT_CACHE_HEADERS" == "1" ]]; then
                    failures=$((failures + 1))
                    warn "健康检查失败[$path] 第 $attempt/$STASH_HEALTH_ATTEMPTS 次：$cache_reason"
                else
                    warn "缓存策略警告[$path]：$cache_reason（设置 STASH_STRICT_CACHE_HEADERS=1 可升级为失败）"
                fi
            fi
        fi
        i=$((i + 1))
    done

    if ((failures == 0)); then
        log "健康检查通过：release=$RELEASE_DIR（$CHECK_COUNT 项，第 $attempt 次尝试）"
        exit "$EX_OK"
    fi
    if ((attempt < STASH_HEALTH_ATTEMPTS)); then
        sleep "$STASH_HEALTH_DELAY"
    fi
    attempt=$((attempt + 1))
done

log "健康检查失败：release=$RELEASE_DIR（$failures 项未通过，已尝试 $STASH_HEALTH_ATTEMPTS 次）"
exit "$EX_HEALTH"
