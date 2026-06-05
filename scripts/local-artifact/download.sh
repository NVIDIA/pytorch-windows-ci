#!/usr/bin/env bash
# Fetch a zip from the internal artifact server and unzip it, mirroring
# `actions/download-artifact@v4` for the on-prem file server. Composite-
# action wrapper: `.github/actions/download-local-artifact/action.yml`.
#
# Inputs (env vars):
#   ARTIFACT_NAME       Base filename for the zip (`.zip` is appended).
#   DEST_DIR            Directory to unzip into. Created if missing.
#   SERVER_URL          Base URL of the artifact server.
#
# Optional inputs:
#   FETCH_TEST_TIMES    'true' also fetches test-times.zip and stages it
#                       under <DEST_DIR>/<run_id>/<env>/build-results/
#                       .additional_ci_files/ (mirrors the upstream
#                       sharding-hints layout). Default: 'false'.
#   BUILD_ENVIRONMENT   Required when FETCH_TEST_TIMES=true.
#   GITHUB_RUN_ID       Required when FETCH_TEST_TIMES=true (set by GHA).
#
# Test hooks:
#   UNZIP_BIN           default: unzip
#   CURL_BIN            default: curl

set -euo pipefail

: "${ARTIFACT_NAME:?ARTIFACT_NAME required}"
: "${DEST_DIR:?DEST_DIR required}"
: "${SERVER_URL:?SERVER_URL required}"
FETCH_TEST_TIMES="${FETCH_TEST_TIMES:-false}"
BUILD_ENVIRONMENT="${BUILD_ENVIRONMENT:-}"
UNZIP_BIN="${UNZIP_BIN:-unzip}"
CURL_BIN="${CURL_BIN:-curl}"

mkdir -p "$DEST_DIR"
zip_file="${ARTIFACT_NAME}.zip"
trap 'rm -f "$zip_file" test-times.zip' EXIT

echo "==> GETting ${SERVER_URL}/download/${zip_file}"
"$CURL_BIN" --fail --silent --show-error \
    -o "$zip_file" \
    "${SERVER_URL}/download/${zip_file}"

echo "==> Unzipping ${zip_file} -> ${DEST_DIR}"
"$UNZIP_BIN" -q -o "$zip_file" -d "$DEST_DIR"

if [[ "$FETCH_TEST_TIMES" == "true" ]]; then
    if [[ -z "${BUILD_ENVIRONMENT}" || -z "${GITHUB_RUN_ID:-}" ]]; then
        echo "ERROR: FETCH_TEST_TIMES=true requires BUILD_ENVIRONMENT and GITHUB_RUN_ID" >&2
        exit 1
    fi
    echo "==> GETting ${SERVER_URL}/download/test-times.zip"
    "$CURL_BIN" --fail --silent --show-error \
        -o test-times.zip \
        "${SERVER_URL}/download/test-times.zip"
    "$UNZIP_BIN" -q -o test-times.zip -d "$DEST_DIR"

    build_path="${DEST_DIR}/${GITHUB_RUN_ID}/${BUILD_ENVIRONMENT}/build-results"
    if [[ -d "$build_path" && -d "${DEST_DIR}/times" ]]; then
        mkdir -p "${build_path}/.additional_ci_files"
        mv "${DEST_DIR}/times/"* "${build_path}/.additional_ci_files/"
        echo "==> Staged test-times into ${build_path}/.additional_ci_files/"
    else
        echo "WARNING: build_path or times/ missing; skipped test-times staging." >&2
        echo "         build_path=${build_path}" >&2
        echo "         times_dir=${DEST_DIR}/times" >&2
    fi
fi

echo "==> Downloaded ${ARTIFACT_NAME}.zip"
