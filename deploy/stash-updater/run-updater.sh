#!/usr/bin/env bash
# shellcheck shell=bash
#
# Cloud systemd timer entry point: check KeLee upstream, rebuild the Stash
# overrides, then hand the staged bundle to the SAME publish transaction that
# the manual Windows deploy uses (publish-release.sh). All pointer/rollback/
# health/retention/state logic lives in that helper; this script only builds.
#
# Configuration comes from systemd EnvironmentFile (<updater-root>/env, rendered
# by render-config.sh). No credentials are used or stored here.
#
# Exit codes: 0 ok / no update, 10 config, 20 validation/build, 75 lock busy
# (a concurrent manual deploy holds the shared lock; skipping is normal).

set -Eeuo pipefail
umask 022

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

resolve_public_config
resolve_paths
resolve_http_config
require_cmd bash flock rsync readlink sha256sum find docker python3 curl mktemp sort date

[[ -f "$SCRIPT_DIR/publish-release.sh" ]] || die_config "缺少共享发布助手：$SCRIPT_DIR/publish-release.sh"
[[ -f "$SCRIPT_DIR/health-check.sh" ]] || die_config "缺少健康检查脚本：$SCRIPT_DIR/health-check.sh"
[[ -f "$SCRIPT_DIR/retention.sh" ]] || die_config "缺少保留策略脚本：$SCRIPT_DIR/retention.sh"
[[ -f "$SCRIPT_DIR/validate.py" ]] || die_config "缺少部署侧校验脚本：$SCRIPT_DIR/validate.py"
[[ -f "$STASH_COMPOSE_FILE" ]] || die_config "找不到 Compose 文件：$STASH_COMPOSE_FILE"
[[ -f "$DATA_DIR/stash/overrides/kelee/targets.json" ]] ||
    die_config "云端更新状态尚未初始化：$DATA_DIR/stash/overrides/kelee（先运行一次手工部署）"

# Acquire the shared publish lock BEFORE the network build: a manual deploy and
# the timer must not overlap at all, and publish-release.sh later reuses the
# inherited fd 9 (same open file description, verified against the lock path).
acquire_lock

RUN_ID="$(date -u '+%Y%m%d-%H%M%S')"
WORK_DIR="$STASH_UPDATER_ROOT/work/$RUN_ID"
STAGING="$RELEASES_DIR/.staging-$RUN_ID"
SUMS_FILE="$STASH_UPDATER_ROOT/work/$RUN_ID.sums"

cleanup() {
    set +e
    rm -rf -- "$WORK_DIR" 2>/dev/null || true
    rm -f -- "$SUMS_FILE" 2>/dev/null || true
    if [[ -d "$STAGING" ]]; then
        rm -rf -- "$STAGING" 2>/dev/null || true
    fi
}
trap cleanup EXIT

log "更新检查开始：run=$RUN_ID remote_root=$STASH_REMOTE_ROOT public_base=$STASH_PUBLIC_BASE"
mkdir -p "$WORK_DIR/stash/overrides/kelee" "$WORK_DIR/scripts" "$RELEASES_DIR" || die_config "无法创建构建工作目录"

# Seed the build from the last successfully published state; a failed build
# never writes back, so retries start from the same baseline.
rsync -a --delete "$DATA_DIR/stash/overrides/kelee/" "$WORK_DIR/stash/overrides/kelee/" ||
    die "$EX_STATE" "无法复制更新状态到工作目录"
rsync -a --delete "$SCRIPT_DIR/scripts/" "$WORK_DIR/scripts/" ||
    die "$EX_STATE" "无法复制构建脚本到工作目录"

export KELEE_PUBLIC_BASE KELEE_MIRROR_BASE STASH_PUBLIC_HOST STASH_PUBLIC_BASE
export STASH_REMOTE_ROOT STASH_UPDATER_ROOT STASH_COMPOSE_FILE STASH_CONTAINER_NAME
export STASH_HTTP_PORT STASH_HTTP_HOST STASH_KEEP_RELEASES STASH_KEEP_ASSET_DAYS

BUNDLE_DIR="$WORK_DIR/stash/overrides/kelee"
BUILDER="$WORK_DIR/scripts/check_kelee_update.py"
[[ -f "$BUILDER" ]] || die_config "缺少构建入口：$BUILDER"

# The build orchestrator owns conversion/asset publishing/index generation/
# validation in one transaction. Use --out-dir when the CLI supports it so the
# build never touches the (read-only) seeded data dir.
BUILD_ARGS=()
if python3 "$BUILDER" --help 2>/dev/null | grep -q -- '--out-dir'; then
    BUILD_ARGS+=(--out-dir "$BUNDLE_DIR")
else
    log "构建入口不支持 --out-dir，使用其默认输出目录（$BUNDLE_DIR）"
fi

log "检查 KeLee 上游并构建 Stash 产物"
set +e
(cd "$WORK_DIR" && python3 "$BUILDER" "${BUILD_ARGS[@]}")
build_status=$?
set -e

case "$build_status" in
0)
    log "上游无更新（构建退出码 0），本次不发布"
    exit "$EX_OK"
    ;;
2)
    log "构建完成（退出码 2），开始部署侧校验"
    ;;
*)
    log "构建失败（退出码 $build_status），保留旧状态等待下次重试"
    exit 20
    ;;
esac

[[ -d "$BUNDLE_DIR" ]] || die_validation "构建成功但输出目录不存在：$BUNDLE_DIR"

# Deploy-side validation: structural + unified public address + no placeholder
# leakage + hashed asset references. (The build orchestrator already ran the
# full Stash schema validation before writing the manifest.)
if ! python3 "$SCRIPT_DIR/validate.py" "$BUNDLE_DIR" "$STASH_PUBLIC_HOST"; then
    die_validation "部署侧校验失败，阻断发布（云端状态未改动，可重试）"
fi

mkdir -p "$STAGING" || die "$EX_STATE" "无法创建暂存目录：$STAGING"
rsync -a --delete "$BUNDLE_DIR/" "$STAGING/" || die "$EX_STATE" "无法复制构建产物到暂存目录"
if ! generate_sums_for_dir "$STAGING" "$SUMS_FILE"; then
    die "$EX_STATE" "无法生成发布包期望哈希清单"
fi

log "移交共享发布事务：staging=$STAGING"
set +e
bash "$SCRIPT_DIR/publish-release.sh" \
    --release-id "$RUN_ID" \
    --staging "$STAGING" \
    --expected-sums "$SUMS_FILE" \
    --source cloud
publish_status=$?
set -e

case "$publish_status" in
0)
    log "更新并发布完成：run=$RUN_ID"
    exit "$EX_OK"
    ;;
"$EX_LOCKED")
    log "另一个发布任务正在运行，本次跳过（锁释放后下次 timer 会重试）"
    exit "$EX_OK"
    ;;
*)
    log "发布失败（退出码 $publish_status），云端更新状态保持旧版本以便重试"
    exit "$publish_status"
    ;;
esac
