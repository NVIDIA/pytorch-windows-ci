# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

<#
.SYNOPSIS
  PyTorch Windows wheel stages: vanilla pip wheel, then CUDA runtime wheel via second pip wheel.

.DESCRIPTION
  Dot-sources ResolveTorchWheel (WHEEL_OUT_ROOT marker, wheel path helpers) and CudaDelveAddPath
  (Copy-CudaRuntimeDllsIntoTorchLib). After the vanilla wheel, repacks it: unzip the vanilla
  torch-*.whl, copy CUDA/cuDNN/CUPTI *.dll into torch\lib inside it, regenerate RECORD, and rezip
  into cuda_embed_dlls/ (no second compile, no delvewheel). Backend-agnostic - works whether torch
  builds with setuptools or scikit-build-core (the latter stages into a temp dir it deletes after
  packing, so there is no persistent build\lib.*\torch\lib to reuse).

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

function Expand-TorchWheelArchive {
    <#
    .SYNOPSIS
      Extract a wheel (zip) into a fresh directory.
    #>
    param(
        [Parameter(Mandatory)][string] $WheelPath,
        [Parameter(Mandatory)][string] $DestDir
    )
    if (-not ('System.IO.Compression.ZipFile' -as [type])) {
        Add-Type -AssemblyName System.IO.Compression.FileSystem | Out-Null
    }
    if (Test-Path -LiteralPath $DestDir) {
        Remove-Item -LiteralPath $DestDir -Recurse -Force
    }
    New-Item -ItemType Directory -Path $DestDir -Force | Out-Null
    [System.IO.Compression.ZipFile]::ExtractToDirectory($WheelPath, $DestDir)
}

function Update-TorchWheelRecord {
    <#
    .SYNOPSIS
      Rewrite <dist-info>\RECORD for an unpacked wheel (sha256=<urlsafe-b64-nopad>,size per file,
      plus a hashless line for RECORD itself). Run after new DLLs are copied into torch\lib.
    #>
    [CmdletBinding()]
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute(
        'PSUseShouldProcessForStateChangingFunctions', '',
        Justification = 'Rewrites a single RECORD file inside a throwaway unpacked-wheel dir; a -WhatIf/-Confirm surface would be noise for an internal repack step.')]
    param([Parameter(Mandatory)][string] $WheelRootDir)
    $rootFull = [System.IO.Path]::GetFullPath($WheelRootDir).TrimEnd('\', '/')
    $distInfo = @(Get-ChildItem -LiteralPath $rootFull -Directory -Filter "*.dist-info" -ErrorAction SilentlyContinue) |
        Select-Object -First 1
    if ($null -eq $distInfo) {
        throw "Update-TorchWheelRecord: no *.dist-info directory under $rootFull"
    }
    $recordRel = "$($distInfo.Name)/RECORD"
    $recordPath = Join-Path $distInfo.FullName "RECORD"

    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $lines = [System.Collections.Generic.List[string]]::new()
        foreach ($f in (Get-ChildItem -LiteralPath $rootFull -Recurse -File)) {
            $rel = $f.FullName.Substring($rootFull.Length + 1).Replace('\', '/')
            if ($rel -eq $recordRel) { continue }
            $fs = [System.IO.File]::OpenRead($f.FullName)
            try {
                $hash = $sha.ComputeHash($fs)
                $size = $fs.Length
            }
            finally { $fs.Dispose() }
            $b64 = [Convert]::ToBase64String($hash).TrimEnd('=').Replace('+', '-').Replace('/', '_')
            $lines.Add("$rel,sha256=$b64,$size")
        }
        $lines.Add("$recordRel,,")
        [System.IO.File]::WriteAllText($recordPath, (($lines -join "`n") + "`n"), [System.Text.UTF8Encoding]::new($false))
    }
    finally { $sha.Dispose() }
}

function Compress-TorchWheelArchive {
    <#
    .SYNOPSIS
      Zip an unpacked wheel dir back into a .whl with forward-slash entry names.

    .DESCRIPTION
      Entries are added by hand rather than via ZipFile.CreateFromDirectory: on Windows PowerShell
      5.1 (.NET Framework) CreateFromDirectory writes backslash separators, which pip/the wheel
      format reject. Streaming each file keeps memory flat on the ~1 GB torch wheel.
    #>
    param(
        [Parameter(Mandatory)][string] $SourceDir,
        [Parameter(Mandatory)][string] $WheelPath
    )
    if (-not ('System.IO.Compression.ZipFile' -as [type])) {
        Add-Type -AssemblyName System.IO.Compression.FileSystem | Out-Null
    }
    if (Test-Path -LiteralPath $WheelPath) {
        Remove-Item -LiteralPath $WheelPath -Force
    }
    $srcFull = [System.IO.Path]::GetFullPath($SourceDir).TrimEnd('\', '/')
    $zip = [System.IO.Compression.ZipFile]::Open($WheelPath, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($f in (Get-ChildItem -LiteralPath $srcFull -Recurse -File)) {
            $rel = $f.FullName.Substring($srcFull.Length + 1).Replace('\', '/')
            $entry = $zip.CreateEntry($rel, [System.IO.Compression.CompressionLevel]::Optimal)
            $es = $entry.Open()
            try {
                $fs = [System.IO.File]::OpenRead($f.FullName)
                try { $fs.CopyTo($es) } finally { $fs.Dispose() }
            }
            finally { $es.Dispose() }
        }
    }
    finally { $zip.Dispose() }
}

function Invoke-PytorchWindowsWheelCudaEmbed {
    <#
    .SYNOPSIS
      Repack the vanilla torch wheel with CUDA runtime DLLs embedded into torch\lib, writing the
      result to cuda_embed_dlls/ (no recompile).

    .PARAMETER WheelOutRoot
      Optional dated wheel directory (parent of cuda_embed_dlls/). If omitted,
      Read-WheelOutRootFromLogs is used.

    .PARAMETER VanillaTorchWheelPath
      Optional explicit path to the vanilla torch-*.whl (used only to infer WheelOutRoot when
      unset).
    #>
    param(
        [string] $WheelOutRoot,
        [string] $VanillaTorchWheelPath
    )

    if (-not [string]::IsNullOrWhiteSpace($VanillaTorchWheelPath)) {
        if (-not (Test-Path -LiteralPath $VanillaTorchWheelPath)) {
            throw "VanillaTorchWheelPath does not exist: $VanillaTorchWheelPath"
        }
        if ([string]::IsNullOrWhiteSpace($WheelOutRoot)) {
            $WheelOutRoot = [System.IO.Path]::GetDirectoryName($VanillaTorchWheelPath)
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

    $embeddedWheelDir = Join-Path $WheelOutRoot "cuda_embed_dlls"
    New-Item -ItemType Directory -Path $embeddedWheelDir -Force | Out-Null

    # Repack the already-built vanilla wheel rather than copying DLLs into the build tree and
    # re-running pip wheel: modern torch builds with scikit-build-core, which stages into a temp
    # dir it deletes after packing (no persistent build\lib.*\torch\lib), and a second pip wheel
    # would be a full multi-hour recompile that discards any injected DLLs. Unzip the vanilla wheel,
    # drop the CUDA runtime DLLs into torch\lib, regenerate RECORD, and rezip - backend-agnostic and
    # no recompile.
    $unpackDir = Join-Path $WheelOutRoot "_cuda_embed_unpack"
    Write-CiPhase -State "START" -Phase "cuda_embed_unpack" -Detail $vanillaTorchWhl
    Expand-TorchWheelArchive -WheelPath $vanillaTorchWhl -DestDir $unpackDir
    Write-CiPhase -State "PASS" -Phase "cuda_embed_unpack" -Detail $unpackDir

    $torchLibDir = Join-Path $unpackDir "torch\lib"
    if (-not (Test-Path -LiteralPath $torchLibDir)) {
        throw "CUDA embed: unpacked vanilla wheel has no torch\lib ($torchLibDir); wheel=$vanillaTorchWhl"
    }

    Write-CiPhase -State "START" -Phase "cuda_dll_stage_torch_lib" -Detail $torchLibDir
    Copy-CudaRuntimeDllsIntoTorchLib -TorchLibDir $torchLibDir -CudaPathRaw $cudaRaw
    Write-CiPhase -State "PASS" -Phase "cuda_dll_stage_torch_lib"

    Write-CiPhase -State "START" -Phase "cuda_embed_record" -Detail $unpackDir
    Update-TorchWheelRecord -WheelRootDir $unpackDir
    Write-CiPhase -State "PASS" -Phase "cuda_embed_record"

    $embedWhl = Join-Path $embeddedWheelDir ([System.IO.Path]::GetFileName($vanillaTorchWhl))
    Write-CiPhase -State "START" -Phase "cuda_embed_repack" -Detail $embedWhl
    Compress-TorchWheelArchive -SourceDir $unpackDir -WheelPath $embedWhl
    Write-CiPhase -State "PASS" -Phase "cuda_embed_repack"

    Remove-Item -LiteralPath $unpackDir -Recurse -Force -ErrorAction SilentlyContinue

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
