# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

#Requires -Version 5.1
<#
.SYNOPSIS
  Job entry: build torchvision wheels against CUDA-embedded PyTorch.

.DESCRIPTION
  Same loader order as torchaudio/build-flow.ps1; implements Invoke-TorchvisionWindowsBuild.

.NOTES
  Log filter: `Select-String "torchvision_"` or `[pytorch-windows-build-flow]`.
  Dot-source for tests; running as -File invokes Invoke-TorchvisionWindowsBuildFlow.
#>

$ErrorActionPreference = 'Stop'

$win = Split-Path -Parent $PSScriptRoot
. (Join-Path $win 'shared\env\All.ps1')
. (Join-Path $win 'shared\log\Phase.ps1')
. (Join-Path $win 'shared\log\Header.ps1')
. (Join-Path $win 'shared\workflow\Prereqs.ps1')
. (Join-Path $win 'shared\workflow\FlowExitCode.ps1')
. (Join-Path $win 'torch\Common.ps1')
. (Join-Path $PSScriptRoot 'Build.ps1')

function Invoke-TorchvisionWindowsBuildFlow {
    <#
    .SYNOPSIS
      Pipeline body for the torchvision extension build job. Returns 0 on success / 1 on failure.
    #>
    [CmdletBinding()]
    [OutputType([int])]
    param()

    $component = 'pytorch-windows-build-flow'
    try {
        Write-CiPhase -State 'START' -Phase 'torchvision_script_entry' -Component $component
        Write-CiSubsetHeader -Component $component -Extra @{
            EXT = 'torchvision'
            CI_PROJECT_DIR = (Resolve-CiEnv -Name 'CI_PROJECT_DIR')
        }
        Assert-CiWorkflowPrereqs -Role Extension -Component $component
        Repair-ProcessPathForWheelBuild
        Invoke-TorchvisionWindowsBuild
        Write-CiPhase -State 'PASS' -Phase 'torchvision_pipeline_complete' -Component $component
        return 0
    }
    catch {
        Write-CiPhase -State 'FAIL' -Phase 'torchvision_pipeline_exception' -Component $component -Detail $_.Exception.Message
        Write-Host $_
        return 1
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    # Reduce the flow's polluted success stream (venv / pip install / git clone / pip wheel stdout)
    # to the trailing return value. A bare `exit (Invoke-...)` coerces the whole array to 0 and
    # masks a failed `pip wheel` (return 1) as a green job - the extension false-positive bug.
    exit (Resolve-BuildFlowExitCode (Invoke-TorchvisionWindowsBuildFlow))
}
