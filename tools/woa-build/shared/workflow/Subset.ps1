#Requires -Version 5.1
<#
.SYNOPSIS
  Get-CiBuildFlowSubset — single decision point for which wheel-build subset a job should run.

.DESCRIPTION
  Carved out of CiWorkflow.ps1. Reads three mutually-orthogonal env vars in priority order and
  returns one of Both / VanillaOnly / CudaEmbedOnly / SkipWheel. Do NOT add ad-hoc skip flags in
  flow scripts — extend this enum so every consumer sees the same classification.

  Dependencies (dot-sourced):
    * ../env/EnvResolve.ps1 - Test-EnvTruthy
#>

. (Join-Path $PSScriptRoot '..' 'env' 'EnvResolve.ps1')

function Get-CiBuildFlowSubset {
    <#
    .SYNOPSIS
      Decide which wheel stages the PyTorch build flow should run.

    .DESCRIPTION
      Reads three mutually-orthogonal env toggles and returns a single canonical token. Priority
      runs from most explicit (SkipWheel) downward so a "diagnostic only" run never accidentally
      builds anything when paired with a stage-selector flag.

      Returns one of:
        Both          - default; run vanilla + cuda_embed.
        VanillaOnly   - PYTORCH_WIN_BUILD_VANILLA_ONLY=true.
        CudaEmbedOnly - PYTORCH_WIN_BUILD_CUDA_EMBED_ONLY=true.
        SkipWheel     - PYTORCH_WIN_BUILD_SKIP_WHEEL=true; diagnostics only, no pip wheel.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param()

    if (Test-EnvTruthy -Name 'PYTORCH_WIN_BUILD_SKIP_WHEEL')      { return 'SkipWheel' }
    if (Test-EnvTruthy -Name 'PYTORCH_WIN_BUILD_VANILLA_ONLY')    { return 'VanillaOnly' }
    if (Test-EnvTruthy -Name 'PYTORCH_WIN_BUILD_CUDA_EMBED_ONLY') { return 'CudaEmbedOnly' }
    return 'Both'
}
