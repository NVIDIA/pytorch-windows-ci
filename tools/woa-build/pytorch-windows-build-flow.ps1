#Requires -Version 5.1
<#
.SYNOPSIS
  Windows ARM64 PyTorch source build: checkout inspect, MSVC / CUDA env, vanilla pip wheel + second
  pip wheel for CUDA DLLs.

.DESCRIPTION
  Entry: bash run-with-checkout.sh bash windows/pytorch-windows-build-pipeline.sh (sets CHECKOUT_ROOT).

  Dot-sources: torch/Common.ps1 (Write-CiPhase), CompilerAndBuildEnv.ps1 (vcvars + CUDA build env),
  WheelPipeline.ps1 (vanilla wheel, stage CUDA DLLs into torch\lib, pip wheel -> cuda_embed_dlls/).
  ResolveTorchWheel is loaded transitively from WheelPipeline.

  Phases (grep "[pytorch-windows-build-flow]"): resolve checkout, optional venv, git inspect, then
  Invoke-PytorchWindowsWheelVanilla then Invoke-PytorchWindowsWheelCudaEmbed (same CHECKOUT_ROOT).

  Build-flow subset (Get-CiBuildFlowSubset):
    Both          - default. Run vanilla wheel + cuda_embed wheel.
    VanillaOnly   - PYTORCH_WIN_BUILD_VANILLA_ONLY=true. Vanilla pip wheel only.
    CudaEmbedOnly - PYTORCH_WIN_BUILD_CUDA_EMBED_ONLY=true. cuda_embed wheel only (reuses CHECKOUT_ROOT).
    SkipWheel     - PYTORCH_WIN_BUILD_SKIP_WHEEL=true. Diagnostics only; both wheel stages skipped.

  PYTORCH_WIN_BUILD_SKIP_CUDA_EMBED=1 is an additional toggle: when build-flow subset=Both, runs
  vanilla but skips the second pip wheel.

  Env inputs mapped by the workflow.

.NOTES
  Phase tags help correlate failures with the last successful step in the CI job logs.
  Dot-source for tests; running as -File invokes Invoke-PytorchWindowsBuildFlow.
#>

$ErrorActionPreference = 'Stop'

$buildHelpers = Join-Path $PSScriptRoot 'torch'
. (Join-Path $PSScriptRoot 'shared\env\All.ps1')
. (Join-Path $PSScriptRoot 'shared\log\Phase.ps1')
. (Join-Path $PSScriptRoot 'shared\log\Header.ps1')
. (Join-Path $PSScriptRoot 'shared\workflow\Subset.ps1')
. (Join-Path $PSScriptRoot 'shared\workflow\Prereqs.ps1')
. (Join-Path $PSScriptRoot 'shared\workflow\LoggedExec.ps1')
. (Join-Path $PSScriptRoot 'shared\workflow\FlowExitCode.ps1')
. (Join-Path $PSScriptRoot 'shared\build\RepoBuildMetadata.ps1')
. (Join-Path $buildHelpers   'Common.ps1')
. (Join-Path $buildHelpers   'CompilerAndBuildEnv.ps1')
. (Join-Path $buildHelpers   'WheelPipeline.ps1')

function Invoke-PytorchWindowsBuildFlow {
    <#
    .SYNOPSIS
      Pipeline body for pytorch_windows_build_vanilla / pytorch_windows_build_cuda_embed jobs.
      Returns the exit code (0 = success, 1 = failure).
    #>
    [CmdletBinding()]
    [OutputType([int])]
    param()

    $component = 'pytorch-windows-build-flow'
    try {
        Write-CiPhase -State 'START' -Phase 'script_entry' -Component $component

        Write-CiSubsetHeader -Component $component -Extra @{
            CHECKOUT_ROOT = (Resolve-CiEnv -Name 'CHECKOUT_ROOT')
            CHECKOUT_REUSE_EXISTING = (Resolve-CiEnv -Name 'CHECKOUT_REUSE_EXISTING')
        }

        $buildSubset = Get-CiBuildFlowSubset
        $skipWheel = ($buildSubset -eq 'SkipWheel')

        Write-CiPhase -State 'START' -Phase 'path_sanitize_process' -Component $component
        Repair-ProcessPathForWheelBuild
        Write-CiPhase -State 'PASS' -Phase 'path_sanitize_process' -Component $component

        Write-CiPhase -State 'START' -Phase 'resolve_checkout_root' -Component $component
        $checkoutRoot = Resolve-CiEnv -Name 'CHECKOUT_ROOT'
        if ([string]::IsNullOrWhiteSpace($checkoutRoot)) {
            Write-CiPhase -State 'FAIL' -Phase 'resolve_checkout_root' -Component $component -Detail 'CHECKOUT_ROOT unset'
            throw 'CHECKOUT_ROOT is not set. Run via bash ci/scripts/run-with-checkout.sh (see windows/pytorch-windows-build-pipeline.sh).'
        }
        Assert-CiWorkflowPrereqs -Role Build -Component $component
        Write-CiPhase -State 'PASS' -Phase 'resolve_checkout_root' -Component $component -Detail $checkoutRoot

        if ($skipWheel) {
            Write-CiPhase -State 'SKIP' -Phase 'wheel_build' -Component $component -Detail 'PYTORCH_WIN_BUILD_SKIP_WHEEL is set'
            $proj = Resolve-CiEnv -Name 'CI_PROJECT_DIR'
            if (-not [string]::IsNullOrWhiteSpace($proj)) {
                $ld = Join-Path $proj 'logs'
                New-Item -ItemType Directory -Path $ld -Force | Out-Null
                Set-Content -LiteralPath (Join-Path $ld 'pip-wheel-skipped.txt') `
                    -Value 'PYTORCH_WIN_BUILD_SKIP_WHEEL set.' -Encoding utf8
            }
        }

        Write-CiPhase -State 'START' -Phase 'git_cd_checkout' -Component $component
        Set-Location -LiteralPath $checkoutRoot
        Write-CiPhase -State 'PASS' -Phase 'git_cd_checkout' -Component $component

        Write-CiPhase -State 'START' -Phase 'git_inspect_head' -Component $component
        $head = git rev-parse HEAD
        Write-Host "HEAD: $head"
        Write-CiPhase -State 'PASS' -Phase 'git_inspect_head' -Component $component -Detail $head

        Write-CiPhase -State 'START' -Phase 'repo_metadata_pytorch' -Component $component
        $proj = Resolve-CiEnv -Name 'CI_PROJECT_DIR'
        $logsMeta = if (-not [string]::IsNullOrWhiteSpace($proj)) {
            Join-Path $proj 'logs'
        }
        else {
            Join-Path (Get-Location).ProviderPath 'logs'
        }
        # Mirror the clone precedence in checkout-repo.sh (CHECKOUT_BRANCH wins over
        # PYTORCH_BUILD_BRANCH) so build-metadata.json records the branch actually built.
        # PYTORCH_BUILD_BRANCH defaults to 'main', so checking it
        # first would mask a CHECKOUT_BRANCH-only override and record 'main' for a non-main build.
        $branchHint = Resolve-CiEnv -Name 'CHECKOUT_BRANCH'
        if ([string]::IsNullOrWhiteSpace($branchHint)) {
            $branchHint = Resolve-CiEnv -Name 'PYTORCH_BUILD_BRANCH'
        }
        Export-RepoMetadataSidecar -CanonicalName 'pytorch' -GitRoot $checkoutRoot -LogsDir $logsMeta `
            -ExtraFields @{ build_branch_env = $branchHint }
        Write-CiPhase -State 'PASS' -Phase 'repo_metadata_pytorch' -Component $component

        Write-CiPhase -State 'START' -Phase 'git_inspect_arm64_tree' -Component $component
        $armRel = Join-Path $checkoutRoot '.ci\pytorch\windows\arm64'
        if (Test-Path -LiteralPath $armRel) {
            Get-ChildItem -LiteralPath $armRel -Recurse -File | ForEach-Object { $_.FullName }
            Write-CiPhase -State 'PASS' -Phase 'git_inspect_arm64_tree' -Component $component -Detail $armRel
        }
        else {
            Write-Host "Missing: $armRel"
            Write-CiPhase -State 'PASS' -Phase 'git_inspect_arm64_tree' -Component $component -Detail 'path missing (non-fatal)'
        }

        Write-CiPhase -State 'START' -Phase 'venv_activate' -Component $component
        $venvActivate = Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_VENV_ACTIVATE' -Default (Get-CiDefault BuildVenvActivate)
        if (Test-Path -LiteralPath $venvActivate) {
            Write-Host "Activating: $venvActivate"
            . $venvActivate
            Write-CiPhase -State 'PASS' -Phase 'venv_activate' -Component $component -Detail $venvActivate
        }
        else {
            Write-Warning "Venv activate not found: $venvActivate"
            if (-not $skipWheel) {
                Write-CiPhase -State 'FAIL' -Phase 'venv_activate' -Component $component -Detail 'missing and wheel not skipped'
                throw 'PYTORCH_WIN_BUILD_VENV_ACTIVATE missing and PYTORCH_WIN_BUILD_SKIP_WHEEL not set'
            }
            Write-CiPhase -State 'SKIP' -Phase 'venv_activate' -Component $component -Detail 'not found; wheel skipped'
        }

        Write-CiPhase -State 'START' -Phase 'python_probe' -Component $component
        Get-Command python -ErrorAction SilentlyContinue | Format-List *
        python --version
        Write-CiPhase -State 'PASS' -Phase 'python_probe' -Component $component

        Write-CiPhase -State 'START' -Phase 'toolchain_metadata' -Component $component
        Export-ToolchainEnvironmentSidecar -LogsDir $logsMeta
        Write-CiPhase -State 'PASS' -Phase 'toolchain_metadata' -Component $component

        Write-CiPhase -State 'START' -Phase 'env_dump_process' -Component $component
        Get-ChildItem Env: | Sort-Object Name | Format-Table -AutoSize -Wrap
        Write-CiPhase -State 'PASS' -Phase 'env_dump_process' -Component $component

        Invoke-PytorchWindowsWheelStages -CheckoutRoot $checkoutRoot -BuildSubset $buildSubset -Component $component

        Write-CiPhase -State 'PASS' -Phase 'pipeline_complete' -Component $component
        return 0
    }
    catch {
        Write-CiPhase -State 'FAIL' -Phase 'pipeline_exception' -Component $component -Detail $_.Exception.Message
        Write-Host $_
        return 1
    }
}

function Invoke-PytorchWindowsWheelStages {
    <#
    .SYNOPSIS
      Run the wheel stage(s) appropriate for the active build-flow subset.

    .PARAMETER BuildSubset
      One of: Both, VanillaOnly, CudaEmbedOnly, SkipWheel.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $CheckoutRoot,
        [Parameter(Mandatory)][string] $BuildSubset,
        [Parameter(Mandatory)][string] $Component
    )

    if ($BuildSubset -eq 'SkipWheel') {
        Write-CiPhase -State 'SKIP' -Phase 'pip_wheel_vanilla' -Component $Component -Detail 'subset=SkipWheel'
        Write-CiPhase -State 'SKIP' -Phase 'cuda_embed_second_pip_wheel' -Component $Component -Detail 'subset=SkipWheel'
        return
    }

    if ($BuildSubset -in @('Both', 'VanillaOnly')) {
        Invoke-PytorchWindowsWheelVanilla -CheckoutRoot $CheckoutRoot
    }
    else {
        Write-CiPhase -State 'SKIP' -Phase 'pip_wheel_vanilla' -Component $Component -Detail "subset=$BuildSubset"
    }

    $skipCudaEmbed = (Test-EnvTruthy 'PYTORCH_WIN_BUILD_SKIP_CUDA_EMBED') -or ($BuildSubset -eq 'VanillaOnly')

    if ($BuildSubset -in @('Both', 'CudaEmbedOnly') -and -not $skipCudaEmbed) {
        Invoke-PytorchWindowsWheelCudaEmbed -CheckoutRoot $CheckoutRoot
        return
    }

    $detail = if ($BuildSubset -eq 'VanillaOnly') {
        'subset=VanillaOnly'
    }
    elseif ($skipCudaEmbed) {
        'PYTORCH_WIN_BUILD_SKIP_CUDA_EMBED'
    }
    else {
        "subset=$BuildSubset"
    }
    Write-CiPhase -State 'SKIP' -Phase 'cuda_embed_second_pip_wheel' -Component $Component -Detail $detail
}

if ($MyInvocation.InvocationName -ne '.') {
    # Resolve-BuildFlowExitCode (shared/workflow/FlowExitCode.ps1) reduces the flow's polluted
    # success stream to the trailing return value; a bare `exit (Invoke-...)` would coerce the
    # whole object array to 0 and mask a `return 1`.
    exit (Resolve-BuildFlowExitCode (Invoke-PytorchWindowsBuildFlow))
}
