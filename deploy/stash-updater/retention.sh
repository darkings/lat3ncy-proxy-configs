#!/usr/bin/env bash
# shellcheck shell=bash
#
# Retention for published KeLee Stash releases and content-addressed assets.
#
# Rules (manifest/ledger driven, never time-of-day guessing):
#   * Keep the newest STASH_KEEP_RELEASES releases (default 3), plus the release
#     that `current` points at and the previous rollback release from publish
#     state, even if they fall outside the newest window.
#   * Keep the newest STASH_KEEP_RELEASES updater application releases plus the
#     one referenced by updater/current.
#   * Releases are self-contained, but old installed overrides keep requesting
#     `scripts/<sha12>-<slug>.js`. Hashed scripts referenced by the current
#     release are always kept; unreferenced ones are kept for
#     STASH_KEEP_ASSET_DAYS (default 30) after they were last published, then
#     removed together with their ledger row.
#   * Remove stale `.staging-*` / `.sync-*` / `work/*` leftovers from crashed runs
#     older than 6 hours.
#
# Exit codes: 0 = done (warnings allowed), 10 = configuration error.

set -Eeuo pipefail
umask 022

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
    cat <<'EOF'
用法：retention.sh [--keep-releases N] [--keep-asset-days D] [--lock-held] [--dry-run]

环境变量：
  STASH_REMOTE_ROOT / STASH_UPDATER_ROOT / STASH_KEEP_RELEASES / STASH_KEEP_ASSET_DAYS
  STASH_LOCK_HELD=1 表示调用方已持有发布锁（publish-release.sh 内部调用时使用）。
EOF
}

KEEP_RELEASES="${STASH_KEEP_RELEASES:-3}"
KEEP_ASSET_DAYS="${STASH_KEEP_ASSET_DAYS:-30}"
LOCK_HELD="${STASH_LOCK_HELD:-0}"
DRY_RUN=0

while (($#)); do
    case "$1" in
    --keep-releases)
        [[ $# -ge 2 ]] || die_config "--keep-releases 缺少参数"
        KEEP_RELEASES="$2"
        shift 2
        ;;
    --keep-asset-days)
        [[ $# -ge 2 ]] || die_config "--keep-asset-days 缺少参数"
        KEEP_ASSET_DAYS="$2"
        shift 2
        ;;
    --lock-held)
        LOCK_HELD=1
        shift
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

resolve_paths
validate_uint "$KEEP_RELEASES" "STASH_KEEP_RELEASES" 1 100
validate_uint "$KEEP_ASSET_DAYS" "STASH_KEEP_ASSET_DAYS" 1 3650
require_cmd find sort sed awk stat rm mv readlink date

if [[ "$LOCK_HELD" != "1" ]]; then
    acquire_lock
fi

NOW=$(now_epoch)
ASSET_CUTOFF=$((NOW - KEEP_ASSET_DAYS * 86400))
STALE_CUTOFF_MINUTES=360

safe_remove() {
    local path="$1"
    [[ -n "$path" && "$path" != "/" ]] || die_config "拒绝删除危险路径：$path"
    [[ "$path" != *"//"* ]] || die_config "拒绝删除含连续斜杠的路径：$path"
    case "$path" in
    "$RELEASES_DIR"/* | "$UPDATER_RELEASES_DIR"/* | "$DATA_DIR"/* | "$STASH_UPDATER_ROOT/work"/*) ;;
    *) die_config "拒绝删除发布目录之外的路径：$path" ;;
    esac
    if ((DRY_RUN == 1)); then
        log "[dry-run] 将删除：$path"
    else
        rm -rf -- "$path"
    fi
}

# Returns 1 when the path must be protected.
is_protected_release() {
    local path="$1"
    [[ -n "${PROTECTED_RELEASES["$path"]:-}" ]]
}

declare -A PROTECTED_RELEASES=()
CURRENT_RELEASE="$(current_release_path || true)"
if [[ -n "$CURRENT_RELEASE" ]]; then
    PROTECTED_RELEASES["$CURRENT_RELEASE"]=1
fi
STATE_CURRENT="$(state_read CURRENT_RELEASE || true)"
if [[ -n "$STATE_CURRENT" && -d "$STATE_CURRENT" ]]; then
    PROTECTED_RELEASES["$STATE_CURRENT"]=1
fi
STATE_PREVIOUS="$(state_read PREVIOUS_RELEASE || true)"
if [[ -n "$STATE_PREVIOUS" && -d "$STATE_PREVIOUS" ]]; then
    PROTECTED_RELEASES["$STATE_PREVIOUS"]=1
fi

removed_releases=0
kept_releases=0
if [[ -d "$RELEASES_DIR" ]]; then
    mapfile -t release_dirs < <(
        find "$RELEASES_DIR" -maxdepth 1 -mindepth 1 -type d -printf '%f\n' 2>/dev/null |
            grep -E '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$' | sort -r || true
    )
    index=0
    for release_name in "${release_dirs[@]}"; do
        [[ -n "$release_name" ]] || continue
        release_path="$RELEASES_DIR/$release_name"
        index=$((index + 1))
        if ((index <= KEEP_RELEASES)) || is_protected_release "$release_path"; then
            kept_releases=$((kept_releases + 1))
            continue
        fi
        log "保留策略：删除旧版本 $release_name（保留最新 $KEEP_RELEASES 个 + current/previous）"
        safe_remove "$release_path"
        removed_releases=$((removed_releases + 1))
    done
fi

# Updater application releases: protect the one behind updater/current.
declare -A PROTECTED_APP_RELEASES=()
if [[ -L "$UPDATER_CURRENT" ]]; then
    APP_TARGET="$(readlink -f "$UPDATER_CURRENT" 2>/dev/null || true)"
    if [[ -n "$APP_TARGET" ]]; then
        PROTECTED_APP_RELEASES["$(dirname "$APP_TARGET")"]=1
    fi
fi
removed_apps=0
if [[ -d "$UPDATER_RELEASES_DIR" ]]; then
    mapfile -t app_release_dirs < <(
        find "$UPDATER_RELEASES_DIR" -maxdepth 1 -mindepth 1 -type d -printf '%f\n' 2>/dev/null |
            grep -E '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$' | sort -r || true
    )
    index=0
    for app_name in "${app_release_dirs[@]}"; do
        [[ -n "$app_name" ]] || continue
        app_path="$UPDATER_RELEASES_DIR/$app_name"
        index=$((index + 1))
        if ((index <= KEEP_RELEASES)) || [[ -n "${PROTECTED_APP_RELEASES["$app_path"]:-}" ]]; then
            continue
        fi
        log "保留策略：删除旧更新器应用 $app_name"
        safe_remove "$app_path"
        removed_apps=$((removed_apps + 1))
    done
fi

# Stale staging / sync / work leftovers.
stale_count=0
cleanup_stale_matches() {
    local root="$1"
    local pattern="$2"
    [[ -d "$root" ]] || return 0
    local path
    while IFS= read -r path; do
        [[ -n "$path" ]] || continue
        log "保留策略：清理陈旧临时目录 $path"
        safe_remove "$path"
        stale_count=$((stale_count + 1))
    done < <(find "$root" -maxdepth 1 -mindepth 1 -name "$pattern" -mmin "+$STALE_CUTOFF_MINUTES" 2>/dev/null | sort || true)
}
cleanup_stale_matches "$RELEASES_DIR" '.staging-*'
cleanup_stale_matches "$DATA_DIR" '.sync-*'
cleanup_stale_matches "$DATA_DIR" '.old-*'
cleanup_stale_matches "$STASH_UPDATER_ROOT/work" '*'

# Prune expired content-addressed assets from the served release and the ledger.
removed_assets=0
if [[ -n "$CURRENT_RELEASE" && -d "$CURRENT_RELEASE/scripts" ]]; then
    declare -A REFERENCED=()
    while IFS= read -r ref; do
        [[ -n "$ref" ]] || continue
        REFERENCED["$ref"]=1
    done < <(bundle_asset_references "$CURRENT_RELEASE")

    declare -A LEDGER_LAST_SEEN=()
    declare -A LEDGER_SHA=()
    declare -A LEDGER_FIRST_SEEN=()
    if [[ -f "$LEDGER_FILE" ]]; then
        while IFS=$'\t' read -r ledger_name ledger_sha ledger_last ledger_first; do
            [[ -n "$ledger_name" ]] || continue
            LEDGER_LAST_SEEN["$ledger_name"]="$ledger_last"
            LEDGER_SHA["$ledger_name"]="$ledger_sha"
            LEDGER_FIRST_SEEN["$ledger_name"]="$ledger_first"
        done <"$LEDGER_FILE"
    fi

    while IFS= read -r asset_file; do
        [[ -n "$asset_file" ]] || continue
        asset_name="scripts/$(basename "$asset_file")"
        asset_is_hashed "$asset_name" || continue
        [[ -n "${REFERENCED["$asset_name"]:-}" ]] && continue
        last_seen="${LEDGER_LAST_SEEN["$asset_name"]:-}"
        if [[ ! "$last_seen" =~ ^[0-9]+$ ]]; then
            last_seen="$(stat -c '%Y' "$asset_file" 2>/dev/null || echo "$NOW")"
        fi
        ((last_seen >= ASSET_CUTOFF)) && continue
        log "保留策略：删除过期内容寻址脚本 $asset_name（最后发布于 $(date -u -d "@$last_seen" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || echo "$last_seen")）"
        if ((DRY_RUN == 1)); then
            log "[dry-run] 将删除：$asset_file"
        else
            rm -f -- "$asset_file"
        fi
        removed_assets=$((removed_assets + 1))
    done < <(find "$CURRENT_RELEASE/scripts" -maxdepth 1 -type f \( -name '*.js' -o -name '*.jq' \) | sort)

    # Rewrite the ledger: keep assets still present or still inside the window.
    if [[ -f "$LEDGER_FILE" ]]; then
        ledger_tmp="$STATE_DIR/.ledger.$$"
        : >"$ledger_tmp" || die "$EX_STATE" "无法写入资产账本：$ledger_tmp"
        while IFS=$'\t' read -r ledger_name ledger_sha ledger_last ledger_first; do
            [[ -n "$ledger_name" ]] || continue
            asset_is_hashed "$ledger_name" || continue
            if [[ -f "$CURRENT_RELEASE/$ledger_name" ]]; then
                if ((DRY_RUN == 1)); then
                    printf '%s\t%s\t%s\t%s\n' "$ledger_name" "$ledger_sha" "$ledger_last" "$ledger_first" >>"$ledger_tmp"
                    continue
                fi
                printf '%s\t%s\t%s\t%s\n' "$ledger_name" "$(sha256_of "$CURRENT_RELEASE/$ledger_name")" "$ledger_last" "$ledger_first" >>"$ledger_tmp"
            elif [[ "$ledger_last" =~ ^[0-9]+$ ]] && ((ledger_last >= ASSET_CUTOFF)); then
                printf '%s\t%s\t%s\t%s\n' "$ledger_name" "$ledger_sha" "$ledger_last" "$ledger_first" >>"$ledger_tmp"
            fi
        done <"$LEDGER_FILE"
        if ((DRY_RUN == 0)); then
            sort -t$'\t' -k1,1 "$ledger_tmp" -o "$ledger_tmp" 2>/dev/null || sort "$ledger_tmp" -o "$ledger_tmp"
            mv -f "$ledger_tmp" "$LEDGER_FILE" || die "$EX_STATE" "无法提交资产账本：$LEDGER_FILE"
        else
            rm -f "$ledger_tmp"
        fi
    fi
fi

log "保留策略完成：releases 保留 $kept_releases/删除 $removed_releases；更新器应用删除 $removed_apps；过期脚本删除 $removed_assets；临时目录清理 $stale_count（keep_releases=$KEEP_RELEASES keep_asset_days=$KEEP_ASSET_DAYS）"
exit "$EX_OK"
