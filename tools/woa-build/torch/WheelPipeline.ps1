<#
.SYNOPSIS
  PyTorch Windows wheel stages: vanilla pip wheel, then CUDA runtime wheel via second pip wheel.

.DESCRIPTION
  Dot-sources ResolveTorchWheel (WHEEL_OUT_ROOT marker, wheel path helpers) and CudaDelveAddPath
  (Copy-CudaRuntimeDllsIntoTorchLib). After the vanilla wheel, copies CUDA/cuDNN/CUPTI *.dll into
  build\lib.*\torch\lib and runs pip wheel again into cuda_embed_dlls/ (no delvewheel).

  Output layout under PYTORCH_WIN_BUILD_WHEEL_OUT_DIR/<yyyy_MM_dd>/:
  - torch-*.whl (vanilla) at dated root
  - cuda_embed_dlls/torch-*.whl plus torchaudio/torchvision *.whl (extensions build against embed
    torch; wheels co-located)
#>

. (Join-Path $PSScriptRoot '..\shared\env\All.ps1')
. (Join-Path $PSScriptRoot '..\shared\log\Phase.ps1')
. (Join-Path $PSScriptRoot '..\shared\log\VariantSuffix.ps1')
. (Join-Path $PSScriptRoot '..\shared\workflow\LoggedExec.ps1')
. (Join-Path $PSScriptRoot '..\shared\build\ResolveTorchWheel.ps1')
. (Join-Path $PSScriptRoot '..\shared\build\CudaDelveAddPath.ps1')

function Write-WheelOutRootMarker {
    <#
    .SYNOPSIS
      Persist dated wheel root to logs/WHEEL_OUT_ROOT for downstream jobs.
    #>
    param([Parameter(Mandatory)][string] $WheelOutRoot)
    $logsDir = Get-ExtensionLogsDir
    New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
    $marker = Join-Path $logsDir "WHEEL_OUT_ROOT"
    Set-Content -LiteralPath $marker -Value $WheelOutRoot.TrimEnd() -Encoding utf8 -NoNewline
    Write-Host "Wrote WHEEL_OUT_ROOT marker (extensions + cuda embed wheel read this): $marker"
}

function Invoke-PytorchWindowsWheelVanilla {
    <#
    .SYNOPSIS
      MSVC/CUDA env, pip wheel PyTorch checkout into a new dated directory, write WHEEL_OUT_ROOT marker.

    .PARAMETER CheckoutRoot
      CHECKOUT_ROOT: cloned pytorch tree.
    #>
    param([Parameter(Mandatory)][string] $CheckoutRoot)

    Initialize-PytorchWindowsCompilerAndBuildEnvironment

    Write-CiPhase -State "START" -Phase "wheel_output_dir"
    $wheelDir = Resolve-CiEnv -Name "PYTORCH_WIN_BUILD_WHEEL_OUT_DIR" -Default (Get-CiDefault WheelOutDir)
    New-Item -ItemType Directory -Path $wheelDir -Force | Out-Null
    # One pipeline-wide date (CI_PIPELINE_CREATED_AT) so wheels + triage report share a dated folder
    # even when tests finish after a UTC/day rollover. See shared/env/PipelineDate.ps1.
    $dateStamp = Get-CiPipelineDateStamp
    $wheelOutRoot = Join-Path $wheelDir $dateStamp
    New-Item -ItemType Directory -Path $wheelOutRoot -Force | Out-Null
    Write-CiPhase -State "PASS" -Phase "wheel_output_dir" -Detail $wheelOutRoot

    $logsDir = Get-ExtensionLogsDir
    New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
    $pipLog = Join-Path $logsDir ("pip-wheel{0}.log" -f (Get-VariantLogSuffix))

    Write-CiPhase -State "START" -Phase "pip_wheel" -Detail "vanilla wheel - $wheelOutRoot"
    Write-Host "pip wheel (vanilla) log: $pipLog"
    $wdEsc = $wheelOutRoot.Replace('"', '""')
    try {
        Invoke-CmdLogged `
            -Command ("python -m pip wheel . --no-deps --no-build-isolation -v -w `"$wdEsc`"") `
            -LogPath $pipLog `
            -WorkingDirectory $CheckoutRoot `
            -FailureMessage "pip wheel (vanilla) failed"
    }
    catch {
        Write-CiPhase -State "FAIL" -Phase "pip_wheel" -Detail $_.Exception.Message
        throw
    }
    Write-CiPhase -State "PASS" -Phase "pip_wheel"

    Write-WheelOutRootMarker -WheelOutRoot $wheelOutRoot

    Write-CiPhase -State "START" -Phase "verify_wheels_vanilla"
    $vanillaWheels = @(Get-ChildItem -LiteralPath $wheelOutRoot -Filter "*.whl" -File -ErrorAction SilentlyContinue)
    Write-Host "Vanilla wheels ($wheelOutRoot):"
    $vanillaWheels | Format-Table Name, Length, LastWriteTime -AutoSize
    if ($vanillaWheels.Count -lt 1) {
        Write-CiPhase -State "FAIL" -Phase "verify_wheels_vanilla" -Detail "no .whl in dated root $wheelOutRoot"
        throw "No vanilla .whl files under $wheelOutRoot"
    }
    Write-CiPhase -State "PASS" -Phase "verify_wheels_vanilla" -Detail "count=$($vanillaWheels.Count)"
}

function Get-PytorchBuildTorchLibDir {
    <#
    .SYNOPSIS
      Locate build\lib.win-arm64-cpython-*\torch\lib under the PyTorch checkout (CUDA DLL staging
      + wheel pack).
    #>
    param([Parameter(Mandatory)][string] $CheckoutRoot)
    $buildDir = Join-Path $CheckoutRoot "build"
    if (-not (Test-Path -LiteralPath $buildDir)) {
        return $null
    }
    $platDirs = @(
        Get-ChildItem -LiteralPath $buildDir -Directory -Filter "lib.win-arm64-cpython-*" -ErrorAction SilentlyContinue |
            Sort-Object -Property LastWriteTime -Descending
    )
    foreach ($p in $platDirs) {
        $lib = Join-Path $p.FullName "torch\lib"
        if (Test-Path -LiteralPath $lib) {
            return $lib
        }
    }
    return $null
}

function Invoke-PytorchWindowsWheelCudaEmbed {
    <#
    .SYNOPSIS
      Stage CUDA DLLs into build torch\lib, then pip wheel again into cuda_embed_dlls/ (same job
      as vanilla).

    .PARAMETER CheckoutRoot
      Same CHECKOUT_ROOT as vanilla stage; build\lib.*\torch\lib must exist after the first
      pip wheel.

    .PARAMETER WheelOutRoot
      Optional dated wheel directory (parent of cuda_embed_dlls/). If omitted,
      Read-WheelOutRootFromLogs is used.

    .PARAMETER VanillaTorchWheelPath
      Optional explicit path to the vanilla torch-*.whl (used only to infer WheelOutRoot when
      unset).
    #>
    param(
        [Parameter(Mandatory)][string] $CheckoutRoot,
        [string] $WheelOutRoot,
        [string] $VanillaTorchWheelPath
    )

    if (-not [string]::IsNullOrWhiteSpace($VanillaTorchWheelPath)) {
        if (-not (Test-Path -LiteralPath $VanillaTorchWheelPath)) {
            throw "VanillaTorchWheelPath does not exist: $VanillaTorchWheelPath"
        }
        if ([string]::IsNullOrWhiteSpace($WheelOutRoot)) {
            $WheelOutRoot = Split-Path -LiteralPath $VanillaTorchWheelPath -Parent
        }
    }
    if ([string]::IsNullOrWhiteSpace($WheelOutRoot)) {
        $WheelOutRoot = Read-WheelOutRootFromLogs
    }
    $WheelOutRoot = $WheelOutRoot.TrimEnd('\', '/')

    if (-not (Test-Path -LiteralPath $WheelOutRoot)) {
        throw "WheelOutRoot path does not exist: $WheelOutRoot"
    }
    Write-CiPhase -State "PASS" -Phase "wheel_output_dir_reuse" -Detail $WheelOutRoot

    if (-not [string]::IsNullOrWhiteSpace($VanillaTorchWheelPath)) {
        $vanillaTorchWhl = [System.IO.Path]::GetFullPath($VanillaTorchWheelPath.Trim())
    }
    else {
        $vanillaTorchWhl = Get-PrimaryTorchWheelPath -WheelOutRoot $WheelOutRoot
    }
    Write-CiPhase -State "PASS" -Phase "cuda_embed_reference_vanilla_wheel" -Detail $vanillaTorchWhl

    $cudaRaw = Resolve-CiEnv -Name "PYTORCH_WIN_BUILD_CUDA_PATH"
    if ([string]::IsNullOrWhiteSpace($cudaRaw)) {
        throw "PYTORCH_WIN_BUILD_CUDA_PATH is not set; cannot stage CUDA DLLs into torch\lib"
    }
    $cudaRaw = $cudaRaw.TrimEnd('\', '/')

    $torchLibDir = Get-PytorchBuildTorchLibDir -CheckoutRoot $CheckoutRoot
    if ([string]::IsNullOrWhiteSpace($torchLibDir)) {
        throw (
            "CUDA embed pip wheel: no build\lib.win-arm64-cpython-*\torch\lib under CHECKOUT_ROOT " +
            "(vanilla pip wheel must complete in this job first)."
        )
    }

    Write-CiPhase -State "START" -Phase "cuda_dll_stage_torch_lib" -Detail $torchLibDir
    Copy-CudaRuntimeDllsIntoTorchLib -TorchLibDir $torchLibDir -CudaPathRaw $cudaRaw
    Write-CiPhase -State "PASS" -Phase "cuda_dll_stage_torch_lib"

    $embeddedWheelDir = Join-Path $WheelOutRoot "cuda_embed_dlls"
    New-Item -ItemType Directory -Path $embeddedWheelDir -Force | Out-Null

    $logsDir = Get-ExtensionLogsDir
    New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
    $pipLogCuda = Join-Path $logsDir ("pip-wheel-cuda-embed{0}.log" -f (Get-VariantLogSuffix))

    Write-CiPhase -State "START" -Phase "pip_wheel_cuda_embed" -Detail $embeddedWheelDir
    Initialize-PytorchWindowsCompilerAndBuildEnvironment
    Write-Host "pip wheel (CUDA embed rebuild - cuda_embed_dlls) log: $pipLogCuda"
    $woEsc = $embeddedWheelDir.Replace('"', '""')
    try {
        Invoke-CmdLogged `
            -Command ("python -m pip wheel . --no-deps --no-build-isolation -v -w `"$woEsc`"") `
            -LogPath $pipLogCuda `
            -WorkingDirectory $CheckoutRoot `
            -FailureMessage "pip wheel (CUDA embed) failed"
    }
    catch {
        Write-CiPhase -State "FAIL" -Phase "pip_wheel_cuda_embed" -Detail $_.Exception.Message
        throw
    }
    Write-CiPhase -State "PASS" -Phase "pip_wheel_cuda_embed"

    Write-CiPhase -State "START" -Phase "verify_wheels_cuda_embed"
    $embeddedWheels = @(Get-ChildItem -LiteralPath $embeddedWheelDir -Filter "*.whl" -File -ErrorAction SilentlyContinue)
    Write-Host "CUDA embed wheels ($embeddedWheelDir):"
    $embeddedWheels | Format-Table Name, Length, LastWriteTime -AutoSize
    if ($embeddedWheels.Count -lt 1) {
        Write-CiPhase -State "FAIL" -Phase "verify_wheels_cuda_embed" -Detail "no .whl under $embeddedWheelDir"
        throw "No cuda-embed .whl files under $embeddedWheelDir"
    }
    Write-CiPhase -State "PASS" -Phase "verify_wheels_cuda_embed" -Detail "count=$($embeddedWheels.Count)"
}
