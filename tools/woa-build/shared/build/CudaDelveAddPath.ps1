<#
.SYNOPSIS
  Resolve CUDA / cuDNN / CUPTI runtime directories and copy *.dll into the PyTorch build tree's
  torch\lib.

.DESCRIPTION
  Get-CudaToolkitRuntimeDelveSegmentDirs lists CUDA toolkit bin (arm64 or x64 fallback), cuDNN bin,
  the CUPTI runtime dir (the folder that actually holds cupti64_*.dll — see Get-CudaCuptiRuntimeDir),
  and an optional PYTORCH_WIN_CUDA_EMBED_DELVE_EXTRA_ADD_PATH (existing dirs only).

  Copy-CudaRuntimeDllsIntoTorchLib merges those CUDA dirs with PYTORCH_WIN_BUILD_LIBUV_ROOT\bin and
  copies *.dll from each directory into build\lib.*\torch\lib before the cuda_embed pip wheel.

.NOTES
  PYTORCH_WIN_BUILD_CUDA_BIN_DIR overrides the auto-detected CUDA bin.
  PYTORCH_WIN_BUILD_CUDNN_BIN_DIR overrides; if unset, cudnn bin is derived from
  PYTORCH_WIN_BUILD_CUDNN_ROOT and the CUDA version folder.

  Libuv bin (vcpkg), same root as CompilerAndBuildEnv libuv_ROOT: PYTORCH_WIN_BUILD_LIBUV_ROOT\bin.
#>

. (Join-Path $PSScriptRoot '..' 'env' 'EnvResolve.ps1')
. (Join-Path $PSScriptRoot '..' 'log' 'Phase.ps1')

function Get-CudaToolkitVersionMajorMinor {
    <#
    .SYNOPSIS
      Parse major.minor from a CUDA install path leaf (e.g. v13.1 -> 13.1).
    #>
    [OutputType([string])]
    param([Parameter(Mandatory)][string] $CudaPath)
    $leaf = Split-Path $CudaPath.TrimEnd('\', '/') -Leaf
    if ($leaf -match '^v?(\d+)\.(\d+)$') {
        return "$($matches[1]).$($matches[2])"
    }
    $default = (Get-CiDefault CudaPath).TrimEnd('\', '/')
    $defaultLeaf = Split-Path $default -Leaf
    if ($defaultLeaf -match '^v?(\d+)\.(\d+)$') {
        return "$($matches[1]).$($matches[2])"
    }
    return '13.1'
}

function Get-CudaBinDirForDelvewheel {
    <#
    .SYNOPSIS
      Prefer bin\arm64, then bin\x64, else bin under the CUDA install root.
    #>
    [OutputType([string])]
    param([Parameter(Mandatory)][string] $CudaPath)
    $root = $CudaPath.TrimEnd('\', '/')
    foreach ($candidate in @('bin\arm64', 'bin\x64')) {
        $p = Join-Path $root $candidate
        if (Test-Path -LiteralPath $p) {
            return $p
        }
    }
    return (Join-Path $root 'bin')
}

function Get-CudaCuptiRuntimeDir {
    <#
    .SYNOPSIS
      Return the directory that actually contains cupti64_*.dll for the given CUDA install, or
      $null if none is found.

    .DESCRIPTION
      CUPTI's runtime DLL moved between CUDA toolkit layouts, so we keep a map of every known
      location and return the first one that actually holds a cupti64_*.dll:

        * LEGACY  (<= CUDA 13.1): the toolkit ships an extras\CUPTI subtree
                                  (doc\ include\ lib64\ samples\); the DLL is in extras\CUPTI\lib64.
        * FLATTENED (CUDA 13.4 early RCs): extras\CUPTI was removed and its contents were hoisted
                                  into the toolkit root — extras\CUPTI\lib64 -> lib,
                                  extras\CUPTI\include -> include, etc.; the DLL is in top-level lib\.
        * ARCH-SPLIT (CUDA 13.4 RC018+): the flattened lib\ was further split into per-arch
                                  subdirs; the toolkit's own arm64 DLL is in lib\arm64\
                                  (e.g. lib\arm64\cupti64_2026.3.0.dll), with an x64 copy in lib\x64\.
        * LEGACY ARCH-SPLIT (CUDA 13.4 GA): the extras\CUPTI subtree is back, but its lib64\ is now
                                  per-arch; the arm64 DLL is in extras\CUPTI\lib64\arm64\
                                  (e.g. extras\CUPTI\lib64\arm64\cupti64_2026.3.0.dll), x64 in lib64\x64\.

      The previous code hard-coded only 'extras\CUPTI\lib64', so on 13.4 (no extras\CUPTI) nothing
      was staged, cupti64_*.dll never made it into torch\lib, and aoti_custom_ops.dll ->
      torch_cpu.dll failed to load with WinError 126 at test time (imports fine on the build box
      because CUDA is on PATH there).

      Only CUPTI needs this map: the main CUDA runtime DLLs stayed in <cuda>\bin across 13.1/13.4
      (staged separately by Get-CudaBinDirForDelvewheel), and cuDNN is a separate install — CUPTI is
      the only staged component whose folder moved. Nsight-shipped copies (nsight-compute\ /
      nsight-systems\ and their x64 target folders) are deliberately NOT in the map — they carry a
      different DLL name/arch than the toolkit's own CUPTI runtime.
    #>
    [OutputType([string])]
    param([Parameter(Mandatory)][string] $CudaPathRaw)
    $cudaPath = $CudaPathRaw.TrimEnd('\', '/')

    # Ordered map of the CUPTI runtime-DLL dir per known toolkit layout (legacy first).
    # arm64 only: this repo builds win_arm64 wheels, so we never fall back to lib\x64 — embedding an
    # x64 cupti would pass this check but still fail to load (WinError 126). A missing arm64 DLL must
    # surface as a build failure, not a silently-wrong wheel.
    $cuptiDirMap = [ordered]@{
        'legacy-lib64-arm64'  = 'extras\CUPTI\lib64\arm64' # 13.4 GA: extras\CUPTI kept, lib64\ split per-arch (confirmed)
        'legacy-lib64'        = 'extras\CUPTI\lib64'   # <= 13.1 (confirmed)
        'flattened-lib'       = 'lib'                   # 13.4 early RCs: DLL directly in lib\ (confirmed)
        'flattened-lib-arm64' = 'lib\arm64'             # 13.4 RC018+: lib\ split into per-arch subdirs (confirmed)
    }
    foreach ($layout in $cuptiDirMap.Keys) {
        $dir = Join-Path $cudaPath $cuptiDirMap[$layout]
        if (-not (Test-Path -LiteralPath $dir)) { continue }
        $hit = Get-ChildItem -LiteralPath $dir -Filter 'cupti64_*.dll' -File -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($null -ne $hit) {
            return $dir
        }
    }
    return $null
}

function Get-CudaToolkitRuntimeDelveSegmentDirs {
    <#
    .SYNOPSIS
      Collect CUDA / cuDNN / CUPTI (and extra env) directory paths for copying *.dll into
      torch\lib.

    .DESCRIPTION
      Returns an array of existing directory paths. Built internally (no List[string] parameter)
      so Windows PowerShell does not hit "Cannot bind argument to parameter 'Segments' because it
      is an empty collection" when binding a mutable generic list argument.

    .PARAMETER CudaPathRaw
      PYTORCH_WIN_BUILD_CUDA_PATH value (trimmed trailing slashes).
    #>
    [OutputType([string[]])]
    param([Parameter(Mandatory)][string] $CudaPathRaw)
    $cudaPath = $CudaPathRaw.TrimEnd('\', '/')
    $out = [System.Collections.Generic.List[string]]::new()

    $cudaBinOverride = Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_CUDA_BIN_DIR'
    if (-not [string]::IsNullOrWhiteSpace($cudaBinOverride)) {
        $cb = $cudaBinOverride.TrimEnd('\', '/')
        if (Test-Path -LiteralPath $cb) {
            $out.Add($cb) | Out-Null
        }
    }
    else {
        $cb = Get-CudaBinDirForDelvewheel -CudaPath $cudaPath
        if (Test-Path -LiteralPath $cb) {
            $out.Add($cb) | Out-Null
        }
    }

    $cudnnBin = Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_CUDNN_BIN_DIR' -Default (Get-CiDefault CudnnBinDir)
    if ([string]::IsNullOrWhiteSpace($cudnnBin)) {
        $cudnnRoot = Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_CUDNN_ROOT' -Default (Get-CiDefault CudnnRoot)
        if (-not [string]::IsNullOrWhiteSpace($cudnnRoot)) {
            $ver = Get-CudaToolkitVersionMajorMinor -CudaPath $cudaPath
            $cudnnBin = Join-Path $cudnnRoot.TrimEnd('\', '/') "bin\$ver"
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($cudnnBin)) {
        $cn = $cudnnBin.TrimEnd('\', '/')
        if (Test-Path -LiteralPath $cn) {
            $out.Add($cn) | Out-Null
        }
    }

    $cupti = Get-CudaCuptiRuntimeDir -CudaPathRaw $cudaPath
    if (-not [string]::IsNullOrWhiteSpace($cupti)) {
        $out.Add($cupti) | Out-Null
    }
    else {
        Write-Warning "[cuda-stage-torch-lib] No cupti64_*.dll found under '$cudaPath' (checked extras\CUPTI\lib64\arm64, extras\CUPTI\lib64, lib, and lib\arm64); torch_cpu.dll may fail to load at import (WinError 126)."
    }

    $extra = Resolve-CiEnv -Name 'PYTORCH_WIN_CUDA_EMBED_DELVE_EXTRA_ADD_PATH'
    if (-not [string]::IsNullOrWhiteSpace($extra)) {
        foreach ($p in ($extra -split ';')) {
            $t = $p.Trim()
            if ($t.Length -gt 0 -and (Test-Path -LiteralPath $t)) {
                $out.Add($t) | Out-Null
            }
        }
    }

    return [string[]]$out.ToArray()
}

function Copy-CudaRuntimeDllsIntoTorchLib {
    <#
    .SYNOPSIS
      Copy CUDA toolkit / cuDNN / CUPTI / extra *.dll into the PyTorch build tree's torch\lib.

    .DESCRIPTION
      Dirs = Get-CudaToolkitRuntimeDelveSegmentDirs + libuv bin. The same loop copies *.dll from
      each dir; later dirs overwrite earlier files with the same name. Run after the first pip
      wheel so build\lib.win-*\torch\lib exists.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $TorchLibDir,
        [Parameter(Mandatory)][string] $CudaPathRaw
    )

    if (-not (Test-Path -LiteralPath $TorchLibDir)) {
        throw "TorchLibDir does not exist: $TorchLibDir"
    }
    New-Item -ItemType Directory -Path $TorchLibDir -Force | Out-Null

    $cudaDirs = @(Get-CudaToolkitRuntimeDelveSegmentDirs -CudaPathRaw $CudaPathRaw)
    if ($cudaDirs.Count -lt 1) {
        Write-Warning "[cuda-stage-torch-lib] No CUDA/cuDNN/CUPTI dirs resolved; only non-CUDA dirs (e.g. libuv) will be copied."
    }

    $libuvRoot = Resolve-CiEnv -Name 'PYTORCH_WIN_BUILD_LIBUV_ROOT' -Default (Get-CiDefault LibuvRoot)
    $libuvBin = Join-Path $libuvRoot.TrimEnd('\', '/') 'bin'
    if (-not (Test-Path -LiteralPath $libuvBin)) {
        throw "[cuda-stage-torch-lib] libuv bin dir not found: $libuvBin (set PYTORCH_WIN_BUILD_LIBUV_ROOT)"
    }
    $uvDllSrc = Join-Path $libuvBin 'uv.dll'
    if (-not (Test-Path -LiteralPath $uvDllSrc)) {
        throw "[cuda-stage-torch-lib] uv.dll not found at $uvDllSrc"
    }

    $dirs = @($cudaDirs) + @($libuvBin)

    $copied = 0
    foreach ($d in $dirs) {
        $dlls = @(Get-ChildItem -LiteralPath $d -Filter '*.dll' -File -ErrorAction SilentlyContinue)
        foreach ($f in $dlls) {
            $dest = Join-Path $TorchLibDir $f.Name
            Copy-Item -LiteralPath $f.FullName -Destination $dest -Force
            $copied++
        }
    }
    Write-CiPhase -State 'INFO' -Phase 'cuda_stage_torch_lib' -Component 'cuda-stage-torch-lib' `
        -Detail "copied $copied DLL file(s) into $TorchLibDir from $($dirs.Count) dir(s)"

    # Fail the build loudly if CUPTI didn't make it in: a cuda_embed wheel without cupti64_*.dll
    # imports fine on the build box (CUDA on PATH) but dies with WinError 126 on the test runner.
    $cuptiStaged = Get-ChildItem -LiteralPath $TorchLibDir -Filter 'cupti64_*.dll' -File -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $cuptiStaged) {
        Write-CiPhase -State 'FAIL' -Phase 'cuda_stage_torch_lib' -Component 'cuda-stage-torch-lib' `
            -Detail "no cupti64_*.dll staged into $TorchLibDir"
        throw "[cuda-stage-torch-lib] cupti64_*.dll was not staged into $TorchLibDir; torch_cpu.dll will fail to load (WinError 126) on runners without CUDA on PATH. Check the CUPTI runtime dir under '$CudaPathRaw'."
    }
}
