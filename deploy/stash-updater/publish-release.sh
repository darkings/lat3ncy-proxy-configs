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
#   6. docker compose up -d --force-recreate --no-build, wait for running.
#   7. health-check real HTTP bytes (homepage, .stoverride, hashed script,
#      provider bundle) with Host header + sha256 comparison.
#   8. atomically sync the update state (data dir) only after the site is healthy.
#   9. update the asset ledger and publish state/log; run retention (best effort).
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

perform_health() {
    local release_dir="$1"
    bash "$SCRIPT_DIR/health-check.sh" --release "$release_dir"
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

rollback_transaction() {
    local reason="$1"
    local code="$2"

    log "发布事务失败：$reason"
    append_event "rollback_start" "$RELEASE_ID" "$reason"

    if [[ -n "${PREVIOUS_RELEASE:-}" && -d "$PREVIOUS_RELEASE" && -f "$PREVIOUS_RELEASE/index.html" ]]; then
        if restore_previous_release "$PREVIOUS_RELEASE"; then
            record_rollback "rollback" "$RELEASE_DIR" "$reason" 1 "$PRIOR_STATE_PREVIOUS"
            append_event "rollback_ok" "$RELEASE_ID" "$reason；current=$PREVIOUS_RELEASE"
            log "回滚完成：$CURRENT_LINK -> $PREVIOUS_RELEASE（容器已重建并通过健康检查）"
            exit "$code"
        fi
        record_rollback "rollback_failed" "$RELEASE_DIR" "$reason" 1 "$PRIOR_STATE_PREVIOUS"
        append_event "rollback_failed" "$RELEASE_ID" "$reason"
        die "$EX_CRITICAL" "回滚失败：上一版本 $PREVIOUS_RELEASE 未能恢复健康，请人工检查 $CURRENT_LINK、Compose 文件与容器日志"
    fi

    # First deploy: never leave a pointer to a release that cannot serve.
    if [[ -L "$CURRENT_LINK" ]]; then
        local link_target
        link_target="$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)"
        if [[ "$link_target" == "$RELEASE_DIR" ]]; then
            rm -f -- "$CURRENT_LINK"
            log "首次部署失败：已移除 current 指针（$CURRENT_LINK）"
        fi
    fi
    record_rollback "first_deploy_failed" "$RELEASE_DIR" "$reason" 1 ""
    append_event "first_deploy_failed" "$RELEASE_ID" "$reason；已移除 current 指针"
    log "发布包保留在 $RELEASE_DIR 便于排查；站点没有指向残缺版本"
    exit "$code"
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

    update_asset_ledger "$target"
    write_publish_state "publish-release.sh" "manual_rollback" "success" "$target" "rollback-to $target_id" \
        "" "$previous" "$((ROLLBACK_COUNT + 1))"
    append_event "manual_rollback" "$target_id" "current=$target previous=${previous:-none}"
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

RELEASE_DIR="$RELEASES_DIR/$RELEASE_ID"
[[ -e "$RELEASE_DIR" ]] && die_validation "版本目录已存在，拒绝覆盖：$RELEASE_DIR"

STAGING_OWNED=0
cleanup_staging() {
    set +e
    if ((STAGING_OWNED == 0)) && [[ -n "$STAGING" && -d "$STAGING" ]]; then
        rm -rf -- "$STAGING" 2>/dev/null || true
    fi
}
trap cleanup_staging EXIT

log "发布开始：release=$RELEASE_ID source=$SOURCE staging=$STAGING"
log "当前版本：${PREVIOUS_RELEASE:-<首次部署>}"

# ---------------------------------------------------------------------------
# Validate the staged bundle before any pointer changes.
# ---------------------------------------------------------------------------

validate_bundle_dir "$STAGING" "$EXPECTED_SUMS"
if [[ -f "$STAGING/.build-manifest.json" ]]; then
    FINGERPRINT="$(manifest_field "$STAGING/.build-manifest.json" fingerprint 2>/dev/null || true)"
fi
log "发布包校验通过：fingerprint=${FINGERPRINT:-unknown}"

# Install the new updater application atomically (same helper for timer+manual).
if [[ -n "$APP_DIR" ]]; then
    validate_app_dir "$APP_DIR"
    ensure_pointer_guard "$UPDATER_CURRENT"
    atomic_symlink "$APP_DIR" "$UPDATER_CURRENT" || die_validation "无法原子切换更新器应用指针：$UPDATER_CURRENT"
    log "更新器应用已切换：$UPDATER_CURRENT -> $APP_DIR"
fi

copy_forward_assets "$STAGING"

if ! mv "$STAGING" "$RELEASE_DIR"; then
    die_validation "无法将暂存目录原子移动到版本目录：$RELEASE_DIR"
fi
STAGING_OWNED=1
log "版本目录就绪：$RELEASE_DIR"

# ---------------------------------------------------------------------------
# Switch pointer -> recreate container -> verify real content.
# ---------------------------------------------------------------------------

ensure_pointer_guard "$CURRENT_LINK"
if ! atomic_symlink "$RELEASE_DIR" "$CURRENT_LINK"; then
    die_validation "无法原子切换 current 指针：$CURRENT_LINK"
fi
log "current 指针已切换：$CURRENT_LINK -> $RELEASE_DIR"

if ! compose_up; then
    rollback_transaction "docker compose 重建失败" "$EX_COMPOSE"
fi
if ! wait_container_running; then
    rollback_transaction "容器 $STASH_CONTAINER_NAME 未进入 running 状态" "$EX_COMPOSE"
fi
if ! perform_health "$RELEASE_DIR"; then
    rollback_transaction "健康检查失败（HTTP/sha256 内容校验未通过）" "$EX_HEALTH"
fi

# ---------------------------------------------------------------------------
# Commit bookkeeping + retention, then atomically advance the update state.
#
# Retention runs BEFORE the state sync so the next cloud build is seeded from a
# data dir that no longer contains expired content-addressed assets; otherwise
# stale files would be carried forward on every build and never expire.
# ---------------------------------------------------------------------------

update_asset_ledger "$RELEASE_DIR"

if ((RUN_RETENTION == 1)); then
    if ! bash "$SCRIPT_DIR/retention.sh" --lock-held \
        --keep-releases "$STASH_KEEP_RELEASES" --keep-asset-days "$STASH_KEEP_ASSET_DAYS"; then
        warn "保留策略运行失败（不影响本次发布，可稍后手动重跑 retention.sh）"
    fi
fi

if ((SYNC_STATE == 1)); then
    if ! sync_state "$RELEASE_DIR" "$RELEASE_ID"; then
        rollback_transaction "更新状态同步失败（旧状态已保留，可重试）" "$EX_STATE"
    fi
    log "更新状态已原子同步：$DATA_DIR"
fi

write_publish_state "publish-release.sh" "publish" "success" "$RELEASE_DIR" "" "$FINGERPRINT" \
    "$PREVIOUS_RELEASE" "$ROLLBACK_COUNT"
append_event "publish" "$RELEASE_ID" "source=$SOURCE current=$RELEASE_DIR previous=${PREVIOUS_RELEASE:-none}"

log "发布完成：release=$RELEASE_ID current=$CURRENT_LINK -> $RELEASE_DIR"
log "发布状态：$STATE_FILE；事件日志：$PUBLIC_LOG"
printf 'PUBLISH_RESULT=success\nPUBLISH_RELEASE=%s\nPUBLISH_CURRENT=%s\nPUBLISH_PREVIOUS=%s\n' \
    "$RELEASE_ID" "$RELEASE_DIR" "${PREVIOUS_RELEASE:-}"
exit "$EX_OK"
