# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

<#
.SYNOPSIS
  PATH-sanitization helpers used before pip wheel on Windows runners.

.DESCRIPTION
  Repair-ProcessPathForWheelBuild strips quotes and line-breaks from PATH segments so cmd.exe
  and vcvars.bat do not misparse a polluted runner environment.

  Dot-sources shared/env/All.ps1 so callers that source only this file still see the standard
  env helpers (Set-CiEnv, Resolve-CiEnv, Test-EnvTruthy). The build-flow scripts rely on that
  ordering; new helpers should prefer dot-sourcing the targeted env/log files directly.
#>

. (Join-Path $PSScriptRoot '..\shared\env\All.ps1')

function Get-WheelBuildSanitizedPathString {
    <#
    .SYNOPSIS
      Return PATH with segments cleaned for cmd.exe / vcvars: no double-quotes, no line breaks,
      trimmed.

    .PARAMETER PathString
      The PATH value to clean (defaults to $env:PATH for convenience in one-off use).
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([string] $PathString = $(if ($null -ne $env:PATH) { $env:PATH } else { '' }))

    if ([string]::IsNullOrWhiteSpace($PathString)) {
        return ''
    }
    $segments = [System.Collections.Generic.List[string]]::new()
    foreach ($raw in $PathString.Split([char]';', [System.StringSplitOptions]::None)) {
        $clean = ($raw -replace '"', '') -creplace "[\r\n]+", ''
        $clean = $clean.Trim()
        if (-not [string]::IsNullOrWhiteSpace($clean)) {
            $segments.Add($clean) | Out-Null
        }
    }
    if ($segments.Count -eq 0) {
        return ''
    }
    return $segments -join ';'
}

function Repair-ProcessPathForWheelBuild {
    <#
    .SYNOPSIS
      Mutates the process PATH so inherited garbage (e.g. stray quotes from broken installers) does
      not break cmd.exe and vcvars.bat during pip wheel.
    #>
    Set-CiEnv -Name 'PATH' -Value (Get-WheelBuildSanitizedPathString) | Out-Null
}
