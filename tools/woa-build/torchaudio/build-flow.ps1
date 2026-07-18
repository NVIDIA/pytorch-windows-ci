#Requires -Version 5.1
<#
.SYNOPSIS
  Job entry: build torchaudio wheels against CUDA-embedded PyTorch.

.DESCRIPTION
  Dot-sources torch/Common.ps1 (path sanitizer + log helpers) then the sibling Build.ps1 in this
  folder, which delegates to the shared Invoke-PytorchExtensionBuild pipeline.

  Expects logs/WHEEL_OUT_ROOT and cuda_embed_dlls/torch-*.whl on the runner disk
  (cuda embed job finished first), or PYTORCH_WIN_PREBUILT_WHEEL_ROOT in extensions-only mode.

.NOTES
  Log filter: `Select-String "torchaudio_"` or `[pytorch-windows-build-flow]`.
  Dot-source for tests; running as -File invokes Invoke-TorchaudioWindowsBuildFlow.
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

function Invoke-TorchaudioWindowsBuildFlow {
    <#
    .SYNOPSIS
      Pipeline body for the torchaudio extension build job. Returns 0 on success / 1 on failure.
    #>
    [CmdletBinding()]
    [OutputType([int])]
    param()

    $component = 'pytorch-windows-build-flow'
    try {
        Write-CiPhase -State 'START' -Phase 'torchaudio_script_entry' -Component $component
        Write-CiSubsetHeader -Component $component -Extra @{
            EXT = 'torchaudio'
            CI_PROJECT_DIR = (Resolve-CiEnv -Name 'CI_PROJECT_DIR')
        }
        Assert-CiWorkflowPrereqs -Role Extension -Component $component
        Repair-ProcessPathForWheelBuild
        Invoke-TorchaudioWindowsBuild
        Write-CiPhase -State 'PASS' -Phase 'torchaudio_pipeline_complete' -Component $component
        return 0
    }
    catch {
        Write-CiPhase -State 'FAIL' -Phase 'torchaudio_pipeline_exception' -Component $component -Detail $_.Exception.Message
        Write-Host $_
        return 1
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    # Reduce the flow's polluted success stream (venv / pip install / git clone / pip wheel stdout)
    # to the trailing return value. A bare `exit (Invoke-...)` coerces the whole array to 0 and
    # masks a failed `pip wheel` (return 1) as a green job - the extension false-positive bug.
    exit (Resolve-BuildFlowExitCode (Invoke-TorchaudioWindowsBuildFlow))
}
