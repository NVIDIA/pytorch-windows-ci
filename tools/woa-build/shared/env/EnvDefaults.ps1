# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

#Requires -Version 5.1
<#
.SYNOPSIS
  Get-CiDefault reader on top of the $Script:CiDefaultsTable populated by Defaults.ps1.

.DESCRIPTION
  Carved out of CiCommon.ps1. Dot-sources Defaults.ps1 so a single load gives callers both
  the populated table and the reader. Lookup is case-insensitive (Defaults.ps1 hosts the
  table in an OrdinalIgnoreCase OrderedDictionary).
#>

. (Join-Path $PSScriptRoot 'Defaults.ps1')

function Get-CiDefault {
    <#
    .SYNOPSIS
      Look up a named default registered by Defaults.ps1.

    .DESCRIPTION
      Throws when an unknown name is requested so typos surface at runtime instead of silently
      returning ''. Defaults.ps1 must have been dot-sourced from the same caller scope (this
      file does that automatically).

      Lookup is case-insensitive (Defaults.ps1 hosts the table in an OrdinalIgnoreCase ordered
      dictionary) so callers using either CamelCase or lowercase variations resolve to the same
      entry. The canonical key casing is preserved for readability of the table itself.

    .PARAMETER Name
      The default key (e.g. VcvarsBat, CudaPath). Matched case-insensitively against the keys in
      $Script:CiDefaultsTable.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)][string] $Name)

    if ($null -eq $Script:CiDefaultsTable) {
        throw "Get-CiDefault: defaults table is not loaded. Dot-source ci/scripts/windows/shared/env/Defaults.ps1 before calling."
    }
    if (-not $Script:CiDefaultsTable.Contains($Name)) {
        $known = ($Script:CiDefaultsTable.Keys -join ', ')
        throw "Get-CiDefault: unknown default '$Name'. Known defaults: $known"
    }
    return [string]$Script:CiDefaultsTable[$Name]
}
