#!/usr/bin/env bash
# Tests for scripts/local-artifact/upload.sh.
#
# The SUT calls out to two binaries (`7z`, `curl`); both are mocked via
# the SUT's ZIP_BIN / CURL_BIN env overrides so the tests run anywhere,
# no on-prem network and no archive tooling required.
#
# Run:
#   bash scripts/local-artifact/tests/test_upload.sh
#
# Exits 0 iff all tests pass.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SUT="$SCRIPT_DIR/../upload.sh"

if [[ ! -f "$SUT" ]]; then
    echo "Script-under-test not found: $SUT" >&2
    exit 2
fi

# ---------- harness ----------------------------------------------------------

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

assert_eq() {
    local actual="$1" expected="$2" msg="${3:-values differ}"
    [[ "$actual" == "$expected" ]] && return 0
    fail "$msg
    expected: [$expected]
    actual:   [$actual]"
    return 1
}

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

TMPROOT="$(mktemp -d -t uploadsh.XXXXXX)"
trap "rm -rf '$TMPROOT'" EXIT

make_case() {
    local d="$TMPROOT/case.$RANDOM.$$"
    mkdir -p "$d"
    echo "$d"
}

# Builds a stub of $1 that records its args (one set per line) to
# <stub>.log and exits with $2 (default 0). Optional $3 is a bash
# fragment to run after recording (e.g. to `touch` a file the SUT
# expects to exist).
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
    if [[ $TESTS_FAILED -eq $before ]]; then
        pass
    fi
}

describe() { echo ""; echo "$(gray '==>') $*"; }

# ---------- tests ------------------------------------------------------------

test_happy_path() {
    local case_dir; case_dir="$(make_case)"
    STUB_DIR="$case_dir/stubs"; mkdir -p "$STUB_DIR"
    mkdir -p "$case_dir/artifact"
    : > "$case_dir/artifact/wheel.whl"

    # 7z stub must `touch` the zip path so curl's -F file=@... finds it.
    make_stub 7z   0 'touch "$3"'
    make_stub curl 0

    (
        cd "$case_dir"
        ARTIFACT_NAME=test-wheel \
        SOURCE_DIR=artifact \
        SERVER_URL=http://server.example \
        ZIP_BIN="$STUB_DIR/7z" \
        CURL_BIN="$STUB_DIR/curl" \
        bash "$SUT"
    ) >/dev/null 2>&1 || { fail "SUT failed unexpectedly"; return; }

    assert_file_contains "$STUB_DIR/7z.log" "a "                    "7z called with archive verb"
    assert_file_contains "$STUB_DIR/7z.log" "test-wheel.zip"        "7z passed zip filename"
    assert_file_contains "$STUB_DIR/7z.log" "artifact"              "7z passed source leaf"
    assert_file_contains "$STUB_DIR/curl.log" "http://server.example/upload" "curl posted to /upload"
    assert_file_contains "$STUB_DIR/curl.log" "file=@"              "curl carried -F file form"
    assert_file_contains "$STUB_DIR/curl.log" "test-wheel.zip"      "curl carried zip name"
}

test_missing_artifact_name_fails() {
    local case_dir; case_dir="$(make_case)"
    mkdir -p "$case_dir/src"
    assert_fails "missing ARTIFACT_NAME" \
        env -u ARTIFACT_NAME \
            SOURCE_DIR="$case_dir/src" \
            SERVER_URL=http://x \
            bash "$SUT"
}

test_missing_source_dir_fails() {
    local case_dir; case_dir="$(make_case)"
    assert_fails "nonexistent SOURCE_DIR" \
        env ARTIFACT_NAME=a \
            SOURCE_DIR="$case_dir/does-not-exist" \
            SERVER_URL=http://x \
            bash "$SUT"
}

test_missing_server_url_fails() {
    local case_dir; case_dir="$(make_case)"
    mkdir -p "$case_dir/src"
    assert_fails "missing SERVER_URL" \
        env -u SERVER_URL \
            ARTIFACT_NAME=a \
            SOURCE_DIR="$case_dir/src" \
            bash "$SUT"
}

test_zip_failure_propagates() {
    local case_dir; case_dir="$(make_case)"
    STUB_DIR="$case_dir/stubs"; mkdir -p "$STUB_DIR"
    mkdir -p "$case_dir/artifact"

    make_stub 7z   1   # zip fails
    make_stub curl 0

    if (
        cd "$case_dir"
        ARTIFACT_NAME=x SOURCE_DIR=artifact SERVER_URL=http://x \
        ZIP_BIN="$STUB_DIR/7z" CURL_BIN="$STUB_DIR/curl" \
        bash "$SUT"
    ) >/dev/null 2>&1; then
        fail "SUT should fail when 7z fails"
        return
    fi
    [[ ! -f "$STUB_DIR/curl.log" ]] || \
        fail "curl must not run after 7z failure (log: $(cat "$STUB_DIR/curl.log"))"
}

test_curl_failure_propagates() {
    local case_dir; case_dir="$(make_case)"
    STUB_DIR="$case_dir/stubs"; mkdir -p "$STUB_DIR"
    mkdir -p "$case_dir/artifact"

    make_stub 7z   0 'touch "$3"'
    make_stub curl 1   # POST fails

    if (
        cd "$case_dir"
        ARTIFACT_NAME=x SOURCE_DIR=artifact SERVER_URL=http://x \
        ZIP_BIN="$STUB_DIR/7z" CURL_BIN="$STUB_DIR/curl" \
        bash "$SUT"
    ) >/dev/null 2>&1; then
        fail "SUT should fail when curl fails"
    fi
}

test_zip_is_cleaned_up_on_success() {
    local case_dir; case_dir="$(make_case)"
    STUB_DIR="$case_dir/stubs"; mkdir -p "$STUB_DIR"
    mkdir -p "$case_dir/artifact"

    make_stub 7z   0 'touch "$3"'
    make_stub curl 0

    (
        cd "$case_dir"
        ARTIFACT_NAME=cleanup SOURCE_DIR=artifact SERVER_URL=http://x \
        ZIP_BIN="$STUB_DIR/7z" CURL_BIN="$STUB_DIR/curl" \
        bash "$SUT"
    ) >/dev/null 2>&1

    [[ ! -f "$case_dir/cleanup.zip" ]] || \
        fail "EXIT trap should have removed zip; found: $case_dir/cleanup.zip"
}

# ---------- runner -----------------------------------------------------------

describe "upload.sh"
run_test "happy path posts the right zip"  test_happy_path
run_test "missing ARTIFACT_NAME fails"     test_missing_artifact_name_fails
run_test "missing SOURCE_DIR fails"        test_missing_source_dir_fails
run_test "missing SERVER_URL fails"        test_missing_server_url_fails
run_test "7z failure propagates"           test_zip_failure_propagates
run_test "curl failure propagates"         test_curl_failure_propagates
run_test "zip is cleaned up on success"    test_zip_is_cleaned_up_on_success

echo ""
echo "========================================================================"
echo "Tests passed: $(green "$TESTS_PASSED")    Tests failed: $( ((TESTS_FAILED)) && red "$TESTS_FAILED" || green "$TESTS_FAILED" )"
if (( TESTS_FAILED > 0 )); then
    echo "Failed cases:"
    for n in "${FAILED_NAMES[@]}"; do echo "  - $n"; done
    exit 1
fi
exit 0
