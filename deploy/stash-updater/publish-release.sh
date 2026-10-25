#!/usr/bin/env bash
# shellcheck shell=bash
#
# The single publish transaction used by BOTH the manual Windows deploy
# (deploy-stash.ps1 over ssh) and the cloud systemd timer (run-updater.sh).
#
# Transaction outline (all mutations after the lock is held):
#   1. flock -n on <updater>/update.lock (shared with the timer; a concurrent
#      attempt exits 75 with a clear message instead of racing).
#   2. validate the staged release: required files, targets.json coverage,
#      provider bundle, referenced hashed assets, .build-manifest.json outputs
#      and/or an externally pinned sha256sum list (expected sums).
#   3. install the new updater app atomically (optional --install-app).
#   4. copy forward still-fresh content-addressed scripts from older releases.
#   5. move staging -> releases/<id> (same filesystem, atomic rename) and switch
#      `current` with an atomic symlink replace (guard: current absent/symlink).
#   6. install staged/validated config (EnvironmentFile/systemd/Caddyfile),
#      switch `current`, recreate the compose services so the running Caddy/
#      systemd side actually loads the new config.
#   7. health-check real HTTP bytes (homepage, .stoverride, hashed script,
#      provider bundle) with the new Host header + actual Cache-Control policy.
#   8. update the asset ledger and run retention (bookkeeping, warnings only).
#   9. atomically sync the authoritative update state (data dir).
#  10. commit point: only bookkeeping follows; a post-commit failure must not
#      roll back a healthy release.
#
# Any failure after the pointer switch restores the previous release, recreates
# the container and re-verifies the previous version. A first deploy that fails
# removes the `current` pointer instead of leaving a broken one. Every rollback
# is recorded in publish state + publish.log.
#
# Exit codes: 0 ok, 10 config, 20 validation, 30 compose, 40 health,
#             50 state sync, 60 critical (rollback failed), 75 lock busy.

set -Eeuo pipefail
umask 022

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
    cat <<'EOF'
用法：
  publish-release.sh --release-id ID --staging DIR [选项]
  publish-release.sh --rollback-to ID [选项]      # 手工回滚到已有版本

选项：
  --release-id ID        版本号（时间戳式，字母数字 . _ -）
  --staging DIR          已上传/构建好的发布目录，必须位于 <REMOTE_ROOT>/releases 下
  --expected-sums FILE   sha256sum -c 兼容的期望哈希清单（发布方本地生成，钉住字节）
  --install-app DIR      事务开始时原子切换 <UPDATER_ROOT>/current -> DIR
  --render-config-script FILE
                         在锁内、发布成功后运行 render-config.sh（配置安装属于同一事务）
  --render-install-units 渲染时安装 systemd 单元（/etc/systemd/system）
  --render-install-caddyfile
                         渲染时安装 Caddyfile（旧文件先备份）
  --render-enable-timer  渲染时启用 stash-kelee-update.timer
  --source NAME          日志来源标记：manual | cloud | rollback（默认 manual）
  --no-state-sync        不同步 <UPDATER_ROOT>/data 更新状态（测试/调试用）
  --skip-retention       发布成功后不运行保留策略（测试用）
  --rollback-to ID       回滚到 releases/ID（同一套容器/健康/状态逻辑）
  --dry-run              只校验参数并打印事务计划，不做任何修改
  -h, --help

环境变量（通常来自 systemd EnvironmentFile 或手工部署注入）：
  STASH_REMOTE_ROOT / STASH_UPDATER_ROOT / STASH_PUBLIC_HOST / KELEE_PUBLIC_BASE
  STASH_HTTP_PORT / STASH_HEALTH_HOST / STASH_CONTAINER_NAME
  STASH_KEEP_RELEASES / STASH_KEEP_ASSET_DAYS
EOF
}

RELEASE_ID=""
STAGING=""
EXPECTED_SUMS=""
APP_DIR=""
RENDER_CONFIG_SCRIPT=""
RENDER_INSTALL_UNITS=0
RENDER_INSTALL_CADDYFILE=0
RENDER_ENABLE_TIMER=0
SOURCE="manual"
SYNC_STATE=1
RUN_RETENTION=1
ROLLBACK_TO=""
DRY_RUN=0

while (($#)); do
    case "$1" in
    --release-id)
        [[ $# -ge 2 ]] || die_config "--release-id 缺少参数"
        RELEASE_ID="$2"
        shift 2
        ;;
    --staging)
        [[ $# -ge 2 ]] || die_config "--staging 缺少参数"
        STAGING="$2"
        shift 2
        ;;
    --expected-sums)
        [[ $# -ge 2 ]] || die_config "--expected-sums 缺少参数"
        EXPECTED_SUMS="$2"
        shift 2
        ;;
    --install-app)
        [[ $# -ge 2 ]] || die_config "--install-app 缺少参数"
        APP_DIR="$2"
        shift 2
        ;;
    --render-config-script)
        [[ $# -ge 2 ]] || die_config "--render-config-script 缺少参数"
        RENDER_CONFIG_SCRIPT="$2"
        shift 2
        ;;
    --render-install-units)
        RENDER_INSTALL_UNITS=1
        shift
        ;;
    --render-install-caddyfile)
        RENDER_INSTALL_CADDYFILE=1
        shift
        ;;
    --render-enable-timer)
        RENDER_ENABLE_TIMER=1
        shift
        ;;
    --source)
        [[ $# -ge 2 ]] || die_config "--source 缺少参数"
        SOURCE="$2"
        shift 2
        ;;
    --no-state-sync)
        SYNC_STATE=0
        shift
        ;;
    --skip-retention)
        RUN_RETENTION=0
        shift
        ;;
    --rollback-to)
        [[ $# -ge 2 ]] || die_config "--rollback-to 缺少参数"
        ROLLBACK_TO="$2"
        shift 2
        ;;
    --dry-run)
        DRY_RUN=1
        shift
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

resolve_public_config
resolve_paths
resolve_http_config
require_cmd bash flock rsync sha256sum python3 find sort sed grep awk mktemp mv ln rm date cut
[[ -f "$STASH_COMPOSE_FILE" ]] || die_config "找不到 Compose 文件：$STASH_COMPOSE_FILE"
[[ -f "$SCRIPT_DIR/health-check.sh" ]] || die_config "缺少健康检查脚本：$SCRIPT_DIR/health-check.sh"

# ---------------------------------------------------------------------------
# Transaction plan (dry-run / diagnostics)
# ---------------------------------------------------------------------------

print_plan() {
    log "发布事务计划（dry-run，不做任何修改）"
    log "  remote_root=$STASH_REMOTE_ROOT updater_root=$STASH_UPDATER_ROOT"
    log "  public_base=$STASH_PUBLIC_BASE container=$STASH_CONTAINER_NAME http=127.0.0.1:$STASH_HTTP_PORT"
    log "  lock=$LOCK_FILE（手动部署与云端 timer 共用）"
    if [[ -n "$ROLLBACK_TO" ]]; then
        log "  action=rollback target=$RELEASES_DIR/$ROLLBACK_TO previous=$(current_release_path || echo none)"
        log "  steps: 校验目标版本 -> 原子切换 current -> compose 重建 -> 健康检查 -> 记录 manual_rollback"
        return 0
    fi
    log "  action=publish release_id=$RELEASE_ID staging=$STAGING"
    log "  expected_sums=${EXPECTED_SUMS:-<未提供，使用 .build-manifest.json outputs>}"
    log "  install_app=${APP_DIR:-<不切换更新器应用>} source=$SOURCE state_sync=$SYNC_STATE retention=$RUN_RETENTION"
    log "  steps: flock -> 校验发布包/清单哈希 -> copy-forward 内容寻址资产"
    log "         -> staging 原子 mv 到 releases/$RELEASE_ID -> 原子切换 current"
    log "         -> docker compose up -d --force-recreate --no-build -> 容器 running"
    log "         -> 健康检查（首页/.stoverride/哈希脚本/provider 合集，HTTP 200 + sha256）"
    log "         -> 原子同步更新状态 -> 资产账本/发布状态/日志 -> 保留策略"
    log "  failure: 自动恢复上一版本 current + compose + 健康复验；首次部署失败移除 current 指针"
}

# ---------------------------------------------------------------------------
# Rollback helpers (used for both automatic rollback and --rollback-to)
# ---------------------------------------------------------------------------

ROLLBACK_COUNT=0
PRIOR_STATE_PREVIOUS=""
PREVIOUS_APP_DIR=""
APP_POINTER_CHANGED=0
CONFIG_BACKUP_DIR=""
CONFIG_PATH_LIST=()
TIMER_WAS_ENABLED=0
CURRENT_SWITCHED=0
CONFIG_TOUCHED=0
SERVICES_TOUCHED=0
COMMIT_DONE=0
FAILURE_HANDLER_RUNNING=0
TRANSACTION_ACTIVE=0
RESTORE_FAILURES=()
PREVIOUS_PUBLIC_BASE=""
PREVIOUS_PUBLIC_HOST=""

record_rollback() {
    local result="$1"
    local release="$2"
    local reason="$3"
    local reason_file="${4:-0}"
    local previous_state="${5:-}"
    local count
    count=$((ROLLBACK_COUNT + 1))
    write_publish_state "publish-release.sh" "$result" "failure" "$release" "$reason" "" "$previous_state" "$count"
    return 0
}

current_app_path() {
    if [[ -L "$UPDATER_CURRENT" ]]; then
        local target=""
        target="$(readlink -f "$UPDATER_CURRENT" 2>/dev/null || true)"
        if [[ -n "$target" && -d "$target" ]]; then
            printf '%s' "$target"
            return 0
        fi
    fi
    return 1
}

restore_previous_release() {
    local previous="$1"
    [[ -n "$previous" && -d "$previous" && -f "$previous/index.html" ]] || return 1
    log "回滚：current -> $previous"
    atomic_symlink "$previous" "$CURRENT_LINK" || return 1
    compose_up || return 1
    wait_container_running || return 1
    perform_health "$previous" || return 1
    return 0
}

restore_app_pointer() {
    ((APP_POINTER_CHANGED == 1)) || return 0
    APP_POINTER_CHANGED=0
    if [[ -n "$PREVIOUS_APP_DIR" && -d "$PREVIOUS_APP_DIR" ]]; then
        if atomic_symlink "$PREVIOUS_APP_DIR" "$UPDATER_CURRENT"; then
            log "更新器应用指针已恢复：$UPDATER_CURRENT -> $PREVIOUS_APP_DIR"
        else
            warn "无法恢复更新器应用指针：$UPDATER_CURRENT -> $PREVIOUS_APP_DIR"
            return 1
        fi
    else
        if [[ -L "$UPDATER_CURRENT" ]]; then
            rm -f -- "$UPDATER_CURRENT" 2>/dev/null || true
            log "首次安装失败：已移除更新器应用指针 $UPDATER_CURRENT"
        fi
    fi
    return 0
}

# Files written by render-config.sh while the publish lock is held. They are
# snapshotted before rendering and restored when the transaction rolls back.
config_snapshot_paths() {
    CONFIG_PATH_LIST=(
        "$STASH_UPDATER_ROOT/env"
        "$STASH_UPDATER_ROOT/systemd/stash-kelee-update.service"
        "$STASH_UPDATER_ROOT/systemd/stash-kelee-update.timer"
    )
    if ((RENDER_INSTALL_UNITS == 1)); then
        CONFIG_PATH_LIST+=(
            "/etc/systemd/system/stash-kelee-update.service"
            "/etc/systemd/system/stash-kelee-update.timer"
        )
    fi
    if ((RENDER_INSTALL_CADDYFILE == 1)) || [[ -f "$STASH_REMOTE_ROOT/Caddyfile" ]]; then
        CONFIG_PATH_LIST+=("$STASH_REMOTE_ROOT/Caddyfile")
    fi
}

snapshot_config() {
    [[ -n "$RENDER_CONFIG_SCRIPT" ]] || return 0
    mkdir -p "$STATE_DIR" || die "$EX_STATE" "无法创建状态目录：$STATE_DIR"
    CONFIG_BACKUP_DIR="$(mktemp -d "$STATE_DIR/.config-backup.${RELEASE_ID}.XXXXXX")" ||
        die "$EX_STATE" "无法创建配置备份目录"
    config_snapshot_paths
    local index=0 path existed
    : >"$CONFIG_BACKUP_DIR/map" || die "$EX_STATE" "无法写入配置备份索引"
    for path in "${CONFIG_PATH_LIST[@]}"; do
        if [[ -e "$path" || -L "$path" ]]; then
            cp -a "$path" "$CONFIG_BACKUP_DIR/$index" || die "$EX_STATE" "无法备份配置：$path"
            existed=1
        else
            existed=0
        fi
        printf '%s\t%s\t%s\n' "$index" "$path" "$existed" >>"$CONFIG_BACKUP_DIR/map"
        index=$((index + 1))
    done
    # Rollback health checks must use the old release's own address, not the
    # new domain from the EnvironmentFile being installed by this transaction.
    PREVIOUS_PUBLIC_BASE=""
    PREVIOUS_PUBLIC_HOST=""
    if [[ -f "$STASH_UPDATER_ROOT/env" ]]; then
        PREVIOUS_PUBLIC_BASE="$(sed -n 's/^KELEE_PUBLIC_BASE=//p' "$STASH_UPDATER_ROOT/env" | tail -n 1)"
        PREVIOUS_PUBLIC_HOST="$(sed -n 's/^STASH_PUBLIC_HOST=//p' "$STASH_UPDATER_ROOT/env" | tail -n 1)"
    fi
    if [[ -z "$PREVIOUS_PUBLIC_BASE" && -n "${PREVIOUS_RELEASE:-}" && -f "$PREVIOUS_RELEASE/.build-manifest.json" ]]; then
        PREVIOUS_PUBLIC_BASE="$(sed -n 's/.*"public_base"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
            "$PREVIOUS_RELEASE/.build-manifest.json" | head -n 1)"
        if [[ -n "$PREVIOUS_PUBLIC_BASE" ]]; then
            PREVIOUS_PUBLIC_HOST="${PREVIOUS_PUBLIC_BASE#*://}"
        fi
    fi
    if [[ -z "$PREVIOUS_PUBLIC_BASE" ]]; then
        PREVIOUS_PUBLIC_BASE="$STASH_PUBLIC_BASE"
        PREVIOUS_PUBLIC_HOST="$STASH_PUBLIC_HOST"
    fi
    if ((RENDER_ENABLE_TIMER == 1)) && command -v systemctl >/dev/null 2>&1; then
        if systemctl is-enabled --quiet stash-kelee-update.timer >/dev/null 2>&1; then
            TIMER_WAS_ENABLED=1
        fi
    fi
    log "已快照发布前配置（${#CONFIG_PATH_LIST[@]} 项）：$CONFIG_BACKUP_DIR（旧地址 ${PREVIOUS_PUBLIC_BASE}）"
}

# Restore every config file; keep going if one file fails so the remaining
# independent restores are still attempted. Returns 1 when any copy failed.
restore_config_snapshot() {
    [[ -n "$CONFIG_BACKUP_DIR" && -d "$CONFIG_BACKUP_DIR" && -f "$CONFIG_BACKUP_DIR/map" ]] || return 0
    local index path existed restored_units=0 failures=0
    while IFS=$'\t' read -r index path existed; do
        [[ -n "$index" && -n "$path" ]] || continue
        if [[ "$existed" == "1" ]]; then
            mkdir -p "$(dirname "$path")"
            if ! cp -a "$CONFIG_BACKUP_DIR/$index" "$path"; then
                warn "恢复配置失败：$path（备份在 $CONFIG_BACKUP_DIR/$index）"
                failures=$((failures + 1))
            fi
        elif ! rm -f -- "$path" 2>/dev/null; then
            warn "删除发布期间新增的配置失败：$path"
            failures=$((failures + 1))
        fi
        case "$path" in
        /etc/systemd/system/*) restored_units=1 ;;
        esac
    done <"$CONFIG_BACKUP_DIR/map"
    if command -v systemctl >/dev/null 2>&1; then
        if ((restored_units == 1)); then
            systemctl daemon-reload >/dev/null 2>&1 ||
                warn "配置已恢复，但 systemctl daemon-reload 失败"
        fi
        if ((RENDER_ENABLE_TIMER == 1)); then
            if ((TIMER_WAS_ENABLED == 1)); then
                systemctl enable --now stash-kelee-update.timer >/dev/null 2>&1 ||
                    warn "配置已恢复，但重新启用 stash-kelee-update.timer 失败"
            else
                systemctl disable --now stash-kelee-update.timer >/dev/null 2>&1 ||
                    warn "配置已恢复，但停用发布期间启用的 stash-kelee-update.timer 失败"
            fi
        fi
    fi
    if ((failures > 0)); then
        return 1
    fi
    log "发布前配置已恢复（$CONFIG_BACKUP_DIR）"
    return 0
}

cleanup_config_backup() {
    [[ -n "$CONFIG_BACKUP_DIR" && -d "$CONFIG_BACKUP_DIR" ]] || return 0
    rm -rf -- "$CONFIG_BACKUP_DIR" 2>/dev/null || true
    CONFIG_BACKUP_DIR=""
}

render_config_install() {
    [[ -n "$RENDER_CONFIG_SCRIPT" ]] || return 0
    local caddy_root="${STASH_CADDY_SITE_ROOT:-/srv}"
    local args=(
        --remote-root "$STASH_REMOTE_ROOT"
        --updater-root "$STASH_UPDATER_ROOT"
        --public-base "$STASH_PUBLIC_BASE"
        --public-host "$STASH_PUBLIC_HOST"
        --mirror-base "$KELEE_MIRROR_BASE"
        --http-port "$STASH_HTTP_PORT"
        --container-name "$STASH_CONTAINER_NAME"
        --keep-releases "$STASH_KEEP_RELEASES"
        --keep-asset-days "$STASH_KEEP_ASSET_DAYS"
        --caddy-site-root "$caddy_root"
    )
    ((RENDER_INSTALL_UNITS == 1)) && args+=(--install-units)
    ((RENDER_INSTALL_CADDYFILE == 1)) && args+=(--install-caddyfile)
    ((RENDER_ENABLE_TIMER == 1)) && args+=(--enable-timer)
    log "锁内渲染/安装更新器配置：$RENDER_CONFIG_SCRIPT（units=$RENDER_INSTALL_UNITS caddyfile=$RENDER_INSTALL_CADDYFILE enable_timer=$RENDER_ENABLE_TIMER）"
    bash "$RENDER_CONFIG_SCRIPT" "${args[@]}"
}

# Recreate the compose services so the running Caddy/systemd side actually
# loads the config installed above (a written file is not a loaded config).
reload_services() {
    SERVICES_TOUCHED=1
    log "重建/重载站点服务以加载新配置：docker compose up --force-recreate"
    compose_up || return 1
    wait_container_running || return 1
    return 0
}

perform_health() {
    local release_dir="$1"
    local public_base="${2:-}"
    local public_host="${3:-}"
    local strict_cache="${4:-}"
    local args=(--release "$release_dir")
    if [[ -n "$public_base" && -n "$public_host" ]]; then
        args+=(--public-base "$public_base" --public-host "$public_host")
    fi
    if [[ -n "$strict_cache" ]]; then
        args+=(--strict-cache-headers "$strict_cache")
    fi
    bash "$SCRIPT_DIR/health-check.sh" "${args[@]}"
}

# Restore site pointer, app pointer, config and the actual running service.
# Every independent step is attempted even if an earlier one fails; the caller
# decides whether the aggregate result is critical.
restore_transaction_state() {
    RESTORE_FAILURES=()
    if [[ -n "$CONFIG_BACKUP_DIR" ]]; then
        restore_config_snapshot || RESTORE_FAILURES+=("配置回滚")
    fi

    if ((CURRENT_SWITCHED == 1)); then
        if [[ -n "${PREVIOUS_RELEASE:-}" && -d "$PREVIOUS_RELEASE" ]]; then
            atomic_symlink "$PREVIOUS_RELEASE" "$CURRENT_LINK" ||
                RESTORE_FAILURES+=("站点指针回滚")
        else
            rm -f -- "$CURRENT_LINK" 2>/dev/null || RESTORE_FAILURES+=("移除首次部署站点指针")
        fi
    fi

    if ((APP_POINTER_CHANGED == 1)); then
        restore_app_pointer || RESTORE_FAILURES+=("更新器应用指针回滚")
    fi

    if ((SERVICES_TOUCHED == 1 || CURRENT_SWITCHED == 1)); then
        compose_up || RESTORE_FAILURES+=("旧服务重建")
        wait_container_running || RESTORE_FAILURES+=("旧容器状态")
        if [[ -n "${PREVIOUS_RELEASE:-}" && -d "$PREVIOUS_RELEASE" ]]; then
            local old_base="${PREVIOUS_PUBLIC_BASE:-$STASH_PUBLIC_BASE}"
            local old_host="${PREVIOUS_PUBLIC_HOST:-$STASH_PUBLIC_HOST}"
            if ! perform_health "$PREVIOUS_RELEASE" "$old_base" "$old_host" 0; then
                RESTORE_FAILURES+=("旧版本健康复验（$old_base）")
            fi
        fi
    fi

    if [[ -z "${PREVIOUS_RELEASE:-}" ]]; then
        if [[ -e "$CURRENT_LINK" || -L "$CURRENT_LINK" ]]; then
            rm -f -- "$CURRENT_LINK" 2>/dev/null || RESTORE_FAILURES+=("清理首次部署站点指针")
        fi
    fi
}

# Single failure entry point for every mutation before the commit point.
transaction_fail() {
    local reason="$1"
    local code="$2"

    if ((FAILURE_HANDLER_RUNNING == 1)); then
        log "回滚处理中再次失败（$reason），避免递归回滚；返回 EX_CRITICAL"
        exit "$EX_CRITICAL"
    fi
    if ((COMMIT_DONE == 1)); then
        log "提交点后失败（$reason）：发布已提交，不执行整站回滚；返回 $code"
        exit "$code"
    fi
    FAILURE_HANDLER_RUNNING=1
    TRANSACTION_ACTIVE=0
    set +e
    log "发布事务失败：$reason"
    append_event "rollback_start" "$RELEASE_ID" "$reason"
    restore_transaction_state

    if ((${#RESTORE_FAILURES[@]} == 0)); then
        if [[ -z "${PREVIOUS_RELEASE:-}" ]]; then
            record_rollback "first_deploy_failed" "$RELEASE_DIR" "$reason" 1 ""
            append_event "first_deploy_failed" "$RELEASE_ID" "$reason；已移除 current 指针并恢复发布前状态"
            log "首次部署失败：已移除 current/应用指针并恢复配置（退出码 $code）"
        else
            record_rollback "rollback" "$RELEASE_DIR" "$reason" 1 "$PRIOR_STATE_PREVIOUS"
            append_event "rollback_ok" "$RELEASE_ID" "$reason；current=$PREVIOUS_RELEASE"
            log "回滚完成：站点/更新器应用/配置/服务均已恢复并复验（退出码 $code）"
        fi
        cleanup_config_backup
        exit "$code"
    fi

    record_rollback "rollback_failed" "$RELEASE_DIR" "$reason" 1 "$PRIOR_STATE_PREVIOUS"
    append_event "rollback_failed" "$RELEASE_ID" \
        "$reason；失败步骤=${RESTORE_FAILURES[*]}；配置备份=$CONFIG_BACKUP_DIR"
    log "回滚失败，以下恢复步骤未通过：${RESTORE_FAILURES[*]}"
    log "配置备份已保留：${CONFIG_BACKUP_DIR:-<无>}；失败版本目录：$RELEASE_DIR"
    log "人工恢复建议：检查 current、$UPDATER_CURRENT、$STASH_UPDATER_ROOT/env 与 systemd 单元，修复后重新运行发布或 --rollback-to"
    die "$EX_CRITICAL" "回滚未能完整恢复（$reason）：${RESTORE_FAILURES[*]}"
}

do_rollback_to() {
    local target_id="$1"
    validate_release_id "$target_id"
    local target="$RELEASES_DIR/$target_id"
    [[ -d "$target" && -f "$target/index.html" ]] || die_validation "回滚目标版本不存在或无效：$target"

    local previous=""
    previous="$(current_release_path || true)"
    if [[ "$target" == "$previous" ]]; then
        log "current 已指向 $target，无需回滚"
        exit "$EX_OK"
    fi

    if ((DRY_RUN == 1)); then
        print_plan
        exit "$EX_OK"
    fi

    acquire_lock
    mkdir -p "$STATE_DIR" "$LOG_DIR" || die "$EX_STATE" "无法创建状态/日志目录"
    PREVIOUS_RELEASE="$previous"
    PRIOR_STATE_PREVIOUS="$(state_read PREVIOUS_RELEASE || true)"
    ROLLBACK_COUNT="$(state_read ROLLBACK_COUNT || true)"
    [[ "$ROLLBACK_COUNT" =~ ^[0-9]+$ ]] || ROLLBACK_COUNT=0

    ensure_pointer_guard "$CURRENT_LINK"
    log "手工回滚：$CURRENT_LINK -> $target（原版本：${previous:-无}）"
    atomic_symlink "$target" "$CURRENT_LINK" || die_validation "无法原子切换 current 指针到 $target"
    if ! compose_up || ! wait_container_running || ! perform_health "$target"; then
        warn "回滚目标 $target 未通过验证，尝试恢复原版本：${previous:-无}"
        if [[ -n "$previous" ]]; then
            if restore_previous_release "$previous"; then
                append_event "manual_rollback_failed" "$target_id" "目标版本不健康，已恢复 $previous"
            else
                append_event "manual_rollback_failed" "$target_id" "目标版本不健康，恢复原版本也失败"
                die "$EX_CRITICAL" "手工回滚失败且原版本恢复失败，请人工检查 $CURRENT_LINK 与容器"
            fi
        else
            rm -f -- "$CURRENT_LINK" 2>/dev/null || true
        fi
        die "$EX_HEALTH" "手工回滚失败：目标版本 $target 未通过健康检查"
    fi

    if ((SYNC_STATE == 1)); then
        if ! sync_state "$target" "$target_id"; then
            warn "手工回滚后更新状态同步失败，尝试恢复原版本 $previous"
            if [[ -n "$previous" ]] && restore_previous_release "$previous"; then
                append_event "manual_rollback_state_failed" "$target_id" "状态同步失败，已恢复 $previous"
                die "$EX_STATE" "手工回滚状态同步失败；已恢复原版本（current=$previous）"
            fi
            append_event "manual_rollback_state_failed" "$target_id" "状态同步失败且原版本恢复失败"
            die "$EX_CRITICAL" "手工回滚状态同步失败，且原版本恢复失败，请人工检查"
        fi
        log "手工回滚后更新状态已同步：$DATA_DIR（对应 release=$target_id）"
    fi
    update_asset_ledger "$target"
    write_publish_state "publish-release.sh" "manual_rollback" "success" "$target" "rollback-to $target_id" \
        "" "$previous" "$((ROLLBACK_COUNT + 1))"
    append_event "manual_rollback" "$target_id" "current=$target previous=${previous:-none} state_synced=$SYNC_STATE"
    log "手工回滚完成：$CURRENT_LINK -> $target"
    exit "$EX_OK"
}

# ---------------------------------------------------------------------------
# Argument / path validation (before touching anything)
# ---------------------------------------------------------------------------

if [[ -n "$ROLLBACK_TO" ]]; then
    if [[ -n "$RELEASE_ID" || -n "$STAGING" || -n "$APP_DIR" ]]; then
        die_config "--rollback-to 不能与 --release-id/--staging/--install-app 同时使用"
    fi
    do_rollback_to "$ROLLBACK_TO"
    exit "$EX_OK"
fi

[[ -n "$RELEASE_ID" ]] || die_config "--release-id 必填"
validate_release_id "$RELEASE_ID"
[[ -n "$STAGING" ]] || die_config "--staging 必填"
validate_abs_path "$STAGING" "STAGING"
case "$STAGING" in
"$RELEASES_DIR"/*) ;;
*) die_config "--staging 必须位于 $RELEASES_DIR 之下（保证同一文件系统原子 rename）：$STAGING" ;;
esac
if [[ -n "$EXPECTED_SUMS" ]]; then
    [[ -f "$EXPECTED_SUMS" ]] || die_config "期望哈希清单不存在：$EXPECTED_SUMS"
    validate_sums_file "$EXPECTED_SUMS"
fi
if [[ -n "$APP_DIR" ]]; then
    validate_abs_path "$APP_DIR" "APP_DIR"
    case "$APP_DIR" in
    "$UPDATER_RELEASES_DIR"/*) ;;
    *) die_config "--install-app 必须位于 $UPDATER_RELEASES_DIR 之下：$APP_DIR" ;;
    esac
fi
if [[ -n "$RENDER_CONFIG_SCRIPT" ]]; then
    validate_abs_path "$RENDER_CONFIG_SCRIPT" "RENDER_CONFIG_SCRIPT"
    [[ -f "$RENDER_CONFIG_SCRIPT" ]] || die_config "配置渲染脚本不存在：$RENDER_CONFIG_SCRIPT"
    case "$RENDER_CONFIG_SCRIPT" in
    "$UPDATER_RELEASES_DIR"/* | "$STASH_UPDATER_ROOT/current/"*) ;;
    *) die_config "--render-config-script 必须位于 $UPDATER_RELEASES_DIR 之下：$RENDER_CONFIG_SCRIPT" ;;
    esac
    [[ -z "$ROLLBACK_TO" ]] || die_config "--rollback-to 不能与 --render-config-script 同时使用"
fi
if [[ -z "$RENDER_CONFIG_SCRIPT" ]] && ((RENDER_INSTALL_CADDYFILE == 1)); then
    die_config "--render-install-caddyfile 需要同时提供 --render-config-script"
fi
if [[ -z "$RENDER_CONFIG_SCRIPT" ]] && ((RENDER_INSTALL_UNITS == 1 || RENDER_INSTALL_CADDYFILE == 1 || RENDER_ENABLE_TIMER == 1)); then
    die_config "渲染选项必须与 --render-config-script 一起使用"
fi

if ((DRY_RUN == 1)); then
    print_plan
    exit "$EX_OK"
fi

# ---------------------------------------------------------------------------
# Lock + prior state snapshot
# ---------------------------------------------------------------------------

acquire_lock
mkdir -p "$STATE_DIR" "$LOG_DIR" "$RELEASES_DIR" || die "$EX_STATE" "无法创建状态/日志/版本目录"

PREVIOUS_RELEASE="$(current_release_path || true)"
PRIOR_STATE_PREVIOUS="$(state_read PREVIOUS_RELEASE || true)"
ROLLBACK_COUNT="$(state_read ROLLBACK_COUNT || true)"
[[ "$ROLLBACK_COUNT" =~ ^[0-9]+$ ]] || ROLLBACK_COUNT=0
FINGERPRINT=""
PREVIOUS_APP_DIR="$(current_app_path || true)"

RELEASE_DIR="$RELEASES_DIR/$RELEASE_ID"
[[ -e "$RELEASE_DIR" ]] && die_validation "版本目录已存在，拒绝覆盖：$RELEASE_DIR"

STAGING_OWNED=0
on_exit() {
    local exit_code=$?
    set +e
    if ((TRANSACTION_ACTIVE == 1 && COMMIT_DONE == 0 && FAILURE_HANDLER_RUNNING == 0)); then
        transaction_fail "未预期的脚本退出（exit $exit_code，可能由 set -e 触发）" "$exit_code"
    fi
    if ((STAGING_OWNED == 0)) && [[ -n "$STAGING" && -d "$STAGING" ]]; then
        rm -rf -- "$STAGING" 2>/dev/null || true
    fi
}
trap on_exit EXIT

log "发布开始：release=$RELEASE_ID source=$SOURCE staging=$STAGING"
log "当前版本：${PREVIOUS_RELEASE:-<首次部署>}"

# ---------------------------------------------------------------------------
# Phase 0: validate everything before the first mutation.
# ---------------------------------------------------------------------------

validate_bundle_dir "$STAGING" "$EXPECTED_SUMS"
if [[ -f "$STAGING/.build-manifest.json" ]]; then
    FINGERPRINT="$(manifest_field "$STAGING/.build-manifest.json" fingerprint 2>/dev/null || true)"
fi
log "发布包校验通过：fingerprint=${FINGERPRINT:-unknown}"

if [[ -n "$APP_DIR" ]]; then
    validate_app_dir "$APP_DIR"
fi

# Snapshot old config/address/timer state before any mutation.
snapshot_config

# ---------------------------------------------------------------------------
# Phase 1: mutate app + release directories. Failures here must restore the
# app pointer/config even though the site pointer has not switched yet.
# ---------------------------------------------------------------------------

TRANSACTION_ACTIVE=1
if [[ -n "$APP_DIR" ]]; then
    ensure_pointer_guard "$UPDATER_CURRENT"
    if ! atomic_symlink "$APP_DIR" "$UPDATER_CURRENT"; then
        transaction_fail "更新器应用指针切换失败" "$EX_VALIDATION"
    fi
    APP_POINTER_CHANGED=1
    log "更新器应用已切换：$UPDATER_CURRENT -> $APP_DIR（上一应用：${PREVIOUS_APP_DIR:-<无>}）"
fi

if ! copy_forward_assets "$STAGING"; then
    transaction_fail "内容寻址资产 copy-forward 失败" "$EX_VALIDATION"
fi

if ! mv "$STAGING" "$RELEASE_DIR"; then
    transaction_fail "无法将暂存目录原子移动到版本目录：$RELEASE_DIR" "$EX_VALIDATION"
fi
STAGING_OWNED=1
log "版本目录就绪：$RELEASE_DIR"

# ---------------------------------------------------------------------------
# Phase 2: install (staged/validated) config while the old site is still
# current. A failure here restores config/app but does not touch the site.
# ---------------------------------------------------------------------------

if [[ -n "$RENDER_CONFIG_SCRIPT" ]]; then
    CONFIG_TOUCHED=1
    if ! render_config_install; then
        transaction_fail "配置渲染/安装失败（EnvironmentFile/systemd/Caddyfile）" "$EX_CONFIG"
    fi
fi

# ---------------------------------------------------------------------------
# Phase 3: switch the site, reload/recreate the actual services (a written
# config is not a loaded config), then health-check the new release under the
# new domain and the new cache policy.
# ---------------------------------------------------------------------------

ensure_pointer_guard "$CURRENT_LINK"
if ! atomic_symlink "$RELEASE_DIR" "$CURRENT_LINK"; then
    transaction_fail "无法原子切换 current 指针：$CURRENT_LINK" "$EX_VALIDATION"
fi
CURRENT_SWITCHED=1
log "current 指针已切换：$CURRENT_LINK -> $RELEASE_DIR"

if ! reload_services; then
    transaction_fail "服务重建/重载失败（docker compose）" "$EX_COMPOSE"
fi

if ! perform_health "$RELEASE_DIR" "$STASH_PUBLIC_BASE" "$STASH_PUBLIC_HOST" "${STASH_STRICT_CACHE_HEADERS:-0}"; then
    transaction_fail "健康检查失败（新版本/新域名/实际缓存头未通过）" "$EX_HEALTH"
fi

# Retention runs before the authoritative state sync so the seeded state does
# not carry already-expired content-addressed assets forward forever. The
# asset ledger is bookkeeping and is committed after the site/state commit.
if ((RUN_RETENTION == 1)); then
    if ! bash "$SCRIPT_DIR/retention.sh" --lock-held \
        --keep-releases "$STASH_KEEP_RELEASES" --keep-asset-days "$STASH_KEEP_ASSET_DAYS"; then
        warn "保留策略运行失败（不影响本次发布，可稍后手动重跑 retention.sh）"
    fi
fi

if ((SYNC_STATE == 1)); then
    if ! sync_state "$RELEASE_DIR" "$RELEASE_ID"; then
        transaction_fail "更新状态同步失败（旧状态已保留，可重试）" "$EX_STATE"
    fi
    log "更新状态已原子同步：$DATA_DIR"
fi

# ---------------------------------------------------------------------------
# Commit point. From here on the release is authoritative; only non-critical
# bookkeeping follows and failures must NOT roll back a healthy publication.
# ---------------------------------------------------------------------------
COMMIT_DONE=1
TRANSACTION_ACTIVE=0

if ! ( update_asset_ledger "$RELEASE_DIR" ); then
    warn "资产账本更新失败（发布已提交，可稍后手动重跑 retention/账本）"
fi
if ! write_publish_state "publish-release.sh" "publish" "success" "$RELEASE_DIR" "" "$FINGERPRINT" \
    "$PREVIOUS_RELEASE" "$ROLLBACK_COUNT"; then
    warn "发布状态写入失败（发布站点已上线，请检查 $STATE_FILE）"
fi
append_event "publish" "$RELEASE_ID" "source=$SOURCE current=$RELEASE_DIR previous=${PREVIOUS_RELEASE:-none}"
cleanup_config_backup

log "发布完成：release=$RELEASE_ID current=$CURRENT_LINK -> $RELEASE_DIR"
log "发布状态：$STATE_FILE；事件日志：$PUBLIC_LOG"
printf 'PUBLISH_RESULT=success\nPUBLISH_RELEASE=%s\nPUBLISH_CURRENT=%s\nPUBLISH_PREVIOUS=%s\n' \
    "$RELEASE_ID" "$RELEASE_DIR" "${PREVIOUS_RELEASE:-}"
exit "$EX_OK"
