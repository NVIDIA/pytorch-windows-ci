# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

#Requires -Version 5.1
<#
.SYNOPSIS
  Environment / argument validators carved out of pytorch-windows-test-shard.ps1.

.DESCRIPTION
  Each helper is a pure assertion: it reads env vars (via Resolve-CiEnv so case-folding
  is handled), throws on misconfiguration, and otherwise returns a structured
  result. No I/O beyond env reads.
#>

. (Join-Path $PSScriptRoot '..' 'env' 'EnvResolve.ps1')

function Assert-TestShardArguments {
    <#
    .SYNOPSIS
      Parse PYTORCH_CI_TEST_SHARD / NUM_SHARDS into ints and validate the range.

    .OUTPUTS
      Hashtable with keys: Shard (int), NumShards (int).
    #>
    [CmdletBinding()]
    param(
        [string] $ShardEnv     = (Resolve-CiEnv -Name 'PYTORCH_CI_TEST_SHARD'),
        [string] $NumShardsEnv = (Resolve-CiEnv -Name 'PYTORCH_CI_TEST_NUM_SHARDS')
    )

    if (-not $ShardEnv -or -not $NumShardsEnv) {
        throw "PYTORCH_CI_TEST_SHARD and PYTORCH_CI_TEST_NUM_SHARDS must be set."
    }

    $s = 0
    $n = 0
    if (-not [int]::TryParse($ShardEnv, [ref]$s) -or -not [int]::TryParse($NumShardsEnv, [ref]$n)) {
        throw "PYTORCH_CI_TEST_SHARD and PYTORCH_CI_TEST_NUM_SHARDS must be integers."
    }
    if ($n -lt 1) {
        throw "PYTORCH_CI_TEST_NUM_SHARDS must be >= 1."
    }
    if ($s -lt 1 -or $s -gt $n) {
        throw "PYTORCH_CI_TEST_SHARD ($s) must be between 1 and PYTORCH_CI_TEST_NUM_SHARDS ($n)."
    }

    return @{ Shard = $s; NumShards = $n }
}

function Assert-CiProjectDir {
    <#
    .SYNOPSIS
      Throws unless CI_PROJECT_DIR is set in the process environment.
    #>
    if ([string]::IsNullOrWhiteSpace((Resolve-CiEnv -Name 'CI_PROJECT_DIR'))) {
        throw "CI_PROJECT_DIR is not set (the WoA entrypoint sets it from GITHUB_WORKSPACE)."
    }
}

function Resolve-TestRepoRoot {
    <#
    .SYNOPSIS
      Pick the first existing path from CHECKOUT_ROOT, then PYTORCH_WIN_TEST_PYTORCH_ROOT.

    .DESCRIPTION
      CHECKOUT_ROOT is the canonical value (set by ci/scripts/run-with-checkout.sh).
      PYTORCH_WIN_TEST_PYTORCH_ROOT is the legacy / local-debug fallback.
      Throws when neither resolves to an existing directory.
    #>
    [CmdletBinding()]
    param(
        [string] $CheckoutRoot = (Resolve-CiEnv -Name 'CHECKOUT_ROOT'),
        [string] $LegacyRoot   = (Resolve-CiEnv -Name 'PYTORCH_WIN_TEST_PYTORCH_ROOT')
    )

    if (-not [string]::IsNullOrWhiteSpace($CheckoutRoot) -and (Test-Path -LiteralPath $CheckoutRoot)) {
        return $CheckoutRoot
    }
    if (-not [string]::IsNullOrWhiteSpace($LegacyRoot) -and (Test-Path -LiteralPath $LegacyRoot)) {
        return $LegacyRoot
    }
    throw @"
PyTorch repo root not found. Run via bash ci/scripts/run-with-checkout.sh (sets CHECKOUT_ROOT), or set PYTORCH_WIN_TEST_PYTORCH_ROOT to an existing checkout for local/debug runs.
"@
}

function Resolve-RunTestScriptPath {
    <#
    .SYNOPSIS
      Absolute path to run_test.py under the resolved repo root.

    .DESCRIPTION
      PYTORCH_WIN_TEST_RUN_TEST_REL_PATH overrides the default 'test/run_test.py'.
      Forward slashes are normalised to the platform separator.
      Throws when the resulting file does not exist, OR when it resolves outside the repo root
      (defence-in-depth against accidental '..' segments or absolute overrides that would have
      the test runner load a script from a sibling checkout).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $RepoRoot,
        [string] $RelPath = (Resolve-CiEnv -Name 'PYTORCH_WIN_TEST_RUN_TEST_REL_PATH')
    )

    if ([string]::IsNullOrWhiteSpace($RelPath)) {
        $RelPath = "test/run_test.py"
    }
    $normalized = $RelPath.Trim() -replace '/', [System.IO.Path]::DirectorySeparatorChar
    $full     = [System.IO.Path]::GetFullPath((Join-Path $RepoRoot $normalized))
    $rootFull = [System.IO.Path]::GetFullPath($RepoRoot).TrimEnd('\', '/')
    $boundary = $rootFull + [System.IO.Path]::DirectorySeparatorChar
    $insideRepo = $full.Equals($rootFull, [System.StringComparison]::OrdinalIgnoreCase) -or
                  $full.StartsWith($boundary, [System.StringComparison]::OrdinalIgnoreCase)
    if (-not $insideRepo) {
        throw "PYTORCH_WIN_TEST_RUN_TEST_REL_PATH ('$RelPath') resolves to '$full', which is outside repo root '$rootFull'."
    }
    if (-not (Test-Path -LiteralPath $full)) {
        throw "run_test.py not found at $full (repo root=$RepoRoot, PYTORCH_WIN_TEST_RUN_TEST_REL_PATH=$RelPath)."
    }
    return $full
}
