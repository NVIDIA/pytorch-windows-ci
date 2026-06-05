#!/usr/bin/env bash
# Zip a directory and POST it to the internal artifact server, mirroring
# `actions/upload-artifact@v4` for the on-prem file server that backs the
# self-hosted Windows pool. Composite-action wrapper:
# `.github/actions/upload-local-artifact/action.yml`.
#
# Inputs (env vars):
#   ARTIFACT_NAME   Base filename for the zip (`.zip` is appended).
#   SOURCE_DIR      Directory to zip (absolute or relative). Its basename
#                   becomes the zip root, so SOURCE_DIR=artifact produces
#                   a zip whose entries are `artifact/...`.
#   SERVER_URL      Base URL of the artifact server (no trailing slash).
#
# Test hooks (override the binary the script invokes):
#   ZIP_BIN         default: 7z
#   CURL_BIN        default: curl

set -euo pipefail

: "${ARTIFACT_NAME:?ARTIFACT_NAME required}"
: "${SOURCE_DIR:?SOURCE_DIR required}"
: "${SERVER_URL:?SERVER_URL required}"
ZIP_BIN="${ZIP_BIN:-7z}"
CURL_BIN="${CURL_BIN:-curl}"

if [[ ! -d "$SOURCE_DIR" ]]; then
    echo "ERROR: source-dir does not exist: $SOURCE_DIR" >&2
    exit 1
fi

abs_source="$(cd "$(dirname "$SOURCE_DIR")" && pwd -P)/$(basename "$SOURCE_DIR")"
parent="$(dirname "$abs_source")"
leaf="$(basename "$abs_source")"
zip_file="${PWD}/${ARTIFACT_NAME}.zip"

# Always clean up the local copy on exit so the workspace doesn't carry
# a half-uploaded artifact forward.
trap 'rm -f "$zip_file"' EXIT

echo "==> Zipping ${abs_source} -> ${zip_file}"
(cd "$parent" && "$ZIP_BIN" a "$zip_file" "$leaf" >/dev/null)

echo "==> POSTing ${zip_file##*/} to ${SERVER_URL}/upload"
"$CURL_BIN" --fail --silent --show-error \
    -X POST -F "file=@${zip_file}" \
    "${SERVER_URL}/upload"

echo "==> Uploaded ${ARTIFACT_NAME}.zip"
