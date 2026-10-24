#!/usr/bin/env bash
# shellcheck shell=bash
#
# Normalise the uploaded updater application before any of its scripts run:
# Windows checkouts may contain CRLF line endings, which bash/sed/systemd
# reject, and tar may not preserve executable bits on every platform.
#
# This helper is intentionally invoked as ``bash normalize-app.sh <app-dir>``
# after deploy-stash.ps1 has normalised *this file* with a one-line sed (a CRLF
# script cannot bootstrap itself). It is idempotent and safe to re-run.
#
# Exit codes: 0 ok, 10 config error.

set -Eeuo pipefail
umask 022

die_config() {
    printf '配置错误：%s\n' "$*" >&2
    exit 10
}

usage() {
    printf '用法：normalize-app.sh <updater-app-dir>\n' >&2
}

[[ $# -eq 1 ]] || { usage; die_config "需要且仅需要一个参数"; }
APP_DIR="$1"
[[ -n "$APP_DIR" ]] || die_config "应用目录不能为空"
case "$APP_DIR" in
*"
"*) die_config "应用目录不能包含换行" ;;
esac
[[ -d "$APP_DIR" ]] || die_config "应用目录不存在：$APP_DIR"

for command_name in find sed chmod; do
    command -v "$command_name" >/dev/null 2>&1 || die_config "缺少命令：$command_name"
done

# 1) CRLF -> LF for every runtime text file. -exec ... + avoids shell quoting
#    issues around find's parentheses (the previous deploy quoted them twice,
#    which broke the remote command).
find "$APP_DIR" -type f -name '*.sh' -exec sed -i 's/\r$//' {} + ||
    die_config "规范化 .sh 换行失败"
find "$APP_DIR" -type f -name '*.in' -exec sed -i 's/\r$//' {} + ||
    die_config "规范化 .in 换行失败"
find "$APP_DIR" -type f -name '*.py' -exec sed -i 's/\r$//' {} + ||
    die_config "规范化 .py 换行失败"

# 2) Required entry points must exist and be executable. validate_app_dir in
#    lib/common.sh enforces the same list inside the publish transaction.
for entry in \
    run-updater.sh \
    publish-release.sh \
    health-check.sh \
    retention.sh \
    render-config.sh \
    normalize-app.sh \
    validate.py \
    lib/common.sh; do
    [[ -f "$APP_DIR/$entry" ]] || die_config "更新器应用缺少文件：$entry"
done
chmod 0755 \
    "$APP_DIR/run-updater.sh" \
    "$APP_DIR/publish-release.sh" \
    "$APP_DIR/health-check.sh" \
    "$APP_DIR/retention.sh" \
    "$APP_DIR/render-config.sh" \
    "$APP_DIR/normalize-app.sh" ||
    die_config "设置更新器入口权限失败"
chmod 0644 "$APP_DIR/validate.py" "$APP_DIR/lib/common.sh" "$APP_DIR"/templates/*.in ||
    die_config "设置更新器数据文件权限失败"

printf '更新器应用已规范化：%s\n' "$APP_DIR"
exit 0
