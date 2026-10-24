#!/usr/bin/env bash
# shellcheck shell=bash
#
# Render the remote configuration (EnvironmentFile + systemd service/timer +
# optional Caddyfile) from templates, so RemoteRoot / PublicHost are plumbed
# through one place instead of being hardcoded in unit files.
#
# Templates live next to this script in templates/*.in and only contain
# @@PLACEHOLDER@@ tokens. Rendering fails loudly if any placeholder is left.
# Credentials are never read or written here.
#
# Exit codes: 0 ok, 10 config error, 20 validation error.

set -Eeuo pipefail
umask 022

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
    cat <<'EOF'
用法：render-config.sh [选项]

必填（或通过环境变量提供）：
  --public-base URL      https://<PublicHost>（或 KELEE_PUBLIC_BASE）
  --public-host HOST     公共站点域名（或 STASH_PUBLIC_HOST）
  --mirror-base URL      镜像地址，必须与 public-base 同源（默认同 public-base）
  --remote-root PATH     远端发布根目录（或 STASH_REMOTE_ROOT，默认 /opt/stash）
  --updater-root PATH    更新器根目录（默认 <remote-root>/updater）

可选：
  --http-port N          本机健康检查端口（默认 STASH_HTTP_PORT/8080）
  --container-name NAME  容器名（默认 stash）
  --keep-releases N / --keep-asset-days N
  --caddy-site-root PATH Caddy 容器内站点根路径（默认 /srv）
  --install-units        把渲染后的 systemd unit 安装到 /etc/systemd/system
  --enable-timer         安装后启用 stash-kelee-update.timer（隐含 --install-units）
  --install-caddyfile    渲染 <remote-root>/Caddyfile（旧文件先备份）
  --dry-run              只打印计划，不写文件
  -h, --help
EOF
}

PUBLIC_BASE_ARG=""
PUBLIC_HOST_ARG=""
MIRROR_BASE_ARG=""
REMOTE_ROOT_ARG=""
UPDATER_ROOT_ARG=""
HTTP_PORT_ARG=""
CONTAINER_NAME_ARG=""
KEEP_RELEASES_ARG=""
KEEP_ASSET_DAYS_ARG=""
CADDY_SITE_ROOT_ARG=""
INSTALL_UNITS=0
ENABLE_TIMER=0
INSTALL_CADDYFILE=0
DRY_RUN=0

while (($#)); do
    case "$1" in
    --public-base) PUBLIC_BASE_ARG="$2"; shift 2 ;;
    --public-host) PUBLIC_HOST_ARG="$2"; shift 2 ;;
    --mirror-base) MIRROR_BASE_ARG="$2"; shift 2 ;;
    --remote-root) REMOTE_ROOT_ARG="$2"; shift 2 ;;
    --updater-root) UPDATER_ROOT_ARG="$2"; shift 2 ;;
    --http-port) HTTP_PORT_ARG="$2"; shift 2 ;;
    --container-name) CONTAINER_NAME_ARG="$2"; shift 2 ;;
    --keep-releases) KEEP_RELEASES_ARG="$2"; shift 2 ;;
    --keep-asset-days) KEEP_ASSET_DAYS_ARG="$2"; shift 2 ;;
    --caddy-site-root) CADDY_SITE_ROOT_ARG="$2"; shift 2 ;;
    --install-units) INSTALL_UNITS=1; shift ;;
    --enable-timer) ENABLE_TIMER=1; INSTALL_UNITS=1; shift ;;
    --install-caddyfile) INSTALL_CADDYFILE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) die_config "未知参数：$1（--help 查看用法）" ;;
    esac
done

[[ -n "$PUBLIC_BASE_ARG" ]] && KELEE_PUBLIC_BASE="$PUBLIC_BASE_ARG"
[[ -n "$PUBLIC_HOST_ARG" ]] && STASH_PUBLIC_HOST="$PUBLIC_HOST_ARG"
[[ -n "$MIRROR_BASE_ARG" ]] && KELEE_MIRROR_BASE="$MIRROR_BASE_ARG"
[[ -n "$REMOTE_ROOT_ARG" ]] && STASH_REMOTE_ROOT="$REMOTE_ROOT_ARG"
[[ -n "$UPDATER_ROOT_ARG" ]] && STASH_UPDATER_ROOT="$UPDATER_ROOT_ARG"
[[ -n "$HTTP_PORT_ARG" ]] && STASH_HTTP_PORT="$HTTP_PORT_ARG"
[[ -n "$CONTAINER_NAME_ARG" ]] && STASH_CONTAINER_NAME="$CONTAINER_NAME_ARG"
[[ -n "$KEEP_RELEASES_ARG" ]] && STASH_KEEP_RELEASES="$KEEP_RELEASES_ARG"
[[ -n "$KEEP_ASSET_DAYS_ARG" ]] && STASH_KEEP_ASSET_DAYS="$KEEP_ASSET_DAYS_ARG"

resolve_public_config
resolve_paths
resolve_http_config
require_cmd sed grep install mktemp mv cp

CADDY_SITE_ROOT="${CADDY_SITE_ROOT_ARG:-${STASH_CADDY_SITE_ROOT:-/srv}}"
validate_abs_path "$CADDY_SITE_ROOT" "CADDY_SITE_ROOT"
export STASH_CADDY_SITE_ROOT="$CADDY_SITE_ROOT"

SYSTEMD_DIR="$STASH_UPDATER_ROOT/systemd"
ENV_FILE="$STASH_UPDATER_ROOT/env"
SERVICE_OUT="$SYSTEMD_DIR/stash-kelee-update.service"
TIMER_OUT="$SYSTEMD_DIR/stash-kelee-update.timer"
CADDYFILE_OUT="$STASH_REMOTE_ROOT/Caddyfile"
TEMPLATE_DIR="$SCRIPT_DIR/templates"

for template in env.in stash-kelee-update.service.in stash-kelee-update.timer.in Caddyfile.in; do
    [[ -f "$TEMPLATE_DIR/$template" ]] || die_config "缺少模板：$TEMPLATE_DIR/$template"
done

if ((DRY_RUN == 1)); then
    log "配置渲染计划（dry-run，不写文件）"
    log "  env=$ENV_FILE"
    log "  units=$SYSTEMD_DIR/stash-kelee-update.{service,timer}（install=$INSTALL_UNITS enable_timer=$ENABLE_TIMER）"
    if ((INSTALL_CADDYFILE == 1)); then
        log "  caddyfile=$CADDYFILE_OUT（旧文件将备份，site_root=$CADDY_SITE_ROOT）"
    fi
    log "  public_base=$STASH_PUBLIC_BASE public_host=$STASH_PUBLIC_HOST container=$STASH_CONTAINER_NAME http_port=$STASH_HTTP_PORT"
    exit "$EX_OK"
fi

render_template() {
    local template="$1"
    local out="$2"
    local tmp
    tmp="$(mktemp "${out}.tmp.XXXXXX")" || die "$EX_STATE" "无法创建临时文件：$out"
    sed \
        -e "s|@@REMOTE_ROOT@@|$STASH_REMOTE_ROOT|g" \
        -e "s|@@UPDATER_ROOT@@|$STASH_UPDATER_ROOT|g" \
        -e "s|@@PUBLIC_BASE@@|$STASH_PUBLIC_BASE|g" \
        -e "s|@@MIRROR_BASE@@|$KELEE_MIRROR_BASE|g" \
        -e "s|@@PUBLIC_HOST@@|$STASH_PUBLIC_HOST|g" \
        -e "s|@@HTTP_PORT@@|$STASH_HTTP_PORT|g" \
        -e "s|@@CONTAINER_NAME@@|$STASH_CONTAINER_NAME|g" \
        -e "s|@@KEEP_RELEASES@@|$STASH_KEEP_RELEASES|g" \
        -e "s|@@KEEP_ASSET_DAYS@@|$STASH_KEEP_ASSET_DAYS|g" \
        -e "s|@@SITE_ROOT@@|$CADDY_SITE_ROOT|g" \
        -e "s|@@CADDY_SITE_ROOT@@|$CADDY_SITE_ROOT|g" \
        "$TEMPLATE_DIR/$template" >"$tmp" || {
        rm -f "$tmp"
        die_validation "模板渲染失败：$TEMPLATE_DIR/$template"
    }
    if grep -q '@@[A-Z_]*@@' "$tmp"; then
        grep -o '@@[A-Z_]*@@' "$tmp" | sort -u >&2 || true
        rm -f "$tmp"
        die_validation "模板仍有未替换的占位符：$template"
    fi
    chmod 0644 "$tmp" || true
    mv -f "$tmp" "$out" || die "$EX_STATE" "无法写入：$out"
}

render_file_to() {
    # render a template to an arbitrary output path (atomic)
    local template="$1"
    local out="$2"
    local out_dir
    out_dir="$(dirname "$out")"
    mkdir -p "$out_dir" || die "$EX_STATE" "无法创建目录：$out_dir"
    render_template "$template" "$out"
}

mkdir -p "$STATE_DIR" "$LOG_DIR" "$SYSTEMD_DIR" || die "$EX_STATE" "无法创建状态/日志/systemd 目录"

# 1) EnvironmentFile (single source for the cloud timer).
render_file_to env.in "$ENV_FILE"
log "已渲染 EnvironmentFile：$ENV_FILE"

# Self-check the rendered env before anything depends on it.
env_expected=(
    "STASH_REMOTE_ROOT=$STASH_REMOTE_ROOT"
    "STASH_UPDATER_ROOT=$STASH_UPDATER_ROOT"
    "KELEE_PUBLIC_BASE=$STASH_PUBLIC_BASE"
    "KELEE_MIRROR_BASE=$STASH_PUBLIC_BASE"
    "STASH_PUBLIC_HOST=$STASH_PUBLIC_HOST"
    "STASH_HTTP_PORT=$STASH_HTTP_PORT"
)
for expected_line in "${env_expected[@]}"; do
    grep -qxF "$expected_line" "$ENV_FILE" || die_validation "渲染后的 EnvironmentFile 缺少/错误：$expected_line"
done

# 2) systemd units.
render_file_to stash-kelee-update.service.in "$SERVICE_OUT"
render_file_to stash-kelee-update.timer.in "$TIMER_OUT"
grep -qF "EnvironmentFile=-$STASH_UPDATER_ROOT/env" "$SERVICE_OUT" ||
    die_validation "service 单元 EnvironmentFile 未指向 $STASH_UPDATER_ROOT/env"
grep -qF "ExecStart=/usr/bin/env bash $STASH_UPDATER_ROOT/current/run-updater.sh" "$SERVICE_OUT" ||
    die_validation "service 单元 ExecStart 未指向 $STASH_UPDATER_ROOT/current/run-updater.sh"
log "已渲染 systemd 单元：$SERVICE_OUT、$TIMER_OUT"

if ((INSTALL_UNITS == 1)); then
    require_cmd systemctl
    install -m 0644 "$SERVICE_OUT" /etc/systemd/system/stash-kelee-update.service ||
        die_config "安装 service 单元失败（需要 root）：/etc/systemd/system/stash-kelee-update.service"
    install -m 0644 "$TIMER_OUT" /etc/systemd/system/stash-kelee-update.timer ||
        die_config "安装 timer 单元失败（需要 root）：/etc/systemd/system/stash-kelee-update.timer"
    systemctl daemon-reload || die_config "systemctl daemon-reload 失败"
    log "systemd 单元已安装：/etc/systemd/system/stash-kelee-update.{service,timer}"
fi

if ((ENABLE_TIMER == 1)); then
    require_cmd systemctl
    systemctl enable --now stash-kelee-update.timer || die_config "启用 stash-kelee-update.timer 失败"
    systemctl is-enabled --quiet stash-kelee-update.timer || die_config "timer 未启用"
    systemctl is-active --quiet stash-kelee-update.timer || die_config "timer 未激活"
    log "systemd timer 已启用：stash-kelee-update.timer（每日 03:20，带随机延迟）"
fi

# 3) Optional Caddyfile (host/root plumbed through the template).
if ((INSTALL_CADDYFILE == 1)); then
    if [[ -f "$CADDYFILE_OUT" ]]; then
        backup="${CADDYFILE_OUT}.bak.$(date -u '+%Y%m%d-%H%M%S')"
        cp -p "$CADDYFILE_OUT" "$backup" || die_config "无法备份现有 Caddyfile：$CADDYFILE_OUT"
        log "已备份原 Caddyfile：$backup"
    fi
    render_file_to Caddyfile.in "$CADDYFILE_OUT"
    grep -qF "@stash host $STASH_PUBLIC_HOST" "$CADDYFILE_OUT" ||
        die_validation "渲染后的 Caddyfile host 匹配器未使用 $STASH_PUBLIC_HOST"
    grep -qF "root * $CADDY_SITE_ROOT" "$CADDYFILE_OUT" ||
        die_validation "渲染后的 Caddyfile 站点根未使用 $CADDY_SITE_ROOT"
    # Ordering matters: the legacy no-cache rule must precede the immutable
    # hashed rule (later header handler wins), and the hashed matcher must be
    # anchored on the 12-hex content address (not a loose *-* glob).
    legacy_line="$(grep -n '@legacy_scripts path_regexp' "$CADDYFILE_OUT" | head -n 1 | cut -d: -f1)"
    hashed_line="$(grep -n '@hashed_scripts path_regexp' "$CADDYFILE_OUT" | head -n 1 | cut -d: -f1)"
    [[ -n "$legacy_line" && -n "$hashed_line" && "$legacy_line" -lt "$hashed_line" ]] ||
        die_validation "渲染后的 Caddyfile 缓存规则顺序错误：legacy 必须位于 hashed 之前"
    grep -qF '[0-9a-f]{12}-' "$CADDYFILE_OUT" ||
        die_validation "渲染后的 Caddyfile 未按 12 位内容寻址前缀匹配 hashed 资产"
    grep -qF 'immutable' "$CADDYFILE_OUT" ||
        die_validation "渲染后的 Caddyfile 缺少 hashed 资产的 immutable 缓存策略"
    log "已渲染 Caddyfile：$CADDYFILE_OUT（host=$STASH_PUBLIC_HOST site_root=$CADDY_SITE_ROOT）"
fi

log "配置渲染完成：remote_root=$STASH_REMOTE_ROOT updater_root=$STASH_UPDATER_ROOT public_base=$STASH_PUBLIC_BASE"
exit "$EX_OK"
