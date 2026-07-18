# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

<#
.SYNOPSIS
  Resolve dated wheel output root and PyTorch wheel paths for extension jobs.

.DESCRIPTION
  The PyTorch Windows build job writes logs/WHEEL_OUT_ROOT (single line: absolute path to the dated
  directory under PYTORCH_WIN_BUILD_WHEEL_OUT_DIR). Extension jobs (torchaudio, torchvision) read
  that marker from GitHub Actions artifacts, then install torch from:
    - CUDA-embedded wheel: torch-*.whl under <dated root>/cuda_embed_dlls/ (Get-CudaEmbeddedTorchWheelPath).
    - Vanilla torch at the dated root (Get-PrimaryTorchWheelPath) is used by other stages
      (e.g. second pip wheel input).

  Extension jobs write torchaudio/torchvision wheels under <dated root>/cuda_embed_dlls/.

.NOTES
  Get-ExtensionLogsDir uses CI_PROJECT_DIR/logs when set (in CI), else the current directory.
#>

. (Join-Path $PSScriptRoot '..' 'env' 'EnvResolve.ps1')
. (Join-Path $PSScriptRoot '..' 'log' 'Phase.ps1')

function Get-ExtensionLogsDir {
    <#
    .SYNOPSIS
      Directory where CI collects logs (WHEEL_OUT_ROOT marker, pip logs).
    #>
    [OutputType([string])]
    param()
    $p = Resolve-CiEnv -Name 'CI_PROJECT_DIR'
    if ([string]::IsNullOrWhiteSpace($p)) {
        $p = (Get-Location).ProviderPath
    }
    return (Join-Path $p 'logs')
}

function Read-WheelOutRootFromLogs {
    <#
    .SYNOPSIS
      Resolve the dated wheel directory: from logs/WHEEL_OUT_ROOT (normal pipeline), or from
      PYTORCH_WIN_PREBUILT_WHEEL_ROOT when PYTORCH_WIN_EXTENSIONS_ONLY is set (skip main PyTorch job).
    #>
    [OutputType([string])]
    param()

    if (Test-EnvTruthy 'PYTORCH_WIN_EXTENSIONS_ONLY') {
        $prebuilt = Resolve-CiEnv -Name 'PYTORCH_WIN_PREBUILT_WHEEL_ROOT'
        if ([string]::IsNullOrWhiteSpace($prebuilt)) {
            throw (
                'PYTORCH_WIN_EXTENSIONS_ONLY is set but PYTORCH_WIN_PREBUILT_WHEEL_ROOT is empty. ' +
                'Set it to the absolute dated wheel directory on the runner (must contain cuda_embed_dlls/torch-*.whl).'
            )
        }
        if (-not (Test-Path -LiteralPath $prebuilt)) {
            throw "PYTORCH_WIN_PREBUILT_WHEEL_ROOT does not exist: $prebuilt"
        }
        $null = Get-CudaEmbeddedTorchWheelPath -WheelOutRoot $prebuilt

        $logsDir = Get-ExtensionLogsDir
        New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
        $marker = Join-Path $logsDir 'WHEEL_OUT_ROOT'
        Set-Content -LiteralPath $marker -Value $prebuilt -Encoding utf8
        Write-CiPhase -State 'INFO' -Phase 'extensions_only_marker' -Component 'resolve-torch-wheel' `
            -Detail "PYTORCH_WIN_EXTENSIONS_ONLY: using prebuilt wheel root $prebuilt (wrote $marker)"
        return $prebuilt
    }

    $marker = Join-Path (Get-ExtensionLogsDir) 'WHEEL_OUT_ROOT'
    if (-not (Test-Path -LiteralPath $marker)) {
        throw (
            "Missing $marker; run the PyTorch wheel build job first " +
            'or set PYTORCH_WIN_EXTENSIONS_ONLY + PYTORCH_WIN_PREBUILT_WHEEL_ROOT.'
        )
    }
    $raw = (Get-Content -LiteralPath $marker -Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($raw)) {
        throw "WHEEL_OUT_ROOT marker is empty: $marker"
    }
    return $raw
}

function Get-PrimaryTorchWheelPath {
    <#
    .SYNOPSIS
      Path to the newest torch-*.whl in the dated wheel root (vanilla build output).

    .PARAMETER WheelOutRoot
      Dated directory (content of WHEEL_OUT_ROOT), not the parent builds folder.
    #>
    [OutputType([string])]
    param([Parameter(Mandatory)][string] $WheelOutRoot)

    $candidates = @(
        Get-ChildItem -LiteralPath $WheelOutRoot -Filter 'torch-*.whl' -File -ErrorAction SilentlyContinue |
            Sort-Object -Property LastWriteTime -Descending
    )
    if ($candidates.Count -lt 1) {
        throw "No torch-*.whl file in dated wheel root: $WheelOutRoot"
    }
    return $candidates[0].FullName
}

function Get-CudaEmbeddedTorchWheelPath {
    <#
    .SYNOPSIS
      Path to the newest torch-*.whl under cuda_embed_dlls/ (second pip wheel after CUDA DLL staging).

    .PARAMETER WheelOutRoot
      Same dated root as Read-WheelOutRootFromLogs; embed wheels live in a subdirectory.
    #>
    [OutputType([string])]
    param([Parameter(Mandatory)][string] $WheelOutRoot)

    $embedDir = Join-Path $WheelOutRoot 'cuda_embed_dlls'
    if (-not (Test-Path -LiteralPath $embedDir)) {
        throw "Missing cuda_embed_dlls under $WheelOutRoot (PyTorch build must produce CUDA torch wheel there)."
    }
    $candidates = @(
        Get-ChildItem -LiteralPath $embedDir -Filter 'torch-*.whl' -File -ErrorAction SilentlyContinue |
            Sort-Object -Property LastWriteTime -Descending
    )
    if ($candidates.Count -lt 1) {
        throw "No torch-*.whl under $embedDir"
    }
    return $candidates[0].FullName
}
