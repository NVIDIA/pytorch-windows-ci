# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

#Requires -Version 5.1
<#
.SYNOPSIS
  Resolve-BuildFlowExitCode - reduce a flow function's full success-stream output to a single
  integer process exit code.

.DESCRIPTION
  Every job entry flow (pytorch-windows-build-flow.ps1, torchaudio/build-flow.ps1,
  torchvision/build-flow.ps1) wraps its body in an Invoke-*Flow function that emits diagnostic
  objects to the success stream (arm64 tree listing, `python --version`, `Get-ChildItem Env:`
  dumps, and - crucially - the raw stdout of native commands like `python -m venv`,
  `python -m pip install`, `pip wheel`, and `git clone`). The function's actual result - its
  `return 0` / `return 1` - is therefore the LAST object on the pipeline, not the only one.

  A bare `exit (Invoke-*Flow)` hands `exit` that whole object array; PowerShell coerces a
  non-scalar argument to 0, so a failed build (`return 1`) silently exits 0 and the CI job
  goes green while nothing was produced. This is the "pip wheel failed (exit 1) ... Job succeeded"
  masking bug observed on the torchaudio/torchvision extension jobs.

  This reducer takes the trailing emitted value and coerces it to an int, defaulting to 1
  (fail-safe) when the output is empty or unparseable.

  Usage at every flow entry point:
    if ($MyInvocation.InvocationName -ne '.') {
        exit (Resolve-BuildFlowExitCode (Invoke-SomeWindowsBuildFlow))
    }
#>

function Resolve-BuildFlowExitCode {
    <#
    .SYNOPSIS
      Reduce whatever a flow function streamed (any/none/array) to a single [int] exit code.

    .PARAMETER FlowOutput
      Whatever Invoke-*Flow streamed to the success stream.

    .OUTPUTS
      [int] 0 on success, non-zero (>=1) otherwise; 1 when output is empty/unparseable.
    #>
    [CmdletBinding()]
    [OutputType([int])]
    param([Parameter()][AllowNull()] $FlowOutput)

    $items = @($FlowOutput)
    if ($items.Count -eq 0) { return 1 }

    $last = $items[-1]
    if ($last -is [int]) {
        return [int]$last
    }

    $parsed = 0
    if ([int]::TryParse([string]$last, [ref] $parsed)) {
        return $parsed
    }
    return 1
}
