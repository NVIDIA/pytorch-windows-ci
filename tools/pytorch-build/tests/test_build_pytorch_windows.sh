#!/usr/bin/env bash
# Tests for tools/pytorch-build/build_pytorch_windows.sh.
#
# Covers the pure / cheaply-mockable helpers. Toolchain steps (MSVC init,
# CUDA toolkit, sccache, the actual build) are integration concerns and are
# not exercised here.
#
# Run:
#   bash tools/pytorch-build/tests/test_build_pytorch_windows.sh
#
# Exits 0 iff all tests pass. Prints a green/red summary at the end.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SUT="$SCRIPT_DIR/../build_pytorch_windows.sh"

if [[ ! -f "$SUT" ]]; then
    echo "Script-under-test not found: $SUT" >&2
    exit 2
fi

# Source the SUT for its helper functions, without triggering main.
# Disable set -e while sourcing because we intentionally call helpers that
# return non-zero in tests.
export PYTORCH_BUILD_WINDOWS_DOT_SOURCE=1
# shellcheck disable=SC1090
source "$SUT"
set +e
unset PYTORCH_BUILD_WINDOWS_DOT_SOURCE

# ---------- assertion helpers -------------------------------------------------

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

pass() {
    TESTS_PASSED=$(( TESTS_PASSED + 1 ))
}

assert_eq() {
    local actual="$1" expected="$2" msg="${3:-values differ}"
    if [[ "$actual" == "$expected" ]]; then
        return 0
    fi
    fail "$msg
    expected: [$expected]
    actual:   [$actual]"
    return 1
}

assert_empty() {
    local actual="$1" msg="${2:-expected empty}"
    if [[ -z "$actual" ]]; then
        return 0
    fi
    fail "$msg
    actual: [$actual]"
    return 1
}

assert_contains() {
    local haystack="$1" needle="$2" msg="${3:-needle not found}"
    if [[ "$haystack" == *"$needle"* ]]; then
        return 0
    fi
    fail "$msg
    needle:   [$needle]
    haystack: [$haystack]"
    return 1
}

# Asserts that running the given command fails (non-zero exit). Captures stderr
# silently so test output stays clean.
assert_fails() {
    local label="$1"; shift
    if "$@" >/dev/null 2>&1; then
        fail "$label: expected command to fail, but it succeeded"
        return 1
    fi
    return 0
}

# Per-suite temp dir. Initialized once in the parent shell (NOT lazily inside
# make_tmp - that helper is always called via $(...), which runs in a subshell;
# a trap set there would fire as soon as the subshell exits and wipe the dir
# before the caller could use the returned path).
TMPROOT="$(mktemp -d -t pytbuildtests.XXXXXX)"
# shellcheck disable=SC2064
trap "rm -rf '$TMPROOT'" EXIT

make_tmp() {
    local name="${1:-case}"
    local d="$TMPROOT/$name.$RANDOM"
    mkdir -p "$d"
    echo "$d"
}

run_test() {
    local name="$1"
    local fn="$2"
    CURRENT_TEST="$name"
    local before_failed=$TESTS_FAILED
    echo "$(gray '  •') $name"
    "$fn"
    if [[ $TESTS_FAILED -eq $before_failed ]]; then
        pass
    fi
}

describe() {
    echo ""
    echo "$(gray '==>') $*"
}

# ---------- env snapshot/restore (for env-mutating helpers) -------------------

snapshot_env() {
    local vars=(BUILD_ENVIRONMENT DISTUTILS_USE_SDK CMAKE_GENERATOR TORCH_CUDA_ARCH_LIST
                MAX_JOBS USE_CUDA BUILD_TEST REBUILD DEBUG BUILD_TYPE
                CUDA_VERSION PYTHON_VERSION)
    SAVED_ENV=()
    local v
    for v in "${vars[@]}"; do
        if [[ -n "${!v+x}" ]]; then
            SAVED_ENV+=("$v=${!v}")
        else
            SAVED_ENV+=("$v=__UNSET__")
        fi
        unset "$v"
    done
}

restore_env() {
    local entry key value
    for entry in "${SAVED_ENV[@]}"; do
        key="${entry%%=*}"
        value="${entry#*=}"
        if [[ "$value" == "__UNSET__" ]]; then
            unset "$key"
        else
            export "$key=$value"
        fi
    done
}

# ============================================================================
# Tests
# ============================================================================

# -------------------- resolve_pytorch_root --------------------

test_resolve_pytorch_root_empty_throws() {
    assert_fails "empty path" resolve_pytorch_root ""
}

test_resolve_pytorch_root_missing_throws() {
    local missing
    missing="$(make_tmp missing)/does-not-exist"
    assert_fails "missing path" resolve_pytorch_root "$missing"
}

test_resolve_pytorch_root_no_setup_py_throws() {
    local d; d="$(make_tmp nosetup)"
    assert_fails "no setup.py" resolve_pytorch_root "$d"
}

test_resolve_pytorch_root_happy() {
    local d; d="$(make_tmp happyroot)"
    : > "$d/setup.py"
    local got; got="$(resolve_pytorch_root "$d")"
    # On Windows, pwd -P may yield /c/... whereas $d is /tmp/...; just check it
    # ends with the same basename and that setup.py is present.
    [[ -f "$got/setup.py" ]] || fail "resolved root missing setup.py: $got"
}

# -------------------- resolve_cuda_version_from_path --------------------

test_cuda_ver_empty() {
    local got; got="$(resolve_cuda_version_from_path "")"
    assert_empty "$got" "empty input should yield empty output"
}

test_cuda_ver_no_match() {
    local got; got="$(resolve_cuda_version_from_path "C:/some/random/path")"
    assert_empty "$got" "non-vX.Y path should yield empty output"
}

test_cuda_ver_typical() {
    local got
    got="$(resolve_cuda_version_from_path "C:\\Program Files\\NVIDIA GPU Computing Toolkit\\CUDA\\v13.0")"
    assert_eq "$got" "13.0" "expected 13.0 for vX.Y suffix"
}

test_cuda_ver_trailing_slash() {
    local got
    got="$(resolve_cuda_version_from_path "/c/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v12.4/")"
    assert_eq "$got" "12.4" "expected 12.4 with trailing slash"
}

# -------------------- resolve_max_jobs --------------------

test_max_jobs_positive() {
    local got; got="$(resolve_max_jobs 4)"
    assert_eq "$got" "4" "explicit value should pass through"
}

test_max_jobs_zero_falls_back() {
    local got; got="$(resolve_max_jobs 0)"
    [[ "$got" =~ ^[0-9]+$ ]] || fail "expected numeric fallback, got: [$got]"
    (( got > 0 )) || fail "expected positive CPU count, got: $got"
}

test_max_jobs_negative_falls_back() {
    local got; got="$(resolve_max_jobs -1)"
    [[ "$got" =~ ^[0-9]+$ ]] || fail "expected numeric fallback, got: [$got]"
    (( got > 0 )) || fail "expected positive CPU count, got: $got"
}

# -------------------- get_default_cuda_arch_list --------------------

test_default_arch_list() {
    local got; got="$(get_default_cuda_arch_list)"
    assert_eq "$got" "8.9;12.0" "OOT RTX default should be sm89 + sm120"
}

# -------------------- get_build_command --------------------

test_build_command_default_wheel() {
    opt_develop=0
    local got; got="$(get_build_command)"
    assert_eq "$got" "wheel" "default should be wheel"
}

test_build_command_develop() {
    opt_develop=1
    local got; got="$(get_build_command)"
    assert_eq "$got" "develop" "--develop should select develop"
    opt_develop=0
}

# -------------------- check_pylong_api_usage --------------------

test_pylong_no_torch_dir() {
    local d; d="$(make_tmp notorch)"
    check_pylong_api_usage "$d"
    local rc=$?
    assert_eq "$rc" "0" "absent torch/ should be a no-op"
}

test_pylong_clean_tree() {
    local d; d="$(make_tmp pylongclean)"
    mkdir -p "$d/torch"
    echo "int main(){return 0;}" > "$d/torch/foo.cpp"
    check_pylong_api_usage "$d" >/dev/null
    local rc=$?
    assert_eq "$rc" "0" "clean tree should pass"
}

test_pylong_flags_violation() {
    local d; d="$(make_tmp pylongbad)"
    mkdir -p "$d/torch"
    echo "PyObject* x = PyLong_FromLong(42);" > "$d/torch/bad.cpp"
    local out rc=0
    out="$(check_pylong_api_usage "$d")" || rc=$?
    assert_eq "$rc" "1" "violation should yield non-zero exit"
    assert_contains "$out" "bad.cpp" "output should list the offending file"
}

test_pylong_skips_allowlist() {
    local d; d="$(make_tmp pylongallowed)"
    mkdir -p "$d/torch"
    echo "PyLong_FromLong(x);"         > "$d/torch/python_numbers.h"
    echo "PyLong_AsLong(x);"            > "$d/torch/pythoncapi_compat.h"
    echo "PyLong_FromUnsignedLong(x);"  > "$d/torch/eval_frame.c"
    check_pylong_api_usage "$d" >/dev/null
    local rc=$?
    assert_eq "$rc" "0" "allow-listed files should not trigger"
}

# -------------------- set_pytorch_build_environment --------------------

test_set_build_env_baseline() {
    snapshot_env
    cuda_arch_list="8.9"
    cuda_version="13.0"
    python_version="3.12"
    max_jobs=8
    opt_no_cuda=0
    opt_rebuild=0
    opt_debug_build=0

    set_pytorch_build_environment >/dev/null

    assert_eq "$BUILD_ENVIRONMENT"    "windows-x64-rtx-local" "BUILD_ENVIRONMENT"
    assert_eq "$DISTUTILS_USE_SDK"    "1"                     "DISTUTILS_USE_SDK"
    assert_eq "$CMAKE_GENERATOR"      "Ninja"                 "CMAKE_GENERATOR"
    assert_eq "$TORCH_CUDA_ARCH_LIST" "8.9"                   "TORCH_CUDA_ARCH_LIST"
    assert_eq "$MAX_JOBS"             "8"                     "MAX_JOBS"
    assert_eq "$USE_CUDA"             "1"                     "USE_CUDA"
    assert_eq "$BUILD_TEST"           "1"                     "BUILD_TEST"
    assert_eq "$BUILD_TYPE"           "release"               "BUILD_TYPE"
    assert_eq "$CUDA_VERSION"         "13.0"                  "CUDA_VERSION"
    assert_eq "$PYTHON_VERSION"       "3.12"                  "PYTHON_VERSION"
    [[ -z "${REBUILD:-}" ]] || fail "REBUILD should be unset"
    [[ -z "${DEBUG:-}"   ]] || fail "DEBUG should be unset"
    restore_env
}

test_set_build_env_default_arch_list() {
    snapshot_env
    cuda_arch_list=""
    cuda_version=""
    python_version=""
    max_jobs=4
    opt_no_cuda=0

    set_pytorch_build_environment >/dev/null
    assert_eq "$TORCH_CUDA_ARCH_LIST" "$(get_default_cuda_arch_list)" "default arch list"
    restore_env
}

test_set_build_env_no_cuda() {
    snapshot_env
    cuda_arch_list="8.9"
    max_jobs=2
    opt_no_cuda=1

    set_pytorch_build_environment >/dev/null
    assert_eq "$USE_CUDA" "0" "USE_CUDA should be 0 with --no-cuda"
    opt_no_cuda=0
    restore_env
}

test_set_build_env_rebuild_debug() {
    snapshot_env
    cuda_arch_list="8.9"
    max_jobs=2
    opt_no_cuda=0
    opt_rebuild=1
    opt_debug_build=1

    set_pytorch_build_environment >/dev/null
    assert_eq "$REBUILD"    "1"     "REBUILD"
    assert_eq "$DEBUG"      "1"     "DEBUG"
    assert_eq "$BUILD_TYPE" "debug" "BUILD_TYPE"
    # BUILD_TEST is hardwired on regardless of other switches.
    assert_eq "$BUILD_TEST" "1"     "BUILD_TEST"

    opt_rebuild=0; opt_debug_build=0
    restore_env
}

# -------------------- find_latest_wheel --------------------

test_find_latest_wheel_missing_dist() {
    local d; d="$(make_tmp nodist)"
    local got; got="$(find_latest_wheel "$d")"
    assert_empty "$got" "missing dist/ should yield empty output"
}

test_find_latest_wheel_picks_newest() {
    local d; d="$(make_tmp withdist)"
    mkdir -p "$d/dist"
    : > "$d/dist/torch-1.0.0-cp312-cp312-win_amd64.whl"
    sleep 1
    : > "$d/dist/torch-2.0.0-cp312-cp312-win_amd64.whl"
    local got; got="$(find_latest_wheel "$d")"
    assert_contains "$got" "torch-2.0.0" "newest wheel should be selected"
}

# -------------------- parse_args --------------------

test_parse_args_positional() {
    pytorch_root=""; opt_help=0; opt_develop=0
    parse_args "/some/path"
    assert_eq "$pytorch_root" "/some/path" "positional should be captured"
    assert_eq "$opt_help"     "0"          "help should default off"
}

test_parse_args_long_options() {
    pytorch_root=""; opt_develop=0; opt_install_wheel=0
    cuda_arch_list=""; max_jobs=0; python_exe=""
    parse_args "/r" --develop --install-wheel --cuda-arch-list "8.9;9.0" --max-jobs 12 --python-exe /tmp/py.exe
    assert_eq "$pytorch_root"   "/r"        "positional"
    assert_eq "$opt_develop"    "1"         "--develop"
    assert_eq "$opt_install_wheel" "1"      "--install-wheel"
    assert_eq "$cuda_arch_list" "8.9;9.0"   "--cuda-arch-list"
    assert_eq "$max_jobs"       "12"        "--max-jobs"
    assert_eq "$python_exe"     "/tmp/py.exe" "--python-exe"
    opt_develop=0; opt_install_wheel=0
}

test_parse_args_help_short_and_long() {
    opt_help=0
    parse_args -h
    assert_eq "$opt_help" "1" "-h"
    opt_help=0
    parse_args --help
    assert_eq "$opt_help" "1" "--help"
    opt_help=0
}

test_parse_args_unknown_option_rejected() {
    assert_fails "unknown option" parse_args --not-a-real-flag
}

# -------------------- show_help --------------------

test_show_help_smoke() {
    local out
    out="$(show_help)"
    assert_contains "$out" "Build PyTorch on Windows" "header"
    assert_contains "$out" "PYTORCH_ROOT" "required arg in help"
    assert_contains "$out" "--cuda-arch-list" "options listed"
}

# ============================================================================
# Runner
# ============================================================================

describe "resolve_pytorch_root"
run_test "rejects empty path"                 test_resolve_pytorch_root_empty_throws
run_test "rejects nonexistent path"           test_resolve_pytorch_root_missing_throws
run_test "rejects dir without setup.py"       test_resolve_pytorch_root_no_setup_py_throws
run_test "accepts dir with setup.py"          test_resolve_pytorch_root_happy

describe "resolve_cuda_version_from_path"
run_test "empty input yields empty output"    test_cuda_ver_empty
run_test "non-vX.Y path yields empty output"  test_cuda_ver_no_match
run_test "extracts vX.Y from typical path"    test_cuda_ver_typical
run_test "tolerates trailing slash"           test_cuda_ver_trailing_slash

describe "resolve_max_jobs"
run_test "positive value passes through"      test_max_jobs_positive
run_test "0 falls back to CPU count"          test_max_jobs_zero_falls_back
run_test "negative falls back to CPU count"   test_max_jobs_negative_falls_back

describe "get_default_cuda_arch_list"
run_test "returns sm89+sm120 default"         test_default_arch_list

describe "get_build_command"
run_test "defaults to wheel mode"             test_build_command_default_wheel
run_test "switches to develop with flag"      test_build_command_develop

describe "check_pylong_api_usage"
run_test "no torch/ is a no-op"               test_pylong_no_torch_dir
run_test "clean tree passes"                  test_pylong_clean_tree
run_test "flags PyLong_FromLong"              test_pylong_flags_violation
run_test "skips allow-listed files"           test_pylong_skips_allowlist

describe "set_pytorch_build_environment"
run_test "baseline release + CUDA"            test_set_build_env_baseline
run_test "defaults arch list when empty"      test_set_build_env_default_arch_list
run_test "wires CPU-only mode"                test_set_build_env_no_cuda
run_test "sets REBUILD/DEBUG"                 test_set_build_env_rebuild_debug

describe "find_latest_wheel"
run_test "no dist/ yields empty"              test_find_latest_wheel_missing_dist
run_test "picks newest .whl by mtime"         test_find_latest_wheel_picks_newest

describe "parse_args"
run_test "captures positional root"           test_parse_args_positional
run_test "captures long options + values"     test_parse_args_long_options
run_test "accepts -h and --help"              test_parse_args_help_short_and_long
run_test "rejects unknown option"             test_parse_args_unknown_option_rejected

describe "show_help"
run_test "prints expected headings"           test_show_help_smoke

# ---------- summary -----------------------------------------------------------

echo ""
echo "========================================================================"
echo "Tests passed: $(green "$TESTS_PASSED")    Tests failed: $( ((TESTS_FAILED)) && red "$TESTS_FAILED" || green "$TESTS_FAILED" )"
if (( TESTS_FAILED > 0 )); then
    echo "Failed cases:"
    for name in "${FAILED_NAMES[@]}"; do
        echo "  - $name"
    done
    exit 1
fi
exit 0
