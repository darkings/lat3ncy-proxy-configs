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
while (($#)); do
    case "$1" in
        --output|-o) out="$2"; shift 2 ;;
        --dump-header|-D) hdr="$2"; shift 2 ;;
        --write-out|-w|--header|-H|--max-time|-m) shift 2 ;;
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
if [[ -n "${FAKE_CURL_CACHE_MODE:-}" && "${FAKE_CURL_CACHE_MODE}" == "bad" && -n "$cache_header" ]]; then
    if [[ "$cache_header" == *immutable* ]]; then
        cache_header="no-cache, must-revalidate"
    else
        cache_header="public, max-age=31536000, immutable"
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

printf '\n== 汇总 ==\n'
printf '通过 %d/%d 项检查\n' "$PASS_COUNT" "$CHECKS"
if ((FAILURES > 0)); then
    printf '失败 %d 项\n' "$FAILURES" >&2
    exit 1
fi
printf 'PASS: publish transaction tests\n'
