# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
#
# Sourced by the `prep` jobs of windows-woa-build-test.yml and _woa-pr-build-test.yml,
# which pin pytorch, torchaudio and torchvision to the SHAs every build and test cell
# then uses. A wrong pick is silent - the cells agree with each other and build the
# wrong source - so both workflows share this one copy.
#
#   resolve_ref <repo-url> <ref>        print the commit <ref> names, or nothing
#   require_sha <what> <sha> <ref>      exit 1 unless <sha> is a full 40-hex SHA

# Resolve a branch/tag to its commit SHA over HTTPS (a full SHA passes through
# unchanged). Prefer a peeled annotated-tag commit (refs/tags/<x>^{}) when present,
# else the first matching ref. `ls-remote` patterns match any ref that merely ENDS
# in the name - `main` also matches pytorch/vision's stale `refs/heads/<user>/main`,
# which sorts first - so only an exact ref name counts.
resolve_ref() {
  local repo_url="$1" ref="$2" lines sha
  if [[ "${ref}" =~ ^[0-9a-f]{40}$ ]]; then printf '%s' "${ref}"; return 0; fi
  lines="$(git ls-remote "${repo_url}" "${ref}" "refs/heads/${ref}" "refs/tags/${ref}" 2>/dev/null || true)"
  sha="$(printf '%s\n' "${lines}" | awk -v r="${ref}" '$2 == r "^{}" || $2 == "refs/tags/" r "^{}" {print $1; exit}')"
  if [ -z "${sha}" ]; then
    sha="$(printf '%s\n' "${lines}" | awk -v r="${ref}" '$2 == r || $2 == "refs/heads/" r || $2 == "refs/tags/" r {print $1; exit}')"
  fi
  printf '%s' "${sha}"
}

require_sha() {
  if ! [[ "$2" =~ ^[0-9a-f]{40}$ ]]; then
    echo "::error::Could not resolve $1 ref '$3' to a 40-hex commit SHA (got '${2:-<empty>}')." >&2
    exit 1
  fi
}
