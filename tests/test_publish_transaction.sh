#!/usr/bin/env bash
# shellcheck shell=bash
#
# Offline transaction tests for deploy/stash-updater/publish-release.sh.
#
# The real publish transaction (flock, rsync, sha256sum, atomic symlink moves,
# copy-forward, rollback) runs against a throw-away REMOTE_ROOT. Only external
# side effects are faked, through PATH wrappers that log their invocations:
#   * docker      -> compose/up/inspect, configurable failures (never a real daemon)
#   * curl        -> serves files from a directory named by the test (never a network)
#   * systemctl   -> logs only (this helper does not manage units, the deploy does)
# No SSH, no real server, no network. Run with: bash tests/test_publish_transaction.sh
#
# Scenarios:
#   1  first deploy success (symlink, state, data sync)
#   2  second deploy success + copy-forward of content-addressed assets
#   3  health-check failure -> automatic rollback to previous release
#   4  compose failure -> automatic rollback
#   5  concurrent deploy blocked by the shared flock (exit 75) and retry works
#   6  update-state sync failure -> rollback, old state preserved, retry succeeds
#   7  retention keeps current/previous/newest N and referenced assets, prunes expired ones
#   8  first deploy with failing health leaves no broken `current` pointer
#   9  manual --rollback-to goes through the same transaction
#   10 deploy-side validate.py accepts a good bundle and rejects bad ones
#   11 render-config.sh renders env/systemd/Caddyfile with no placeholders left
#   12 the inherited publish lock is reusable by the helper spawned by run-updater.sh
#   13 run-updater.sh builds then publishes through the SAME helper (cloud timer path)
#   14 health-check verifies the Cache-Control policy of hashed/legacy scripts

set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPDATER="$REPO_ROOT/deploy/stash-updater"
PUBLISH="$UPDATER/publish-release.sh"
RETENTION="$UPDATER/retention.sh"

[[ -f "$PUBLISH" ]] || { echo "missing publisher: $PUBLISH" >&2; exit 1; }
[[ -f "$RETENTION" ]] || { echo "missing retention: $RETENTION" >&2; exit 1; }

TEST_ROOT="$(mktemp -d /tmp/kelee-publish-test.XXXXXX)"
FAKE_BIN="$TEST_ROOT/fake-bin"
REAL_PYTHON3="$(command -v python3 || true)"
[[ -n "$REAL_PYTHON3" ]] || { echo "python3 is required for these tests" >&2; exit 1; }
CALLS_LOG="$TEST_ROOT/calls.log"
DOCKER_UP_FAIL_FILE="$TEST_ROOT/docker-up-fail-times"
DOCKER_STATUS_FILE="$TEST_ROOT/docker-status"
HTTP_STATE_FILE="$TEST_ROOT/http-source"
RUN_LOG="$TEST_ROOT/last-run.log"
LOCK_HELD_MARK="$TEST_ROOT/lock-held"

FAILURES=0
CHECKS=0
PASS_COUNT=0

cleanup() {
    set +e
    if [[ -n "${HOLDER_PID:-}" ]]; then
        kill "$HOLDER_PID" 2>/dev/null || true
        wait "$HOLDER_PID" 2>/dev/null || true
    fi
    if [[ "${KEEP_TEST_ROOT:-0}" == "1" ]]; then
        printf '保留测试目录：%s\n' "$TEST_ROOT" >&2
        return 0
    fi
    rm -rf -- "$TEST_ROOT"
}
trap cleanup EXIT

printf '测试临时目录：%s\n' "$TEST_ROOT"

pass() {
    CHECKS=$((CHECKS + 1))
    PASS_COUNT=$((PASS_COUNT + 1))
    printf 'PASS: %s\n' "$*"
}
fail() {
    CHECKS=$((CHECKS + 1))
    FAILURES=$((FAILURES + 1))
    printf 'FAIL: %s\n' "$*" >&2
    if [[ -f "$RUN_LOG" ]]; then
        printf -- '--- last publish log ---\n' >&2
        sed -n '1,120p' "$RUN_LOG" >&2 || true
        printf -- '--- end log ---\n' >&2
    fi
}

expect_eq() {
    local desc="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
        pass "$desc"
    else
        fail "$desc（期望：$expected，实际：$actual）"
    fi
}

expect_rc() {
    local desc="$1" expected="$2"
    expect_eq "$desc" "$expected" "${LAST_RC:-missing}"
}

expect_file() {
    local desc="$1" path="$2"
    if [[ -f "$path" ]]; then pass "$desc"; else fail "$desc（缺少文件：$path）"; fi
}

expect_no_file() {
    local desc="$1" path="$2"
    if [[ ! -e "$path" ]]; then pass "$desc"; else fail "$desc（不应存在：$path）"; fi
}

expect_link() {
    local desc="$1" link="$2" target="$3"
    local actual=""
    actual="$(readlink -f "$link" 2>/dev/null || true)"
    expect_eq "$desc" "$(readlink -f "$target")" "$actual"
}

expect_link_absent() {
    local desc="$1" link="$2"
    if [[ ! -e "$link" && ! -L "$link" ]]; then pass "$desc"; else fail "$desc（指针仍然存在：$link）"; fi
}

expect_contains() {
    local desc="$1" file="$2" needle="$3"
    if [[ -f "$file" ]] && grep -qF -- "$needle" "$file"; then
        pass "$desc"
    else
        fail "$desc（$file 中未找到：$needle）"
    fi
}

state_get() {
    local key="$1"
    [[ -f "$TEST_ROOT/remote/updater/state/publish.env" ]] || return 0
    sed -n "s/^${key}=//p" "$TEST_ROOT/remote/updater/state/publish.env" | tail -n 1
}

# ---------------------------------------------------------------------------
# Fake external commands (PATH wrappers). They only log and read test state.
# ---------------------------------------------------------------------------

install_fake_tools() {
    mkdir -p "$FAKE_BIN"

    cat >"$FAKE_BIN/docker" <<'SH'
#!/usr/bin/env bash
set -u
printf 'docker %s\n' "$*" >>"${FAKE_CALLS_LOG:-/dev/null}"
if [[ "${1:-}" == "compose" ]]; then
    for arg in "$@"; do
        if [[ "$arg" == "up" ]]; then
            fails=0
            if [[ -f "${FAKE_DOCKER_UP_FAIL_FILE:-/dev/null}" ]]; then
                fails="$(cat "$FAKE_DOCKER_UP_FAIL_FILE")"
            fi
            if [[ "$fails" =~ ^[0-9]+$ ]] && ((fails > 0)); then
                echo "$((fails - 1))" >"$FAKE_DOCKER_UP_FAIL_FILE"
                echo 'fake: docker compose up failed' >&2
                exit 1
            fi
            if [[ -n "${FAKE_DOCKER_FAIL_AFTER:-}" ]]; then
                counter_file="${FAKE_DOCKER_COUNTER_FILE:-/dev/null}"
                count=0
                [[ -f "$counter_file" ]] && count="$(cat "$counter_file")"
                [[ "$count" =~ ^[0-9]+$ ]] || count=0
                count=$((count + 1))
                echo "$count" >"$counter_file"
                if ((count > FAKE_DOCKER_FAIL_AFTER)); then
                    echo 'fake: docker compose up failed after limit' >&2
                    exit 1
                fi
            fi
            if [[ -n "${FAKE_DOCKER_AFTER_UP_HOOK:-}" && -f "$FAKE_DOCKER_AFTER_UP_HOOK" ]]; then
                bash "$FAKE_DOCKER_AFTER_UP_HOOK" || true
            fi
            exit 0
        fi
    done
    exit 0
fi
if [[ "${1:-}" == "inspect" ]]; then
    cat "${FAKE_DOCKER_STATUS_FILE:-/dev/null}" 2>/dev/null || echo running
    exit 0
fi
exit 0
SH

    cat >"$FAKE_BIN/curl" <<'SH'
#!/usr/bin/env bash
set -u
printf 'curl %s\n' "$*" >>"${FAKE_CALLS_LOG:-/dev/null}"
out=""
hdr=""
url=""
req_host=""
while (($#)); do
    case "$1" in
        --output|-o) out="$2"; shift 2 ;;
        --dump-header|-D) hdr="$2"; shift 2 ;;
        --header|-H)
            if [[ "${2:-}" == Host:* ]]; then req_host="${2#Host: }"; fi
            shift 2
            ;;
        --write-out|-w|--max-time|-m) shift 2 ;;
        --silent|--show-error|-s|-S) shift ;;
        http://*|https://*) url="$1"; shift ;;
        *) shift ;;
    esac
done
if [[ -z "$url" ]]; then
    echo 'fake curl: no url' >&2
    exit 2
fi
rest="${url#*://}"
path="/${rest#*/}"
if [[ -n "${FAKE_CURL_TOUCH_FILE:-}" && "$path" == "/" ]]; then
    : >"$FAKE_CURL_TOUCH_FILE"
fi
if [[ "$path" == "/" ]]; then
    path="/index.html"
fi
root=""
if [[ -f "${FAKE_HTTP_STATE_FILE:-/dev/null}" ]]; then
    root="$(cat "$FAKE_HTTP_STATE_FILE")"
fi
file="$root$path"
status="404"
cache_header=""
if [[ -f "$file" ]]; then
    status="200"
    if [[ "$path" =~ ^/scripts/[0-9a-f]{12}-[A-Za-z0-9._-]+\.(js|jq)$ ]]; then
        cache_header="public, max-age=31536000, immutable"
    elif [[ "$path" =~ ^/scripts/[^/]+\.js$ ]]; then
        cache_header="no-cache, must-revalidate"
    fi
fi
cache_mode="${FAKE_CURL_CACHE_MODE:-}"
if [[ -n "${FAKE_CURL_CACHE_MODE_REQUIRE_FILE:-}" ]]; then
    if [[ -f "$FAKE_CURL_CACHE_MODE_REQUIRE_FILE" ]]; then
        cache_mode=""
    else
        cache_mode="bad"
    fi
fi
if [[ "$cache_mode" == "bad" && -n "$cache_header" ]]; then
    if [[ "$cache_header" == *immutable* ]]; then
        cache_header="no-cache, must-revalidate"
    else
        cache_header="public, max-age=31536000, immutable"
    fi
fi
caddyfile_for_host="${FAKE_CURL_LOADED_CADDYFILE:-${FAKE_CURL_CADDYFILE:-}}"
if [[ -n "${FAKE_CURL_LOADED_MARKER:-}" && ! -f "$FAKE_CURL_LOADED_MARKER" ]]; then
    status="404"
fi
if [[ -n "$caddyfile_for_host" && -f "$caddyfile_for_host" ]]; then
    required_host="$(sed -n 's/^[[:space:]]*@stash host \([^[:space:]]*\).*/\1/p' "$caddyfile_for_host" | head -n 1)"
    if [[ -n "$required_host" && "$req_host" != "$required_host" ]]; then
        status="404"
    fi
fi
if [[ -n "$hdr" ]]; then
    {
        if [[ "$status" == "200" ]]; then printf 'HTTP/1.1 200 OK\r\n'; else printf 'HTTP/1.1 404 Not Found\r\n'; fi
        [[ -n "$cache_header" ]] && printf 'Cache-Control: %s\r\n' "$cache_header"
        printf '\r\n'
    } >"$hdr"
fi
if [[ "$status" == "200" ]]; then
    if [[ -n "$out" ]]; then
        cat "$file" >"$out"
    fi
    printf '%s' '200'
    exit 0
fi
if [[ -n "$out" ]]; then
    : >"$out"
fi
printf '%s' '404'
exit 0
SH

    cat >"$FAKE_BIN/systemctl" <<'SH'
#!/usr/bin/env bash
set -u
printf 'systemctl %s\n' "$*" >>"${FAKE_CALLS_LOG:-/dev/null}"
state_dir="${FAKE_SYSTEMCTL_STATE:-/tmp/fake-systemctl-state}"
mkdir -p "$state_dir" 2>/dev/null || true
enabled_file="$state_dir/enabled"
active_file="$state_dir/active"
command="${1:-}"
shift || true

fail_marker() {
    [[ -n "${1:-}" && -f "${1}" ]]
}

post_mutation_query_fail() {
    local marker="${FAKE_SYSTEMCTL_POST_MUTATION_QUERY_FAIL:-}"
    if [[ -n "$marker" && -f "$marker" ]]; then
        echo 'fake: systemctl query failed after a recovery mutation' >&2
        exit 4
    fi
}

mark_post_mutation() {
    local marker="${FAKE_SYSTEMCTL_POST_MUTATION_QUERY_FAIL:-}"
    [[ -n "$marker" ]] && : >"$marker"
    return 0
}

case "$command" in
is-enabled)
    post_mutation_query_fail
    if [[ -n "${FAKE_SYSTEMCTL_ENABLED_FAIL_ONCE:-}" && -f "$FAKE_SYSTEMCTL_ENABLED_FAIL_ONCE" ]]; then
        rm -f "$FAKE_SYSTEMCTL_ENABLED_FAIL_ONCE"
        echo 'fake: first is-enabled query failed (empty stdout)' >&2
        exit 1
    fi
    if [[ -n "${FAKE_SYSTEMCTL_ENABLED_TEXT+x}" ]]; then
        printf '%s' "$FAKE_SYSTEMCTL_ENABLED_TEXT"
        exit "${FAKE_SYSTEMCTL_ENABLED_RC:-0}"
    fi
    if [[ "${FAKE_SYSTEMCTL_UNKNOWN_STATE:-0}" == "1" ]]; then
        echo unknown
        exit 4
    fi
    if [[ -f "$enabled_file" ]]; then
        echo enabled
        exit 0
    fi
    echo disabled
    exit 1
    ;;
is-active)
    post_mutation_query_fail
    if [[ -n "${FAKE_SYSTEMCTL_ACTIVE_FAIL_ONCE:-}" && -f "$FAKE_SYSTEMCTL_ACTIVE_FAIL_ONCE" ]]; then
        rm -f "$FAKE_SYSTEMCTL_ACTIVE_FAIL_ONCE"
        echo 'fake: first is-active query failed (empty stdout)' >&2
        exit 1
    fi
    if [[ -n "${FAKE_SYSTEMCTL_ACTIVE_TEXT+x}" ]]; then
        printf '%s' "$FAKE_SYSTEMCTL_ACTIVE_TEXT"
        exit "${FAKE_SYSTEMCTL_ACTIVE_RC:-0}"
    fi
    if [[ "${FAKE_SYSTEMCTL_UNKNOWN_STATE:-0}" == "1" ]]; then
        echo unknown
        exit 4
    fi
    if [[ -f "$active_file" ]]; then
        echo active
        exit 0
    fi
    echo inactive
    exit 3
    ;;
enable)
    mark_post_mutation
    if fail_marker "${FAKE_SYSTEMCTL_ENABLE_FAIL:-}"; then
        echo 'fake: enable failed' >&2
        exit 1
    fi
    : >"$enabled_file"
    case " $* " in *" --now "*) : >"$active_file" ;; esac
    exit 0
    ;;
disable)
    mark_post_mutation
    if fail_marker "${FAKE_SYSTEMCTL_DISABLE_FAIL:-}"; then
        echo 'fake: disable failed' >&2
        exit 1
    fi
    rm -f "$enabled_file"
    case " $* " in *" --now "*) rm -f "$active_file" ;; esac
    exit 0
    ;;
start)
    mark_post_mutation
    if fail_marker "${FAKE_SYSTEMCTL_START_FAIL:-}"; then
        echo 'fake: start failed' >&2
        exit 1
    fi
    : >"$active_file"
    exit 0
    ;;
stop)
    mark_post_mutation
    if fail_marker "${FAKE_SYSTEMCTL_STOP_FAIL:-}"; then
        echo 'fake: stop failed' >&2
        exit 1
    fi
    rm -f "$active_file"
    exit 0
    ;;
daemon-reload)
    if fail_marker "${FAKE_SYSTEMCTL_DAEMON_RELOAD_FAIL:-}"; then
        echo 'fake: daemon-reload failed' >&2
        exit 1
    fi
    exit 0
    ;;
esac
exit 0
SH

    # python3 wrapper: emulate the build orchestrator, otherwise delegate to the
    # real interpreter (manifest parsing / deploy-side validate.py must be real).
    cat >"$FAKE_BIN/python3" <<'SH'
#!/usr/bin/env bash
set -u
REAL_PYTHON3="@REAL_PYTHON3@"
is_builder=0
want_help=0
out=""
prev=""
for arg in "$@"; do
    case "$arg" in
        *check_kelee_update.py) is_builder=1 ;;
        --help) want_help=1 ;;
    esac
    if [[ "$prev" == "--out-dir" ]]; then out="$arg"; fi
    prev="$arg"
done
if ((is_builder == 1)) && ((want_help == 1)); then
    printf '%s\n' '--out-dir DIR' '--force'
    exit 0
fi
if ((is_builder == 1)); then
    mode="${FAKE_RUNUPD_BUILD:-ok}"
    if [[ "$mode" == "fail" ]]; then
        echo 'fake builder: requested failure' >&2
        exit 1
    fi
    if [[ -z "$out" ]]; then out="$PWD/stash/overrides/kelee"; fi
    OUT="$out" "$REAL_PYTHON3" - <<'PY'
import hashlib
import json
import os
import pathlib

out = pathlib.Path(os.environ["OUT"])
base = os.environ.get("KELEE_PUBLIC_BASE", "")
if not base:
    raise SystemExit(3)
(out / "scripts").mkdir(parents=True, exist_ok=True)
outputs = {}


def write(rel, text):
    path = out / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    outputs[rel] = {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size": path.stat().st_size,
        "counts": {},
    }


write("index.html", f'<!DOCTYPE html><html><body>KeLee runupdater <a href="{base}/A_Test.stoverride">A</a></body></html>\n')
write("targets.json", json.dumps([{"filename": "A_Test.lpx", "name": "A_Test"}], ensure_ascii=False) + "\n")
write("A_Test.stoverride", f"name: A_Test\ndesc: runupdater fake\nurl: {base}/scripts/0123456789ab-test.js\n")
write(
    "kelee-scripts.stoverride",
    "name: kelee-scripts\nscript-providers:\n  test:\n"
    f"    url: {base}/scripts/0123456789ab-test.js\n    interval: 86400\n",
)
write("scripts/0123456789ab-test.js", 'console.log("runupdater");\n')
manifest = {
    "schema": 2,
    "public_base": base,
    "mirror_base": base,
    "fingerprint": "runupdater-fake",
    "outputs": outputs,
    "resources": {},
    "errors": [],
    "warnings": [],
}
(out / ".build-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
PY
    exit 2
fi
exec "$REAL_PYTHON3" "$@"
SH
    sed -i "s|@REAL_PYTHON3@|$REAL_PYTHON3|" "$FAKE_BIN/python3"

    chmod +x "$FAKE_BIN/docker" "$FAKE_BIN/curl" "$FAKE_BIN/systemctl" "$FAKE_BIN/python3"
}

# ---------------------------------------------------------------------------
# Fixtures and runner
# ---------------------------------------------------------------------------

BASE="https://test.example"

write_bundle() {
    local dir="$1"
    local marker="$2"
    local extra_assets="${3:-0}"
    rm -rf -- "$dir"
    mkdir -p "$dir/scripts"

    cat >"$dir/index.html" <<EOF
<!DOCTYPE html>
<html><body><h1>KeLee $marker</h1>
<a href="$BASE/A_Test.stoverride">A</a>
<a href="$BASE/scripts/0123456789ab-test.js">script</a>
</body></html>
EOF

    cat >"$dir/targets.json" <<'EOF'
[{"filename":"A_Test.lpx","name":"A_Test"}]
EOF

    cat >"$dir/A_Test.stoverride" <<EOF
name: A_Test
desc: publish transaction fixture $marker
url: $BASE/scripts/0123456789ab-test.js
EOF

    cat >"$dir/kelee-scripts.stoverride" <<EOF
name: kelee-scripts
desc: publish transaction fixture
script-providers:
  test:
    url: $BASE/scripts/0123456789ab-test.js
    interval: 86400
EOF

    printf 'console.log(%s);\n' "\"$marker\"" >"$dir/scripts/0123456789ab-test.js"
    printf 'console.log("legacy");\n' >"$dir/scripts/test.js"
    if [[ "$extra_assets" == "1" ]]; then
        printf 'console.log("old hashed asset");\n' >"$dir/scripts/111111111111-old.js"
    fi

    BASE_TO_WRITE="$BASE" python3 - "$dir" <<'PY'
import hashlib
import json
import os
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
base = os.environ["BASE_TO_WRITE"]
outputs = {}
for path in sorted(root.rglob("*")):
    if path.is_file() and path.name != ".build-manifest.json":
        rel = path.relative_to(root).as_posix()
        data = path.read_bytes()
        outputs[rel] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "counts": {}}
manifest = {
    "schema": 2,
    "public_base": base,
    "mirror_base": base,
    "fingerprint": "fixture-" + outputs["index.html"]["sha256"][:12],
    "generated_at": "2026-01-01T00:00:00Z",
    "outputs": outputs,
    "resources": {},
    "errors": [],
    "warnings": [],
}
(root / ".build-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
PY
}

make_sums() {
    local dir="$1" out="$2"
    (
        cd "$dir"
        find . -type f -print0 | sort -z | while IFS= read -r -d '' file; do
            printf '%s  %s\n' "$(sha256sum "$file" | cut -d' ' -f1)" "${file#./}"
        done
    ) >"$out"
}

stage_bundle() {
    local remote="$1" release_id="$2" marker="$3" extra_assets="${4:-0}"
    local staging="$remote/releases/.staging-$release_id"
    mkdir -p "$remote/releases"
    write_bundle "$staging" "$marker" "$extra_assets"
    make_sums "$staging" "$TEST_ROOT/$release_id.sums"
}

make_env() {
    local remote="$1"
    RUN_ENV=(
        "PATH=$FAKE_BIN:$PATH"
        "STASH_REMOTE_ROOT=$remote"
        "STASH_UPDATER_ROOT=$remote/updater"
        "KELEE_PUBLIC_BASE=$BASE"
        "KELEE_MIRROR_BASE=$BASE"
        "STASH_PUBLIC_HOST=test.example"
        "STASH_PUBLIC_SCHEME=https"
        "STASH_HTTP_PORT=18080"
        "STASH_HEALTH_HOST=127.0.0.1"
        "STASH_HEALTH_ATTEMPTS=2"
        "STASH_HEALTH_DELAY=0"
        "STASH_HEALTH_TIMEOUT=2"
        "STASH_CONTAINER_ATTEMPTS=2"
        "STASH_CONTAINER_DELAY=0"
        "STASH_CONTAINER_NAME=stash"
        "STASH_KEEP_RELEASES=3"
        "STASH_KEEP_ASSET_DAYS=30"
        "STASH_STRICT_CACHE_HEADERS=1"
        "FAKE_CALLS_LOG=$CALLS_LOG"
        "FAKE_DOCKER_UP_FAIL_FILE=$DOCKER_UP_FAIL_FILE"
        "FAKE_DOCKER_STATUS_FILE=$DOCKER_STATUS_FILE"
        "FAKE_HTTP_STATE_FILE=$HTTP_STATE_FILE"
        "FAKE_SYSTEMCTL_STATE=$TEST_ROOT/systemctl-state"
    )
}

prepare_remote() {
    local remote="$1"
    mkdir -p "$remote/releases" "$remote/updater/data" "$remote/updater/state" "$remote/updater/logs"
    : >"$remote/docker-compose.yml"
    : >"$CALLS_LOG"
    echo 0 >"$DOCKER_UP_FAIL_FILE"
    echo running >"$DOCKER_STATUS_FILE"
    echo "$remote/current" >"$HTTP_STATE_FILE"
    rm -rf "$TEST_ROOT/systemctl-state"
    mkdir -p "$TEST_ROOT/systemctl-state"
}

run_publish() {
    : >"$RUN_LOG"
    LAST_RC=0
    env "${RUN_ENV[@]}" bash "$PUBLISH" "$@" >"$RUN_LOG" 2>&1 || LAST_RC=$?
    return 0
}

run_retention() {
    : >"$RUN_LOG"
    LAST_RC=0
    env "${RUN_ENV[@]}" bash "$RETENTION" "$@" >"$RUN_LOG" 2>&1 || LAST_RC=$?
    return 0
}

run_stage_publish() {
    local remote="$1" release_id="$2" marker="$3" extra_assets="${4:-0}"
    stage_bundle "$remote" "$release_id" "$marker" "$extra_assets"
    run_publish --release-id "$release_id" \
        --staging "$remote/releases/.staging-$release_id" \
        --expected-sums "$TEST_ROOT/$release_id.sums" \
        --source cloud
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

install_fake_tools

REMOTE="$TEST_ROOT/remote"
make_env "$REMOTE"
prepare_remote "$REMOTE"

printf '== 场景 1：首次发布成功 ==\n'
run_stage_publish "$REMOTE" "20260101-000001" "one" 1
expect_rc "首次发布返回 0" 0
expect_link "current 指向 release1" "$REMOTE/current" "$REMOTE/releases/20260101-000001"
expect_file "发布后 index.html 存在" "$REMOTE/current/index.html"
expect_file "更新状态已同步到 data 目录" "$REMOTE/updater/data/stash/overrides/kelee/index.html"
expect_file "发布状态文件已写入" "$REMOTE/updater/state/publish.env"
expect_file "发布事件日志已写入" "$REMOTE/updater/logs/publish.log"
expect_file "内容寻址资产账本已写入" "$REMOTE/updater/state/asset-ledger.tsv"
expect_contains "事件日志记录 publish" "$REMOTE/updater/logs/publish.log" "|publish|"
expect_contains "docker compose up 被调用" "$CALLS_LOG" "compose"
expect_contains "发布状态 CURRENT_RELEASE 已记录" "$REMOTE/updater/state/publish.env" "CURRENT_RELEASE=$REMOTE/releases/20260101-000001"

printf '== 场景 2：第二次发布成功 + copy-forward 旧内容寻址资产 ==\n'
run_stage_publish "$REMOTE" "20260101-000002" "two" 0
expect_rc "第二次发布返回 0" 0
expect_link "current 指向 release2" "$REMOTE/current" "$REMOTE/releases/20260101-000002"
expect_file "旧内容寻址脚本已 copy-forward 到新版本" "$REMOTE/releases/20260101-000002/scripts/111111111111-old.js"
expect_contains "发布状态保留上一版本" "$REMOTE/updater/state/publish.env" "PREVIOUS_RELEASE=$REMOTE/releases/20260101-000001"

printf '== 场景 3：健康检查失败 -> 自动回滚 ==\n'
# Simulate a container/proxy that still serves the previous release: hashes of
# the new homepage cannot match, so the transaction must roll back.
echo "$REMOTE/releases/20260101-000002" >"$HTTP_STATE_FILE"
run_stage_publish "$REMOTE" "20260101-000003" "three" 0
expect_rc "健康检查失败返回 40" 40
expect_link "回滚后 current 回到 release2" "$REMOTE/current" "$REMOTE/releases/20260101-000002"
expect_contains "回滚记录写入发布状态" "$REMOTE/updater/state/publish.env" "LAST_ACTION=rollback"
expect_contains "事件日志记录 rollback_ok" "$REMOTE/updater/logs/publish.log" "|rollback_ok|"
expect_file "失败版本目录保留用于排查" "$REMOTE/releases/20260101-000003/index.html"
echo "$REMOTE/current" >"$HTTP_STATE_FILE"

printf '== 场景 4：compose 重建失败 -> 自动回滚 ==\n'
echo 1 >"$DOCKER_UP_FAIL_FILE"
run_stage_publish "$REMOTE" "20260101-000004" "four" 0
expect_rc "compose 失败返回 30" 30
expect_link "回滚后 current 回到 release2" "$REMOTE/current" "$REMOTE/releases/20260101-000002"
expect_contains "compose 失败回滚记录" "$REMOTE/updater/logs/publish.log" "|rollback_ok|"
expect_eq "fake docker 失败次数已消耗" "0" "$(cat "$DOCKER_UP_FAIL_FILE")"

printf '== 场景 5：并发部署被共享 flock 阻塞，随后可重试 ==\n'
rm -f "$LOCK_HELD_MARK"
(
    exec 9>"$REMOTE/updater/update.lock"
    flock -n 9 || exit 1
    printf 'held\n' >"$LOCK_HELD_MARK"
    sleep 10
) &
HOLDER_PID=$!
for _ in $(seq 1 100); do
    [[ -f "$LOCK_HELD_MARK" ]] && break
    sleep 0.05
done
stage_bundle "$REMOTE" "20260101-000005" "five" 0
run_publish --release-id "20260101-000005" \
    --staging "$REMOTE/releases/.staging-20260101-000005" \
    --expected-sums "$TEST_ROOT/20260101-000005.sums" --source cloud
expect_rc "锁被占用时返回 75" 75
expect_contains "锁占用输出明确提示跳过" "$RUN_LOG" "正在运行"
expect_contains "失败不改变 current" "$REMOTE/updater/state/publish.env" "CURRENT_RELEASE=$REMOTE/releases/20260101-000002"
kill "$HOLDER_PID" 2>/dev/null || true
wait "$HOLDER_PID" 2>/dev/null || true
HOLDER_PID=""
run_publish --release-id "20260101-000005" \
    --staging "$REMOTE/releases/.staging-20260101-000005" \
    --expected-sums "$TEST_ROOT/20260101-000005.sums" --source cloud
expect_rc "锁释放后重试发布成功" 0
expect_link "current 指向 release5" "$REMOTE/current" "$REMOTE/releases/20260101-000005"

printf '== 场景 6：更新状态同步失败 -> 回滚；修复后重试成功 ==\n'
mv "$REMOTE/updater/data" "$REMOTE/updater/data.bak"
: >"$REMOTE/updater/data"
run_stage_publish "$REMOTE" "20260101-000006" "six" 0
expect_rc "状态同步失败返回 50" 50
expect_link "状态同步失败后回滚到 release5" "$REMOTE/current" "$REMOTE/releases/20260101-000005"
expect_contains "状态同步失败记录 rollback" "$REMOTE/updater/state/publish.env" "LAST_ACTION=rollback"
expect_file "旧状态目录保持为文件（未被半同步破坏）" "$REMOTE/updater/data"
rm -f "$REMOTE/updater/data"
mv "$REMOTE/updater/data.bak" "$REMOTE/updater/data"
run_stage_publish "$REMOTE" "20260101-000006b" "six-b" 0
expect_rc "修复状态目录后重试成功" 0
expect_link "current 指向 release6b" "$REMOTE/current" "$REMOTE/releases/20260101-000006b"
expect_file "重试后状态目录包含新 index.html" "$REMOTE/updater/data/stash/overrides/kelee/index.html"
expect_contains "重试后状态同步新版本 index" "$REMOTE/updater/data/stash/overrides/kelee/index.html" "KeLee six-b"

printf '== 场景 7：保留策略（release + 资产账本） ==\n'
for old in 20000101-000000 20000102-000000 20000103-000000 20000104-000000; do
    mkdir -p "$REMOTE/releases/$old/scripts"
    printf 'stale\n' >"$REMOTE/releases/$old/index.html"
done
CURRENT_RELEASE="$REMOTE/releases/20260101-000006b"
# Unreferenced hashed assets inside the current release: one tracked in the
# ledger with an expired timestamp, one unknown to the ledger but with an old
# mtime. Both must be pruned; the referenced script must survive.
printf 'console.log("expired one");\n' >"$CURRENT_RELEASE/scripts/222222222222-expired.js"
printf 'console.log("legacy expired");\n' >"$CURRENT_RELEASE/scripts/111111111111-old.js"
touch -d '10 days ago' "$CURRENT_RELEASE/scripts/222222222222-expired.js"
ledger="$REMOTE/updater/state/asset-ledger.tsv"
python3 - "$ledger" "$CURRENT_RELEASE/scripts/111111111111-old.js" <<'PY'
import hashlib
import sys
from pathlib import Path

ledger = Path(sys.argv[1])
expired_file = Path(sys.argv[2])
rows = []
known = False
for line in ledger.read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    name, sha, last, first = line.split("\t")
    if name == "scripts/111111111111-old.js":
        sha = hashlib.sha256(expired_file.read_bytes()).hexdigest()
        last, first = "0", "0"
        known = True
    rows.append((name, sha, last, first))
if not known:
    sha = hashlib.sha256(expired_file.read_bytes()).hexdigest()
    rows.append(("scripts/111111111111-old.js", sha, "0", "0"))
ledger.write_text("\n".join("\t".join(row) for row in rows) + "\n", encoding="utf-8")
PY
run_retention --lock-held --keep-releases 3 --keep-asset-days 1
expect_rc "保留策略返回 0" 0
expect_file "保留策略保留 current 版本" "$REMOTE/releases/20260101-000006b/index.html"
expect_file "保留策略保留 previous 版本" "$REMOTE/releases/20260101-000005/index.html"
expect_no_file "保留策略删除超期旧版本" "$REMOTE/releases/20260101-000001"
expect_no_file "保留策略删除过期且未被引用的脚本" "$CURRENT_RELEASE/scripts/222222222222-expired.js"
expect_no_file "保留策略删除过期 copy-forward 脚本" "$CURRENT_RELEASE/scripts/111111111111-old.js"
expect_file "保留策略保留当前版本引用的脚本" "$CURRENT_RELEASE/scripts/0123456789ab-test.js"

printf '== 场景 8：首次部署失败不留残缺指针 ==\n'
REMOTE2="$TEST_ROOT/remote2"
mkdir -p "$REMOTE2/releases" "$REMOTE2/updater/data" "$REMOTE2/updater/state" "$REMOTE2/updater/logs"
: >"$REMOTE2/docker-compose.yml"
make_env "$REMOTE2"
echo "$TEST_ROOT/empty-http-root" >"$HTTP_STATE_FILE"
mkdir -p "$TEST_ROOT/empty-http-root"
stage_bundle "$REMOTE2" "20260101-000100" "first-fail" 0
run_publish --release-id "20260101-000100" \
    --staging "$REMOTE2/releases/.staging-20260101-000100" \
    --expected-sums "$TEST_ROOT/20260101-000100.sums" --source manual
expect_rc "首次部署健康检查失败返回 40" 40
expect_link_absent "首次部署失败后不存在 current 指针" "$REMOTE2/current"
expect_contains "首次部署失败记录 first_deploy_failed" "$REMOTE2/updater/state/publish.env" "LAST_ACTION=first_deploy_failed"
expect_file "首次部署失败版本目录保留用于排查" "$REMOTE2/releases/20260101-000100/index.html"

printf '== 场景 9：手工 --rollback-to 使用同一事务逻辑 ==\n'
make_env "$REMOTE"
echo "$REMOTE/releases/20260101-000005" >"$HTTP_STATE_FILE"
: >"$RUN_LOG"
LAST_RC=0
env "${RUN_ENV[@]}" bash "$PUBLISH" --rollback-to "20260101-000005" >"$RUN_LOG" 2>&1 || LAST_RC=$?
expect_rc "手工回滚返回 0" 0
expect_link "手工回滚后 current 指向目标版本" "$REMOTE/current" "$REMOTE/releases/20260101-000005"
expect_contains "手工回滚记录 manual_rollback" "$REMOTE/updater/state/publish.env" "LAST_ACTION=manual_rollback"
expect_contains "手工回滚后更新状态同步到目标版本" "$REMOTE/updater/data/stash/overrides/kelee/index.html" "KeLee five"
expect_eq "手工回滚状态与 current 内容一致" "$(sha256sum "$REMOTE/updater/data/stash/overrides/kelee/index.html" | cut -d' ' -f1)" "$(sha256sum "$REMOTE/releases/20260101-000005/index.html" | cut -d' ' -f1)"

printf '== 场景 10：部署侧 validate.py 校验统一地址与引用完整性 ==\n'
validate_dir="$TEST_ROOT/validate-bundle"
write_bundle "$validate_dir" "validate" 0
run_validate() {
    : >"$RUN_LOG"
    LAST_RC=0
    env "${RUN_ENV[@]}" python3 "$UPDATER/validate.py" "$@" >"$RUN_LOG" 2>&1 || LAST_RC=$?
    return 0
}
run_validate "$validate_dir" "test.example"
expect_rc "validate.py 接受地址一致的有效发布包" 0
run_validate "$validate_dir" "other.example"
expect_rc "validate.py 拒绝公共地址不一致的发布包" 1
printf 'name: A_Test\nbroken: ${urlMatch.1}\n' >"$validate_dir/A_Test.stoverride"
run_validate "$validate_dir" "test.example"
expect_rc "validate.py 拒绝残留 Loon 占位符" 1
write_bundle "$validate_dir" "validate" 0
printf '\n# tampered after manifest\n' >>"$validate_dir/index.html"
run_validate "$validate_dir" "test.example"
expect_rc "validate.py 拒绝与构建清单不一致的文件" 1

printf '== 场景 11：render-config 渲染 EnvironmentFile/systemd/Caddyfile ==\n'
render_root="$TEST_ROOT/render-root"
mkdir -p "$render_root/updater"
run_render() {
    : >"$RUN_LOG"
    LAST_RC=0
    env "${RUN_ENV[@]}" bash "$UPDATER/render-config.sh" \
        --remote-root "$render_root" \
        --updater-root "$render_root/updater" \
        --public-base "$BASE" \
        --public-host "test.example" \
        --mirror-base "$BASE" \
        --http-port 18081 \
        --container-name stash \
        --keep-releases 3 \
        --keep-asset-days 30 \
        --caddy-site-root /srv \
        --install-caddyfile "$@" >"$RUN_LOG" 2>&1 || LAST_RC=$?
    return 0
}
run_render
expect_rc "render-config 渲染成功" 0
expect_contains "EnvironmentFile 写入统一公共地址" "$render_root/updater/env" "KELEE_PUBLIC_BASE=$BASE"
expect_contains "EnvironmentFile 写入 RemoteRoot" "$render_root/updater/env" "STASH_REMOTE_ROOT=$render_root"
expect_contains "EnvironmentFile 写入健康检查端口" "$render_root/updater/env" "STASH_HTTP_PORT=18081"
expect_contains "service ExecStart 使用 UpdaterRoot" "$render_root/updater/systemd/stash-kelee-update.service" "ExecStart=/usr/bin/env bash $render_root/updater/current/run-updater.sh"
expect_contains "service EnvironmentFile 使用 UpdaterRoot" "$render_root/updater/systemd/stash-kelee-update.service" "EnvironmentFile=-$render_root/updater/env"
expect_contains "timer 单元存在调度" "$render_root/updater/systemd/stash-kelee-update.timer" "OnCalendar="
expect_contains "Caddyfile host 使用占位符渲染值" "$render_root/Caddyfile" "@stash host test.example"
expect_contains "Caddyfile root 使用渲染值" "$render_root/Caddyfile" "root * /srv"
expect_contains "Caddyfile hashed 规则锚定 12 位内容寻址前缀" "$render_root/Caddyfile" '[0-9a-f]{12}-'
expect_contains "Caddyfile legacy 脚本缓存需 revalidate" "$render_root/Caddyfile" 'no-cache, must-revalidate'
expect_contains "Caddyfile hashed 脚本缓存 immutable" "$render_root/Caddyfile" 'immutable'
legacy_line="$(grep -n '@legacy_scripts path_regexp' "$render_root/Caddyfile" | head -n 1 | cut -d: -f1)"
hashed_line="$(grep -n '@hashed_scripts path_regexp' "$render_root/Caddyfile" | head -n 1 | cut -d: -f1)"
if [[ -n "$legacy_line" && -n "$hashed_line" && "$legacy_line" -lt "$hashed_line" ]]; then
    pass "Caddyfile legacy 缓存规则位于 hashed 之前（后者覆盖前者）"
else
    fail "Caddyfile 缓存规则顺序错误：legacy=$legacy_line hashed=$hashed_line"
fi
if grep -rq '@@[A-Z_]*@@' "$render_root/updater/env" "$render_root/updater/systemd" "$render_root/Caddyfile" 2>/dev/null; then
    fail "render-config 输出仍包含未替换占位符"
else
    pass "render-config 输出无未替换占位符"
fi
# 地址来源不一致必须显式失败（--public-host 与调用方 KELEE_PUBLIC_BASE 不一致）
run_render_mismatch() {
    : >"$RUN_LOG"
    LAST_RC=0
    env "${RUN_ENV[@]}" bash "$UPDATER/render-config.sh" \
        --remote-root "$render_root" \
        --updater-root "$render_root/updater" \
        --public-host "other.example" >"$RUN_LOG" 2>&1 || LAST_RC=$?
    return 0
}
run_render_mismatch
expect_rc "render-config 拒绝地址来源不一致" 10
expect_contains "render-config 明确报告来源不一致" "$RUN_LOG" "不一致"

printf '== 场景 12：run-updater 预锁后 publish 助手复用继承锁 ==\n'
: >"$TEST_ROOT/lock-reuse.log"
LAST_RC=0
(
    export STASH_REMOTE_ROOT="$REMOTE"
    export STASH_UPDATER_ROOT="$REMOTE/updater"
    # shellcheck disable=SC1091
    source "$UPDATER/lib/common.sh"
    resolve_paths
    acquire_lock
    bash -c '
        set -Eeuo pipefail
        source "$1/lib/common.sh"
        export STASH_REMOTE_ROOT="$2" STASH_UPDATER_ROOT="$2/updater"
        resolve_paths
        acquire_lock
        echo inherited-lock-reuse-ok
    ' _ "$UPDATER" "$REMOTE"
) >"$TEST_ROOT/lock-reuse.log" 2>&1 || LAST_RC=$?
expect_rc "run-updater 预锁后助手复用继承锁成功" 0
expect_contains "继承锁复用输出" "$TEST_ROOT/lock-reuse.log" "inherited-lock-reuse-ok"

printf '== 场景 13：run-updater.sh 走同一发布助手（云端 timer 路径） ==\n'
REMOTE3="$TEST_ROOT/remote-cloud"
APP3="$TEST_ROOT/app-cloud"
mkdir -p "$REMOTE3/releases" "$REMOTE3/updater/data/stash/overrides/kelee" "$REMOTE3/updater/state" \
    "$REMOTE3/updater/logs" "$APP3/scripts"
: >"$REMOTE3/docker-compose.yml"
cp -R "$UPDATER"/. "$APP3/"
# Minimal script bundle the cloud app would upload (python wrapper dispatches on
# the file name, so a placeholder is enough here).
printf 'placeholder\n' >"$APP3/scripts/check_kelee_update.py"
printf '[{"filename":"A_Test.lpx"}]\n' >"$REMOTE3/updater/data/stash/overrides/kelee/targets.json"
# Simulate a content-addressed asset left in the seeded update state long ago:
# it must not be re-marked fresh on every build and must be pruned from both the
# served release and the synced state once outside the retention window.
STALE_ASSET="$REMOTE3/updater/data/stash/overrides/kelee/scripts/999999999999-stale.js"
mkdir -p "$(dirname "$STALE_ASSET")"
printf 'console.log("stale seeded asset");\n' >"$STALE_ASSET"
touch -d '40 days ago' "$STALE_ASSET"
echo "$REMOTE3/current" >"$HTTP_STATE_FILE"
make_env "$REMOTE3"
run_updater() {
    : >"$RUN_LOG"
    LAST_RC=0
    env "${RUN_ENV[@]}" "FAKE_RUNUPD_BUILD=$1" bash "$APP3/run-updater.sh" >"$RUN_LOG" 2>&1 || LAST_RC=$?
    return 0
}
run_updater ok
expect_rc "run-updater 构建后发布成功" 0
CLOUD_CURRENT="$(readlink -f "$REMOTE3/current" 2>/dev/null || true)"
expect_eq "run-updater 切换了 current 指针" "yes" "$([[ -n "$CLOUD_CURRENT" && -f "$CLOUD_CURRENT/index.html" ]] && echo yes || echo no)"
expect_contains "run-updater 通过共享事务发布" "$REMOTE3/updater/logs/publish.log" "|publish|"
expect_contains "run-updater 发布来源标记 cloud" "$REMOTE3/updater/logs/publish.log" "source=cloud"
expect_contains "run-updater 成功同步更新状态" "$REMOTE3/updater/data/stash/overrides/kelee/index.html" "runupdater"
expect_contains "run-updater 调用了 compose 重建" "$CALLS_LOG" "compose"
expect_no_file "过期遗留资产未进入 current 发布包" "$REMOTE3/current/scripts/999999999999-stale.js"
expect_no_file "过期遗留资产未留在同步后的更新状态中" "$REMOTE3/updater/data/stash/overrides/kelee/scripts/999999999999-stale.js"
expect_file "新构建的内容寻址资产已发布" "$REMOTE3/current/scripts/0123456789ab-test.js"
run_updater fail
expect_rc "run-updater 构建失败返回 20" 20
expect_eq "构建失败后 current 保持不变" "$CLOUD_CURRENT" "$(readlink -f "$REMOTE3/current" 2>/dev/null || true)"
make_env "$REMOTE"

printf '== 场景 14：健康检查校验 hashed/legacy 脚本的 Cache-Control 策略 ==\n'
make_env "$REMOTE"
echo "$REMOTE/current" >"$HTTP_STATE_FILE"
run_health() {
    : >"$RUN_LOG"
    LAST_RC=0
    env "${RUN_ENV[@]}" "$@" bash "$UPDATER/health-check.sh" --release "$REMOTE/current" \
        --attempts 1 --delay 0 >"$RUN_LOG" 2>&1 || LAST_RC=$?
    return 0
}
run_health
expect_rc "缓存策略正确时健康检查通过" 0
run_health "FAKE_CURL_CACHE_MODE=bad"
expect_rc "缓存策略错误且严格模式时健康检查失败" 40
expect_contains "健康检查报告 Cache-Control 问题" "$RUN_LOG" "Cache-Control"
run_health "STASH_STRICT_CACHE_HEADERS=0" "FAKE_CURL_CACHE_MODE=bad"
expect_rc "宽松模式仅警告不阻断" 0


printf '== 场景 15：更新器应用换行/权限规范化（远端部署命令回归） ==\n'
NORM_ROOT="$TEST_ROOT/normalize-app"
cp -R "$UPDATER" "$NORM_ROOT"
for crlf_file in normalize-app.sh publish-release.sh lib/common.sh templates/env.in; do
    sed -i 's/$/\r/' "$NORM_ROOT/$crlf_file"
done
# deploy-stash.ps1 bootstraps the helper with a one-line sed (a CRLF script
# cannot run itself), then invokes it with bash.
sed -i 's/\r$//' "$NORM_ROOT/normalize-app.sh"
LAST_RC=0
env "${RUN_ENV[@]}" bash "$NORM_ROOT/normalize-app.sh" "$NORM_ROOT" >"$RUN_LOG" 2>&1 || LAST_RC=$?
expect_rc "normalize-app 在 CRLF 输入上成功（等同远端部署命令）" 0
if grep -q $'\r' "$NORM_ROOT/publish-release.sh" "$NORM_ROOT/lib/common.sh" "$NORM_ROOT/templates/env.in"; then
    fail "normalize-app 未清除 CRLF"
else
    pass "normalize-app 已清除 .sh/.in 的 CRLF"
fi
if [[ -x "$NORM_ROOT/publish-release.sh" && -x "$NORM_ROOT/normalize-app.sh" ]]; then
    pass "normalize-app 已恢复入口执行权限"
else
    fail "normalize-app 未恢复入口执行权限"
fi
LAST_RC=0
env "${RUN_ENV[@]}" bash "$NORM_ROOT/normalize-app.sh" "$NORM_ROOT" >"$RUN_LOG" 2>&1 || LAST_RC=$?
expect_rc "normalize-app 可重复执行（幂等）" 0
DEPLOY_PS="$REPO_ROOT/deploy-stash.ps1"
expect_contains "deploy-stash 使用 normalize-app.sh" "$DEPLOY_PS" "normalize-app.sh"
if grep -qF "xargs -0 -r sed" "$DEPLOY_PS"; then
    fail "deploy-stash 仍使用会中断的 xargs/sed 远端命令"
else
    pass "deploy-stash 不再使用有转义问题的 xargs/sed"
fi

printf '== 场景 16：发布失败恢复更新器应用指针并回滚锁内配置安装 ==\n'
REMOTE4="$TEST_ROOT/remote4"
prepare_remote "$REMOTE4"
mkdir -p "$REMOTE4/updater/releases/app-old/app" "$REMOTE4/updater/releases/app-new/app"
cp -R "$UPDATER/." "$REMOTE4/updater/releases/app-old/app/"
cp -R "$UPDATER/." "$REMOTE4/updater/releases/app-new/app/"
for app_dir in "$REMOTE4/updater/releases/app-old/app" "$REMOTE4/updater/releases/app-new/app"; do
    mkdir -p "$app_dir/scripts"
    printf 'placeholder\n' >"$app_dir/scripts/check_kelee_update.py"
done
cat >"$REMOTE4/updater/releases/app-old/app/render-config.sh" <<'SH'
#!/usr/bin/env bash
set -u
log_file="${FAKE_RENDER_LOG:-/dev/null}"
lock_file="${LOCK_FILE:-}"
if [[ -n "$lock_file" ]] && flock -n -x "$lock_file" true 2>/dev/null; then
    printf 'lock-not-held\n' >>"$log_file"
    exit 10
fi
printf 'lock-held\n' >>"$log_file"
printf 'FAKE_RENDER=%s\n' "${FAKE_RENDER_EXIT:-0}" >"$STASH_UPDATER_ROOT/env"
exit "${FAKE_RENDER_EXIT:-0}"
SH
cp "$REMOTE4/updater/releases/app-old/app/render-config.sh" "$REMOTE4/updater/releases/app-new/app/render-config.sh"
chmod +x "$REMOTE4/updater/releases/app-old/app/render-config.sh" "$REMOTE4/updater/releases/app-new/app/render-config.sh"
ln -sfn "$REMOTE4/updater/releases/app-old/app" "$REMOTE4/updater/current"
printf 'OLD_ENV\n' >"$REMOTE4/updater/env"
echo "$REMOTE4/current" >"$HTTP_STATE_FILE"
make_env "$REMOTE4"
export FAKE_RENDER_EXIT=0
export FAKE_RENDER_LOG="$TEST_ROOT/render.log"
: >"$FAKE_RENDER_LOG"
stage_bundle "$REMOTE4" "20260101-000201" "render-ok" 0
run_publish --release-id "20260101-000201" \
    --staging "$REMOTE4/releases/.staging-20260101-000201" \
    --expected-sums "$TEST_ROOT/20260101-000201.sums" \
    --install-app "$REMOTE4/updater/releases/app-old/app" \
    --render-config-script "$REMOTE4/updater/releases/app-old/app/render-config.sh" \
    --render-enable-timer
expect_rc "带配置渲染的发布成功" 0
expect_contains "render-config 在共享锁内执行" "$FAKE_RENDER_LOG" "lock-held"
expect_contains "EnvironmentFile 已由事务写入" "$REMOTE4/updater/env" "FAKE_RENDER=0"
expect_link "更新器应用指针指向新应用" "$REMOTE4/updater/current" "$REMOTE4/updater/releases/app-old/app"
PREV_RELEASE="$(readlink -f "$REMOTE4/current")"

stage_bundle "$REMOTE4" "20260101-000202" "render-fail" 0
export FAKE_RENDER_EXIT=10
: >"$FAKE_RENDER_LOG"
run_publish --release-id "20260101-000202" \
    --staging "$REMOTE4/releases/.staging-20260101-000202" \
    --expected-sums "$TEST_ROOT/20260101-000202.sums" \
    --install-app "$REMOTE4/updater/releases/app-new/app" \
    --render-config-script "$REMOTE4/updater/releases/app-new/app/render-config.sh" \
    --render-enable-timer
expect_rc "配置渲染失败时事务返回 10" 10
expect_link "配置失败后 current 恢复上一版本" "$REMOTE4/current" "$PREV_RELEASE"
expect_link "配置失败后更新器应用指针恢复上一应用" "$REMOTE4/updater/current" "$REMOTE4/updater/releases/app-old/app"
expect_contains "配置失败后 EnvironmentFile 恢复发布前内容" "$REMOTE4/updater/env" "FAKE_RENDER=0"
if grep -qF "FAKE_RENDER=10" "$REMOTE4/updater/env"; then
    fail "配置失败后仍残留新 EnvironmentFile"
else
    pass "新 EnvironmentFile 已被回滚"
fi
if find "$REMOTE4/updater/state" -maxdepth 1 -name '.config-backup.*' | grep -q .; then
    fail "配置备份目录未在事务结束后清理"
else
    pass "配置备份目录已在回滚后清理"
fi
expect_contains "发布日志记录配置失败回滚" "$REMOTE4/updater/logs/publish.log" "配置渲染/安装失败"
expect_contains "配置回滚时恢复 timer 禁用状态" "$CALLS_LOG" "disable stash-kelee-update.timer"
expect_contains "配置回滚时停止发布期间激活的 timer" "$CALLS_LOG" "stop stash-kelee-update.timer"
unset FAKE_RENDER_EXIT FAKE_RENDER_LOG
make_env "$REMOTE"


# ===========================================================================
# Round 3: configuration/service ordering, partial-failure rollback, and
# commit-point behaviour.
# ===========================================================================

make_render_script() {
    local app="$1"
    cat >"$app/render-config.sh" <<'SH'
#!/usr/bin/env bash
set -u
printf 'RENDER_ENV=%s\n' "$KELEE_PUBLIC_BASE" >"$STASH_UPDATER_ROOT/env"
printf '@stash host %s\n' "$STASH_PUBLIC_HOST" >"$STASH_REMOTE_ROOT/Caddyfile"
if [[ -n "${FAKE_RENDER_MARKER:-}" ]]; then touch "$FAKE_RENDER_MARKER"; fi
exit "${FAKE_RENDER_EXIT:-0}"
SH
    chmod +x "$app/render-config.sh"
}

install_service_load_hook() {
    local remote="$1"
    cat >"$TEST_ROOT/reload-hook.sh" <<'SH'
#!/usr/bin/env bash
if [[ -n "${FAKE_LOADED_CADDYFILE:-}" && -f "$STASH_REMOTE_ROOT/Caddyfile" ]]; then
    cp -f "$STASH_REMOTE_ROOT/Caddyfile" "$FAKE_LOADED_CADDYFILE" 2>/dev/null || true
fi
if [[ -n "${FAKE_SERVICE_LOADED_MARKER:-}" ]]; then
    touch "$FAKE_SERVICE_LOADED_MARKER"
fi
SH
    chmod +x "$TEST_ROOT/reload-hook.sh"
    export FAKE_DOCKER_AFTER_UP_HOOK="$TEST_ROOT/reload-hook.sh"
    export FAKE_SERVICE_LOADED_MARKER="$TEST_ROOT/service-loaded"
    export FAKE_CURL_LOADED_MARKER="$FAKE_SERVICE_LOADED_MARKER"
    export FAKE_CURL_LOADED_CADDYFILE="$TEST_ROOT/loaded-Caddyfile"
    rm -f "$FAKE_SERVICE_LOADED_MARKER" "$FAKE_CURL_LOADED_CADDYFILE"
}

prepare_old_domain_remote() {
    local remote="$1" old_base="$2" old_host="$3"
    prepare_remote "$remote"
    local saved_base="$BASE"
    BASE="$old_base"
    write_bundle "$remote/releases/legacy" "legacy-domain" 1
    BASE="$saved_base"
    ln -sfn "$remote/releases/legacy" "$remote/current"
    printf 'KELEE_PUBLIC_BASE=%s\nSTASH_PUBLIC_HOST=%s\n' "$old_base" "$old_host" >"$remote/updater/env"
    printf '@stash host %s\n' "$old_host" >"$remote/Caddyfile"
    echo "$remote/current" >"$HTTP_STATE_FILE"
    make_env "$remote"
    export FAKE_CURL_CADDYFILE="$remote/Caddyfile"
    unset FAKE_CURL_CACHE_MODE_REQUIRE_FILE
    install_service_load_hook "$remote"
}

printf '== 场景 17：旧“全部 immutable”缓存策略迁移，配置安装先于健康检查 ==\n'
REMOTE6="$TEST_ROOT/remote6"
prepare_remote "$REMOTE6"
make_env "$REMOTE6"
APP6="$REMOTE6/updater/releases/render/app"
mkdir -p "$APP6"
make_render_script "$APP6"
install_service_load_hook "$REMOTE6"
export FAKE_CURL_CACHE_MODE_REQUIRE_FILE="$FAKE_SERVICE_LOADED_MARKER"
rm -f "$FAKE_CURL_CACHE_MODE_REQUIRE_FILE"
export FAKE_RENDER_MARKER="$TEST_ROOT/config-ready"
rm -f "$FAKE_RENDER_MARKER"
stage_bundle "$REMOTE6" "r3-cache-migration" "cache-migration" 0
run_publish --release-id "r3-cache-migration" \
    --staging "$REMOTE6/releases/.staging-r3-cache-migration" \
    --expected-sums "$TEST_ROOT/r3-cache-migration.sums" \
    --render-config-script "$APP6/render-config.sh" --render-install-caddyfile
expect_rc "旧缓存策略下健康检查在配置安装后通过" 0
expect_file "缓存迁移：配置渲染已执行" "$FAKE_RENDER_MARKER"
expect_file "缓存迁移：健康检查只在服务重载后执行" "$FAKE_SERVICE_LOADED_MARKER"
expect_contains "缓存迁移：新 Caddyfile 已落盘" "$REMOTE6/Caddyfile" "@stash host test.example"
unset FAKE_CURL_CACHE_MODE_REQUIRE_FILE FAKE_RENDER_MARKER
unset FAKE_DOCKER_AFTER_UP_HOOK FAKE_SERVICE_LOADED_MARKER FAKE_CURL_LOADED_MARKER FAKE_CURL_LOADED_CADDYFILE

printf '== 场景 18：域名迁移（先安装新 Caddyfile，再健康检查新域名） ==\n'
REMOTE7="$TEST_ROOT/remote7"
prepare_old_domain_remote "$REMOTE7" "https://old.example" "old.example"
make_env "$REMOTE7"
APP7="$REMOTE7/updater/releases/render/app"
mkdir -p "$APP7"
make_render_script "$APP7"
stage_bundle "$REMOTE7" "r3-domain-migration" "domain-migration" 0
run_publish --release-id "r3-domain-migration" \
    --staging "$REMOTE7/releases/.staging-r3-domain-migration" \
    --expected-sums "$TEST_ROOT/r3-domain-migration.sums" \
    --render-config-script "$APP7/render-config.sh" --render-install-caddyfile
expect_rc "域名迁移发布成功（新 Host/Caddyfile 生效后检查）" 0
expect_contains "域名迁移：新 Caddyfile host" "$REMOTE7/Caddyfile" "@stash host test.example"
expect_contains "域名迁移：EnvironmentFile 使用新地址" "$REMOTE7/updater/env" "RENDER_ENV=https://test.example"
expect_contains "域名迁移：新首页使用新地址" "$REMOTE7/current/index.html" "https://test.example"

printf '== 场景 19：配置安装成功但服务重载失败 -> 恢复旧配置/服务/站点 ==\n'
REMOTE8="$TEST_ROOT/remote8"
prepare_old_domain_remote "$REMOTE8" "https://old.example" "old.example"
make_env "$REMOTE8"
APP8="$REMOTE8/updater/releases/render/app"
mkdir -p "$APP8"
make_render_script "$APP8"
echo 1 >"$DOCKER_UP_FAIL_FILE"
stage_bundle "$REMOTE8" "r3-reload-fail" "reload-fail" 0
run_publish --release-id "r3-reload-fail" \
    --staging "$REMOTE8/releases/.staging-r3-reload-fail" \
    --expected-sums "$TEST_ROOT/r3-reload-fail.sums" \
    --render-config-script "$APP8/render-config.sh" --render-install-caddyfile
expect_rc "配置安装后服务重载失败返回 30" 30
expect_contains "重载失败后旧 Caddyfile 恢复" "$REMOTE8/Caddyfile" "@stash host old.example"
expect_contains "重载失败后旧 EnvironmentFile 恢复" "$REMOTE8/updater/env" "KELEE_PUBLIC_BASE=https://old.example"
expect_link "重载失败后 current 回到旧版本" "$REMOTE8/current" "$REMOTE8/releases/legacy"
expect_contains "重载失败记录 rollback_ok" "$REMOTE8/updater/logs/publish.log" "|rollback_ok|"
unset FAKE_CURL_CADDYFILE

printf '== 场景 20：新服务健康失败 -> 恢复旧配置并重新加载旧服务 ==\n'
REMOTE9="$TEST_ROOT/remote9"
prepare_old_domain_remote "$REMOTE9" "https://old.example" "old.example"
make_env "$REMOTE9"
APP9="$REMOTE9/updater/releases/render/app"
mkdir -p "$APP9"
make_render_script "$APP9"
echo "$REMOTE9/releases/legacy" >"$HTTP_STATE_FILE"   # new health sees old bytes -> fails
stage_bundle "$REMOTE9" "r3-health-fail" "health-fail" 0
run_publish --release-id "r3-health-fail" \
    --staging "$REMOTE9/releases/.staging-r3-health-fail" \
    --expected-sums "$TEST_ROOT/r3-health-fail.sums" \
    --render-config-script "$APP9/render-config.sh" --render-install-caddyfile
expect_rc "新域名健康失败返回 40" 40
expect_link "健康失败后 current 回到旧版本" "$REMOTE9/current" "$REMOTE9/releases/legacy"
expect_contains "健康失败后旧 Caddyfile 恢复" "$REMOTE9/Caddyfile" "@stash host old.example"
expect_contains "健康失败后旧 EnvironmentFile 恢复" "$REMOTE9/updater/env" "KELEE_PUBLIC_BASE=https://old.example"
expect_contains "健康失败后旧服务健康复验通过" "$REMOTE9/updater/logs/publish.log" "|rollback_ok|"
unset FAKE_CURL_CADDYFILE

printf '== 场景 21：配置恢复 cp 失败 -> EX_CRITICAL、保留备份、回滚失败记录 ==\n'
REMOTE10="$TEST_ROOT/remote10"
prepare_old_domain_remote "$REMOTE10" "https://old.example" "old.example"
make_env "$REMOTE10"
APP10="$REMOTE10/updater/releases/render/app"
mkdir -p "$APP10"
make_render_script "$APP10"
echo "$REMOTE10/releases/legacy" >"$HTTP_STATE_FILE"   # force new health failure
cat >"$FAKE_BIN/cp" <<'SH'
#!/usr/bin/env bash
source_path=""
for arg in "$@"; do
    case "$arg" in
    -*) ;;
    *) source_path="$arg"; break ;;
    esac
done
case "$source_path" in
*"/.config-backup."*) exit 1 ;;
esac
exec /usr/bin/cp "$@"
SH
chmod +x "$FAKE_BIN/cp"
stage_bundle "$REMOTE10" "r3-restore-cp-fail" "restore-cp-fail" 0
run_publish --release-id "r3-restore-cp-fail" \
    --staging "$REMOTE10/releases/.staging-r3-restore-cp-fail" \
    --expected-sums "$TEST_ROOT/r3-restore-cp-fail.sums" \
    --render-config-script "$APP10/render-config.sh" --render-install-caddyfile --skip-retention
expect_rc "配置恢复失败返回 EX_CRITICAL(60)" 60
if grep -qF "RENDER_ENV=https://test.example" "$REMOTE10/updater/env"; then
    pass "配置恢复失败时保留失败发布的新配置以便诊断"
else
    fail "配置恢复失败后未保留可诊断的新配置"
fi
if find "$REMOTE10/updater/state" -maxdepth 1 -name '.config-backup.*' | grep -q .; then
    pass "配置恢复失败时保留唯一备份"
else
    fail "配置恢复失败后备份被错误删除"
fi
expect_contains "配置恢复失败记录 rollback_failed" "$REMOTE10/updater/logs/publish.log" "|rollback_failed|"
rm -f "$FAKE_BIN/cp"
unset FAKE_CURL_CADDYFILE

printf '== 场景 22：健康复验失败 -> EX_CRITICAL，站点/配置恢复但服务未通过 ==\n'
REMOTE11="$TEST_ROOT/remote11"
prepare_old_domain_remote "$REMOTE11" "https://old.example" "old.example"
make_env "$REMOTE11"
APP11="$REMOTE11/updater/releases/render/app"
mkdir -p "$APP11"
make_render_script "$APP11"
mkdir -p "$TEST_ROOT/empty-http-root"
echo "$TEST_ROOT/empty-http-root" >"$HTTP_STATE_FILE"   # both new and old health fail
stage_bundle "$REMOTE11" "r3-recheck-fail" "recheck-fail" 0
run_publish --release-id "r3-recheck-fail" \
    --staging "$REMOTE11/releases/.staging-r3-recheck-fail" \
    --expected-sums "$TEST_ROOT/r3-recheck-fail.sums" \
    --render-config-script "$APP11/render-config.sh" --render-install-caddyfile --skip-retention
expect_rc "旧服务健康复验失败返回 EX_CRITICAL(60)" 60
expect_contains "健康复验失败记录 rollback_failed" "$REMOTE11/updater/logs/publish.log" "|rollback_failed|"
if find "$REMOTE11/updater/state" -maxdepth 1 -name '.config-backup.*' | grep -q .; then
    pass "健康复验失败时保留配置备份"
else
    fail "健康复验失败后备份被错误删除"
fi
unset FAKE_CURL_CADDYFILE
unset FAKE_DOCKER_AFTER_UP_HOOK FAKE_SERVICE_LOADED_MARKER FAKE_CURL_LOADED_MARKER FAKE_CURL_LOADED_CADDYFILE

printf '== 场景 23：暂存目录 mv 失败（站点未切换）也要恢复应用指针/配置 ==\n'
REMOTE12="$TEST_ROOT/remote12"
prepare_remote "$REMOTE12"
make_env "$REMOTE12"
mkdir -p "$REMOTE12/updater/releases/app-old/app" "$REMOTE12/updater/releases/app-new/app"
cp -R "$UPDATER/." "$REMOTE12/updater/releases/app-old/app/"
cp -R "$UPDATER/." "$REMOTE12/updater/releases/app-new/app/"
for app_dir in "$REMOTE12/updater/releases/app-old/app" "$REMOTE12/updater/releases/app-new/app"; do
    mkdir -p "$app_dir/scripts"
    printf 'placeholder\n' >"$app_dir/scripts/check_kelee_update.py"
done
ln -sfn "$REMOTE12/updater/releases/app-old/app" "$REMOTE12/updater/current"
write_bundle "$REMOTE12/releases/legacy" "mv-legacy" 0
ln -sfn "$REMOTE12/releases/legacy" "$REMOTE12/current"
echo "$REMOTE12/current" >"$HTTP_STATE_FILE"
stage_bundle "$REMOTE12" "r3-mv-fail" "mv-fail" 0
cat >"$FAKE_BIN/mv" <<'SH'
#!/usr/bin/env bash
if [[ "${1:-}" == *"/.staging-r3-mv-fail" ]]; then exit 1; fi
exec /usr/bin/mv "$@"
SH
chmod +x "$FAKE_BIN/mv"
run_publish --release-id "r3-mv-fail" \
    --staging "$REMOTE12/releases/.staging-r3-mv-fail" \
    --expected-sums "$TEST_ROOT/r3-mv-fail.sums" \
    --install-app "$REMOTE12/updater/releases/app-new/app"
expect_rc "mv 失败返回 20" 20
expect_link "mv 失败后更新器应用指针恢复旧应用" "$REMOTE12/updater/current" "$REMOTE12/updater/releases/app-old/app"
expect_contains "mv 失败记录 rollback_ok" "$REMOTE12/updater/logs/publish.log" "|rollback_ok|"
expect_no_file "mv 失败后不留下版本目录" "$REMOTE12/releases/r3-mv-fail"
rm -f "$FAKE_BIN/mv"

printf '== 场景 24：copy-forward 失败也要恢复应用指针 ==\n'
REMOTE13="$TEST_ROOT/remote13"
prepare_remote "$REMOTE13"
make_env "$REMOTE13"
mkdir -p "$REMOTE13/updater/releases/app-old/app" "$REMOTE13/updater/releases/app-new/app"
cp -R "$UPDATER/." "$REMOTE13/updater/releases/app-old/app/"
cp -R "$UPDATER/." "$REMOTE13/updater/releases/app-new/app/"
for app_dir in "$REMOTE13/updater/releases/app-old/app" "$REMOTE13/updater/releases/app-new/app"; do
    mkdir -p "$app_dir/scripts"
    printf 'placeholder\n' >"$app_dir/scripts/check_kelee_update.py"
done
ln -sfn "$REMOTE13/updater/releases/app-old/app" "$REMOTE13/updater/current"
BASE_SAVE="$BASE"
write_bundle "$REMOTE13/releases/legacy" "copy-legacy" 1
ln -sfn "$REMOTE13/releases/legacy" "$REMOTE13/current"
mkdir -p "$REMOTE13/updater/state"
printf 'scripts/111111111111-old.js\t%s\t%s\t%s\n' \
    "$(sha256sum "$REMOTE13/releases/legacy/scripts/111111111111-old.js" | cut -d' ' -f1)" \
    "$(date +%s)" "$(date +%s)" >"$REMOTE13/updater/state/asset-ledger.tsv"
echo "$REMOTE13/current" >"$HTTP_STATE_FILE"
stage_bundle "$REMOTE13" "r3-copy-fail" "copy-fail" 0
cat >"$FAKE_BIN/cp" <<'SH'
#!/usr/bin/env bash
is_staging_copy=0
for arg in "$@"; do
    case "$arg" in
    *"/.staging-r3-copy-fail/"*) is_staging_copy=1 ;;
    esac
done
if ((is_staging_copy == 1)); then echo "fake cp: staging copy failure" >&2; exit 1; fi
exec /usr/bin/cp "$@"
SH
chmod +x "$FAKE_BIN/cp"
run_publish --release-id "r3-copy-fail" \
    --staging "$REMOTE13/releases/.staging-r3-copy-fail" \
    --expected-sums "$TEST_ROOT/r3-copy-fail.sums" \
    --install-app "$REMOTE13/updater/releases/app-new/app"
expect_rc "copy-forward 失败返回 20" 20
expect_link "copy-forward 失败后应用指针恢复" "$REMOTE13/updater/current" "$REMOTE13/updater/releases/app-old/app"
expect_link "copy-forward 失败后站点指针不变" "$REMOTE13/current" "$REMOTE13/releases/legacy"
rm -f "$FAKE_BIN/cp"

printf '== 场景 25：首次部署无旧版本时 mv 失败 -> 清理应用指针且无 current ==\n'
REMOTE14="$TEST_ROOT/remote14"
prepare_remote "$REMOTE14"
make_env "$REMOTE14"
mkdir -p "$REMOTE14/updater/releases/app-new/app"
cp -R "$UPDATER/." "$REMOTE14/updater/releases/app-new/app/"
mkdir -p "$REMOTE14/updater/releases/app-new/app/scripts"
printf 'placeholder\n' >"$REMOTE14/updater/releases/app-new/app/scripts/check_kelee_update.py"
echo "$REMOTE14/current" >"$HTTP_STATE_FILE"
stage_bundle "$REMOTE14" "r3-first-mv-fail" "first-mv-fail" 0
cat >"$FAKE_BIN/mv" <<'SH'
#!/usr/bin/env bash
if [[ "${1:-}" == *"/.staging-r3-first-mv-fail" ]]; then exit 1; fi
exec /usr/bin/mv "$@"
SH
chmod +x "$FAKE_BIN/mv"
run_publish --release-id "r3-first-mv-fail" \
    --staging "$REMOTE14/releases/.staging-r3-first-mv-fail" \
    --expected-sums "$TEST_ROOT/r3-first-mv-fail.sums" \
    --install-app "$REMOTE14/updater/releases/app-new/app"
expect_rc "首次部署 mv 失败返回 20" 20
expect_link_absent "首次部署 mv 失败后无 current 指针" "$REMOTE14/current"
expect_link_absent "首次部署 mv 失败后无更新器应用指针" "$REMOTE14/updater/current"
rm -f "$FAKE_BIN/mv"
make_env "$REMOTE"


printf '== 场景 26：更新器应用指针恢复失败 -> EX_CRITICAL、保留备份 ==\n'
REMOTE15="$TEST_ROOT/remote15"
prepare_old_domain_remote "$REMOTE15" "https://old.example" "old.example"
make_env "$REMOTE15"
mkdir -p "$REMOTE15/updater/releases/app-old/app" "$REMOTE15/updater/releases/app-new/app"
cp -R "$UPDATER/." "$REMOTE15/updater/releases/app-old/app/"
cp -R "$UPDATER/." "$REMOTE15/updater/releases/app-new/app/"
for app_dir in "$REMOTE15/updater/releases/app-old/app" "$REMOTE15/updater/releases/app-new/app"; do
    mkdir -p "$app_dir/scripts"
    printf 'placeholder\n' >"$app_dir/scripts/check_kelee_update.py"
done
ln -sfn "$REMOTE15/updater/releases/app-old/app" "$REMOTE15/updater/current"
APP15="$REMOTE15/updater/releases/render/app"
mkdir -p "$APP15"
make_render_script "$APP15"
mkdir -p "$TEST_ROOT/empty-http-root"
echo "$TEST_ROOT/empty-http-root" >"$HTTP_STATE_FILE"   # force health failure
cat >"$FAKE_BIN/mv" <<'SH'
#!/usr/bin/env bash
if [[ -n "${FAKE_MV_FAIL_MARKER:-}" && -f "$FAKE_MV_FAIL_MARKER" ]]; then
    for arg in "$@"; do
        case "$arg" in
        *"/updater/current"*) exit 1 ;;
        esac
    done
fi
exec /usr/bin/mv "$@"
SH
chmod +x "$FAKE_BIN/mv"
export FAKE_MV_FAIL_MARKER="$TEST_ROOT/mv-fail-armed"
rm -f "$FAKE_MV_FAIL_MARKER"
export FAKE_CURL_TOUCH_FILE="$FAKE_MV_FAIL_MARKER"
stage_bundle "$REMOTE15" "r3-app-restore-fail" "app-restore-fail" 0
run_publish --release-id "r3-app-restore-fail" \
    --staging "$REMOTE15/releases/.staging-r3-app-restore-fail" \
    --expected-sums "$TEST_ROOT/r3-app-restore-fail.sums" \
    --install-app "$REMOTE15/updater/releases/app-new/app" \
    --render-config-script "$APP15/render-config.sh" --render-install-caddyfile --skip-retention
expect_rc "更新器应用指针恢复失败返回 EX_CRITICAL(60)" 60
expect_link "指针恢复失败时仍保留当前(新)应用指针" "$REMOTE15/updater/current" "$REMOTE15/updater/releases/app-new/app"
expect_contains "指针恢复失败记录 rollback_failed" "$REMOTE15/updater/logs/publish.log" "|rollback_failed|"
if find "$REMOTE15/updater/state" -maxdepth 1 -name '.config-backup.*' | grep -q .; then
    pass "指针恢复失败时保留配置备份"
else
    fail "指针恢复失败后配置备份被删除"
fi
rm -f "$FAKE_BIN/mv"
unset FAKE_MV_FAIL_MARKER FAKE_CURL_TOUCH_FILE

printf '== 场景 27：回滚时旧服务重建失败 -> EX_CRITICAL、保留备份与状态 ==\n'
REMOTE16="$TEST_ROOT/remote16"
prepare_old_domain_remote "$REMOTE16" "https://old.example" "old.example"
make_env "$REMOTE16"
APP16="$REMOTE16/updater/releases/render/app"
mkdir -p "$APP16"
make_render_script "$APP16"
mkdir -p "$TEST_ROOT/empty-http-root"
echo "$TEST_ROOT/empty-http-root" >"$HTTP_STATE_FILE"   # new health fails
export FAKE_DOCKER_FAIL_AFTER=1
export FAKE_DOCKER_COUNTER_FILE="$TEST_ROOT/docker-up-count"
: >"$FAKE_DOCKER_COUNTER_FILE"
stage_bundle "$REMOTE16" "r3-service-restore-fail" "service-restore-fail" 0
run_publish --release-id "r3-service-restore-fail" \
    --staging "$REMOTE16/releases/.staging-r3-service-restore-fail" \
    --expected-sums "$TEST_ROOT/r3-service-restore-fail.sums" \
    --render-config-script "$APP16/render-config.sh" --render-install-caddyfile --skip-retention
expect_rc "回滚服务重建失败返回 EX_CRITICAL(60)" 60
expect_contains "服务恢复失败记录 rollback_failed" "$REMOTE16/updater/logs/publish.log" "|rollback_failed|"
if find "$REMOTE16/updater/state" -maxdepth 1 -name '.config-backup.*' | grep -q .; then
    pass "服务恢复失败时保留配置备份"
else
    fail "服务恢复失败后配置备份被删除"
fi
unset FAKE_DOCKER_FAIL_AFTER FAKE_DOCKER_COUNTER_FILE
make_env "$REMOTE"


printf '== 场景 28：提交点后 publish.env 写失败不得把成功发布报告为回滚 ==\n'
REMOTE17="$TEST_ROOT/remote17"
prepare_remote "$REMOTE17"
make_env "$REMOTE17"
echo "$REMOTE17/current" >"$HTTP_STATE_FILE"
stage_bundle "$REMOTE17" "r3-post-commit-write-fail" "post-commit-write-fail" 0
cat >"$FAKE_BIN/mv" <<'SH'
#!/usr/bin/env bash
for arg in "$@"; do
    case "$arg" in
    *"/updater/state/publish.env") exit 1 ;;
    esac
done
exec /usr/bin/mv "$@"
SH
chmod +x "$FAKE_BIN/mv"
run_publish --release-id "r3-post-commit-write-fail" \
    --staging "$REMOTE17/releases/.staging-r3-post-commit-write-fail" \
    --expected-sums "$TEST_ROOT/r3-post-commit-write-fail.sums" \
    --skip-retention
expect_rc "提交点后状态写失败仍返回成功 0（不误报回滚）" 0
expect_link "提交点后状态写失败仍保留新版本 current" "$REMOTE17/current" "$REMOTE17/releases/r3-post-commit-write-fail"
expect_contains "提交点后状态写失败给出警告" "$RUN_LOG" "发布状态写入失败"
expect_contains "提交点后状态写失败仍记录 publish 事件" "$REMOTE17/updater/logs/publish.log" "|publish|"
rm -f "$FAKE_BIN/mv"

printf '== 场景 29：旧地址不可推导时回滚健康复验跳过而不是误报 critical ==\n'
REMOTE18="$TEST_ROOT/remote18"
prepare_remote "$REMOTE18"
mkdir -p "$REMOTE18/releases/legacy-no-manifest"
printf '<html><body>legacy without manifest/env</body></html>\n' >"$REMOTE18/releases/legacy-no-manifest/index.html"
printf 'name: legacy\n' >"$REMOTE18/releases/legacy-no-manifest/A_Test.stoverride"
printf 'name: legacy-scripts\n' >"$REMOTE18/releases/legacy-no-manifest/kelee-scripts.stoverride"
ln -sfn "$REMOTE18/releases/legacy-no-manifest" "$REMOTE18/current"
rm -f "$REMOTE18/updater/env"
mkdir -p "$TEST_ROOT/empty-http-root"
echo "$TEST_ROOT/empty-http-root" >"$HTTP_STATE_FILE"
make_env "$REMOTE18"
stage_bundle "$REMOTE18" "r3-unknown-old-address" "unknown-old-address" 0
run_publish --release-id "r3-unknown-old-address" \
    --staging "$REMOTE18/releases/.staging-r3-unknown-old-address" \
    --expected-sums "$TEST_ROOT/r3-unknown-old-address.sums" \
    --skip-retention
expect_rc "旧地址不可推导时回滚返回原错误码 40（非 critical）" 40
expect_link "旧地址不可推导时 current 仍恢复旧版本" "$REMOTE18/current" "$REMOTE18/releases/legacy-no-manifest"
expect_contains "旧地址不可推导时记录 rollback_ok" "$REMOTE18/updater/logs/publish.log" "|rollback_ok|"
expect_contains "旧地址不可推导时明确警告跳过复验" "$RUN_LOG" "无法从旧环境/构建清单推导旧版本公开地址"
make_env "$REMOTE"


make_timer_render_script() {
    local app="$1" action="$2" exit_code="$3"
    cat >"$app/render-config.sh" <<SH
#!/usr/bin/env bash
set -u
printf 'TIMER_RENDER=%s\n' "$action" >"\$STASH_UPDATER_ROOT/env"
systemctl $action stash-kelee-update.timer >/dev/null 2>&1 || true
exit $exit_code
SH
    chmod +x "$app/render-config.sh"
}

set_timer_state() {
    local enabled="$1" active="$2"
    rm -rf "$TEST_ROOT/systemctl-state"
    mkdir -p "$TEST_ROOT/systemctl-state"
    ((enabled == 1)) && : >"$TEST_ROOT/systemctl-state/enabled"
    ((active == 1)) && : >"$TEST_ROOT/systemctl-state/active"
    return 0
}

timer_state() {
    local enabled=disabled active=inactive
    [[ -f "$TEST_ROOT/systemctl-state/enabled" ]] && enabled=enabled
    [[ -f "$TEST_ROOT/systemctl-state/active" ]] && active=active
    printf '%s/%s' "$enabled" "$active"
}

printf '== 场景 30：timer 正常恢复路径（原 disabled/inactive -> 发布启用 -> 回滚禁用/停止） ==\n'
REMOTE20="$TEST_ROOT/remote20"
prepare_old_domain_remote "$REMOTE20" "https://old.example" "old.example"
make_env "$REMOTE20"
set_timer_state 0 0
APP20="$REMOTE20/updater/releases/render/app"
mkdir -p "$APP20"
make_timer_render_script "$APP20" "enable --now" 10
stage_bundle "$REMOTE20" "r4-timer-normal" "timer-normal" 0
run_publish --release-id "r4-timer-normal" \
    --staging "$REMOTE20/releases/.staging-r4-timer-normal" \
    --expected-sums "$TEST_ROOT/r4-timer-normal.sums" \
    --render-config-script "$APP20/render-config.sh" --render-enable-timer --skip-retention
expect_rc "timer 正常恢复返回原错误码 10" 10
expect_eq "timer 正常恢复最终为 disabled/inactive" "disabled/inactive" "$(timer_state)"
expect_contains "timer 正常恢复记录 rollback_ok" "$REMOTE20/updater/logs/publish.log" "|rollback_ok|"
expect_contains "timer 正常恢复执行 disable（非 enable）" "$CALLS_LOG" "disable stash-kelee-update.timer"
expect_contains "timer 正常恢复执行 stop" "$CALLS_LOG" "stop stash-kelee-update.timer"
if find "$REMOTE20/updater/state" -maxdepth 1 -name '.config-backup.*' | grep -q .; then
    fail "timer 正常恢复后备份未清理"
else
    pass "timer 正常恢复后备份已清理"
fi

printf '== 场景 31：原 disabled -> 发布 enabled -> 回滚 disable 失败 -> EX_CRITICAL ==\n'
REMOTE21="$TEST_ROOT/remote21"
prepare_old_domain_remote "$REMOTE21" "https://old.example" "old.example"
make_env "$REMOTE21"
set_timer_state 0 0
APP21="$REMOTE21/updater/releases/render/app"
mkdir -p "$APP21"
make_timer_render_script "$APP21" "enable --now" 10
export FAKE_SYSTEMCTL_DISABLE_FAIL="$TEST_ROOT/timer-disable-fail"
: >"$FAKE_SYSTEMCTL_DISABLE_FAIL"
stage_bundle "$REMOTE21" "r4-timer-disable-fail" "timer-disable-fail" 0
run_publish --release-id "r4-timer-disable-fail" \
    --staging "$REMOTE21/releases/.staging-r4-timer-disable-fail" \
    --expected-sums "$TEST_ROOT/r4-timer-disable-fail.sums" \
    --render-config-script "$APP21/render-config.sh" --render-enable-timer --skip-retention
expect_rc "timer disable 恢复失败返回 EX_CRITICAL(60)" 60
expect_eq "timer disable 失败后仍为 enabled" "enabled/inactive" "$(timer_state)"
expect_contains "timer disable 失败记录 rollback_failed" "$REMOTE21/updater/logs/publish.log" "|rollback_failed|"
if find "$REMOTE21/updater/state" -maxdepth 1 -name '.config-backup.*' | grep -q .; then
    pass "timer disable 失败时保留配置备份"
else
    fail "timer disable 失败后配置备份被删除"
fi
unset FAKE_SYSTEMCTL_DISABLE_FAIL

printf '== 场景 32：原 enabled/active -> 回滚 enable 失败 -> EX_CRITICAL ==\n'
REMOTE22="$TEST_ROOT/remote22"
prepare_old_domain_remote "$REMOTE22" "https://old.example" "old.example"
make_env "$REMOTE22"
set_timer_state 1 1
APP22="$REMOTE22/updater/releases/render/app"
mkdir -p "$APP22"
make_timer_render_script "$APP22" "disable --now" 10
export FAKE_SYSTEMCTL_ENABLE_FAIL="$TEST_ROOT/timer-enable-fail"
: >"$FAKE_SYSTEMCTL_ENABLE_FAIL"
stage_bundle "$REMOTE22" "r4-timer-enable-fail" "timer-enable-fail" 0
run_publish --release-id "r4-timer-enable-fail" \
    --staging "$REMOTE22/releases/.staging-r4-timer-enable-fail" \
    --expected-sums "$TEST_ROOT/r4-timer-enable-fail.sums" \
    --render-config-script "$APP22/render-config.sh" --render-enable-timer --skip-retention
expect_rc "timer enable 恢复失败返回 EX_CRITICAL(60)" 60
expect_eq "timer enable 失败后仍为 disabled" "disabled/active" "$(timer_state)"
expect_contains "timer enable 失败记录 rollback_failed" "$REMOTE22/updater/logs/publish.log" "|rollback_failed|"
if find "$REMOTE22/updater/state" -maxdepth 1 -name '.config-backup.*' | grep -q .; then
    pass "timer enable 失败时保留配置备份"
else
    fail "timer enable 失败后配置备份被删除"
fi
unset FAKE_SYSTEMCTL_ENABLE_FAIL

printf '== 场景 33：daemon-reload 失败 -> EX_CRITICAL、保留备份 ==\n'
REMOTE23="$TEST_ROOT/remote23"
prepare_old_domain_remote "$REMOTE23" "https://old.example" "old.example"
make_env "$REMOTE23"
APP23="$REMOTE23/updater/releases/render/app"
mkdir -p "$APP23"
cat >"$APP23/render-config.sh" <<'SH'
#!/usr/bin/env bash
set -u
printf 'DAEMON_RENDER=1\n' >"$STASH_UPDATER_ROOT/env"
exit 10
SH
chmod +x "$APP23/render-config.sh"
export FAKE_SYSTEMCTL_DAEMON_RELOAD_FAIL="$TEST_ROOT/daemon-reload-fail"
: >"$FAKE_SYSTEMCTL_DAEMON_RELOAD_FAIL"
stage_bundle "$REMOTE23" "r4-daemon-reload-fail" "daemon-reload-fail" 0
run_publish --release-id "r4-daemon-reload-fail" \
    --staging "$REMOTE23/releases/.staging-r4-daemon-reload-fail" \
    --expected-sums "$TEST_ROOT/r4-daemon-reload-fail.sums" \
    --render-config-script "$APP23/render-config.sh" --render-install-units --skip-retention
expect_rc "daemon-reload 失败返回 EX_CRITICAL(60)" 60
expect_contains "daemon-reload 失败记录 rollback_failed" "$REMOTE23/updater/logs/publish.log" "|rollback_failed|"
if find "$REMOTE23/updater/state" -maxdepth 1 -name '.config-backup.*' | grep -q .; then
    pass "daemon-reload 失败时保留配置备份"
else
    fail "daemon-reload 失败后配置备份被删除"
fi
unset FAKE_SYSTEMCTL_DAEMON_RELOAD_FAIL

printf '== 场景 34：无法读取发布前 timer 状态 -> 明确诊断并拒绝修改 ==\n'
REMOTE24="$TEST_ROOT/remote24"
prepare_old_domain_remote "$REMOTE24" "https://old.example" "old.example"
make_env "$REMOTE24"
set_timer_state 0 0
APP24="$REMOTE24/updater/releases/render/app"
mkdir -p "$APP24"
make_timer_render_script "$APP24" "enable --now" 10
export FAKE_SYSTEMCTL_UNKNOWN_STATE=1
stage_bundle "$REMOTE24" "r4-timer-unknown" "timer-unknown" 0
run_publish --release-id "r4-timer-unknown" \
    --staging "$REMOTE24/releases/.staging-r4-timer-unknown" \
    --expected-sums "$TEST_ROOT/r4-timer-unknown.sums" \
    --render-config-script "$APP24/render-config.sh" --render-enable-timer --skip-retention
expect_rc "无法读取 timer 状态返回配置错误 10" 10
expect_contains "无法读取 timer 状态给出明确诊断" "$RUN_LOG" "无法读取发布前 stash-kelee-update.timer 状态"
expect_eq "无法读取 timer 状态时 timer 未改变" "disabled/inactive" "$(timer_state)"
if find "$REMOTE24/updater/state" -maxdepth 1 -name '.config-backup.*' | grep -q .; then
    fail "无法读取 timer 状态后备份未清理"
else
    pass "无法读取 timer 状态后备份已清理"
fi
unset FAKE_SYSTEMCTL_UNKNOWN_STATE

printf '== 场景 35：原 enabled/inactive -> 回滚不得意外启动 timer ==\n'
REMOTE25="$TEST_ROOT/remote25"
prepare_old_domain_remote "$REMOTE25" "https://old.example" "old.example"
make_env "$REMOTE25"
set_timer_state 1 0
APP25="$REMOTE25/updater/releases/render/app"
mkdir -p "$APP25"
make_timer_render_script "$APP25" "enable --now" 10
stage_bundle "$REMOTE25" "r4-timer-enabled-inactive" "timer-enabled-inactive" 0
run_publish --release-id "r4-timer-enabled-inactive" \
    --staging "$REMOTE25/releases/.staging-r4-timer-enabled-inactive" \
    --expected-sums "$TEST_ROOT/r4-timer-enabled-inactive.sums" \
    --render-config-script "$APP25/render-config.sh" --render-enable-timer --skip-retention
expect_rc "enabled/inactive 恢复返回原错误码 10" 10
expect_eq "enabled/inactive 恢复后仍为 enabled/inactive" "enabled/inactive" "$(timer_state)"
expect_contains "enabled/inactive 恢复记录 rollback_ok" "$REMOTE25/updater/logs/publish.log" "|rollback_ok|"
make_env "$REMOTE"


# ---------------------------------------------------------------------------
# Round 5: systemctl state probing must not treat empty stdout / unexpected rc
# as disabled, and unknown state must be rejected before any mutation.
# ---------------------------------------------------------------------------

make_probe_render_script() {
    local app="$1"
    cat >"$app/render-config.sh" <<'SH'
#!/usr/bin/env bash
set -u
if [[ -n "${FAKE_RENDER_CALLED:-}" ]]; then
    : >"$FAKE_RENDER_CALLED"
fi
printf 'R5_RENDER=1\n' >"$STASH_UPDATER_ROOT/env"
systemctl enable --now stash-kelee-update.timer >/dev/null 2>&1 || true
exit 10
SH
    chmod +x "$app/render-config.sh"
}

prepare_probe_remote() {
    local remote="$1" release_id="$2"
    prepare_old_domain_remote "$remote" "https://old.example" "old.example"
    make_env "$remote"
    set_timer_state 1 1
    local app="$remote/updater/releases/render/app"
    mkdir -p "$app"
    make_probe_render_script "$app"
    stage_bundle "$remote" "$release_id" "$release_id" 0
    APP="$app"
}

assert_snapshot_rejected_unchanged() {
    local remote="$1" desc="$2" app="$3"
    expect_rc "$desc 返回配置错误 10" 10
    expect_eq "$desc timer 保持 enabled/active" "enabled/active" "$(timer_state)"
    expect_link "$desc current 仍指向旧版本" "$remote/current" "$remote/releases/legacy"
    expect_contains "$desc 旧 EnvironmentFile 未变" "$remote/updater/env" "KELEE_PUBLIC_BASE=https://old.example"
    expect_contains "$desc 旧 Caddyfile 未变" "$remote/Caddyfile" "@stash host old.example"
    if [[ -n "${FAKE_RENDER_CALLED:-}" && -f "$FAKE_RENDER_CALLED" ]]; then
        fail "$desc 不应调用渲染脚本"
    else
        pass "$desc 未调用渲染脚本"
    fi
    if grep -Eq 'systemctl (enable|disable|start|stop)' "$CALLS_LOG"; then
        fail "$desc 不应调用 timer 变更命令"
    else
        pass "$desc 未调用 timer 变更命令"
    fi
    if find "$remote/updater/data" -type f | grep -q .; then
        fail "$desc 不应修改权威数据目录"
    else
        pass "$desc 权威数据目录未变"
    fi
    if find "$remote/updater/state" -maxdepth 1 -name '.config-backup.*' | grep -q .; then
        fail "$desc 备份未清理"
    else
        pass "$desc 备份已清理"
    fi
    if [[ -f "$remote/updater/logs/publish.log" ]] && grep -Eq '\|(rollback_ok|rollback_failed|publish)\|' "$remote/updater/logs/publish.log"; then
        fail "$desc 不应记录发布/回滚事件"
    else
        pass "$desc 未记录发布/回滚事件"
    fi
}

printf '== 场景 36：首次 is-enabled 查询失败（stdout 为空）-> 变更前拒绝 ==\n'
REMOTE26="$TEST_ROOT/remote26"
prepare_probe_remote "$REMOTE26" "r5-enabled-query-fail"
mkdir -p "$REMOTE26/updater/releases/app-old/app" "$REMOTE26/updater/releases/app-new/app"
cp -R "$UPDATER/." "$REMOTE26/updater/releases/app-old/app/"
cp -R "$UPDATER/." "$REMOTE26/updater/releases/app-new/app/"
for app_dir in "$REMOTE26/updater/releases/app-old/app" "$REMOTE26/updater/releases/app-new/app"; do
    mkdir -p "$app_dir/scripts"
    printf 'placeholder\n' >"$app_dir/scripts/check_kelee_update.py"
done
ln -sfn "$REMOTE26/updater/releases/app-old/app" "$REMOTE26/updater/current"
export FAKE_SYSTEMCTL_ENABLED_FAIL_ONCE="$TEST_ROOT/enabled-query-fail-once"
: >"$FAKE_SYSTEMCTL_ENABLED_FAIL_ONCE"
export FAKE_RENDER_CALLED="$TEST_ROOT/render-called-36"
rm -f "$FAKE_RENDER_CALLED"
run_publish --release-id "r5-enabled-query-fail" \
    --staging "$REMOTE26/releases/.staging-r5-enabled-query-fail" \
    --expected-sums "$TEST_ROOT/r5-enabled-query-fail.sums" \
    --install-app "$REMOTE26/updater/releases/app-new/app" \
    --render-config-script "$APP/render-config.sh" --render-enable-timer --skip-retention
assert_snapshot_rejected_unchanged "$REMOTE26" "首次 is-enabled 查询失败" "$APP"
expect_link "首次 is-enabled 查询失败 更新器应用指针未变" "$REMOTE26/updater/current" "$REMOTE26/updater/releases/app-old/app"
expect_contains "首次 is-enabled 查询失败诊断包含 stdout/rc" "$RUN_LOG" "is-enabled"
unset FAKE_SYSTEMCTL_ENABLED_FAIL_ONCE FAKE_RENDER_CALLED

printf '== 场景 37：is-enabled 空输出但返回 0 -> 仍按未知拒绝 ==\n'
REMOTE27="$TEST_ROOT/remote27"
prepare_probe_remote "$REMOTE27" "r5-enabled-empty-ok"
export FAKE_SYSTEMCTL_ENABLED_TEXT=""
export FAKE_SYSTEMCTL_ENABLED_RC=0
export FAKE_RENDER_CALLED="$TEST_ROOT/render-called-37"
rm -f "$FAKE_RENDER_CALLED"
run_publish --release-id "r5-enabled-empty-ok" \
    --staging "$REMOTE27/releases/.staging-r5-enabled-empty-ok" \
    --expected-sums "$TEST_ROOT/r5-enabled-empty-ok.sums" \
    --render-config-script "$APP/render-config.sh" --render-enable-timer --skip-retention
assert_snapshot_rejected_unchanged "$REMOTE27" "is-enabled 空输出 rc0" "$APP"
unset FAKE_SYSTEMCTL_ENABLED_TEXT FAKE_SYSTEMCTL_ENABLED_RC FAKE_RENDER_CALLED

printf '== 场景 38：is-active 查询失败 -> 变更前拒绝 ==\n'
REMOTE28="$TEST_ROOT/remote28"
prepare_probe_remote "$REMOTE28" "r5-active-query-fail"
export FAKE_SYSTEMCTL_ACTIVE_FAIL_ONCE="$TEST_ROOT/active-query-fail-once"
: >"$FAKE_SYSTEMCTL_ACTIVE_FAIL_ONCE"
export FAKE_RENDER_CALLED="$TEST_ROOT/render-called-38"
rm -f "$FAKE_RENDER_CALLED"
run_publish --release-id "r5-active-query-fail" \
    --staging "$REMOTE28/releases/.staging-r5-active-query-fail" \
    --expected-sums "$TEST_ROOT/r5-active-query-fail.sums" \
    --render-config-script "$APP/render-config.sh" --render-enable-timer --skip-retention
assert_snapshot_rejected_unchanged "$REMOTE28" "is-active 查询失败" "$APP"
unset FAKE_SYSTEMCTL_ACTIVE_FAIL_ONCE FAKE_RENDER_CALLED

printf '== 场景 39：矛盾/未知的 enabled 文本与退出码 -> 变更前拒绝 ==\n'
for combo in "enabled:1" "disabled:0" "unknown:0" "disabled:4"; do
    text="${combo%:*}"
    rc="${combo#*:}"
    release_id="r5-enabled-$(printf '%s' "$combo" | tr ':' '-')"
    remote="$TEST_ROOT/remote-$release_id"
    prepare_probe_remote "$remote" "$release_id"
    export FAKE_SYSTEMCTL_ENABLED_TEXT="$text"
    export FAKE_SYSTEMCTL_ENABLED_RC="$rc"
    export FAKE_RENDER_CALLED="$TEST_ROOT/render-called-$release_id"
    rm -f "$FAKE_RENDER_CALLED"
    run_publish --release-id "$release_id" \
        --staging "$remote/releases/.staging-$release_id" \
        --expected-sums "$TEST_ROOT/$release_id.sums" \
        --render-config-script "$APP/render-config.sh" --render-enable-timer --skip-retention
    assert_snapshot_rejected_unchanged "$remote" "enabled 组合 $text/$rc" "$APP"
    unset FAKE_SYSTEMCTL_ENABLED_TEXT FAKE_SYSTEMCTL_ENABLED_RC FAKE_RENDER_CALLED
done

printf '== 场景 40：矛盾的 active 文本与退出码 -> 变更前拒绝 ==\n'
REMOTE29="$TEST_ROOT/remote29"
prepare_probe_remote "$REMOTE29" "r5-active-contradiction"
export FAKE_SYSTEMCTL_ACTIVE_TEXT="inactive"
export FAKE_SYSTEMCTL_ACTIVE_RC=0
export FAKE_RENDER_CALLED="$TEST_ROOT/render-called-40"
rm -f "$FAKE_RENDER_CALLED"
run_publish --release-id "r5-active-contradiction" \
    --staging "$REMOTE29/releases/.staging-r5-active-contradiction" \
    --expected-sums "$TEST_ROOT/r5-active-contradiction.sums" \
    --render-config-script "$APP/render-config.sh" --render-enable-timer --skip-retention
assert_snapshot_rejected_unchanged "$REMOTE29" "active 组合 inactive/0" "$APP"
unset FAKE_SYSTEMCTL_ACTIVE_TEXT FAKE_SYSTEMCTL_ACTIVE_RC FAKE_RENDER_CALLED

printf '== 场景 41：首次部署 not-found/inactive 明确证据可正常发布 ==\n'
REMOTE30="$TEST_ROOT/remote30"
prepare_remote "$REMOTE30"
make_env "$REMOTE30"
export FAKE_SYSTEMCTL_ENABLED_TEXT="not-found"
export FAKE_SYSTEMCTL_ENABLED_RC=1
export FAKE_SYSTEMCTL_ACTIVE_TEXT="inactive"
export FAKE_SYSTEMCTL_ACTIVE_RC=3
APP30="$REMOTE30/updater/releases/render/app"
mkdir -p "$APP30"
make_render_script "$APP30"
stage_bundle "$REMOTE30" "r5-first-deploy-notfound" "first-deploy-notfound" 0
run_publish --release-id "r5-first-deploy-notfound" \
    --staging "$REMOTE30/releases/.staging-r5-first-deploy-notfound" \
    --expected-sums "$TEST_ROOT/r5-first-deploy-notfound.sums" \
    --render-config-script "$APP30/render-config.sh" --render-enable-timer --skip-retention
expect_rc "not-found/inactive 首次部署发布成功" 0
expect_link "not-found/inactive 首次部署 current 指向新版本" "$REMOTE30/current" "$REMOTE30/releases/r5-first-deploy-notfound"
expect_contains "not-found/inactive 被记录为 enabled=0 active=0" "$RUN_LOG" "发布前 timer 状态：enabled=0 active=0"
expect_eq "not-found/inactive 首次部署 timer 保持 disabled/inactive" "disabled/inactive" "$(timer_state)"
unset FAKE_SYSTEMCTL_ENABLED_TEXT FAKE_SYSTEMCTL_ENABLED_RC FAKE_SYSTEMCTL_ACTIVE_TEXT FAKE_SYSTEMCTL_ACTIVE_RC

printf '== 场景 42：恢复后状态复验查询未知 -> EX_CRITICAL、保留备份 ==\n'
REMOTE31="$TEST_ROOT/remote31"
prepare_probe_remote "$REMOTE31" "r5-verify-query-unknown"
set_timer_state 0 0
export FAKE_SYSTEMCTL_POST_MUTATION_QUERY_FAIL="$TEST_ROOT/post-mutation-query-fail"
rm -f "$FAKE_SYSTEMCTL_POST_MUTATION_QUERY_FAIL"
export FAKE_RENDER_CALLED="$TEST_ROOT/render-called-42"
rm -f "$FAKE_RENDER_CALLED"
run_publish --release-id "r5-verify-query-unknown" \
    --staging "$REMOTE31/releases/.staging-r5-verify-query-unknown" \
    --expected-sums "$TEST_ROOT/r5-verify-query-unknown.sums" \
    --render-config-script "$APP/render-config.sh" --render-enable-timer --skip-retention
expect_rc "恢复后状态复验未知返回 EX_CRITICAL(60)" 60
expect_eq "恢复后复验未知时 timer 实际已禁用/停止" "disabled/inactive" "$(timer_state)"
expect_contains "恢复后复验未知记录 rollback_failed" "$REMOTE31/updater/logs/publish.log" "|rollback_failed|"
if find "$REMOTE31/updater/state" -maxdepth 1 -name '.config-backup.*' | grep -q .; then
    pass "恢复后复验未知时保留配置备份"
else
    fail "恢复后复验未知后配置备份被删除"
fi
unset FAKE_SYSTEMCTL_POST_MUTATION_QUERY_FAIL FAKE_RENDER_CALLED

printf '\n== 汇总 ==\n'
printf '通过 %d/%d 项检查\n' "$PASS_COUNT" "$CHECKS"
if ((FAILURES > 0)); then
    printf '失败 %d 项\n' "$FAILURES" >&2
    exit 1
fi
printf 'PASS: publish transaction tests\n'
