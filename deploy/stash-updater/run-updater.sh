#!/usr/bin/env bash
set -Eeuo pipefail

# 云端定时执行：检查 KeLee 上游、转换为 Stash、验证后再发布。
# 本文件不包含任何认证信息；上游资源使用公开 URL，站点地址来自 EnvironmentFile。

umask 022

REMOTE_ROOT="${STASH_REMOTE_ROOT:-/opt/stash}"
UPDATER_ROOT="${STASH_UPDATER_ROOT:-$REMOTE_ROOT/updater}"
DATA_ROOT="$UPDATER_ROOT/data"
APP_ROOT="$(dirname "$(readlink -f "$0")")"
WORK_ROOT="$UPDATER_ROOT/work"
LOCK_PATH="$UPDATER_ROOT/update.lock"
PUBLIC_HOST="${STASH_PUBLIC_HOST:-stash.ponyo.fun}"
COMPOSE_FILE="$REMOTE_ROOT/docker-compose.yml"

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S%z')" "$*"
}

for command_name in python3 rsync flock readlink sha256sum find docker; do
    command -v "$command_name" >/dev/null 2>&1 || {
        log "缺少云端命令：$command_name"
        exit 1
    }
done

[[ -f "$COMPOSE_FILE" ]] || { log "找不到 Compose 文件：$COMPOSE_FILE"; exit 1; }
[[ -f "$DATA_ROOT/stash/overrides/kelee/targets.json" ]] || {
    log "云端更新数据尚未初始化：$DATA_ROOT/stash/overrides/kelee"
    exit 1
}

mkdir -p "$WORK_ROOT"
exec 9>"$LOCK_PATH"
if ! flock -n 9; then
    log "已有更新任务运行，本次跳过"
    exit 0
fi

run_id="$(date -u '+%Y%m%d-%H%M%S')"
work_dir="$WORK_ROOT/$run_id"
staging_release="$REMOTE_ROOT/releases/.staging-$run_id"
release_dir="$REMOTE_ROOT/releases/$run_id"

cleanup() {
    rm -rf -- "$work_dir" "$staging_release"
}
trap cleanup EXIT

mkdir -p "$work_dir/scripts" "$work_dir/stash/overrides/kelee"
rsync -a --delete "$DATA_ROOT/stash/overrides/kelee/" "$work_dir/stash/overrides/kelee/"
rsync -a --delete "$APP_ROOT/scripts/" "$work_dir/scripts/"

export KELEE_PUBLIC_BASE="https://$PUBLIC_HOST"
export KELEE_MIRROR_BASE="https://$PUBLIC_HOST"

log "检查 KeLee 上游并转换"
set +e
python3 "$work_dir/scripts/check_kelee_update.py"
check_status=$?
set -e

if [[ "$check_status" -eq 0 ]]; then
    log "上游无更新"
    exit 0
fi
if [[ "$check_status" -ne 2 ]]; then
    log "更新检查失败，停止发布（退出码 $check_status）"
    exit "$check_status"
fi

# check_kelee_update.py 兼容 GitHub Actions，生成页面时可能使用默认 Raw 地址；
# 云端发布前强制用自托管地址重新生成一次。
python3 "$work_dir/scripts/generate_kelee_html.py"
# 先跑离线 Stash 校验（基于 Loon vs Stash 差异分析，不依赖 Stash 运行时），失败则阻断发布
python3 "$work_dir/scripts/validate_stash.py"
python3 "$APP_ROOT/validate.py" "$work_dir/stash/overrides/kelee" "$PUBLIC_HOST"

if [[ -e "$REMOTE_ROOT/current" && ! -L "$REMOTE_ROOT/current" ]]; then
    log "拒绝覆盖普通目录：$REMOTE_ROOT/current"
    exit 1
fi

mkdir -p "$staging_release"
rsync -a --delete "$work_dir/stash/overrides/kelee/" "$staging_release/"
mv "$staging_release" "$release_dir"
ln -sfn "$release_dir" "$REMOTE_ROOT/current"

docker compose -f "$COMPOSE_FILE" up -d --force-recreate --no-build

test -f "$REMOTE_ROOT/current/index.html"
expected_count="$(find "$work_dir/stash/overrides/kelee" -type f | wc -l | tr -d ' ')"
actual_count="$(find -L "$REMOTE_ROOT/current" -type f | wc -l | tr -d ' ')"
[[ "$expected_count" = "$actual_count" ]] || {
    log "发布后文件数不一致：预期 $expected_count，实际 $actual_count"
    exit 1
}
expected_hash="$(sha256sum "$work_dir/stash/overrides/kelee/index.html" | awk '{print $1}')"
actual_hash="$(sha256sum "$REMOTE_ROOT/current/index.html" | awk '{print $1}')"
[[ "$expected_hash" = "$actual_hash" ]] || {
    log "发布后 index.html 哈希不一致"
    exit 1
}
[[ "$(docker inspect --format '{{.State.Status}}' stash)" = "running" ]] || {
    log "stash 容器未处于 running"
    exit 1
}

# 更新检查状态是云端的权威输入；发布成功后再写回，失败时保留旧状态便于重试。
rsync -a --delete "$work_dir/stash/overrides/kelee/" "$DATA_ROOT/stash/overrides/kelee/"
log "更新并发布完成：$release_dir（$actual_count 个文件，$actual_hash）"
