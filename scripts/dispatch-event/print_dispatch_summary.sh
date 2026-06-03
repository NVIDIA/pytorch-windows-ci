#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
#
# Render a markdown summary of an incoming `repository_dispatch` event.
#
# The expected payload layout mirrors `pytorch/crcr-test`'s
# `crcr-dispatch-receiver.yml` (see
# https://github.com/pytorch/crcr-test/actions/runs/26880172411/workflow):
#
#   github.event.client_payload = {
#     event_type: "pull_request" | "push",
#     payload:    <verbatim GitHub webhook payload from upstream>,
#   }
#
# As an interim compatibility shim the script also recognises the legacy
# RFC-0050 flat schema used by this repo's relay
# (`pytorch-pr-trigger` / `pytorch-ping`), where the relevant fields live
# directly on `client_payload` rather than under `payload.*`.
#
# Side effects:
#   - Appends a markdown table + the full event JSON to $GITHUB_STEP_SUMMARY.
#   - Emits the same content (sans markdown) into the live job log via
#     ::group::/::endgroup:: blocks for grep-friendly triage.
#
# Inputs (all env-vars; every one is optional; empty cells render blank):
#   GITHUB_EVENT_NAME       - injected by GitHub Actions
#   GITHUB_EVENT_PATH       - injected by GitHub Actions; path to the
#                             full event JSON for the "Full Event Payload"
#                             section. When unset or missing the section
#                             is skipped.
#   GITHUB_STEP_SUMMARY     - injected by GitHub Actions. When unset, the
#                             markdown is written to stdout instead so
#                             the script remains useful in tests and ad
#                             hoc runs.
#   DISPATCH_ACTION         - github.event.action
#   CLIENT_EVENT_TYPE       - client_payload.event_type
#   UPSTREAM_REPO           - client_payload.payload.repository.full_name
#   PR_NUMBER               - client_payload.payload.pull_request.number
#   PR_ACTION               - client_payload.payload.action
#   PR_TITLE                - client_payload.payload.pull_request.title
#   PR_HEAD_SHA             - client_payload.payload.pull_request.head.sha
#   PR_HEAD_REPO            - client_payload.payload.pull_request.head.repo.full_name
#   PUSH_REF                - client_payload.payload.ref
#   PUSH_AFTER              - client_payload.payload.after
#   PUSH_BASE_REF           - client_payload.payload.base_ref
#   PUSH_DELETED            - client_payload.payload.deleted
#   LEGACY_PR_NUMBER        - client_payload.pr_number  (RFC-0050 flat)
#   LEGACY_HEAD_SHA         - client_payload.head_sha
#   LEGACY_BASE_REF         - client_payload.base_ref
#
# Exit code: 0 on success. The script never fails the calling job - the
# whole point is observability, so reporting issues should not block CI.

set -uo pipefail

# Allow tests / direct callers to source for helper access without firing.
if [[ "${DISPATCH_SUMMARY_DOT_SOURCE:-}" == "1" ]]; then
    return 0 2>/dev/null || true
fi

# ---------- helpers ----------------------------------------------------------

# Render a value for a markdown table cell. Empty values become an em dash
# so the table stays visually aligned and the absence is explicit.
_cell() {
    local v="${1-}"
    if [[ -z "$v" ]]; then
        printf -- '\xE2\x80\x94'   # U+2014 EM DASH
    else
        # Escape backticks inside values so they don't break the inline
        # code spans we wrap them in.
        printf '`%s`' "${v//\`/\\\`}"
    fi
}

_row() {
    local field="$1" value
    value=$(_cell "${2-}")
    printf '| %s | %s |\n' "$field" "$value"
}

_write() {
    if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
        printf '%s\n' "$*" >> "$GITHUB_STEP_SUMMARY"
    else
        printf '%s\n' "$*"
    fi
}

# ---------- table ------------------------------------------------------------

print_summary_table() {
    _write '### Dispatch Summary'
    _write ''
    _write '| Field | Value |'
    _write '|-------|-------|'
    _write "$(_row 'Event name'        "${GITHUB_EVENT_NAME:-}")"
    _write "$(_row 'Dispatch action'   "${DISPATCH_ACTION:-}")"
    _write "$(_row 'Client event_type' "${CLIENT_EVENT_TYPE:-}")"
    _write "$(_row 'Upstream repo'     "${UPSTREAM_REPO:-}")"

    case "${CLIENT_EVENT_TYPE:-}" in
        pull_request)
            _write "$(_row 'PR number' "${PR_NUMBER:-}")"
            _write "$(_row 'PR action' "${PR_ACTION:-}")"
            _write "$(_row 'PR title'  "${PR_TITLE:-}")"
            _write "$(_row 'Head SHA'  "${PR_HEAD_SHA:-}")"
            _write "$(_row 'Head repo' "${PR_HEAD_REPO:-}")"
            ;;
        push)
            _write "$(_row 'Ref'       "${PUSH_REF:-}")"
            _write "$(_row 'SHA'       "${PUSH_AFTER:-}")"
            _write "$(_row 'Base ref'  "${PUSH_BASE_REF:-}")"
            _write "$(_row 'Deleted'   "${PUSH_DELETED:-}")"
            ;;
        *)
            # Legacy RFC-0050 flat schema (e.g. pytorch-pr-trigger).
            # Only render the legacy block when at least one legacy field
            # carries data, otherwise the table would just be empty rows.
            if [[ -n "${LEGACY_PR_NUMBER:-}${LEGACY_HEAD_SHA:-}${LEGACY_BASE_REF:-}" ]]; then
                _write "$(_row 'PR number (flat)' "${LEGACY_PR_NUMBER:-}")"
                _write "$(_row 'Head SHA (flat)'  "${LEGACY_HEAD_SHA:-}")"
                _write "$(_row 'Base ref (flat)'  "${LEGACY_BASE_REF:-}")"
            fi
            ;;
    esac
}

# ---------- full payload -----------------------------------------------------

print_full_payload() {
    local path="${GITHUB_EVENT_PATH:-}"
    if [[ -z "$path" || ! -f "$path" ]]; then
        return 0
    fi

    _write ''
    _write '### Full Event Payload'
    _write ''
    _write '```json'
    if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
        cat -- "$path" >> "$GITHUB_STEP_SUMMARY"
        printf '\n' >> "$GITHUB_STEP_SUMMARY"
    else
        cat -- "$path"
        printf '\n'
    fi
    _write '```'
}

# ---------- live-log echo ----------------------------------------------------

print_log_echo() {
    printf '::group::repository_dispatch summary\n'
    printf 'event_name=%s\n'        "${GITHUB_EVENT_NAME:-}"
    printf 'dispatch_action=%s\n'   "${DISPATCH_ACTION:-}"
    printf 'client_event_type=%s\n' "${CLIENT_EVENT_TYPE:-}"
    printf 'upstream_repo=%s\n'     "${UPSTREAM_REPO:-}"
    case "${CLIENT_EVENT_TYPE:-}" in
        pull_request)
            printf 'pr_number=%s\n'   "${PR_NUMBER:-}"
            printf 'pr_action=%s\n'   "${PR_ACTION:-}"
            printf 'pr_head_sha=%s\n' "${PR_HEAD_SHA:-}"
            printf 'pr_head_repo=%s\n' "${PR_HEAD_REPO:-}"
            ;;
        push)
            printf 'push_ref=%s\n'      "${PUSH_REF:-}"
            printf 'push_after=%s\n'    "${PUSH_AFTER:-}"
            printf 'push_base_ref=%s\n' "${PUSH_BASE_REF:-}"
            ;;
        *)
            if [[ -n "${LEGACY_PR_NUMBER:-}${LEGACY_HEAD_SHA:-}${LEGACY_BASE_REF:-}" ]]; then
                printf 'legacy_pr_number=%s\n' "${LEGACY_PR_NUMBER:-}"
                printf 'legacy_head_sha=%s\n'  "${LEGACY_HEAD_SHA:-}"
                printf 'legacy_base_ref=%s\n'  "${LEGACY_BASE_REF:-}"
            fi
            ;;
    esac
    printf '::endgroup::\n'
}

main() {
    print_summary_table
    print_full_payload
    print_log_echo
}

main "$@"
