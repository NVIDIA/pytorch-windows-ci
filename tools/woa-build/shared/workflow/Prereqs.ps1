# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

#Requires -Version 5.1
<#
.SYNOPSIS
  Assert-CiWorkflowPrereqs — fail fast when role-specific env prereqs are missing.

.DESCRIPTION
  Carved out of CiWorkflow.ps1.

  Dependencies (dot-sourced):
    * ../env/EnvResolve.ps1 - Resolve-CiEnv
    * ../log/Phase.ps1      - Write-CiPhase
#>

. (Join-Path $PSScriptRoot '..' 'env' 'EnvResolve.ps1')
. (Join-Path $PSScriptRoot '..' 'log' 'Phase.ps1')

function Assert-CiWorkflowPrereqs {
    <#
    .SYNOPSIS
      Fail fast when role-specific env prereqs are missing.

    .DESCRIPTION
      CI sets CI_PROJECT_DIR for scheduled runs, but locally-invoked flow scripts can run
      without it. This assertion produces an actionable error early so downstream
      `Join-Path $proj …` calls don't fail later with a confusing message.

      The required-var matrix is intentionally small. Each flow already verifies its own deep
      prerequisites (vcvars path, WHEEL_OUT_ROOT marker, etc.) closer to the point of use.

    .PARAMETER Role
      One of Build, Extension, Test, Publish, Triage. Roles map to a small set of required env
      vars; unknown roles throw (ValidateSet).

    .PARAMETER Component
      Log-component tag used when emitting the structured FAIL phase line on missing prereqs.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateSet('Build', 'Extension', 'Test', 'Publish', 'Triage')]
        [string] $Role,

        [string] $Component = 'pytorch-windows-build-flow'
    )

    $required = switch ($Role) {
        'Build'     { @('CI_PROJECT_DIR', 'CHECKOUT_ROOT') }
        'Extension' { @('CI_PROJECT_DIR') }
        'Test'      { @('CI_PROJECT_DIR') }
        'Publish'   { @('CI_PROJECT_DIR') }
        'Triage'    { @('CI_PROJECT_DIR') }
    }

    $missing = New-Object System.Collections.Generic.List[string]
    foreach ($name in $required) {
        if ([string]::IsNullOrWhiteSpace((Resolve-CiEnv -Name $name))) {
            $missing.Add($name) | Out-Null
        }
    }

    if ($missing.Count -gt 0) {
        $detail = "role=$Role missing=$($missing -join ',')"
        Write-CiPhase -State 'FAIL' -Phase 'workflow_prereqs' -Component $Component -Detail $detail
        throw "Workflow prereqs for role '$Role' missing: $($missing -join ', ')"
    }

    Write-CiPhase -State 'PASS' -Phase 'workflow_prereqs' -Component $Component -Detail "role=$Role"
}
