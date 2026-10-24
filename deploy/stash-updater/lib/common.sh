#!/usr/bin/env bash
# shellcheck shell=bash
#
# Shared configuration, validation, locking, ledger and state helpers for the
# KeLee Stash publisher.
#
# Sourced by run-updater.sh (cloud systemd timer), publish-release.sh (the one
# transaction implementation used by both the timer and the manual Windows
# deploy), health-check.sh, retention.sh and render-config.sh. Keeping every
# path/address/lock/state rule in one file is what stops the manual deploy and
# the timer from diverging.
#
# Design rules:
#   * No credentials or secrets are read or written here.
#   * The public address derives from exactly one source
#     (KELEE_PUBLIC_BASE / STASH_PUBLIC_HOST / a manual -PublicHost).
#     Inconsistent sources abort with an explicit error instead of guessing.
#   * There is no fallback production hostname in any deploy file.
#   * All publish mutations happen under a single flock on update.lock that is
#     shared by the manual deploy and the cloud timer.

if [[ -n "${KELEE_PUBLISH_COMMON_SH:-}" ]]; then
    return 0
fi
KELEE_PUBLISH_COMMON_SH=1

EX_OK=0
EX_CONFIG=10
EX_VALIDATION=20
EX_COMPOSE=30
EX_HEALTH=40
EX_STATE=50
EX_CRITICAL=60
EX_LOCKED=75

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S%z')" "$*"
}

warn() {
    printf '[%s] WARN: %s\n' "$(date '+%Y-%m-%d %H:%M:%S%z')" "$*" >&2
}

die() {
    local code="$1"
    shift
    printf '[%s] ERROR: %s\n' "$(date '+%Y-%m-%d %H:%M:%S%z')" "$*" >&2
    exit "$code"
}

die_config() { die "$EX_CONFIG" "$@"; }
die_validation() { die "$EX_VALIDATION" "$@"; }

require_cmd() {
    local name
    for name in "$@"; do
        command -v "$name" >/dev/null 2>&1 || die_config "缺少命令：$name"
    done
}

now_epoch() { date +%s; }
now_utc() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

sha256_of() {
    local file="$1"
    sha256sum "$file" | cut -d' ' -f1
}

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

validate_abs_path() {
    local value="$1"
    local name="$2"
    [[ -n "$value" ]] || die_config "$name 不能为空"
    [[ "$value" == /* ]] || die_config "$name 必须是绝对路径：$value"
    [[ "$value" != "/" ]] || die_config "$name 不能是根目录 /"
    [[ "$value" != *"//"* ]] || die_config "$name 不能包含连续斜杠：$value"
    [[ "$value" != */ ]] || die_config "$name 不能以斜杠结尾：$value"
    [[ "$value" =~ ^/[A-Za-z0-9._/-]+$ ]] || die_config "$name 含有不安全字符（只允许字母数字 . _ - /）：$value"
    [[ ! "$value" =~ (^|/)\.\.?(/|$) ]] || die_config "$name 不能包含 . 或 .. 路径段：$value"
    [[ ${#value} -le 200 ]] || die_config "$name 过长：$value"
}

validate_uint() {
    local value="$1"
    local name="$2"
    local min="$3"
    local max="$4"
    [[ "$value" =~ ^(0|[1-9][0-9]*)$ ]] || die_config "$name 必须是十进制整数：$value"
    ((value >= min && value <= max)) || die_config "$name 超出允许范围 [$min, $max]：$value"
}

validate_host() {
    local host="$1"
    local name="$2"
    [[ -n "$host" ]] || die_config "$name 不能为空"
    [[ "$host" != *"://"* ]] || die_config "$name 必须是不带协议的域名：$host"
    [[ "$host" != *"/"* ]] || die_config "$name 不能包含路径：$host"
    [[ "$host" != *":"* ]] || die_config "$name 不能包含端口，请单独使用 STASH_HTTP_PORT：$host"
    [[ "$host" != *".."* ]] || die_config "$name 不能包含连续点：$host"
    [[ "$host" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || die_config "$name 不是合法域名：$host"
    [[ ${#host} -le 253 ]] || die_config "$name 过长：$host"
}

validate_release_id() {
    local value="$1"
    [[ -n "$value" ]] || die_config "版本号不能为空"
    [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || die_config "版本号只允许字母数字 . _ -（且不能以符号开头）：$value"
    [[ "$value" != "." && "$value" != ".." ]] || die_config "非法版本号：$value"
}

normalize_host() {
    local value="$1"
    if [[ "$value" == *"://"* ]]; then
        value="${value#*://}"
    fi
    value="${value%%/*}"
    printf '%s' "$value"
}

# ---------------------------------------------------------------------------
# Unified public address resolution (single source of truth)
#
# Accepted sources, in priority order:
#   KELEE_PUBLIC_BASE     https://host (builder + renderers)
#   STASH_PUBLIC_HOST     host (systemd EnvironmentFile / manual deploy)
#   KELEE_MIRROR_BASE     must resolve to the same host as the public base
# If more than one is provided and they disagree, fail explicitly.
# ---------------------------------------------------------------------------

declare -a PUBLIC_SOURCE_NAMES=()
declare -a PUBLIC_SOURCE_VALUES=()
declare -a PUBLIC_SOURCE_HOSTS=()
declare -a PUBLIC_SOURCE_SCHEMES=()

_add_public_source() {
    local name="$1"
    local value="$2"
    [[ -n "$value" ]] || return 0
    local host scheme=""
    host="$(normalize_host "$value")"
    validate_host "$host" "$name"
    if [[ "$value" == *"://"* ]]; then
        scheme="${value%%://*}"
        [[ "$scheme" == "https" || "$scheme" == "http" ]] || die_config "$name 只支持 http/https：$value"
    fi
    PUBLIC_SOURCE_NAMES+=("$name")
    PUBLIC_SOURCE_VALUES+=("$value")
    PUBLIC_SOURCE_HOSTS+=("$host")
    PUBLIC_SOURCE_SCHEMES+=("$scheme")
}

resolve_public_config() {
    [[ -n "${PUBLIC_CONFIG_RESOLVED:-}" ]] && return 0

    PUBLIC_SOURCE_NAMES=()
    PUBLIC_SOURCE_VALUES=()
    PUBLIC_SOURCE_HOSTS=()
    PUBLIC_SOURCE_SCHEMES=()

    _add_public_source "KELEE_PUBLIC_BASE" "${KELEE_PUBLIC_BASE:-}"
    _add_public_source "STASH_PUBLIC_HOST" "${STASH_PUBLIC_HOST:-}"
    _add_public_source "KELEE_MIRROR_BASE" "${KELEE_MIRROR_BASE:-}"

    ((${#PUBLIC_SOURCE_HOSTS[@]} > 0)) || die_config \
        "未设置公共站点地址：请提供 KELEE_PUBLIC_BASE（https://域名）或 STASH_PUBLIC_HOST；部署文件不再内置生产域名"

    local primary_host="${PUBLIC_SOURCE_HOSTS[0]}"
    local primary_scheme="${PUBLIC_SOURCE_SCHEMES[0]}"
    local i
    for i in "${!PUBLIC_SOURCE_HOSTS[@]}"; do
        [[ "${PUBLIC_SOURCE_HOSTS[$i]}" == "$primary_host" ]] || die_config \
            "公共地址来源不一致：${PUBLIC_SOURCE_NAMES[$i]}=${PUBLIC_SOURCE_VALUES[$i]} 与 ${PUBLIC_SOURCE_NAMES[0]}=${PUBLIC_SOURCE_VALUES[0]}"
        if [[ -n "${PUBLIC_SOURCE_SCHEMES[$i]}" ]]; then
            if [[ -n "$primary_scheme" ]]; then
                [[ "${PUBLIC_SOURCE_SCHEMES[$i]}" == "$primary_scheme" ]] || die_config \
                    "公共地址协议不一致：${PUBLIC_SOURCE_NAMES[$i]}=${PUBLIC_SOURCE_VALUES[$i]} 与 ${PUBLIC_SOURCE_NAMES[0]}=${PUBLIC_SOURCE_VALUES[0]}"
            else
                primary_scheme="${PUBLIC_SOURCE_SCHEMES[$i]}"
            fi
        fi
    done
    primary_scheme="${primary_scheme:-${STASH_PUBLIC_SCHEME:-https}}"
    [[ "$primary_scheme" == "https" || "$primary_scheme" == "http" ]] || die_config \
        "STASH_PUBLIC_SCHEME 只支持 http/https：$primary_scheme"

    STASH_PUBLIC_HOST="$primary_host"
    STASH_PUBLIC_BASE="$primary_scheme://$primary_host"
    KELEE_PUBLIC_BASE="$STASH_PUBLIC_BASE"
    KELEE_MIRROR_BASE="$STASH_PUBLIC_BASE"
    PUBLIC_CONFIG_RESOLVED=1
    export STASH_PUBLIC_HOST STASH_PUBLIC_BASE KELEE_PUBLIC_BASE KELEE_MIRROR_BASE
    log "公共站点地址：$STASH_PUBLIC_BASE（来源：${PUBLIC_SOURCE_NAMES[0]}）"
}

# ---------------------------------------------------------------------------
# Remote paths / ports / retention configuration
# ---------------------------------------------------------------------------

resolve_http_config() {
    [[ -n "${HTTP_CONFIG_RESOLVED:-}" ]] && return 0
    STASH_HTTP_PORT="${STASH_HTTP_PORT:-8080}"
    validate_uint "$STASH_HTTP_PORT" "STASH_HTTP_PORT" 1 65535
    STASH_HEALTH_HOST="${STASH_HEALTH_HOST:-127.0.0.1}"
    validate_host "$STASH_HEALTH_HOST" "STASH_HEALTH_HOST"
    STASH_HEALTH_ATTEMPTS="${STASH_HEALTH_ATTEMPTS:-10}"
    validate_uint "$STASH_HEALTH_ATTEMPTS" "STASH_HEALTH_ATTEMPTS" 1 120
    STASH_HEALTH_DELAY="${STASH_HEALTH_DELAY:-1}"
    validate_uint "$STASH_HEALTH_DELAY" "STASH_HEALTH_DELAY" 0 60
    STASH_HEALTH_TIMEOUT="${STASH_HEALTH_TIMEOUT:-5}"
    validate_uint "$STASH_HEALTH_TIMEOUT" "STASH_HEALTH_TIMEOUT" 1 120
    STASH_CONTAINER_ATTEMPTS="${STASH_CONTAINER_ATTEMPTS:-30}"
    validate_uint "$STASH_CONTAINER_ATTEMPTS" "STASH_CONTAINER_ATTEMPTS" 1 120
    STASH_CONTAINER_DELAY="${STASH_CONTAINER_DELAY:-1}"
    validate_uint "$STASH_CONTAINER_DELAY" "STASH_CONTAINER_DELAY" 0 60
    export STASH_HTTP_PORT STASH_HEALTH_HOST STASH_HEALTH_ATTEMPTS STASH_HEALTH_DELAY STASH_HEALTH_TIMEOUT
    export STASH_CONTAINER_ATTEMPTS STASH_CONTAINER_DELAY
    HTTP_CONFIG_RESOLVED=1
}

resolve_paths() {
    [[ -n "${PATHS_RESOLVED:-}" ]] && return 0

    STASH_REMOTE_ROOT="${STASH_REMOTE_ROOT:-/opt/stash}"
    validate_abs_path "$STASH_REMOTE_ROOT" "STASH_REMOTE_ROOT"

    STASH_UPDATER_ROOT="${STASH_UPDATER_ROOT:-$STASH_REMOTE_ROOT/updater}"
    validate_abs_path "$STASH_UPDATER_ROOT" "STASH_UPDATER_ROOT"
    [[ "$STASH_UPDATER_ROOT" == "$STASH_REMOTE_ROOT/"* ]] || die_config \
        "STASH_UPDATER_ROOT 必须位于 STASH_REMOTE_ROOT 之下：$STASH_UPDATER_ROOT"

    STASH_COMPOSE_FILE="${STASH_COMPOSE_FILE:-$STASH_REMOTE_ROOT/docker-compose.yml}"
    validate_abs_path "$STASH_COMPOSE_FILE" "STASH_COMPOSE_FILE"

    STASH_CONTAINER_NAME="${STASH_CONTAINER_NAME:-stash}"
    [[ "$STASH_CONTAINER_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$ ]] || die_config \
        "STASH_CONTAINER_NAME 非法：$STASH_CONTAINER_NAME"

    STASH_KEEP_RELEASES="${STASH_KEEP_RELEASES:-3}"
    validate_uint "$STASH_KEEP_RELEASES" "STASH_KEEP_RELEASES" 1 100
    STASH_KEEP_ASSET_DAYS="${STASH_KEEP_ASSET_DAYS:-30}"
    validate_uint "$STASH_KEEP_ASSET_DAYS" "STASH_KEEP_ASSET_DAYS" 1 3650

    RELEASES_DIR="$STASH_REMOTE_ROOT/releases"
    CURRENT_LINK="$STASH_REMOTE_ROOT/current"
    UPDATER_RELEASES_DIR="$STASH_UPDATER_ROOT/releases"
    UPDATER_CURRENT="$STASH_UPDATER_ROOT/current"
    DATA_DIR="${STASH_DATA_DIR:-$STASH_UPDATER_ROOT/data}"
    validate_abs_path "$DATA_DIR" "STASH_DATA_DIR"
    STATE_DIR="${STASH_STATE_DIR:-$STASH_UPDATER_ROOT/state}"
    validate_abs_path "$STATE_DIR" "STASH_STATE_DIR"
    LOG_DIR="${STASH_LOG_DIR:-$STASH_UPDATER_ROOT/logs}"
    validate_abs_path "$LOG_DIR" "STASH_LOG_DIR"
    LOCK_FILE="${STASH_LOCK_FILE:-$STASH_UPDATER_ROOT/update.lock}"
    validate_abs_path "$LOCK_FILE" "STASH_LOCK_FILE"
    STATE_FILE="$STATE_DIR/publish.env"
    LEDGER_FILE="$STATE_DIR/asset-ledger.tsv"
    PUBLIC_LOG="$LOG_DIR/publish.log"

    export STASH_REMOTE_ROOT STASH_UPDATER_ROOT STASH_COMPOSE_FILE STASH_CONTAINER_NAME
    export STASH_KEEP_RELEASES STASH_KEEP_ASSET_DAYS
    export DATA_DIR STATE_DIR LOG_DIR LOCK_FILE
    PATHS_RESOLVED=1
}

# ---------------------------------------------------------------------------
# Single shared publish lock
#
# run-updater.sh acquires the lock before its (network) build so a manual
# deploy and the timer cannot overlap at all. The helper it later invokes
# inherits fd 9; flock locks belong to the open file description, so the child
# can re-validate the lock without racing. Any other process opening the lock
# file exclusively loses the non-blocking flock and exits 75.
# ---------------------------------------------------------------------------

acquire_lock() {
    [[ -n "${LOCK_FILE:-}" ]] || die_config "发布锁路径未解析"
    local lock_dir
    lock_dir="$(dirname "$LOCK_FILE")"
    mkdir -p "$lock_dir" || die_config "无法创建发布锁目录：$lock_dir"
    local lock_abs
    lock_abs="$(readlink -m "$LOCK_FILE")" || die_config "无法解析发布锁路径：$LOCK_FILE"

    if [[ -e /proc/self/fd/9 ]]; then
        local inherited=""
        inherited="$(readlink -f /proc/self/fd/9 2>/dev/null || true)"
        if [[ -n "$inherited" && "$inherited" == "$lock_abs" ]]; then
            flock -n 9 || die "$EX_LOCKED" "另一个发布任务正在运行（继承的锁：$lock_abs）"
            log "复用父进程已持有的发布锁：$lock_abs"
            return 0
        fi
    fi

    exec 9>"$lock_abs" || die_config "无法打开发布锁：$lock_abs"
    if ! flock -n 9; then
        log "另一个发布/更新任务正在运行，跳过本次操作（锁：$lock_abs）"
        exit "$EX_LOCKED"
    fi
    log "已获取发布锁：$lock_abs"
}

# ---------------------------------------------------------------------------
# Atomic pointer switching
# ---------------------------------------------------------------------------

ensure_pointer_guard() {
    local link="$1"
    if [[ -e "$link" && ! -L "$link" ]]; then
        die_validation "拒绝覆盖非符号链接（current 必须是符号链接或不存在）：$link"
    fi
    if [[ -L "$link" && ! -e "$link" ]]; then
        warn "现有指针是悬空符号链接，将被原子替换：$link"
    fi
}

atomic_symlink() {
    local target="$1"
    local link="$2"
    local tmp="${link}.tmp.$$"
    rm -f "$tmp" 2>/dev/null || true
    ln -s "$target" "$tmp" || return 1
    if ! mv -Tf "$tmp" "$link"; then
        rm -f "$tmp" 2>/dev/null || true
        return 1
    fi
    return 0
}

current_release_path() {
    if [[ -L "$CURRENT_LINK" ]]; then
        local target=""
        target="$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)"
        if [[ -n "$target" && -d "$target" && -f "$target/index.html" ]]; then
            printf '%s' "$target"
            return 0
        fi
    fi
    return 1
}

# ---------------------------------------------------------------------------
# Publish state / event log (plain text: readable without jq and easy to test)
# ---------------------------------------------------------------------------

state_read() {
    local key="$1"
    local file="${2:-$STATE_FILE}"
    [[ -f "$file" ]] || return 0
    sed -n "s/^${key}=//p" "$file" | tail -n 1
}

state_write() {
    mkdir -p "$STATE_DIR" || die "$EX_STATE" "无法创建发布状态目录：$STATE_DIR"
    local tmp="$STATE_DIR/.publish.env.$$"
    : >"$tmp" || die "$EX_STATE" "无法写入发布状态：$tmp"
    local key value
    while (($# >= 2)); do
        key="$1"
        value="${2//$'\n'/ }"
        value="${value//$'\r'/}"
        printf '%s=%s\n' "$key" "$value" >>"$tmp"
        shift 2
    done
    mv -f "$tmp" "$STATE_FILE" || die "$EX_STATE" "无法提交发布状态：$STATE_FILE"
}

append_event() {
    local action="$1"
    local release="$2"
    local reason="${3:-}"
    mkdir -p "$LOG_DIR" "$STATE_DIR" || true
    reason="${reason//$'\n'/ }"
    reason="${reason//$'\r'/}"
    printf '%s|%s|%s|%s\n' "$(now_utc)" "$action" "$release" "$reason" >>"$PUBLIC_LOG" 2>/dev/null || \
        warn "无法追加发布日志：$PUBLIC_LOG"
    log "事件：action=$action release=$release${reason:+ reason=$reason}"
}

# ---------------------------------------------------------------------------
# Bundle validation (defence in depth before anything is switched live)
# ---------------------------------------------------------------------------

# Extract `<sha256>  <relpath>` lines from .build-manifest.json outputs.
manifest_outputs_to_sums() {
    local manifest="$1"
    local out="$2"
    python3 - "$manifest" >"$out" <<'PY'
import json
import re
import sys

path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
except Exception as exc:  # noqa: BLE001 - surfaced to the shell as exit 2
    print(f"manifest parse error: {exc}", file=sys.stderr)
    sys.exit(2)

outputs = data.get("outputs")
if not isinstance(outputs, dict) or not outputs:
    print("manifest outputs missing/empty", file=sys.stderr)
    sys.exit(2)

pattern = re.compile(r"^[A-Za-z0-9._/-]+$")
for rel, meta in sorted(outputs.items()):
    if not isinstance(rel, str) or rel.startswith("/") or ".." in rel.split("/") or not pattern.match(rel):
        print(f"unsafe manifest path: {rel!r}", file=sys.stderr)
        sys.exit(2)
    sha = meta.get("sha256") if isinstance(meta, dict) else None
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha.lower()):
        print(f"invalid sha256 for {rel}", file=sys.stderr)
        sys.exit(2)
    print(f"{sha.lower()}  {rel}")
PY
}

manifest_field() {
    local manifest="$1"
    local field="$2"
    python3 - "$manifest" "$field" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    data = json.load(handle)
value = data.get(sys.argv[2], "")
if isinstance(value, (dict, list)):
    value = json.dumps(value, ensure_ascii=False)
print(value if value is not None else "")
PY
}

# List `<Name>.stoverride` files expected from targets.json.
targets_overrides() {
    local targets="$1"
    python3 - "$targets" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    data = json.load(handle)
if not isinstance(data, list) or not data:
    print("targets.json must be a non-empty array", file=sys.stderr)
    sys.exit(2)
for item in data:
    if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
        print("targets.json entry without filename", file=sys.stderr)
        sys.exit(2)
    filename = item["filename"].strip()
    if not filename:
        print("targets.json entry with empty filename", file=sys.stderr)
        sys.exit(2)
    if filename.endswith(".lpx"):
        filename = filename[:-4] + ".stoverride"
    elif not filename.endswith(".stoverride"):
        filename = filename + ".stoverride"
    print(filename)
PY
}

validate_sums_file() {
    local sums="$1"
    [[ -f "$sums" ]] || die_validation "期望哈希清单不存在：$sums"
    if grep -vE '^[0-9a-f]{64}  [A-Za-z0-9._/:-]+$' "$sums" | grep -q .; then
        die_validation "期望哈希清单格式非法（应为 sha256sum 兼容格式）：$sums"
    fi
    if grep -E '  (\.\./|/)' "$sums" | grep -q .; then
        die_validation "期望哈希清单包含绝对/越界路径：$sums"
    fi
    if grep -E '  [^ ]*\.\.' "$sums" | grep -q .; then
        die_validation "期望哈希清单包含 .. 路径：$sums"
    fi
}

verify_sums() {
    local dir="$1"
    local sums="$2"
    (cd "$dir" && sha256sum -c --quiet --strict "$sums") >/dev/null 2>&1
}

generate_sums_for_dir() {
    local dir="$1"
    local out="$2"
    (
        cd "$dir" || exit 1
        find . -type f -print0 | sort -z | while IFS= read -r -d '' file; do
            rel="${file#./}"
            printf '%s  %s\n' "$(sha256sum "$file" | cut -d' ' -f1)" "$rel"
        done
    ) >"$out" || return 1
    return 0
}

# Collect `scripts/...` references from text files in a bundle.
bundle_asset_references() {
    local dir="$1"
    grep -rhoE 'scripts/[A-Za-z0-9][A-Za-z0-9._-]*\.(js|jq)' "$dir" \
        --include='*.stoverride' --include='*.html' --include='*.json' 2>/dev/null |
        sort -u || true
}

validate_bundle_dir() {
    local dir="$1"
    local sums="${2:-}"

    [[ -d "$dir" ]] || die_validation "发布暂存目录不存在：$dir"
    local file_count
    file_count="$(find "$dir" -type f | wc -l | tr -d ' ')"
    ((file_count > 0)) || die_validation "发布包为空：$dir"
    [[ -s "$dir/index.html" ]] || die_validation "发布包缺少有效的 index.html：$dir"
    [[ -f "$dir/targets.json" ]] || die_validation "发布包缺少 targets.json：$dir"
    [[ -f "$dir/kelee-scripts.stoverride" ]] || die_validation "发布包缺少脚本 provider 合集 kelee-scripts.stoverride：$dir"
    [[ -d "$dir/scripts" ]] || die_validation "发布包缺少 scripts/ 目录：$dir"
    local scripts_count
    scripts_count="$(find "$dir/scripts" -maxdepth 1 -type f | wc -l | tr -d ' ')"
    ((scripts_count > 0)) || die_validation "发布包 scripts/ 目录为空：$dir"

    local overrides
    if ! overrides="$(targets_overrides "$dir/targets.json")"; then
        die_validation "targets.json 格式无效：$dir/targets.json"
    fi
    local override
    while IFS= read -r override; do
        [[ -n "$override" ]] || continue
        [[ "$override" =~ ^[A-Za-z0-9._/-]+$ ]] || die_validation "targets.json 隐含不安全文件名：$override"
        [[ -f "$dir/$override" ]] || die_validation "发布包缺少目标覆写文件：$override"
    done <<<"$overrides"

    grep -qE '^script-providers:' "$dir/kelee-scripts.stoverride" ||
        die_validation "kelee-scripts.stoverride 缺少 script-providers 段"

    if [[ -n "${STASH_PUBLIC_BASE:-}" ]]; then
        grep -qF "$STASH_PUBLIC_BASE/" "$dir/index.html" ||
            die_validation "index.html 未使用本次部署地址 $STASH_PUBLIC_BASE/（构建时 KELEE_PUBLIC_BASE 可能不一致）"
        grep -qF "$STASH_PUBLIC_BASE/" "$dir/kelee-scripts.stoverride" ||
            die_validation "kelee-scripts.stoverride 未使用本次部署地址 $STASH_PUBLIC_BASE/"
    fi

    # Manifest is authoritative for byte hashes when present.
    if [[ -f "$dir/.build-manifest.json" ]]; then
        local manifest_base
        manifest_base="$(manifest_field "$dir/.build-manifest.json" public_base 2>/dev/null || true)"
        if [[ -n "$manifest_base" && -n "${STASH_PUBLIC_BASE:-}" && "$manifest_base" != "$STASH_PUBLIC_BASE" ]]; then
            die_validation "构建清单 public_base=$manifest_base 与本次部署地址 $STASH_PUBLIC_BASE 不一致"
        fi
        local manifest_sums
        manifest_sums="$(mktemp)" || die_validation "无法创建临时文件"
        if ! manifest_outputs_to_sums "$dir/.build-manifest.json" "$manifest_sums"; then
            rm -f "$manifest_sums"
            die_validation "构建清单 .build-manifest.json 的 outputs 无效：$dir"
        fi
        if ! verify_sums "$dir" "$manifest_sums"; then
            rm -f "$manifest_sums"
            die_validation "发布包文件与构建清单 outputs 哈希不一致：$dir"
        fi
        rm -f "$manifest_sums"
        log "构建清单校验通过：$(manifest_field "$dir/.build-manifest.json" fingerprint 2>/dev/null || true)"
    else
        warn "发布包缺少 .build-manifest.json（旧版构建器产物），仅使用期望哈希清单校验"
    fi

    if [[ -n "$sums" ]]; then
        validate_sums_file "$sums"
        verify_sums "$dir" "$sums" || die_validation "发布包文件与期望哈希清单不一致：$sums"
    fi

    local missing=0 ref
    while IFS= read -r ref; do
        [[ -n "$ref" ]] || continue
        if [[ ! -f "$dir/$ref" ]]; then
            warn "发布包引用了不存在的脚本资源：$ref"
            missing=1
        fi
    done < <(bundle_asset_references "$dir")
    ((missing == 0)) || die_validation "发布包引用完整性校验失败（缺少被引用的脚本资源）"

    if grep -rqE '\$\{[A-Za-z_][A-Za-z0-9_.]*\}' "$dir" --include='*.stoverride' 2>/dev/null; then
        die_validation "发布包仍包含未展开的 Loon 占位符 \${...}：$dir"
    fi
}

validate_app_dir() {
    local dir="$1"
    [[ -d "$dir" ]] || die_validation "更新器应用目录不存在：$dir"
    local required=(
        run-updater.sh
        publish-release.sh
        health-check.sh
        retention.sh
        render-config.sh
        normalize-app.sh
        validate.py
        lib/common.sh
        templates/env.in
        templates/stash-kelee-update.service.in
        templates/stash-kelee-update.timer.in
    )
    local entry
    for entry in "${required[@]}"; do
        [[ -f "$dir/$entry" ]] || die_validation "更新器应用缺少文件：$entry"
    done
    [[ -d "$dir/scripts" ]] || die_validation "更新器应用缺少 scripts/ 目录"
}

# ---------------------------------------------------------------------------
# Asset ledger + copy-forward of content-addressed scripts
#
# Released sites are self-contained, but devices that installed an older
# override keep requesting `https://<host>/scripts/<sha12>-<slug>.js`. Those
# immutable assets must survive the next publish until the configured
# retention window elapses. The ledger records when each hashed asset was last
# present in a release; copy-forward copies still-fresh assets from older
# releases into the new one, retention prunes expired ones.
# ---------------------------------------------------------------------------

asset_is_hashed() {
    [[ "$1" =~ ^scripts/[0-9a-f]{12}-[A-Za-z0-9._-]+\.(js|jq)$ ]]
}

find_asset_source() {
    local name="$1"
    local expected_sha="${2:-}"
    local candidates=()
    local rel
    candidates+=("$CURRENT_LINK/$name")
    if [[ -n "${PREVIOUS_RELEASE:-}" ]]; then
        candidates+=("$PREVIOUS_RELEASE/$name")
    fi
    if [[ -d "$RELEASES_DIR" ]]; then
        while IFS= read -r rel; do
            [[ -n "$rel" ]] || continue
            candidates+=("$RELEASES_DIR/$rel/$name")
        done < <(ls -1r "$RELEASES_DIR" 2>/dev/null || true)
    fi
    local candidate
    for candidate in "${candidates[@]}"; do
        [[ -f "$candidate" ]] || continue
        if [[ -n "$expected_sha" ]]; then
            [[ "$(sha256_of "$candidate")" == "$expected_sha" ]] || continue
        fi
        printf '%s' "$candidate"
        return 0
    done
    return 1
}

# Names copied forward by copy_forward_assets() in this shell. update_asset_ledger
# must NOT refresh their "last seen in a fresh build" timestamp, otherwise the
# retention window would never expire.
declare -a COPIED_ASSET_NAMES=()

copy_forward_assets() {
    local staging="$1"
    local cutoff
    cutoff=$(($(now_epoch) - STASH_KEEP_ASSET_DAYS * 86400))
    local copied=0
    local name sha last_seen first_seen source
    COPIED_ASSET_NAMES=()

    mkdir -p "$staging/scripts" || die_validation "无法创建暂存 scripts 目录"

    if [[ -f "$LEDGER_FILE" ]]; then
        while IFS=$'\t' read -r name sha last_seen first_seen; do
            asset_is_hashed "$name" || continue
            [[ "$last_seen" =~ ^[0-9]+$ ]] || continue
            ((last_seen >= cutoff)) || continue
            [[ -f "$staging/$name" ]] && continue
            if source="$(find_asset_source "$name" "$sha")"; then
                cp -p "$source" "$staging/$name" || die_validation "copy-forward 失败：$name"
                COPIED_ASSET_NAMES+=("$name")
                copied=$((copied + 1))
            else
                warn "无法 copy-forward 旧内容寻址脚本（源文件缺失）：$name"
            fi
        done <"$LEDGER_FILE"
    fi

    # First upgrade after installing this helper may not have a ledger yet:
    # carry over hashed assets from the currently served release.
    if [[ -n "${PREVIOUS_RELEASE:-}" && -d "$PREVIOUS_RELEASE/scripts" ]]; then
        local known
        while IFS= read -r source; do
            name="scripts/$(basename "$source")"
            asset_is_hashed "$name" || continue
            [[ -f "$staging/$name" ]] && continue
            known=0
            for entry in "${COPIED_ASSET_NAMES[@]}"; do
                [[ "$entry" == "$name" ]] && known=1
            done
            ((known == 0)) || continue
            cp -p "$source" "$staging/$name" || die_validation "copy-forward 失败：$name"
            COPIED_ASSET_NAMES+=("$name")
            copied=$((copied + 1))
        done < <(find "$PREVIOUS_RELEASE/scripts" -maxdepth 1 -type f \( -name '*.js' -o -name '*.jq' \) | sort)
    fi

    log "内容寻址资产 copy-forward 完成：$copied 个文件（保留窗口 ${STASH_KEEP_ASSET_DAYS} 天）"
}

_update_asset_ledger_is_copied() {
    local candidate="$1"
    local entry
    for entry in "${COPIED_ASSET_NAMES[@]}"; do
        [[ "$entry" == "$candidate" ]] && return 0
    done
    return 1
}

# Hashed asset relpaths that the build manifest says were produced by this
# build. Anything else in scripts/ was carried over from the seeded update state
# (or an old build) and must keep its original retention timestamp.
manifest_hashed_assets() {
    local manifest="$1"
    python3 - "$manifest" <<'PY'
import json
import re
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    data = json.load(handle)
pattern = re.compile(r"^scripts/[0-9a-f]{12}-[A-Za-z0-9._-]+\.(js|jq)$")
found = set()
for section in ("outputs", "resources"):
    block = data.get(section) or {}
    if not isinstance(block, dict):
        continue
    for key, meta in block.items():
        if isinstance(key, str) and pattern.match(key):
            found.add(key)
        rel = meta.get("relpath") if isinstance(meta, dict) else None
        if isinstance(rel, str) and pattern.match(rel):
            found.add(rel)
for rel in sorted(found):
    print(rel)
PY
}

update_asset_ledger() {
    local release="$1"
    local now
    now=$(now_epoch)
    mkdir -p "$STATE_DIR" || die "$EX_STATE" "无法创建状态目录：$STATE_DIR"
    local tmp="$STATE_DIR/.ledger.$$"
    : >"$tmp" || die "$EX_STATE" "无法写入资产账本：$tmp"

    local name sha last_seen first_seen
    if [[ -f "$LEDGER_FILE" ]]; then
        while IFS=$'\t' read -r name sha last_seen first_seen; do
            [[ -n "$name" ]] || continue
            asset_is_hashed "$name" || continue
            [[ -f "$release/$name" ]] && continue
            printf '%s\t%s\t%s\t%s\n' "$name" "$sha" "$last_seen" "$first_seen" >>"$tmp"
        done <"$LEDGER_FILE"
    fi

    local manifest_assets=""
    if [[ -f "$release/.build-manifest.json" ]]; then
        manifest_assets="$(mktemp)" || manifest_assets=""
        if [[ -n "$manifest_assets" ]]; then
            if ! manifest_hashed_assets "$release/.build-manifest.json" >"$manifest_assets"; then
                warn "无法解析构建清单的资源列表，退回按文件修改时间计算保留窗口"
                rm -f "$manifest_assets"
                manifest_assets=""
            fi
        fi
    fi

    if [[ -d "$release/scripts" ]]; then
        local file base old_row old_sha old_last old_first fresh extracted
        while IFS= read -r file; do
            base="$(basename "$file")"
            [[ "$base" =~ ^[0-9a-f]{12}-.*\.(js|jq)$ ]] || continue
            name="scripts/$base"
            sha="$(sha256_of "$file")"
            if _update_asset_ledger_is_copied "$name"; then
                # Copy-forwarded asset: keep the original retention timestamps.
                old_row=""
                if [[ -f "$LEDGER_FILE" ]]; then
                    old_row="$(awk -F'\t' -v n="$name" '$1 == n { print; exit }' "$LEDGER_FILE")"
                fi
                if [[ -n "$old_row" ]]; then
                    IFS=$'\t' read -r _ old_sha old_last old_first <<<"$old_row"
                    if [[ "$old_last" =~ ^[0-9]+$ && "$old_first" =~ ^[0-9]+$ ]]; then
                        printf '%s\t%s\t%s\t%s\n' "$name" "$sha" "$old_last" "$old_first" >>"$tmp"
                        continue
                    fi
                fi
                printf '%s\t%s\t%s\t%s\n' "$name" "$sha" "$now" "$now" >>"$tmp"
                continue
            fi
            fresh=1
            if [[ -n "$manifest_assets" ]]; then
                grep -qxF -- "$name" "$manifest_assets" || fresh=0
            fi
            if ((fresh == 1)); then
                first_seen="$now"
                if [[ -f "$LEDGER_FILE" ]]; then
                    old_first="$(awk -F'\t' -v n="$name" '$1 == n { print $4; exit }' "$LEDGER_FILE")"
                    if [[ -n "$old_first" && "$old_first" =~ ^[0-9]+$ ]]; then
                        first_seen="$old_first"
                    fi
                fi
                printf '%s\t%s\t%s\t%s\n' "$name" "$sha" "$now" "$first_seen" >>"$tmp"
                continue
            fi
            # Carried-over asset from the seeded state or an older build: never
            # refresh its timestamp, otherwise retention would never expire it.
            old_row=""
            if [[ -f "$LEDGER_FILE" ]]; then
                old_row="$(awk -F'\t' -v n="$name" '$1 == n { print; exit }' "$LEDGER_FILE")"
            fi
            if [[ -n "$old_row" ]]; then
                IFS=$'\t' read -r _ old_sha old_last old_first <<<"$old_row"
                if [[ "$old_last" =~ ^[0-9]+$ && "$old_first" =~ ^[0-9]+$ ]]; then
                    printf '%s\t%s\t%s\t%s\n' "$name" "$sha" "$old_last" "$old_first" >>"$tmp"
                    continue
                fi
            fi
            extracted="$(stat -c '%Y' "$file" 2>/dev/null || true)"
            [[ "$extracted" =~ ^[0-9]+$ ]] || extracted="$now"
            printf '%s\t%s\t%s\t%s\n' "$name" "$sha" "$extracted" "$extracted" >>"$tmp"
        done < <(find "$release/scripts" -maxdepth 1 -type f \( -name '*.js' -o -name '*.jq' \) | sort)
    fi
    [[ -n "$manifest_assets" ]] && rm -f "$manifest_assets"

    sort -t$'\t' -k1,1 "$tmp" -o "$tmp" 2>/dev/null || sort "$tmp" -o "$tmp"
    mv -f "$tmp" "$LEDGER_FILE" || die "$EX_STATE" "无法提交资产账本：$LEDGER_FILE"
}

# ---------------------------------------------------------------------------
# Update-state sync (build artifacts + .hashes.json) from a published release
#
# Atomic swap: rsync into data/.sync-<id>, move the old tree aside, move the
# new tree in, then delete the old tree. Failure leaves the previous state
# untouched so the timer can retry with a known-good baseline.
# ---------------------------------------------------------------------------

sync_state() {
    local release_dir="$1"
    local release_id="$2"
    local target="$DATA_DIR/stash/overrides/kelee"
    local tmp="$DATA_DIR/.sync-$release_id"
    local old="$DATA_DIR/.old-kelee-$release_id"

    rm -rf "$tmp" "$old" 2>/dev/null || true
    mkdir -p "$tmp/stash/overrides" || {
        warn "状态同步失败：无法创建 $tmp/stash/overrides"
        return 1
    }
    if ! rsync -a --delete "$release_dir/" "$tmp/stash/overrides/kelee/"; then
        warn "状态同步失败：rsync 到 $tmp 出错"
        rm -rf "$tmp" 2>/dev/null || true
        return 1
    fi
    mkdir -p "$DATA_DIR/stash/overrides" || {
        warn "状态同步失败：无法创建 $DATA_DIR/stash/overrides"
        rm -rf "$tmp" 2>/dev/null || true
        return 1
    }
    if [[ -e "$target" ]]; then
        mv "$target" "$old" || {
            warn "状态同步失败：无法暂存旧状态 $target"
            rm -rf "$tmp" 2>/dev/null || true
            return 1
        }
    fi
    if ! mv "$tmp/stash/overrides/kelee" "$target"; then
        [[ -e "$old" ]] && mv "$old" "$target" 2>/dev/null || true
        rm -rf "$tmp" 2>/dev/null || true
        warn "状态同步失败：无法切换到新状态 $target"
        return 1
    fi
    rm -rf "$old" "$tmp" 2>/dev/null || true
    return 0
}

# ---------------------------------------------------------------------------
# Container lifecycle helpers
# ---------------------------------------------------------------------------

compose_up() {
    log "重建容器：docker compose -f $STASH_COMPOSE_FILE up -d --force-recreate --no-build"
    docker compose -f "$STASH_COMPOSE_FILE" up -d --force-recreate --no-build
}

wait_container_running() {
    local attempt status
    for ((attempt = 1; attempt <= STASH_CONTAINER_ATTEMPTS; attempt++)); do
        status="$(docker inspect --format '{{.State.Status}}' "$STASH_CONTAINER_NAME" 2>/dev/null || true)"
        if [[ "$status" == "running" ]]; then
            log "容器 $STASH_CONTAINER_NAME 状态：running（第 $attempt 次检查）"
            return 0
        fi
        sleep "$STASH_CONTAINER_DELAY"
    done
    warn "容器 $STASH_CONTAINER_NAME 未在预期时间内进入 running（最后状态：${status:-unknown}）"
    return 1
}

# ---------------------------------------------------------------------------
# State writers
# ---------------------------------------------------------------------------

write_publish_state() {
    local caller="$1"
    local action="$2"
    local result="$3"
    local release="$4"
    local reason="${5:-}"
    local fingerprint="${6:-}"
    local previous="${7:-}"
    local rollback_count="${8:-0}"

    local current=""
    current="$(current_release_path || true)"
    state_write \
        PUBLISH_SCHEMA 1 \
        CURRENT_RELEASE "$current" \
        PREVIOUS_RELEASE "$previous" \
        LAST_ACTION "$action" \
        LAST_RESULT "$result" \
        LAST_RELEASE "$release" \
        LAST_REASON "$reason" \
        LAST_UTC "$(now_utc)" \
        LAST_CALLER "$caller" \
        ROLLBACK_COUNT "$rollback_count" \
        FINGERPRINT "$fingerprint" \
        PUBLIC_BASE "${STASH_PUBLIC_BASE:-}"
}
