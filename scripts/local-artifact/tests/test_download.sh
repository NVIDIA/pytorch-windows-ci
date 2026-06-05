#!/usr/bin/env bash
# Tests for scripts/local-artifact/download.sh.
#
# Network and unzip are mocked via the SUT's CURL_BIN / UNZIP_BIN env
# overrides, so these tests run anywhere.
#
# Run:
#   bash scripts/local-artifact/tests/test_download.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SUT="$SCRIPT_DIR/../download.sh"

if [[ ! -f "$SUT" ]]; then
    echo "Script-under-test not found: $SUT" >&2
    exit 2
fi

TESTS_PASSED=0
TESTS_FAILED=0
FAILED_NAMES=()
CURRENT_TEST=""

color() { local code="$1"; shift; printf '\033[%sm%s\033[0m' "$code" "$*"; }
green() { color "32" "$@"; }
red()   { color "31" "$@"; }
gray()  { color "90" "$@"; }

fail() {
    local msg="$*"
    echo "  $(red FAIL): $msg" >&2
    TESTS_FAILED=$(( TESTS_FAILED + 1 ))
    FAILED_NAMES+=("$CURRENT_TEST")
}

pass() { TESTS_PASSED=$(( TESTS_PASSED + 1 )); }

assert_file_contains() {
    local file="$1" needle="$2" msg="${3:-needle not in file}"
    if [[ ! -f "$file" ]]; then
        fail "$msg
    file does not exist: $file"
        return 1
    fi
    if ! grep -qF -- "$needle" "$file"; then
        fail "$msg
    needle:   [$needle]
    contents: [$(cat "$file")]"
        return 1
    fi
    return 0
}

assert_fails() {
    local label="$1"; shift
    if "$@" >/dev/null 2>&1; then
        fail "$label: expected failure, got success"
        return 1
    fi
    return 0
}

TMPROOT="$(mktemp -d -t downloadsh.XXXXXX)"
trap "rm -rf '$TMPROOT'" EXIT

make_case() {
    local d="$TMPROOT/case.$RANDOM.$$"
    mkdir -p "$d"
    echo "$d"
}

make_stub() {
    local name="$1" rc="${2:-0}" extra="${3:-}"
    local out="$STUB_DIR/$name"
    cat > "$out" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$STUB_DIR/${name}.log"
$extra
exit $rc
EOF
    chmod +x "$out"
}

run_test() {
    local name="$1" fn="$2"
    CURRENT_TEST="$name"
    local before=$TESTS_FAILED
    echo "$(gray '  •') $name"
    "$fn"
    if [[ $TESTS_FAILED -eq $before ]]; then pass; fi
}

describe() { echo ""; echo "$(gray '==>') $*"; }

# ---------- tests ------------------------------------------------------------

test_happy_path() {
    local case_dir; case_dir="$(make_case)"
    STUB_DIR="$case_dir/stubs"; mkdir -p "$STUB_DIR"

    # curl stub creates the destination file so the script's subsequent
    # `unzip` invocation has something to point at. (unzip itself is a
    # stub - it won't actually extract anything.)
    make_stub curl  0 'cp /dev/null "${@: -2:1}"'
    make_stub unzip 0

    (
        cd "$case_dir"
        ARTIFACT_NAME=test-wheel \
        DEST_DIR="$case_dir/dest" \
        SERVER_URL=http://server.example \
        CURL_BIN="$STUB_DIR/curl" \
        UNZIP_BIN="$STUB_DIR/unzip" \
        bash "$SUT"
    ) >/dev/null 2>&1 || { fail "SUT failed unexpectedly"; return; }

    assert_file_contains "$STUB_DIR/curl.log"  "http://server.example/download/test-wheel.zip" \
        "curl downloads from /download/<name>.zip"
    assert_file_contains "$STUB_DIR/unzip.log" "test-wheel.zip"            "unzip targets the downloaded zip"
    assert_file_contains "$STUB_DIR/unzip.log" "$case_dir/dest"            "unzip extracts to DEST_DIR"
    [[ -d "$case_dir/dest" ]] || fail "DEST_DIR should be created"
}

test_missing_artifact_name_fails() {
    local case_dir; case_dir="$(make_case)"
    assert_fails "missing ARTIFACT_NAME" \
        env -u ARTIFACT_NAME \
            DEST_DIR="$case_dir/d" \
            SERVER_URL=http://x \
            bash "$SUT"
}

test_missing_dest_dir_fails() {
    assert_fails "missing DEST_DIR" \
        env -u DEST_DIR \
            ARTIFACT_NAME=a \
            SERVER_URL=http://x \
            bash "$SUT"
}

test_missing_server_url_fails() {
    local case_dir; case_dir="$(make_case)"
    assert_fails "missing SERVER_URL" \
        env -u SERVER_URL \
            ARTIFACT_NAME=a \
            DEST_DIR="$case_dir/d" \
            bash "$SUT"
}

test_curl_failure_propagates() {
    local case_dir; case_dir="$(make_case)"
    STUB_DIR="$case_dir/stubs"; mkdir -p "$STUB_DIR"

    make_stub curl  1   # GET fails (404, network unreachable, etc.)
    make_stub unzip 0

    if (
        cd "$case_dir"
        ARTIFACT_NAME=x DEST_DIR="$case_dir/dest" SERVER_URL=http://x \
        CURL_BIN="$STUB_DIR/curl" UNZIP_BIN="$STUB_DIR/unzip" \
        bash "$SUT"
    ) >/dev/null 2>&1; then
        fail "SUT should fail when curl fails"
        return
    fi
    [[ ! -f "$STUB_DIR/unzip.log" ]] || \
        fail "unzip must not run after curl failure (log: $(cat "$STUB_DIR/unzip.log"))"
}

test_unzip_failure_propagates() {
    local case_dir; case_dir="$(make_case)"
    STUB_DIR="$case_dir/stubs"; mkdir -p "$STUB_DIR"

    make_stub curl  0 'cp /dev/null "${@: -2:1}"'
    make_stub unzip 1

    if (
        cd "$case_dir"
        ARTIFACT_NAME=x DEST_DIR="$case_dir/dest" SERVER_URL=http://x \
        CURL_BIN="$STUB_DIR/curl" UNZIP_BIN="$STUB_DIR/unzip" \
        bash "$SUT"
    ) >/dev/null 2>&1; then
        fail "SUT should fail when unzip fails"
    fi
}

test_fetch_test_times_calls_curl_twice() {
    local case_dir; case_dir="$(make_case)"
    STUB_DIR="$case_dir/stubs"; mkdir -p "$STUB_DIR"

    make_stub curl  0 'cp /dev/null "${@: -2:1}"'
    make_stub unzip 0

    (
        cd "$case_dir"
        ARTIFACT_NAME=test-wheel \
        DEST_DIR="$case_dir/dest" \
        SERVER_URL=http://server.example \
        FETCH_TEST_TIMES=true \
        BUILD_ENVIRONMENT=win-rtx-x \
        GITHUB_RUN_ID=42 \
        CURL_BIN="$STUB_DIR/curl" \
        UNZIP_BIN="$STUB_DIR/unzip" \
        bash "$SUT"
    ) >/dev/null 2>&1 || { fail "SUT failed unexpectedly"; return; }

    local lines
    lines=$(wc -l < "$STUB_DIR/curl.log" | tr -d ' ')
    [[ "$lines" -ge 2 ]] || \
        fail "expected >=2 curl invocations with FETCH_TEST_TIMES=true, saw $lines"
    assert_file_contains "$STUB_DIR/curl.log" "test-wheel.zip"   "first fetch"
    assert_file_contains "$STUB_DIR/curl.log" "test-times.zip"   "second fetch"
}

test_fetch_test_times_requires_build_env_and_run_id() {
    local case_dir; case_dir="$(make_case)"
    STUB_DIR="$case_dir/stubs"; mkdir -p "$STUB_DIR"

    make_stub curl  0 'cp /dev/null "${@: -2:1}"'
    make_stub unzip 0

    # Missing BUILD_ENVIRONMENT and GITHUB_RUN_ID; the first curl runs
    # (it's before the guard), but the second must fail loudly.
    if (
        cd "$case_dir"
        ARTIFACT_NAME=x DEST_DIR="$case_dir/dest" SERVER_URL=http://x \
        FETCH_TEST_TIMES=true \
        CURL_BIN="$STUB_DIR/curl" UNZIP_BIN="$STUB_DIR/unzip" \
        bash "$SUT"
    ) >/dev/null 2>&1; then
        fail "FETCH_TEST_TIMES without BUILD_ENVIRONMENT must error"
    fi
}

# ---------- runner -----------------------------------------------------------

describe "download.sh"
run_test "happy path GETs and unzips"          test_happy_path
run_test "missing ARTIFACT_NAME fails"         test_missing_artifact_name_fails
run_test "missing DEST_DIR fails"              test_missing_dest_dir_fails
run_test "missing SERVER_URL fails"            test_missing_server_url_fails
run_test "curl failure propagates"             test_curl_failure_propagates
run_test "unzip failure propagates"            test_unzip_failure_propagates
run_test "fetch-test-times triggers 2 fetches" test_fetch_test_times_calls_curl_twice
run_test "test-times needs build env + run id" test_fetch_test_times_requires_build_env_and_run_id

echo ""
echo "========================================================================"
echo "Tests passed: $(green "$TESTS_PASSED")    Tests failed: $( ((TESTS_FAILED)) && red "$TESTS_FAILED" || green "$TESTS_FAILED" )"
if (( TESTS_FAILED > 0 )); then
    echo "Failed cases:"
    for n in "${FAILED_NAMES[@]}"; do echo "  - $n"; done
    exit 1
fi
exit 0
