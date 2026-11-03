# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

#Requires -Version 5.1
<#
.SYNOPSIS
  Single source of truth for site-specific PowerShell defaults referenced by ci/scripts/windows.

.DESCRIPTION
  Dot-source this file (or env/EnvDefaults.ps1 which dot-sources it) from any pwsh entrypoint
  that needs a hard-coded fallback URL / UNC / toolchain path. Every value is a *default only* —
  runtime callers MUST honour the corresponding CI/CD env var first via Resolve-CiEnv, so
  behaviour is unchanged for any CI variable override that already worked.

  Read defaults via Get-CiDefault <Name> (canonical lookup, defined in EnvDefaults.ps1).

  Internally this file aggregates per-domain default tables from .\defaults\*.psd1. Each .psd1
  declares a Domain name and a Defaults hashtable. The aggregator:

    * Loads every .psd1 lexicographically (deterministic ordering on every runner).
    * Rejects duplicate keys across files (fail loud — typos used to silently override).
    * Expands {Key} references to other defaults (any file), so a version that several paths
      share is written once: CudnnLibDir = '{CudnnRoot}\lib\{CudaVersion}\arm64'. Unknown or
      circular references throw at load time; Get-CiDefault only ever sees the expanded string.
    * Merges into a case-insensitive OrderedDictionary so Get-CiDefault matches CudaPath /
      cudapath / CUDAPATH alike while preserving canonical CamelCase casing on enumeration.

  Domains live under defaults\:
    build-toolchain   CUDA/cuDNN/APL/libuv/vcvars paths
    build-flags       BLAS/USE_*/CMAKE flags/CL fragments
    build-meta        venv activator, wheel output dir
    test-runtime      test venv activator
    extensions        torchaudio/torchvision URLs, codec toggles, vcpkg layout
    metadata          toolchain metadata overlay

.NOTES
  CHANGES TO THE PER-DOMAIN FILES ARE A RUNTIME CONTRACT.
  Update tests/pester/windows/shared/Defaults.Tests.ps1 in the same PR as any value change.
#>

# ----------------------------------------------------------------------------------------------
# Aggregation
# ----------------------------------------------------------------------------------------------

$__defaultsDir = Join-Path $PSScriptRoot 'defaults'
if (-not (Test-Path -LiteralPath $__defaultsDir)) {
    throw "Defaults.ps1: per-domain defaults dir not found: $__defaultsDir"
}

# Lexicographic ordering — deterministic across runners regardless of FS ordering quirks.
$__domainFiles = @(
    Get-ChildItem -LiteralPath $__defaultsDir -Filter '*.psd1' -File |
        Sort-Object -Property Name -Culture ([System.Globalization.CultureInfo]::InvariantCulture)
)
if ($__domainFiles.Count -eq 0) {
    throw "Defaults.ps1: no .psd1 default files under $__defaultsDir"
}

# Build a merged literal first (so we can audit duplicates with helpful 'first defined in <file>'
# diagnostics), then re-host into the case-insensitive OrderedDictionary the rest of the codebase
# expects.
$__merged       = [ordered]@{}
$__ownerByKey   = @{}   # canonical-key -> originating .psd1 file name (for duplicate diagnostics)

foreach ($__file in $__domainFiles) {
    $__data = Import-PowerShellDataFile -LiteralPath $__file.FullName
    if ($null -eq $__data) {
        throw "Defaults.ps1: $($__file.Name) is empty or could not be imported."
    }
    if (-not $__data.ContainsKey('Defaults') -or $null -eq $__data.Defaults) {
        throw "Defaults.ps1: $($__file.Name) has no 'Defaults' hashtable."
    }

    foreach ($__entry in ([hashtable]$__data.Defaults).GetEnumerator()) {
        $__name = [string]$__entry.Key
        if ($__ownerByKey.ContainsKey($__name)) {
            throw ("Defaults.ps1: duplicate key '{0}' redefined in {1} (first defined in {2})." `
                -f $__name, $__file.Name, $__ownerByKey[$__name])
        }
        $__ownerByKey[$__name] = $__file.Name
        $__merged[$__name]     = $__entry.Value
    }
}

# Expand {Key} references depth-first. $__inProgress holds the chain being expanded, so a cycle
# (a key reaching itself, directly or through others) is reported instead of growing forever.
$__refPattern = '\{([A-Za-z][A-Za-z0-9]*)\}'
$__inProgress = [ordered]@{}
$__expand = {
    param([string] $Key)
    $value = $__merged[$Key]
    if ($value -isnot [string] -or $value -notmatch $__refPattern) { return }
    if ($__inProgress.Contains($Key)) {
        throw ("Defaults.ps1: circular {{Key}} reference: {0} -> {1}." -f (@($__inProgress.Keys) -join ' -> '), $Key)
    }
    $__inProgress[$Key] = $true
    foreach ($match in [regex]::Matches($value, $__refPattern)) {
        $ref = $match.Groups[1].Value
        if (-not $__merged.Contains($ref)) {
            throw ("Defaults.ps1: {0} in {1} references unknown default '{2}'." -f $Key, $__ownerByKey[$Key], $match.Value)
        }
        & $__expand $ref
        $value = $value.Replace($match.Value, [string]$__merged[$ref])
    }
    $__merged[$Key] = $value
    $__inProgress.Remove($Key)
}
foreach ($__name in @($__merged.Keys)) { & $__expand $__name }

# Re-host the merged literal in a case-insensitive OrderedDictionary so Get-CiDefault matches
# keys without regard to casing. Enumeration order and key casing are preserved.
$Script:CiDefaultsTable = [System.Collections.Specialized.OrderedDictionary]::new(
    [System.StringComparer]::OrdinalIgnoreCase
)
foreach ($__entry in $__merged.GetEnumerator()) {
    $Script:CiDefaultsTable[[string]$__entry.Key] = $__entry.Value
}

# Surface the owning-file map for tests / diagnostics that want to inspect provenance.
$Script:CiDefaultsOwner = $__ownerByKey

# Tidy aggregation scratch.
Remove-Variable -Name '__merged','__ownerByKey','__domainFiles','__defaultsDir','__data','__entry','__name','__file',
    '__refPattern','__inProgress','__expand' `
    -Scope Script -ErrorAction SilentlyContinue
