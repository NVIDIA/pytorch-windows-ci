#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
#
# Tests for scripts/dispatch-event/print_dispatch_summary.sh.
#
# Run:
#   bash scripts/dispatch-event/tests/test_print_dispatch_summary.sh
#
# The script under test renders markdown into $GITHUB_STEP_SUMMARY (or
# stdout when that env var is unset) and echoes parsed fields into the
# job log via ::group::/::endgroup:: blocks. The tests drive it as a
# subprocess, capture both streams, and assert on observable output.

set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SUT="$SCRIPT_DIR/../print_dispatch_summary.sh"

# U+2014 EM DASH - the placeholder the SUT renders for empty cells.
EM_DASH=$'\xe2\x80\x94'

if [[ ! -f "$SUT" ]]; then
    echo "Script-under-test not found: $SUT" >&2
    exit 2
fi

# ---------- assertion helpers ------------------------------------------------

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

assert_not_contains() {
    local haystack="$1" needle="$2" msg="${3:-needle unexpectedly present}"
    if [[ "$haystack" != *"$needle"* ]]; then
        return 0
    fi
    fail "$msg
    needle:   [$needle]
    haystack: [$haystack]"
    return 1
}

run_test() {
    local name="$1" fn="$2"
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

# ---------- test fixtures ----------------------------------------------------

TMPROOT="$(mktemp -d -t dispatchtests.XXXXXX)"
# shellcheck disable=SC2064
trap "rm -rf '$TMPROOT'" EXIT

make_tmp() {
    local name="${1:-case}"
    local d="$TMPROOT/$name.$RANDOM"
    mkdir -p "$d"
    echo "$d"
}

# Run the SUT in a clean child env so test-level env doesn't bleed in.
# Args:  <summary_path|""> <event_path|""> <KEY=VAL> ...
# Echoes the script's combined stdout+stderr; the summary file (if set)
# is left on disk for the caller to inspect.
invoke_sut() {
    local summary_path="$1"; shift
    local event_path="$1"; shift
    env -i \
        PATH="$PATH" \
        HOME="${HOME:-/tmp}" \
        BASH="${BASH:-/bin/bash}" \
        GITHUB_EVENT_NAME="repository_dispatch" \
        GITHUB_STEP_SUMMARY="$summary_path" \
        GITHUB_EVENT_PATH="$event_path" \
        "$@" \
        bash "$SUT" 2>&1
}

read_summary() {
    local path="$1"
    if [[ -f "$path" ]]; then
        cat "$path"
    fi
}

# ============================================================================
# Tests
# ============================================================================

# -------------------- pull_request event -------------------------------------

test_pull_request_renders_pr_block() {
    local d; d="$(make_tmp pr)"
    local summary="$d/summary.md"
    local event="$d/event.json"
    cat > "$event" <<'JSON'
{"action":"received","client_payload":{"event_type":"pull_request","payload":{"action":"opened","repository":{"full_name":"pytorch/pytorch"},"pull_request":{"number":12345,"title":"Add new feature","head":{"sha":"deadbeefdeadbeefdeadbeefdeadbeefdeadbeef","repo":{"full_name":"contrib/pytorch-fork"}}}}}}
JSON
    : > "$summary"

    local log
    log=$(invoke_sut "$summary" "$event" \
        DISPATCH_ACTION="received" \
        CLIENT_EVENT_TYPE="pull_request" \
        UPSTREAM_REPO="pytorch/pytorch" \
        PR_NUMBER="12345" \
        PR_ACTION="opened" \
        PR_TITLE="Add new feature" \
        PR_HEAD_SHA="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef" \
        PR_HEAD_REPO="contrib/pytorch-fork")

    local out; out=$(read_summary "$summary")
    assert_contains "$out" "### Dispatch Summary" "summary heading present"
    assert_contains "$out" "| Event name | \`repository_dispatch\` |" "event name row"
    assert_contains "$out" "| Client event_type | \`pull_request\` |" "client event_type row"
    assert_contains "$out" "| Upstream repo | \`pytorch/pytorch\` |" "upstream repo row"
    assert_contains "$out" "| PR number | \`12345\` |" "PR number row"
    assert_contains "$out" "| PR action | \`opened\` |" "PR action row"
    assert_contains "$out" "| PR title | \`Add new feature\` |" "PR title row"
    assert_contains "$out" "| Head SHA | \`deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\` |" "head SHA row"
    assert_contains "$out" "| Head repo | \`contrib/pytorch-fork\` |" "head repo row"
    assert_not_contains "$out" "| Ref |" "push-only rows should be absent"
    assert_not_contains "$out" "PR number (flat)" "legacy block should be absent"

    assert_contains "$log" "::group::repository_dispatch summary" "log group opened"
    assert_contains "$log" "pr_number=12345" "log echoes pr_number"
    assert_contains "$log" "::endgroup::" "log group closed"
}

test_pull_request_embeds_full_payload() {
    local d; d="$(make_tmp pr_full)"
    local summary="$d/summary.md"
    local event="$d/event.json"
    local payload='{"client_payload":{"event_type":"pull_request","payload":{"pull_request":{"number":7}}}}'
    printf '%s' "$payload" > "$event"
    : > "$summary"

    invoke_sut "$summary" "$event" \
        CLIENT_EVENT_TYPE="pull_request" \
        PR_NUMBER="7" >/dev/null

    local out; out=$(read_summary "$summary")
    assert_contains "$out" "### Full Event Payload" "full payload heading"
    assert_contains "$out" '```json' "json fence opened"
    assert_contains "$out" "$payload"           "raw payload embedded"
}

# -------------------- push event ---------------------------------------------

test_push_renders_push_block() {
    local d; d="$(make_tmp push)"
    local summary="$d/summary.md"
    local event="$d/event.json"
    cat > "$event" <<'JSON'
{"action":"received","client_payload":{"event_type":"push","payload":{"repository":{"full_name":"pytorch/pytorch"},"ref":"refs/heads/main","after":"cafebabecafebabecafebabecafebabecafebabe","base_ref":"","deleted":false}}}
JSON
    : > "$summary"

    invoke_sut "$summary" "$event" \
        CLIENT_EVENT_TYPE="push" \
        UPSTREAM_REPO="pytorch/pytorch" \
        PUSH_REF="refs/heads/main" \
        PUSH_AFTER="cafebabecafebabecafebabecafebabecafebabe" \
        PUSH_BASE_REF="" \
        PUSH_DELETED="false" >/dev/null

    local out; out=$(read_summary "$summary")
    assert_contains "$out" "| Client event_type | \`push\` |" "client event_type row"
    assert_contains "$out" "| Ref | \`refs/heads/main\` |" "ref row"
    assert_contains "$out" "| SHA | \`cafebabecafebabecafebabecafebabecafebabe\` |" "sha row"
    assert_contains "$out" "| Deleted | \`false\` |" "deleted row"
    # Empty base_ref should render as the em-dash placeholder, not a literal
    # backtick-wrapped empty cell that breaks alignment.
    assert_contains "$out" "| Base ref | $EM_DASH |" "empty base_ref renders em-dash"
    assert_not_contains "$out" "| PR number |" "PR-only rows should be absent"
}

# -------------------- legacy flat schema -------------------------------------

test_legacy_flat_schema_pytorch_pr_trigger() {
    local d; d="$(make_tmp legacy)"
    local summary="$d/summary.md"
    local event="$d/event.json"
    cat > "$event" <<'JSON'
{"action":"pytorch-pr-trigger","client_payload":{"pr_number":4242,"head_sha":"0123456789012345678901234567890123456789","base_ref":"main"}}
JSON
    : > "$summary"

    invoke_sut "$summary" "$event" \
        DISPATCH_ACTION="pytorch-pr-trigger" \
        LEGACY_PR_NUMBER="4242" \
        LEGACY_HEAD_SHA="0123456789012345678901234567890123456789" \
        LEGACY_BASE_REF="main" >/dev/null

    local out; out=$(read_summary "$summary")
    assert_contains "$out" "| Dispatch action | \`pytorch-pr-trigger\` |" "dispatch action row"
    assert_contains "$out" "| PR number (flat) | \`4242\` |" "legacy pr_number row"
    assert_contains "$out" "| Head SHA (flat) | \`0123456789012345678901234567890123456789\` |" "legacy head_sha row"
    assert_contains "$out" "| Base ref (flat) | \`main\` |" "legacy base_ref row"
    # Client event_type is absent so the typed PR/push blocks must be skipped.
    assert_not_contains "$out" "| PR title |"      "PR-block rows must be skipped"
    assert_not_contains "$out" "| Ref |"           "push-block rows must be skipped"
}

test_legacy_block_omitted_when_no_legacy_fields() {
    local d; d="$(make_tmp legacy_empty)"
    local summary="$d/summary.md"
    local event="$d/event.json"
    printf '{}' > "$event"
    : > "$summary"

    invoke_sut "$summary" "$event" >/dev/null

    local out; out=$(read_summary "$summary")
    assert_not_contains "$out" "PR number (flat)" "legacy rows must not appear when empty"
    assert_not_contains "$out" "Head SHA (flat)"  "legacy rows must not appear when empty"
}

# -------------------- empty / missing inputs ---------------------------------

test_empty_cells_render_em_dash() {
    local d; d="$(make_tmp emdash)"
    local summary="$d/summary.md"
    local event="$d/event.json"
    printf '{}' > "$event"
    : > "$summary"

    invoke_sut "$summary" "$event" \
        CLIENT_EVENT_TYPE="pull_request" >/dev/null

    local out; out=$(read_summary "$summary")
    # Upstream repo, PR fields are all empty -> em-dash placeholders.
    assert_contains "$out" "| Upstream repo | $EM_DASH |" "empty upstream repo em-dash"
    assert_contains "$out" "| PR number | $EM_DASH |"     "empty PR number em-dash"
    assert_contains "$out" "| PR title | $EM_DASH |"      "empty PR title em-dash"
}

test_missing_event_path_skips_full_payload() {
    local d; d="$(make_tmp nopath)"
    local summary="$d/summary.md"
    : > "$summary"

    invoke_sut "$summary" "" \
        CLIENT_EVENT_TYPE="pull_request" >/dev/null

    local out; out=$(read_summary "$summary")
    assert_contains "$out"     "### Dispatch Summary"  "table still renders"
    assert_not_contains "$out" "### Full Event Payload" "no full-payload section without event file"
}

test_missing_step_summary_falls_back_to_stdout() {
    local d; d="$(make_tmp stdout)"
    local event="$d/event.json"
    printf '{"hello":"world"}' > "$event"

    local out
    out=$(invoke_sut "" "$event" \
        CLIENT_EVENT_TYPE="pull_request" \
        PR_NUMBER="1")
    assert_contains "$out" "### Dispatch Summary"   "table goes to stdout"
    assert_contains "$out" "| PR number | \`1\` |"  "PR number row on stdout"
    assert_contains "$out" '{"hello":"world"}'      "full payload on stdout"
}

# -------------------- escaping -----------------------------------------------

test_backticks_in_title_are_escaped() {
    local d; d="$(make_tmp escape)"
    local summary="$d/summary.md"
    local event="$d/event.json"
    printf '{}' > "$event"
    : > "$summary"

    invoke_sut "$summary" "$event" \
        CLIENT_EVENT_TYPE="pull_request" \
        PR_TITLE='Fix `torch.compile` regression' >/dev/null

    local out; out=$(read_summary "$summary")
    # Backticks inside the value get backslash-escaped so the wrapping
    # inline-code span stays well-formed.
    assert_contains "$out" 'Fix \`torch.compile\` regression' "backticks escaped"
}

# -------------------- non-dispatch events ------------------------------------

test_non_dispatch_event_still_renders_table() {
    # The action.yml's `if:` filters out non-dispatch events, but if the
    # script is ever invoked directly it should still emit a usable table
    # rather than blowing up under `set -u`.
    local d; d="$(make_tmp nondispatch)"
    local summary="$d/summary.md"
    local event="$d/event.json"
    printf '{}' > "$event"
    : > "$summary"

    env -i \
        PATH="$PATH" \
        HOME="${HOME:-/tmp}" \
        BASH="${BASH:-/bin/bash}" \
        GITHUB_EVENT_NAME="workflow_dispatch" \
        GITHUB_STEP_SUMMARY="$summary" \
        GITHUB_EVENT_PATH="$event" \
        bash "$SUT" >/dev/null 2>&1
    local rc=$?

    [[ $rc -eq 0 ]] || fail "script should exit 0 on non-dispatch events (got $rc)"
    local out; out=$(read_summary "$summary")
    assert_contains "$out" "| Event name | \`workflow_dispatch\` |" "event name carried"
}

# ============================================================================
# Runner
# ============================================================================

describe "pull_request event"
run_test "renders the PR block"               test_pull_request_renders_pr_block
run_test "embeds the full event payload"      test_pull_request_embeds_full_payload

describe "push event"
run_test "renders the push block"             test_push_renders_push_block

describe "legacy flat schema (RFC-0050)"
run_test "renders pytorch-pr-trigger fields"  test_legacy_flat_schema_pytorch_pr_trigger
run_test "omits legacy block when empty"      test_legacy_block_omitted_when_no_legacy_fields

describe "empty / missing inputs"
run_test "empty cells render em-dash"         test_empty_cells_render_em_dash
run_test "missing event path skips payload"   test_missing_event_path_skips_full_payload
run_test "missing summary falls back stdout"  test_missing_step_summary_falls_back_to_stdout

describe "escaping"
run_test "backticks in title are escaped"     test_backticks_in_title_are_escaped

describe "non-dispatch events"
run_test "still renders a usable table"       test_non_dispatch_event_still_renders_table

# ---------- summary ----------------------------------------------------------

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
