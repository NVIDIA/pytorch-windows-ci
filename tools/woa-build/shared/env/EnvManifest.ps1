# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

#Requires -Version 5.1
<#
.SYNOPSIS
  Lazy-loaded accessor for CiEnvManifest.psd1.

.DESCRIPTION
  Carved out of CiCommon.ps1. The manifest declares every CI/CD env var this project
  understands: its type, domain, default key (if any), secret flag, and description.

  Lookup is case-insensitive (OrdinalIgnoreCase) so callers may use either the canonical
  upper-case names or the lower-case forms some CI runners hand us. Loaded once per
  session and cached in $Script:CiEnvManifest.

  No dependencies; safe to dot-source standalone.
#>

function Get-CiEnvManifestPath {
    [CmdletBinding()]
    [OutputType([string])]
    param()
    return (Join-Path $PSScriptRoot 'CiEnvManifest.psd1')
}

function Get-CiEnvManifest {
    <#
    .SYNOPSIS
      Return the merged CI env-var manifest (lazy load, cached for the session).

    .DESCRIPTION
      The manifest is a hashtable keyed by env var name. Lookup is case-insensitive
      (OrdinalIgnoreCase) so callers may use either canonical upper-case names or the
      lower-case forms some CI runners hand us. Each value is a hashtable with keys:
        Type        - 'String' | 'Bool' | 'Int' | 'Path' | 'Url' | 'Date'
        Domain      - logical grouping ('Build', 'Test', 'Publish', ...)
        DefaultKey  - $null or a key in $Script:CiDefaultsTable
        Required    - $true if downstream code throws when unset
        Secret      - $true if the resolved value should be redacted in logs
        Description - short one-line summary

      Internally re-hosted in a case-insensitive [hashtable] so Get-CiEnvManifestEntry is
      O(1) instead of the previous O(N) linear scan; that matters because Resolve-CiEnv
      consults the manifest on every call from every script.
    #>
    [CmdletBinding()]
    [OutputType([hashtable])]
    param()
    if ($null -ne $Script:CiEnvManifest) {
        return $Script:CiEnvManifest
    }
    $manifestPath = Get-CiEnvManifestPath
    if (-not (Test-Path -LiteralPath $manifestPath)) {
        throw "Get-CiEnvManifest: manifest not found at $manifestPath"
    }
    $loaded = Import-PowerShellDataFile -LiteralPath $manifestPath
    if ($null -eq $loaded -or $null -eq $loaded.Variables) {
        throw "Get-CiEnvManifest: $manifestPath has no 'Variables' table."
    }

    $insensitive = [hashtable]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in ([hashtable]$loaded.Variables).GetEnumerator()) {
        $insensitive[[string]$entry.Key] = $entry.Value
    }
    $Script:CiEnvManifest = $insensitive
    return $Script:CiEnvManifest
}

function Get-CiEnvManifestEntry {
    <#
    .SYNOPSIS
      Look up a single manifest entry by env var name (case-insensitive on the name).

    .DESCRIPTION
      Returns $null when the name is not registered. Use Test-CiEnvManifestKnown for a
      boolean check. Backed by a case-insensitive hashtable so lookup is O(1).
    #>
    [CmdletBinding()]
    [OutputType([hashtable])]
    param([Parameter(Mandatory)][string] $Name)
    $manifest = Get-CiEnvManifest
    if ($manifest.ContainsKey($Name)) {
        return [hashtable]$manifest[$Name]
    }
    return $null
}

function Test-CiEnvManifestKnown {
    <#
    .SYNOPSIS
      $true when -Name is registered in CiEnvManifest.psd1.
    #>
    [CmdletBinding()]
    [OutputType([bool])]
    param([Parameter(Mandatory)][string] $Name)
    return ($null -ne (Get-CiEnvManifestEntry -Name $Name))
}
